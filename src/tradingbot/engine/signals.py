"""Signal store — directional views that drive the signal strategy.

A signal is a *belief about fair value*: "this market's true YES probability is
0.62, confidence 0.7." The agent (its alpha/regime skills) or any model pushes
signals here; the SignalStrategy reads them and takes Kelly-sized positions when
the market price diverges from the believed fair value. Views go stale, so each
carries a timestamp and the strategy ignores old ones.

Kept separate from the strategy so the signal *source* (LLM, model, manual) and
the *executor* (the strategy) are decoupled.
"""

from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Signal:
    market_key: str
    fair_value: float   # believed true probability of YES, in [0, 1]
    confidence: float   # [0, 1]
    ts: float


class SignalStore:
    def __init__(self, clock=time.time):
        self._clock = clock
        self._signals: dict[str, Signal] = {}

    def set(self, market_key: str, fair_value: float, confidence: float,
            ts: float | None = None) -> Signal:
        fair_value = max(0.0, min(1.0, float(fair_value)))
        confidence = max(0.0, min(1.0, float(confidence)))
        sig = Signal(market_key, fair_value, confidence,
                     self._clock() if ts is None else ts)
        self._signals[market_key] = sig
        return sig

    def get(self, market_key: str) -> Signal | None:
        return self._signals.get(market_key)

    def clear(self, market_key: str) -> None:
        self._signals.pop(market_key, None)

    def all(self) -> list[Signal]:
        return list(self._signals.values())

    def active(self, max_age_s: float, now: float | None = None) -> dict[str, Signal]:
        """Signals fresher than max_age_s."""
        now = self._clock() if now is None else now
        return {k: s for k, s in self._signals.items() if now - s.ts <= max_age_s}
