"""Paper-trading adapter.

Wraps a *real* read-only exchange for live market data, but simulates order
execution locally against the observed order book. This is the default mode:
the entire strategy/engine/risk stack runs identically to live, only the
fill happens in-memory. Run here for days before ever setting TB_LIVE=true.

Fill model (intentionally conservative):
  - MARKET / marketable LIMIT orders fill by walking the opposite side of the
    book, paying the spread. Size beyond available depth is left unfilled.
  - Resting (non-marketable) LIMIT orders are acknowledged as OPEN and filled
    on a later tick if the market trades through them.
A small synthetic fee is applied so strategies can't profit on dust edges.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import structlog

from tradingbot.exchanges.base import Exchange
from tradingbot.models import (
    Fill,
    Market,
    Order,
    OrderBook,
    OrderStatus,
    OrderType,
    Position,
    Side,
    Venue,
)

log = structlog.get_logger(__name__)

PAPER_FEE_RATE = Decimal("0.001")  # 0.1% of notional, per fill


class PaperExchange(Exchange):
    """Simulated execution layered on top of a live market-data source."""

    venue = Venue.PAPER

    def __init__(self, data_source: Exchange):
        # data_source provides real books via list_markets/fetch_order_book.
        self._data = data_source
        self._positions: dict[str, Position] = {}
        self._resting: list[Order] = []
        self.fills: list[Fill] = []
        self._book_source = None  # optional fast in-memory book provider (the stream)

    def set_book_source(self, fn) -> None:
        """Use an in-memory book provider (e.g. the WS stream cache) for fill
        simulation instead of a REST fetch — required for low-latency paper fills."""
        self._book_source = fn

    async def _book_for(self, market: Market) -> OrderBook:
        if self._book_source is not None:
            b = self._book_source(market)
            if b is not None:
                return b
        return await self._data.fetch_order_book(market)

    async def connect(self) -> None:
        await self._data.connect()

    async def close(self) -> None:
        await self._data.close()

    async def list_markets(self, *, event_filter: str | None = None) -> list[Market]:
        return await self._data.list_markets(event_filter=event_filter)

    async def fetch_order_book(self, market: Market, depth: int = 10) -> OrderBook:
        book = await self._data.fetch_order_book(market, depth)
        self._match_resting(book)
        return book

    async def place_order(self, order: Order) -> Order:
        order.venue_id = f"paper-{uuid.uuid4().hex[:12]}"
        book = await self._book_for(order.market)
        levels = book.asks if order.side is Side.BUY else book.bids

        if order.type is OrderType.MARKET or self._is_marketable(order, book):
            self._fill_against(order, levels)
        else:
            order.status = OrderStatus.OPEN
            self._resting.append(order)
            log.debug("paper.resting", market=order.market.key, price=order.price)
        return order

    async def cancel_order(self, order: Order) -> Order:
        if order in self._resting:
            self._resting.remove(order)
        if not order.is_terminal:
            order.status = OrderStatus.CANCELED
        return order

    async def fetch_positions(self) -> list[Position]:
        return [p for p in self._positions.values() if p.size != 0]

    def match_resting(self, books: dict[str, OrderBook]) -> None:
        """Fill any resting orders the given books have traded through. The engine
        calls this each tick so market-maker quotes can fill on live data."""
        for book in books.values():
            self._match_resting(book)

    # --- internal simulation -------------------------------------------------

    def _is_marketable(self, order: Order, book: OrderBook) -> bool:
        if order.price is None:
            return True
        if order.side is Side.BUY:
            return book.best_ask is not None and order.price >= book.best_ask.price
        return book.best_bid is not None and order.price <= book.best_bid.price

    def _fill_against(self, order: Order, levels) -> None:
        remaining = order.remaining
        notional = Decimal(0)
        filled = Decimal(0)
        for lvl in levels:
            if remaining <= 0:
                break
            if order.price is not None:
                if order.side is Side.BUY and lvl.price > order.price:
                    break
                if order.side is Side.SELL and lvl.price < order.price:
                    break
            take = min(remaining, lvl.size)
            self._book_fill(order, take, lvl.price)
            notional += Decimal(str(lvl.price)) * take
            filled += take
            remaining -= take

        if filled == 0:
            order.status = OrderStatus.REJECTED
            order.reason = "no liquidity at limit"
            return
        # Running average across ALL fills of this order, not just this batch —
        # a resting order can partially fill on several ticks.
        prev_notional = (
            Decimal(str(order.avg_fill_price)) * order.filled_size
            if order.avg_fill_price is not None
            else Decimal(0)
        )
        order.filled_size += filled
        order.avg_fill_price = float((prev_notional + notional) / order.filled_size)
        order.status = OrderStatus.FILLED if order.remaining <= 0 else OrderStatus.PARTIAL

    def _book_fill(self, order: Order, size: Decimal, price: float) -> None:
        fee = Decimal(str(price)) * size * PAPER_FEE_RATE
        fill = Fill(
            market_key=order.market.key,
            side=order.side,
            size=size,
            price=price,
            fee=fee,
            order_client_id=order.client_id,
        )
        self.fills.append(fill)
        pos = self._positions.setdefault(order.market.key, Position(market=order.market))
        pos.apply(fill)
        log.debug("paper.fill", market=order.market.key, side=order.side.value,
                  size=str(size), price=price)

    def _match_resting(self, book: OrderBook) -> None:
        """On each fresh book, fill any resting orders the market traded through."""
        still_resting: list[Order] = []
        for order in self._resting:
            if order.market.key != book.market_key:
                still_resting.append(order)
                continue
            levels = book.asks if order.side is Side.BUY else book.bids
            if self._is_marketable(order, book):
                self._fill_against(order, levels)
                if not order.is_terminal and order.remaining > 0:
                    still_resting.append(order)
            else:
                still_resting.append(order)
        self._resting = still_resting
