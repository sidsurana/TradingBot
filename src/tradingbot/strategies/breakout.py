"""1h momentum breakout on crypto (DATA venue, asset_class "crypto").

Enter when the newest COMPLETED bar (the "signal bar") closes through the
Donchian channel extreme of the bars BEFORE it, confirmed by volume at least
volume_mult x the prior-bar average. Exit when the close crosses back through
the channel mid against the position. The last candle of the feed is treated
as in-progress and never used for signals.
"""

from __future__ import annotations

import time
from decimal import Decimal

import structlog

from tradingbot.config import BreakoutSettings, SizingSettings
from tradingbot.engine.sizing import atr, position_size
from tradingbot.models import Market, Order, OrderType, Side, Venue
from tradingbot.strategies.base import Context, Strategy, register
from tradingbot.strategies.indicators import donchian, sma
from tradingbot.strategies.mean_reversion import interval_seconds

log = structlog.get_logger(__name__)


@register
class BreakoutStrategy(Strategy):
    name = "breakout"
    asset_class = "crypto"

    def __init__(self, *, cfg: BreakoutSettings | None = None,
                 sizing: SizingSettings | None = None, **params):
        super().__init__(**params)
        self.cfg = cfg or BreakoutSettings()
        self.sizing = sizing or SizingSettings()
        self._last_acted: dict[str, float] = {}  # market key -> bar ts already acted on

    def generate(self, ctx: Context) -> list[Order]:
        now = time.time()
        orders: list[Order] = []
        for market in ctx.markets:
            if market.venue is not Venue.DATA:
                continue
            if market.metadata.get("asset_class") != self.asset_class:
                continue
            orders.extend(self._evaluate(market, ctx, now))
        return orders

    def _evaluate(self, market: Market, ctx: Context, now: float) -> list[Order]:
        cfg = self.cfg
        candles = ctx.candles_for(market, cfg.interval)
        if len(candles) < 2:
            return []
        bars = candles[:-1]  # completed bars; bars[-1] is the signal bar
        if len(bars) < cfg.min_bars:
            return []
        isec = interval_seconds(cfg.interval)
        if isec <= 0 or now - bars[-1].ts > 3 * isec:
            return []  # stale feed
        if self._last_acted.get(market.key) == bars[-1].ts:
            return []  # already acted on this bar

        # donchian drops the final candle of what it's given, so passing the
        # completed bars yields the channel of the bars BEFORE the signal bar.
        channel = donchian(bars, cfg.channel_period)
        if channel is None:
            return []
        ch_high, ch_low, ch_mid = channel
        signal = bars[-1]

        pos = ctx.positions.get(market.key)
        pos_size = pos.size if pos else Decimal(0)

        if pos_size > 0:  # holding long: exit when close falls back through mid
            if signal.close < ch_mid:
                return self._emit(market, signal.ts, [self._exit(market, Side.SELL, pos_size)])
            return []
        if pos_size < 0:  # holding short: exit when close rises back through mid
            if signal.close > ch_mid:
                return self._emit(market, signal.ts, [self._exit(market, Side.BUY, pos_size)])
            return []

        vol_avg = sma([b.volume for b in bars[:-1]], cfg.volume_period)
        if vol_avg is None or vol_avg <= 0:
            return []
        volume_ok = signal.volume >= cfg.volume_mult * vol_avg
        if signal.close > ch_high and volume_ok:
            side = Side.BUY
        elif signal.close < ch_low and volume_ok:
            side = Side.SELL
        else:
            return []

        size = position_size(ctx.equity, signal.close, atr(candles, self.sizing.atr_period),
                             self.sizing)
        if size == 0:
            return []
        log.info("breakout.entry", market=market.key, side=side.value, size=str(size),
                 close=signal.close, channel_high=ch_high, channel_low=ch_low,
                 volume=signal.volume, volume_avg=round(vol_avg, 2))
        order = Order(market=market, side=side, size=size, type=OrderType.MARKET,
                      reason=f"bo_entry close={signal.close:.2f} "
                             f"ch=[{ch_low:.2f},{ch_high:.2f}] vol={signal.volume:.0f}")
        return self._emit(market, signal.ts, [order])

    def _exit(self, market: Market, side: Side, pos_size: Decimal) -> Order:
        return Order(market=market, side=side, size=abs(pos_size), type=OrderType.MARKET,
                     reason="bo_exit_mid")

    def _emit(self, market: Market, bar_ts: float, orders: list[Order]) -> list[Order]:
        self._last_acted[market.key] = bar_ts
        return orders
