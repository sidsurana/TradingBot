"""15m mean reversion on equity indices (DATA venue, asset_class "equity").

Fade stretched moves: when the close's z-score over a rolling window exceeds
entry_z AND a fast Wilder RSI confirms the exhaustion, enter against the move;
close the position once the snap-back has largely happened (|z| back inside
exit_z). Signals use COMPLETED bars only — the last candle of the feed is
treated as in-progress and dropped. Position size comes from the shared
volatility-aware sizer (engine/sizing.position_size).
"""

from __future__ import annotations

import time
from decimal import Decimal

import structlog

from tradingbot.config import MeanReversionSettings, SizingSettings
from tradingbot.engine.sizing import atr, position_size
from tradingbot.models import Market, Order, OrderType, Side, Venue
from tradingbot.strategies.base import Context, Strategy, register
from tradingbot.strategies.indicators import rsi, zscore

log = structlog.get_logger(__name__)

_UNIT_S = {"m": 60.0, "h": 3600.0, "d": 86400.0}


def interval_seconds(interval: str) -> float:
    """'15m' -> 900.0; 0.0 when unparseable (callers treat that as unusable)."""
    try:
        return float(interval[:-1]) * _UNIT_S[interval[-1]]
    except (KeyError, ValueError, IndexError):
        return 0.0


@register
class MeanReversionStrategy(Strategy):
    name = "mean_reversion"
    asset_class = "equity"

    def __init__(self, *, cfg: MeanReversionSettings | None = None,
                 sizing: SizingSettings | None = None, **params):
        super().__init__(**params)
        self.cfg = cfg or MeanReversionSettings()
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
        bars = candles[:-1]  # completed bars only; last candle may be in-progress
        if len(bars) < cfg.min_bars:
            return []
        isec = interval_seconds(cfg.interval)
        if isec <= 0 or now - bars[-1].ts > 3 * isec:
            return []  # stale feed / market closed / weekend
        if self._last_acted.get(market.key) == bars[-1].ts:
            return []  # already acted on this bar

        closes = [b.close for b in bars]
        z = zscore(closes, cfg.lookback)
        if z is None:
            return []

        pos = ctx.positions.get(market.key)
        pos_size = pos.size if pos else Decimal(0)

        if pos_size > 0:  # holding long: exit once the snap-back is done
            if z >= -cfg.exit_z:
                return self._emit(market, bars[-1].ts, [self._exit(market, Side.SELL, pos_size)])
            return []
        if pos_size < 0:  # holding short
            if z <= cfg.exit_z:
                return self._emit(market, bars[-1].ts, [self._exit(market, Side.BUY, pos_size)])
            return []

        r = rsi(closes, cfg.rsi_period)
        if r is None:
            return []
        if z <= -cfg.entry_z and r <= cfg.rsi_oversold:
            side = Side.BUY
        elif z >= cfg.entry_z and r >= cfg.rsi_overbought:
            side = Side.SELL
        else:
            return []

        size = position_size(ctx.equity, closes[-1], atr(candles, self.sizing.atr_period),
                             self.sizing)
        if size == 0:
            return []
        log.info("mean_reversion.entry", market=market.key, side=side.value, size=str(size),
                 z=round(z, 2), rsi=round(r, 1))
        order = Order(market=market, side=side, size=size, type=OrderType.MARKET,
                      reason=f"mr_entry z={z:.2f} rsi={r:.1f}")
        return self._emit(market, bars[-1].ts, [order])

    def _exit(self, market: Market, side: Side, pos_size: Decimal) -> Order:
        return Order(market=market, side=side, size=abs(pos_size), type=OrderType.MARKET,
                     reason="mr_exit_z")

    def _emit(self, market: Market, bar_ts: float, orders: list[Order]) -> list[Order]:
        self._last_acted[market.key] = bar_ts
        return orders
