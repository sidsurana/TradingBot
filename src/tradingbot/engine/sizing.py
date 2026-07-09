"""Volatility-aware position sizing for directional strategies.

The contract with the hard 1% stop: risk a fixed fraction of equity per trade,
where per-unit risk is the LARGER of the hard stop distance and a multiple of
ATR. When the market is calm the stop distance dominates and the position is
at its largest; when ATR swells past the stop, size shrinks proportionally so
a noise-range bar can't blow through the risk budget before the stop fires.
"""

from __future__ import annotations

from decimal import Decimal

from tradingbot.config import SizingSettings
from tradingbot.models import Candle


def atr(candles: tuple[Candle, ...], period: int) -> float:
    """Wilder's Average True Range over the last `period` COMPLETED bars.
    Returns 0.0 with insufficient history (callers must treat 0 as 'unknown',
    not 'no volatility')."""
    if len(candles) < period + 2:
        return 0.0
    # Drop the possibly in-progress last bar.
    bars = candles[:-1]
    trs: list[float] = []
    for prev, cur in zip(bars[-period - 1 : -1], bars[-period:]):
        trs.append(max(cur.high - cur.low,
                       abs(cur.high - prev.close),
                       abs(cur.low - prev.close)))
    return sum(trs) / len(trs) if trs else 0.0


def position_size(equity: float, price: float, bar_atr: float,
                  cfg: SizingSettings) -> Decimal:
    """Units to trade so that hitting the stop loses ~risk_per_trade_pct of
    equity. Returns Decimal(0) when inputs can't support a sane size."""
    if equity <= 0 or price <= 0:
        return Decimal(0)
    risk_dollars = equity * cfg.risk_per_trade_pct
    per_unit_risk = max(price * cfg.hard_stop_pct, cfg.atr_mult * bar_atr)
    if per_unit_risk <= 0:
        return Decimal(0)
    units = risk_dollars / per_unit_risk
    max_units = float(cfg.max_notional_per_trade) / price
    units = min(units, max_units)
    if units * price < 1.0:  # sub-$1 positions are dust
        return Decimal(0)
    return Decimal(str(round(units, 6)))
