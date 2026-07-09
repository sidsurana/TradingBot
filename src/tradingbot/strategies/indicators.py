"""Pure-python technical indicators for the directional strategies.

No pandas/numpy — these run over small in-memory candle histories every tick,
and plain loops keep them dependency-free and easy to unit-test. All functions
return None (never raise, never divide by zero) when there isn't enough data.

Convention: like engine/sizing.atr, `donchian` treats the LAST candle of the
series it is given as in-progress and drops it; the scalar-sequence helpers
(sma/ema/rsi/zscore) operate on exactly the values they are handed — callers
strip the in-progress bar before extracting closes.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from tradingbot.models import Candle


def sma(values: Sequence[float], period: int) -> float | None:
    """Simple moving average of the last `period` values."""
    if period <= 0 or len(values) < period:
        return None
    return sum(values[-period:]) / period


def ema(values: Sequence[float], period: int) -> float | None:
    """Exponential moving average, seeded with the SMA of the first `period`
    values (the standard convention, so results are deterministic and don't
    depend on an arbitrary first-value seed)."""
    if period <= 0 or len(values) < period:
        return None
    e = sum(values[:period]) / period
    k = 2.0 / (period + 1)
    for v in values[period:]:
        e = v * k + e * (1.0 - k)
    return e


def rsi(values: Sequence[float], period: int) -> float | None:
    """Wilder's RSI. Needs at least period+1 values (period deltas).
    Flat series -> 50 (neutral); no losses -> 100; no gains -> 0."""
    if period <= 0 or len(values) < period + 1:
        return None
    deltas = [b - a for a, b in zip(values, values[1:])]
    gains = [max(d, 0.0) for d in deltas]
    losses = [max(-d, 0.0) for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for g, loss in zip(gains[period:], losses[period:]):
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
    if avg_loss == 0.0:
        return 100.0 if avg_gain > 0.0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def zscore(values: Sequence[float], lookback: int) -> float | None:
    """Z-score of the LAST value against the mean/stddev (population) of the
    trailing `lookback` window (window includes the last value). Returns None
    on a flat window (zero stddev) — a z-score is meaningless there and
    callers must not trade on it."""
    if lookback < 2 or len(values) < lookback:
        return None
    window = values[-lookback:]
    mean = sum(window) / lookback
    var = sum((v - mean) ** 2 for v in window) / lookback
    if var <= 0.0:
        return None
    return (window[-1] - mean) / math.sqrt(var)


def donchian(candles: Sequence[Candle], period: int) -> tuple[float, float, float] | None:
    """Donchian channel (high, low, mid) over the last `period` COMPLETED bars.
    Mirrors engine/sizing.atr: the final candle of `candles` is treated as
    in-progress and dropped, so pass a series whose last bar you want excluded
    (e.g. the raw feed, or completed bars when the channel must not include
    the signal bar)."""
    if period <= 0 or len(candles) < period + 1:
        return None
    bars = candles[:-1][-period:]
    hi = max(b.high for b in bars)
    lo = min(b.low for b in bars)
    return hi, lo, (hi + lo) / 2.0
