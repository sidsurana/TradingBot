"""Directional strategies (mean_reversion / breakout / trend) + indicators.

Contexts are built directly (markets list + candles dict + positions) — no
engine needed. Candle series are engineered to trigger each rule, and the
negative cases (insufficient bars, stale feed, volume filter, no-pyramid,
per-bar dedupe, zero stddev, exit sizing exactness) are pinned explicitly.
"""

import time
from decimal import Decimal

import pytest

from tradingbot.config import (
    BreakoutSettings,
    MeanReversionSettings,
    SizingSettings,
    TrendSettings,
)
from tradingbot.engine.sizing import atr, position_size
from tradingbot.models import Candle, Market, OrderType, Position, Side, Venue
from tradingbot.strategies import available, build
from tradingbot.strategies.base import Context
from tradingbot.strategies.indicators import donchian, ema, rsi, sma, zscore
from tradingbot.strategies.mean_reversion import interval_seconds

EQUITY = 10_000.0

SIZING = SizingSettings(risk_per_trade_pct=0.005, hard_stop_pct=0.01,
                        atr_period=14, atr_mult=1.5,
                        max_notional_per_trade=Decimal(500))

MR_CFG = MeanReversionSettings(interval="15m", lookback=20, entry_z=2.0, exit_z=0.3,
                               rsi_period=2, rsi_oversold=10.0, rsi_overbought=90.0,
                               min_bars=40)
BO_CFG = BreakoutSettings(interval="1h", channel_period=20, volume_period=20,
                          volume_mult=1.5, min_bars=40)
TR_CFG = TrendSettings(interval="4h", fast_ema=5, slow_ema=10, min_bars=20)


def mkt(symbol: str, asset_class: str) -> Market:
    return Market(venue=Venue.DATA, market_id=symbol, event_id=symbol, title=symbol,
                  outcome="", metadata={"asset_class": asset_class})


def make_candles(closes, interval_s, *, volumes=None, end_offset=2.0, in_progress=True):
    """Bars oldest-first; newest COMPLETED bar opens `end_offset` intervals ago
    (default 2 => fresh). high/low default to close +/- 0.5."""
    now = time.time()
    n = len(closes)
    last_ts = now - end_offset * interval_s
    out = []
    for i, c in enumerate(closes):
        v = volumes[i] if volumes is not None else 100.0
        out.append(Candle(ts=last_ts - (n - 1 - i) * interval_s, open=c,
                          high=c + 0.5, low=c - 0.5, close=c, volume=v))
    if in_progress:
        out.append(Candle(ts=last_ts + interval_s, open=closes[-1], high=closes[-1],
                          low=closes[-1], close=closes[-1], volume=1.0))
    return tuple(out)


def ctx_for(market, candles, interval, position=None, equity=EQUITY):
    positions = {}
    if position is not None:
        positions[market.key] = position
    return Context([market], {}, positions,
                   candles={market.key: {interval: candles}}, equity=equity)


def expected_entry_size(candles, sizing=SIZING, equity=EQUITY):
    last_close = candles[:-1][-1].close
    return position_size(equity, last_close, atr(candles, sizing.atr_period), sizing)


# --- indicators -----------------------------------------------------------

def test_sma():
    assert sma([1.0, 2.0, 3.0, 4.0], 2) == 3.5
    assert sma([1.0, 2.0, 3.0, 4.0], 4) == 2.5
    assert sma([1.0], 2) is None
    assert sma([1.0, 2.0], 0) is None


def test_ema_seeded_with_sma():
    # seed = sma([1,2]) = 1.5; k = 2/3: 3 -> 2.5, 4 -> 3.5
    assert ema([1.0, 2.0, 3.0, 4.0], 2) == pytest.approx(3.5)
    assert ema([1.0], 2) is None


def test_rsi_wilder():
    # period=2: deltas [1,1,-1] -> init avg_gain=1, avg_loss=0;
    # smooth: gain=(1+0)/2=0.5, loss=(0+1)/2=0.5 -> RS=1 -> RSI=50
    assert rsi([1.0, 2.0, 3.0, 2.0], 2) == pytest.approx(50.0)
    assert rsi([1.0, 2.0, 3.0, 4.0], 2) == 100.0   # no losses
    assert rsi([4.0, 3.0, 2.0, 1.0], 2) == 0.0     # no gains
    assert rsi([5.0, 5.0, 5.0, 5.0], 2) == 50.0    # flat -> neutral, no div-by-zero
    assert rsi([1.0, 2.0], 2) is None              # needs period+1 values


def test_zscore():
    # window [1..5]: mean 3, population std sqrt(2)
    assert zscore([1.0, 2.0, 3.0, 4.0, 5.0], 5) == pytest.approx(2 / 2**0.5)
    assert zscore([7.0, 7.0, 7.0, 7.0], 4) is None  # flat stddev -> None, no div-by-zero
    assert zscore([1.0, 2.0], 5) is None            # not enough values


def test_donchian_drops_in_progress_bar():
    closes = [100.0, 105.0, 95.0, 200.0]  # last bar (200, hi 200.5) must be excluded
    candles = make_candles(closes, 3600, in_progress=False)
    hi, lo, mid = donchian(candles, 3)
    assert hi == 105.5 and lo == 94.5 and mid == 100.0
    assert donchian(candles, 4) is None  # needs period+1 candles


def test_interval_seconds():
    assert interval_seconds("15m") == 900.0
    assert interval_seconds("1h") == 3600.0
    assert interval_seconds("4h") == 14400.0
    assert interval_seconds("junk") == 0.0


def test_strategies_registered():
    for name in ("mean_reversion", "breakout", "trend"):
        assert name in available()


# --- mean reversion -------------------------------------------------------

def mr():
    return build("mean_reversion", cfg=MR_CFG, sizing=SIZING)


def test_mr_long_entry_sized_by_position_size():
    m = mkt("SPY", "equity")
    candles = make_candles([100.0] * 39 + [90.0], 900)  # sharp drop: z << -2, RSI(2) ~ 0
    orders = mr().generate(ctx_for(m, candles, "15m"))
    assert len(orders) == 1
    o = orders[0]
    assert o.side is Side.BUY and o.type is OrderType.MARKET
    assert o.reason.startswith("mr_entry")
    exp = expected_entry_size(candles)
    assert exp > 0 and o.size == exp


def test_mr_short_entry():
    m = mkt("SPY", "equity")
    candles = make_candles([100.0] * 39 + [110.0], 900)  # spike: z >> +2, RSI(2) = 100
    orders = mr().generate(ctx_for(m, candles, "15m"))
    assert len(orders) == 1
    assert orders[0].side is Side.SELL
    assert orders[0].size == expected_entry_size(candles)


def test_mr_exit_long_exact_size():
    m = mkt("SPY", "equity")
    # window mean == last close == 100 -> z == 0 >= -exit_z
    closes = ([99.0, 101.0] * 19)[:38] + [100.0, 100.0]
    candles = make_candles(closes, 900)
    pos = Position(market=m, size=Decimal("2.5"), avg_price=95.0)
    orders = mr().generate(ctx_for(m, candles, "15m", position=pos))
    assert len(orders) == 1
    o = orders[0]
    assert o.side is Side.SELL and o.size == Decimal("2.5") and o.reason == "mr_exit_z"


def test_mr_exit_short_exact_size():
    m = mkt("SPY", "equity")
    closes = ([99.0, 101.0] * 19)[:38] + [100.0, 100.0]
    candles = make_candles(closes, 900)
    pos = Position(market=m, size=Decimal("-3.5"), avg_price=105.0)
    orders = mr().generate(ctx_for(m, candles, "15m", position=pos))
    assert len(orders) == 1
    o = orders[0]
    assert o.side is Side.BUY and o.size == Decimal("3.5") and o.reason == "mr_exit_z"


def test_mr_no_pyramiding_when_positioned():
    m = mkt("SPY", "equity")
    candles = make_candles([100.0] * 39 + [90.0], 900)  # entry conditions met...
    pos = Position(market=m, size=Decimal(5), avg_price=95.0)  # ...but already long
    assert mr().generate(ctx_for(m, candles, "15m", position=pos)) == []


def test_mr_insufficient_bars():
    m = mkt("SPY", "equity")
    candles = make_candles([100.0] * 30 + [90.0], 900)  # 31 completed < min_bars 40
    assert mr().generate(ctx_for(m, candles, "15m")) == []


def test_mr_stale_feed():
    m = mkt("SPY", "equity")
    candles = make_candles([100.0] * 39 + [90.0], 900, end_offset=10.0)  # > 3 intervals old
    assert mr().generate(ctx_for(m, candles, "15m")) == []


def test_mr_zero_stddev_is_safe():
    m = mkt("SPY", "equity")
    candles = make_candles([100.0] * 40, 900)  # flat: zscore must not divide by zero
    assert mr().generate(ctx_for(m, candles, "15m")) == []


def test_mr_one_action_per_bar():
    m = mkt("SPY", "equity")
    candles = make_candles([100.0] * 39 + [90.0], 900)
    strat = mr()
    ctx = ctx_for(m, candles, "15m")
    assert len(strat.generate(ctx)) == 1
    assert strat.generate(ctx) == []  # same bar again -> deduped


def test_mr_ignores_other_asset_classes_and_venues():
    candles = make_candles([100.0] * 39 + [90.0], 900)
    crypto = mkt("BTC-USD", "crypto")
    assert mr().generate(ctx_for(crypto, candles, "15m")) == []
    non_data = Market(venue=Venue.KALSHI, market_id="SPY", event_id="SPY", title="SPY",
                      outcome="YES", metadata={"asset_class": "equity"})
    assert mr().generate(ctx_for(non_data, candles, "15m")) == []


def test_mr_zero_equity_no_entry():
    m = mkt("SPY", "equity")
    candles = make_candles([100.0] * 39 + [90.0], 900)
    assert mr().generate(ctx_for(m, candles, "15m", equity=0.0)) == []


# --- breakout -------------------------------------------------------------

def bo():
    return build("breakout", cfg=BO_CFG, sizing=SIZING)


def test_bo_long_breakout_with_volume():
    m = mkt("BTC-USD", "crypto")
    # 43 prior bars at 100 (channel hi 100.5 / lo 99.5 / mid 100); signal closes 105
    # on volume 200 >= 1.5 * 100.
    closes = [100.0] * 43 + [105.0]
    candles = make_candles(closes, 3600, volumes=[100.0] * 43 + [200.0])
    orders = bo().generate(ctx_for(m, candles, "1h"))
    assert len(orders) == 1
    o = orders[0]
    assert o.side is Side.BUY and o.type is OrderType.MARKET
    assert o.reason.startswith("bo_entry")
    exp = expected_entry_size(candles)
    assert exp > 0 and o.size == exp


def test_bo_short_breakout_with_volume():
    m = mkt("BTC-USD", "crypto")
    closes = [100.0] * 43 + [95.0]  # below channel low 99.5
    candles = make_candles(closes, 3600, volumes=[100.0] * 43 + [200.0])
    orders = bo().generate(ctx_for(m, candles, "1h"))
    assert len(orders) == 1
    assert orders[0].side is Side.SELL


def test_bo_volume_filter_rejects():
    m = mkt("BTC-USD", "crypto")
    closes = [100.0] * 43 + [105.0]
    candles = make_candles(closes, 3600, volumes=[100.0] * 43 + [120.0])  # < 1.5x avg
    assert bo().generate(ctx_for(m, candles, "1h")) == []


def test_bo_exit_long_through_mid_exact_size():
    m = mkt("BTC-USD", "crypto")
    closes = [100.0] * 43 + [99.0]  # signal close 99 < channel mid 100
    candles = make_candles(closes, 3600)
    pos = Position(market=m, size=Decimal("1.25"), avg_price=101.0)
    orders = bo().generate(ctx_for(m, candles, "1h", position=pos))
    assert len(orders) == 1
    o = orders[0]
    assert o.side is Side.SELL and o.size == Decimal("1.25") and o.reason == "bo_exit_mid"


def test_bo_exit_short_through_mid():
    m = mkt("BTC-USD", "crypto")
    closes = [100.0] * 43 + [101.0]  # back above mid 100 against a short
    candles = make_candles(closes, 3600)
    pos = Position(market=m, size=Decimal("-2"), avg_price=99.0)
    orders = bo().generate(ctx_for(m, candles, "1h", position=pos))
    assert len(orders) == 1
    assert orders[0].side is Side.BUY and orders[0].size == Decimal(2)


def test_bo_no_pyramiding():
    m = mkt("BTC-USD", "crypto")
    closes = [100.0] * 43 + [105.0]  # breakout, but already long and above mid
    candles = make_candles(closes, 3600, volumes=[100.0] * 43 + [200.0])
    pos = Position(market=m, size=Decimal(1), avg_price=100.0)
    assert bo().generate(ctx_for(m, candles, "1h", position=pos)) == []


def test_bo_negative_cases():
    m = mkt("BTC-USD", "crypto")
    short = make_candles([100.0] * 20 + [105.0], 3600)  # 21 < min_bars
    assert bo().generate(ctx_for(m, short, "1h")) == []
    stale = make_candles([100.0] * 43 + [105.0], 3600,
                         volumes=[100.0] * 43 + [200.0], end_offset=5.0)
    assert bo().generate(ctx_for(m, stale, "1h")) == []
    zero_vol = make_candles([100.0] * 43 + [105.0], 3600, volumes=[0.0] * 44)
    assert bo().generate(ctx_for(m, zero_vol, "1h")) == []  # zero avg volume is safe


def test_bo_one_action_per_bar():
    m = mkt("BTC-USD", "crypto")
    candles = make_candles([100.0] * 43 + [105.0], 3600, volumes=[100.0] * 43 + [200.0])
    strat = bo()
    ctx = ctx_for(m, candles, "1h")
    assert len(strat.generate(ctx)) == 1
    assert strat.generate(ctx) == []


# --- trend ----------------------------------------------------------------

def tr():
    return build("trend", cfg=TR_CFG, sizing=SIZING)


def _cross_closes(direction: int):
    """Closes where fast(5) crosses slow(10) in `direction` exactly on the
    last value (verified with the same ema used by the strategy)."""
    step = -0.5 * direction
    closes = [100.0 + step * i for i in range(25)]  # steady counter-trend
    for _ in range(100):
        closes.append(closes[-1] + 2.0 * direction)
        f, s = ema(closes, TR_CFG.fast_ema), ema(closes, TR_CFG.slow_ema)
        if (f - s) * direction > 0:
            return closes
    raise AssertionError("no cross produced")


def test_trend_cross_up_enters_long():
    m = mkt("GC=F", "commodity")
    closes = _cross_closes(+1)
    candles = make_candles(closes, 14400)
    orders = tr().generate(ctx_for(m, candles, "4h"))
    assert len(orders) == 1
    o = orders[0]
    assert o.side is Side.BUY and o.type is OrderType.MARKET
    assert o.reason == "trend_cross_up"
    exp = expected_entry_size(candles)
    assert exp > 0 and o.size == exp


def test_trend_cross_down_enters_short():
    m = mkt("CL=F", "commodity")
    candles = make_candles(_cross_closes(-1), 14400)
    orders = tr().generate(ctx_for(m, candles, "4h"))
    assert len(orders) == 1
    assert orders[0].side is Side.SELL and orders[0].reason == "trend_cross_down"


def test_trend_flip_closes_opposing_then_enters():
    m = mkt("GC=F", "commodity")
    candles = make_candles(_cross_closes(+1), 14400)
    pos = Position(market=m, size=Decimal("-4"), avg_price=100.0)
    orders = tr().generate(ctx_for(m, candles, "4h", position=pos))
    assert len(orders) == 2
    exit_o, entry_o = orders
    assert exit_o.side is Side.BUY and exit_o.size == Decimal(4)
    assert exit_o.reason == "trend_flip"
    assert entry_o.side is Side.BUY and entry_o.size == expected_entry_size(candles)


def test_trend_no_entry_without_fresh_cross():
    m = mkt("GC=F", "commodity")
    closes = _cross_closes(+1) + [200.0, 202.0, 204.0]  # trend established bars ago
    candles = make_candles(closes, 14400)
    assert tr().generate(ctx_for(m, candles, "4h")) == []


def test_trend_no_pyramiding_same_direction_cross():
    m = mkt("GC=F", "commodity")
    candles = make_candles(_cross_closes(+1), 14400)
    pos = Position(market=m, size=Decimal(3), avg_price=90.0)  # already long
    assert tr().generate(ctx_for(m, candles, "4h", position=pos)) == []


def test_trend_negative_cases():
    m = mkt("GC=F", "commodity")
    short = make_candles([100.0, 101.0] * 5, 14400)  # 10 completed < min_bars 20
    assert tr().generate(ctx_for(m, short, "4h")) == []
    stale = make_candles(_cross_closes(+1), 14400, end_offset=6.0)
    assert tr().generate(ctx_for(m, stale, "4h")) == []


def test_trend_one_action_per_bar():
    m = mkt("GC=F", "commodity")
    candles = make_candles(_cross_closes(+1), 14400)
    strat = tr()
    ctx = ctx_for(m, candles, "4h")
    assert len(strat.generate(ctx)) == 1
    assert strat.generate(ctx) == []
