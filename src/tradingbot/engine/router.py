"""Exchange router.

Implements the Exchange interface over several concrete venues, dispatching each
call to the right one by `market.venue`, and aggregating discovery. This is what
lets the rest of the system treat "the market" as one place while orders flow to
Kalshi or Polymarket underneath.
"""

from __future__ import annotations

from tradingbot.exchanges.base import Exchange
from tradingbot.models import Market, Order, OrderBook, Position, Venue


class ExchangeRouter(Exchange):
    venue = Venue.PAPER  # nominal; routing is per-market

    def __init__(self, venues: dict[Venue, Exchange]):
        self._venues = venues

    def _for(self, market: Market) -> Exchange:
        ex = self._venues.get(market.venue)
        if ex is None:
            raise KeyError(f"no adapter registered for venue {market.venue}")
        return ex

    async def connect(self) -> None:
        for ex in self._venues.values():
            await ex.connect()

    async def close(self) -> None:
        for ex in self._venues.values():
            await ex.close()

    async def list_markets(self, *, event_filter: str | None = None) -> list[Market]:
        out: list[Market] = []
        for ex in self._venues.values():
            try:
                out += await ex.list_markets(event_filter=event_filter)
            except Exception:  # one venue down shouldn't blind the others
                continue
        return out

    async def fetch_order_book(self, market: Market, depth: int = 10) -> OrderBook:
        return await self._for(market).fetch_order_book(market, depth)

    async def place_order(self, order: Order) -> Order:
        return await self._for(order.market).place_order(order)

    async def cancel_order(self, order: Order) -> Order:
        return await self._for(order.market).cancel_order(order)

    async def fetch_positions(self) -> list[Position]:
        out: list[Position] = []
        for ex in self._venues.values():
            out += await ex.fetch_positions()
        return out
