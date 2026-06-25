"""Market-making strategy.

A passive two-sided maker: when a market's spread is wide enough to be profitable
after fees, quote on both sides of the inside and earn the spread when both fill.
Inventory is bounded — as the position grows toward the cap on one side, that
side's quote size shrinks to zero, so the book naturally mean-reverts inventory
back toward flat. This is the classic Avellaneda-style intuition in its simplest,
robust form (size-skew rather than price-skew).

It quotes the widest-spread markets first (most profit per round-trip), capped to
`max_markets` so it doesn't blanket hundreds of books. Returns desired quotes via
quotes(); the engine handles place/cancel/replace as prices move.

This is **how a market maker hits a daily target**: many small spread captures,
high frequency — which is exactly what the WebSocket streaming (sub-second, fresh
quotes) unlocks. Stale quotes get picked off; fresh quotes earn the spread.
"""

from __future__ import annotations

import time
from decimal import Decimal

import structlog

from tradingbot.models import Order, OrderType, Side
from tradingbot.strategies.base import Context, Strategy, register

log = structlog.get_logger(__name__)


@register
class MarketMakerStrategy(Strategy):
    name = "market_maker"
    is_market_maker = True

    def __init__(self, *, min_spread: float = 0.02, quote_size: Decimal = Decimal(10),
                 max_inventory: Decimal = Decimal(40), max_markets: int = 15,
                 exclude_sports: bool = True, min_hours_to_resolution: float = 24.0, **params):
        super().__init__(min_spread=min_spread, quote_size=quote_size,
                         max_inventory=max_inventory, max_markets=max_markets,
                         exclude_sports=exclude_sports,
                         min_hours_to_resolution=min_hours_to_resolution, **params)
        self.min_spread = min_spread
        self.quote_size = quote_size
        self.max_inventory = max_inventory
        self.max_markets = max_markets
        self.exclude_sports = exclude_sports
        self.min_hours_to_resolution = min_hours_to_resolution

    def _tradeable(self, m, now: float) -> bool:
        """Never make markets in event-driven / in-play / about-to-resolve contracts —
        their wide spreads are adverse selection, not edge."""
        md = m.metadata
        if self.exclude_sports and md.get("is_sports"):
            return False
        end_ts = md.get("end_ts") or 0
        if end_ts and (end_ts - now) < self.min_hours_to_resolution * 3600:
            return False
        return True

    def generate(self, ctx: Context) -> list[Order]:
        return []  # market makers quote, they don't fire marketable orders

    def quotes(self, ctx: Context) -> list[Order]:
        # Rank tradeable markets by spread (widest = most profitable to make).
        now = time.time()
        candidates = []
        for m in ctx.markets:
            if not self._tradeable(m, now):
                continue
            book = ctx.book(m)
            if not book or book.best_bid is None or book.best_ask is None:
                continue
            spread = book.best_ask.price - book.best_bid.price
            if spread >= self.min_spread:
                candidates.append((spread, m, book))
        candidates.sort(key=lambda x: x[0], reverse=True)

        orders: list[Order] = []
        for _spread, m, book in candidates[: self.max_markets]:
            pos = ctx.positions.get(m.key)
            inventory = pos.size if pos else Decimal(0)

            # Room to add on each side before hitting the inventory cap.
            buy_room = self.max_inventory - inventory     # how much more we can be long
            sell_room = self.max_inventory + inventory    # how much more we can be short
            buy_size = min(self.quote_size, buy_room)
            sell_size = min(self.quote_size, sell_room)

            if buy_size > 0:
                orders.append(Order(market=m, side=Side.BUY, size=buy_size,
                                    type=OrderType.LIMIT, price=book.best_bid.price,
                                    reason="mm_bid"))
            if sell_size > 0:
                orders.append(Order(market=m, side=Side.SELL, size=sell_size,
                                    type=OrderType.LIMIT, price=book.best_ask.price,
                                    reason="mm_ask"))
        return orders
