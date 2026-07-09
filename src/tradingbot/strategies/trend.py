"""4h trend following on commodities (DATA venue, asset_class "commodity").

EMA-cross direction: state is sign(fast EMA - slow EMA) over completed bars.
Act ONLY on a fresh cross (the sign changed versus the EMAs one bar earlier):
close any opposing position and enter the new direction — two orders in the
same tick is fine. No entry without a cross: we never chase a trend that is
already established mid-way.
"""

from __future__ import annotations

import time
from decimal import Decimal

import structlog

from tradingbot.config import SizingSettings, TrendSettings
from tradingbot.engine.sizing import atr, position_size
from tradingbot.models import Market, Order, OrderType, Side, Venue
from tradingbot.strategies.base import Context, Strategy, register
from tradingbot.strategies.indicators import ema
from tradingbot.strategies.mean_reversion import interval_seconds

log = structlog.get_logger(__name__)


def _sign(x: float) -> int:
    return (x > 0) - (x < 0)


@register
class TrendStrategy(Strategy):
    name = "trend"
    asset_class = "commodity"

    def __init__(self, *, cfg: TrendSettings | None = None,
                 sizing: SizingSettings | None = None, **params):
        super().__init__(**params)
        self.cfg = cfg or TrendSettings()
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
        bars = candles[:-1]  # completed bars only
        if len(bars) < cfg.min_bars:
            return []
        isec = interval_seconds(cfg.interval)
        if isec <= 0 or now - bars[-1].ts > 3 * isec:
            return []  # stale feed
        if self._last_acted.get(market.key) == bars[-1].ts:
            return []  # already acted on this bar

        closes = [b.close for b in bars]
        fast_now = ema(closes, cfg.fast_ema)
        slow_now = ema(closes, cfg.slow_ema)
        fast_prev = ema(closes[:-1], cfg.fast_ema)
        slow_prev = ema(closes[:-1], cfg.slow_ema)
        if None in (fast_now, slow_now, fast_prev, slow_prev):
            return []
        sign_now = _sign(fast_now - slow_now)
        sign_prev = _sign(fast_prev - slow_prev)
        if sign_now == 0 or sign_now == sign_prev:
            return []  # no fresh cross this bar

        pos = ctx.positions.get(market.key)
        pos_size = pos.size if pos else Decimal(0)
        orders: list[Order] = []

        if pos_size != 0:
            if (pos_size > 0) == (sign_now > 0):
                return []  # already positioned with the new trend — no pyramiding
            exit_side = Side.SELL if pos_size > 0 else Side.BUY
            orders.append(Order(market=market, side=exit_side, size=abs(pos_size),
                                type=OrderType.MARKET, reason="trend_flip"))

        entry_side = Side.BUY if sign_now > 0 else Side.SELL
        size = position_size(ctx.equity, closes[-1], atr(candles, self.sizing.atr_period),
                             self.sizing)
        if size != 0:
            direction = "up" if sign_now > 0 else "down"
            log.info("trend.cross", market=market.key, direction=direction,
                     side=entry_side.value, size=str(size),
                     fast=round(fast_now, 4), slow=round(slow_now, 4))
            orders.append(Order(market=market, side=entry_side, size=size,
                                type=OrderType.MARKET, reason=f"trend_cross_{direction}"))
        if not orders:
            return []
        self._last_acted[market.key] = bars[-1].ts
        return orders
