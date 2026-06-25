"""Unified domain models.

Both Kalshi (binary "contracts", priced 1-99 cents) and Polymarket (ERC-1155
"outcome tokens", priced 0.00-1.00 USDC) normalize onto these types so that
strategies can reason about price/probability identically across venues.

Internal price convention: **probability in [0, 1]** (a float). Each adapter is
responsible for converting its native unit to/from this convention.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum


class Venue(str, Enum):
    KALSHI = "kalshi"
    POLYMARKET = "polymarket"
    PAPER = "paper"


class Side(str, Enum):
    """Direction of an order on a single binary outcome token.

    BUY = acquire YES exposure; SELL = shed it. NO exposure is modeled as
    BUY on the complementary outcome, keeping every market two one-sided books.
    """

    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    LIMIT = "limit"
    MARKET = "market"


class OrderStatus(str, Enum):
    PENDING = "pending"      # created locally, not yet acked by venue
    OPEN = "open"            # resting on the book
    FILLED = "filled"
    PARTIAL = "partial"
    CANCELED = "canceled"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class Market:
    """A single binary outcome that can be traded.

    A real-world event with multiple outcomes (e.g. an election) is represented
    as several `Market`s sharing an `event_id`. `outcome` names this leg.
    """

    venue: Venue
    market_id: str                 # venue-native id (Kalshi ticker / Polymarket token id)
    event_id: str                  # groups mutually-exclusive outcomes
    title: str
    outcome: str                   # e.g. "YES", "Candidate A"
    close_time: float | None = None  # unix ts when trading halts
    tick_size: float = 0.01        # min price increment, in probability units
    min_size: Decimal = Decimal(1)
    metadata: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.venue.value}:{self.market_id}"


@dataclass(frozen=True, slots=True)
class PriceLevel:
    price: float          # probability [0,1]
    size: Decimal         # number of contracts/tokens


@dataclass(frozen=True, slots=True)
class OrderBook:
    market_key: str
    bids: tuple[PriceLevel, ...]   # sorted high -> low
    asks: tuple[PriceLevel, ...]   # sorted low -> high
    ts: float = field(default_factory=time.time)

    @property
    def best_bid(self) -> PriceLevel | None:
        return self.bids[0] if self.bids else None

    @property
    def best_ask(self) -> PriceLevel | None:
        return self.asks[0] if self.asks else None

    @property
    def mid(self) -> float | None:
        if self.best_bid and self.best_ask:
            return (self.best_bid.price + self.best_ask.price) / 2
        return None

    @property
    def spread(self) -> float | None:
        if self.best_bid and self.best_ask:
            return self.best_ask.price - self.best_bid.price
        return None


@dataclass(slots=True)
class Order:
    market: Market
    side: Side
    size: Decimal
    type: OrderType = OrderType.LIMIT
    price: float | None = None        # required for LIMIT; probability units
    client_id: str = ""               # idempotency key we generate
    venue_id: str | None = None       # id assigned by the venue once acked
    status: OrderStatus = OrderStatus.PENDING
    filled_size: Decimal = Decimal(0)
    avg_fill_price: float | None = None
    created_at: float = field(default_factory=time.time)
    reason: str = ""                  # strategy annotation / reject reason

    @property
    def remaining(self) -> Decimal:
        return self.size - self.filled_size

    @property
    def is_terminal(self) -> bool:
        return self.status in (
            OrderStatus.FILLED,
            OrderStatus.CANCELED,
            OrderStatus.REJECTED,
        )


@dataclass(slots=True)
class Fill:
    market_key: str
    side: Side
    size: Decimal
    price: float
    fee: Decimal = Decimal(0)
    order_client_id: str = ""
    ts: float = field(default_factory=time.time)


@dataclass(slots=True)
class Position:
    """Net exposure in a single market, with running average cost."""

    market: Market
    size: Decimal = Decimal(0)        # signed: + is long YES, - is short
    avg_price: float = 0.0            # avg entry probability of current exposure
    realized_pnl: Decimal = Decimal(0)

    def apply(self, fill: Fill) -> None:
        signed = fill.size if fill.side is Side.BUY else -fill.size
        new_size = self.size + signed

        # Adding to the same direction -> blend average price.
        if self.size == 0 or (self.size > 0) == (signed > 0):
            total = abs(self.size) + abs(signed)
            if total > 0:
                self.avg_price = (
                    self.avg_price * float(abs(self.size)) + fill.price * float(abs(signed))
                ) / float(total)
        else:
            # Reducing/closing -> realize PnL on the closed portion.
            closed = min(abs(signed), abs(self.size))
            direction = 1 if self.size > 0 else -1
            self.realized_pnl += Decimal(str((fill.price - self.avg_price) * direction)) * closed
            if abs(signed) > abs(self.size):  # flipped through zero
                self.avg_price = fill.price

        self.size = new_size
        self.realized_pnl -= fill.fee
        if self.size == 0:
            self.avg_price = 0.0

    def unrealized_pnl(self, mark: float) -> Decimal:
        if self.size == 0:
            return Decimal(0)
        direction = 1 if self.size > 0 else -1
        return Decimal(str((mark - self.avg_price) * direction)) * abs(self.size)
