"""Abstract exchange adapter.

Every venue (Kalshi, Polymarket, the paper simulator) implements this interface.
Strategies and the engine depend ONLY on this interface — they never import a
concrete venue. This is what makes the bot platform-agnostic and lets us add a
third venue, or swap a live adapter for its paper twin, without touching
strategy code.
"""

from __future__ import annotations

import abc
from collections.abc import Iterable

from tradingbot.models import Market, Order, OrderBook, Position, Venue


class Exchange(abc.ABC):
    venue: Venue

    @abc.abstractmethod
    async def connect(self) -> None:
        """Establish sessions / authenticate. Idempotent."""

    @abc.abstractmethod
    async def close(self) -> None:
        """Tear down connections cleanly."""

    @abc.abstractmethod
    async def list_markets(self, *, event_filter: str | None = None) -> list[Market]:
        """Discover tradeable markets, optionally filtered by event/series."""

    @abc.abstractmethod
    async def fetch_order_book(self, market: Market, depth: int = 10) -> OrderBook:
        """Snapshot the top-of-book for a market."""

    @abc.abstractmethod
    async def place_order(self, order: Order) -> Order:
        """Submit an order. Returns the same Order mutated with venue_id/status."""

    @abc.abstractmethod
    async def cancel_order(self, order: Order) -> Order:
        """Cancel a resting order."""

    @abc.abstractmethod
    async def fetch_positions(self) -> list[Position]:
        """Current open positions as reported by the venue."""

    async def fetch_order_books(
        self, markets: Iterable[Market], depth: int = 10
    ) -> dict[str, OrderBook]:
        """Convenience batch fetch. Override for venues with bulk endpoints."""
        books: dict[str, OrderBook] = {}
        for m in markets:
            books[m.key] = await self.fetch_order_book(m, depth)
        return books
