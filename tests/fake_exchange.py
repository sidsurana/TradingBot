"""In-memory exchange for tests: serves scripted books, no network."""

from __future__ import annotations

from decimal import Decimal

from tradingbot.exchanges.base import Exchange
from tradingbot.models import Market, Order, OrderBook, Position, PriceLevel, Venue


class FakeExchange(Exchange):
    def __init__(self, venue: Venue, markets: list[Market], books: dict[str, OrderBook]):
        self.venue = venue
        self._markets = markets
        self._books = books

    async def connect(self) -> None: ...
    async def close(self) -> None: ...

    async def list_markets(self, *, event_filter: str | None = None) -> list[Market]:
        return list(self._markets)

    async def fetch_order_book(self, market: Market, depth: int = 10) -> OrderBook:
        return self._books[market.key]

    async def place_order(self, order: Order) -> Order:  # not used in paper tests
        raise NotImplementedError

    async def cancel_order(self, order: Order) -> Order:
        raise NotImplementedError

    async def fetch_positions(self) -> list[Position]:
        return []


def book(market: Market, *, bid: float, bid_sz: float, ask: float, ask_sz: float) -> OrderBook:
    return OrderBook(
        market_key=market.key,
        bids=(PriceLevel(price=bid, size=Decimal(str(bid_sz))),),
        asks=(PriceLevel(price=ask, size=Decimal(str(ask_sz))),),
    )


def market(venue: Venue, mid: str, event: str, outcome: str = "YES") -> Market:
    return Market(venue=venue, market_id=mid, event_id=event, title=event, outcome=outcome)
