"""Strategy interface + registry.

A strategy is a pure-ish function of market state -> desired orders. It does NOT
place orders itself, touch the network, or know about venues beyond the unified
models. The engine collects orders, runs them through risk, and executes. This
keeps strategies testable and composable (many can run at once).
"""

from __future__ import annotations

import abc
from collections.abc import Sequence

from tradingbot.models import Candle, Market, Order, OrderBook, Position


class Context:
    """Read-only snapshot handed to a strategy each tick.

    `candles` is optional history for candle-driven (directional) strategies:
    market_key -> interval ("15m"/"1h"/"4h") -> bars oldest-first, last bar
    possibly in-progress. Prediction-market strategies ignore it. `equity` is
    the portfolio's current marked equity (0.0 when unknown) for sizing.
    """

    def __init__(
        self,
        markets: Sequence[Market],
        books: dict[str, OrderBook],
        positions: dict[str, Position],
        candles: dict[str, dict[str, tuple[Candle, ...]]] | None = None,
        equity: float = 0.0,
    ):
        self.markets = markets
        self.books = books
        self.positions = positions
        self.candles = candles or {}
        self.equity = equity

    def book(self, market: Market) -> OrderBook | None:
        return self.books.get(market.key)

    def candles_for(self, market: Market, interval: str) -> tuple[Candle, ...]:
        return self.candles.get(market.key, {}).get(interval, ())


class Strategy(abc.ABC):
    name: str = "base"
    # Market makers manage resting quotes via quotes() + the engine's quote
    # reconciler, instead of firing marketable orders via generate().
    is_market_maker: bool = False

    def __init__(self, **params):
        self.params = params

    @abc.abstractmethod
    def generate(self, ctx: Context) -> list[Order]:
        """Return marketable orders to fire this tick (may be empty)."""

    def quotes(self, ctx: Context) -> list[Order]:
        """Return desired resting quotes this tick (market makers only). The engine
        reconciles these against live quotes — placing new ones, and cancelling or
        replacing ones whose price changed. Default: none."""
        return []


_REGISTRY: dict[str, type[Strategy]] = {}


def register(cls: type[Strategy]) -> type[Strategy]:
    _REGISTRY[cls.name] = cls
    return cls


def build(name: str, **params) -> Strategy:
    if name not in _REGISTRY:
        raise KeyError(f"unknown strategy {name!r}; have {sorted(_REGISTRY)}")
    return _REGISTRY[name](**params)


def available() -> list[str]:
    return sorted(_REGISTRY)
