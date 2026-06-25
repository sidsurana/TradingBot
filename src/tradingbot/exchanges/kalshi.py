"""Kalshi adapter.

Kalshi is a CFTC-regulated US exchange. REST base documented at
https://trading-api.readme.io/. Auth uses an API key id plus an RSA private key
that signs each request (timestamp + method + path). Prices are integer cents
1..99; we convert to/from probability [0,1].

STATUS: market-data reads are structured and ready to wire to live endpoints.
Order placement raises until credentials + request signing are completed — this
is deliberate so paper mode is the only thing that can run unconfigured.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import httpx
import structlog

from tradingbot.config import KalshiCreds
from tradingbot.exchanges.base import Exchange
from tradingbot.models import (
    Market,
    Order,
    OrderBook,
    OrderStatus,
    PriceLevel,
    Position,
    Side,
    Venue,
)

log = structlog.get_logger(__name__)

ORDERS_PATH = "/trade-api/v2/portfolio/orders"
POSITIONS_PATH = "/trade-api/v2/portfolio/positions"

# Kalshi order status -> our status.
_STATUS = {
    "resting": OrderStatus.OPEN,
    "executed": OrderStatus.FILLED,
    "canceled": OrderStatus.CANCELED,
    "pending": OrderStatus.PENDING,
}


def cents_to_prob(cents: int) -> float:
    return cents / 100.0


def prob_to_cents(prob: float) -> int:
    return max(1, min(99, round(prob * 100)))


class KalshiExchange(Exchange):
    venue = Venue.KALSHI

    def __init__(self, creds: KalshiCreds):
        self._creds = creds
        self._client: httpx.AsyncClient | None = None
        self._sign = None  # (method, path) -> auth headers, set on connect if configured

    async def connect(self) -> None:
        self._client = httpx.AsyncClient(base_url=self._creds.base_url, timeout=10.0)
        if self._creds.configured:
            from tradingbot.exchanges.kalshi_auth import load_signer

            self._sign = load_signer(self._creds.api_key_id, self._creds.private_key_path)
        log.info("kalshi.connected", base_url=self._creds.base_url,
                 authenticated=self._creds.configured)

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def list_markets(self, *, event_filter: str | None = None) -> list[Market]:
        assert self._client is not None, "connect() first"
        params: dict = {"status": "open", "limit": 200}
        if event_filter:
            params["series_ticker"] = event_filter
        resp = await self._client.get("/trade-api/v2/markets", params=params)
        resp.raise_for_status()
        out: list[Market] = []
        for m in resp.json().get("markets", []):
            out.append(
                Market(
                    venue=self.venue,
                    market_id=m["ticker"],
                    event_id=m.get("event_ticker", m["ticker"]),
                    title=m.get("title", m["ticker"]),
                    outcome="YES",
                    close_time=None,
                    tick_size=0.01,
                    min_size=Decimal(1),
                    metadata={"raw": m},
                )
            )
        return out

    async def fetch_order_book(self, market: Market, depth: int = 10) -> OrderBook:
        assert self._client is not None, "connect() first"
        resp = await self._client.get(
            f"/trade-api/v2/markets/{market.market_id}/orderbook",
            params={"depth": depth},
        )
        resp.raise_for_status()
        ob = resp.json().get("orderbook", {})
        # Kalshi returns YES bids and NO bids; a NO bid at price p is a YES ask
        # at (100 - p). Normalize both onto the YES probability book.
        yes = ob.get("yes") or []
        no = ob.get("no") or []
        bids = tuple(
            PriceLevel(price=cents_to_prob(c), size=Decimal(str(s)))
            for c, s in sorted(yes, reverse=True)
        )
        asks = tuple(
            PriceLevel(price=cents_to_prob(100 - c), size=Decimal(str(s)))
            for c, s in sorted(no)
        )
        return OrderBook(market_key=market.key, bids=bids, asks=asks)

    async def place_order(self, order: Order) -> Order:
        assert self._client is not None, "connect() first"
        if self._sign is None:
            raise RuntimeError("Kalshi not authenticated: set TB_KALSHI_API_KEY_ID + "
                               "TB_KALSHI_PRIVATE_KEY_PATH to place live orders.")
        if order.price is None:
            raise ValueError("Kalshi limit order requires a price")
        order.client_id = order.client_id or uuid.uuid4().hex
        payload = {
            "ticker": order.market.market_id,
            "client_order_id": order.client_id,
            "side": "yes",  # we normalize NO exposure to the complementary market
            "action": "buy" if order.side is Side.BUY else "sell",
            "count": int(order.size),
            "type": "limit",
            "yes_price": prob_to_cents(order.price),
        }
        resp = await self._client.post(
            ORDERS_PATH, json=payload, headers=self._sign("POST", ORDERS_PATH)
        )
        if resp.status_code >= 400:
            order.status = OrderStatus.REJECTED
            order.reason = f"kalshi {resp.status_code}: {resp.text[:200]}"
            log.warning("kalshi.order_rejected", reason=order.reason)
            return order
        data = resp.json().get("order", {})
        order.venue_id = data.get("order_id")
        order.status = _STATUS.get(data.get("status", ""), OrderStatus.PENDING)
        if order.status is OrderStatus.FILLED:
            order.filled_size = order.size
            order.avg_fill_price = order.price
        log.info("kalshi.order_placed", ticker=order.market.market_id,
                 venue_id=order.venue_id, status=order.status.value)
        return order

    async def cancel_order(self, order: Order) -> Order:
        assert self._client is not None and self._sign is not None
        if not order.venue_id:
            return order
        path = f"{ORDERS_PATH}/{order.venue_id}"
        resp = await self._client.delete(path, headers=self._sign("DELETE", path))
        if resp.status_code < 400 and not order.is_terminal:
            order.status = OrderStatus.CANCELED
        return order

    async def fetch_positions(self) -> list[Position]:
        if self._sign is None or self._client is None:
            return []
        resp = await self._client.get(POSITIONS_PATH, headers=self._sign("GET", POSITIONS_PATH))
        resp.raise_for_status()
        out: list[Position] = []
        for mp in resp.json().get("market_positions", []):
            count = int(mp.get("position", 0))
            if count == 0:
                continue
            ticker = mp.get("ticker", "")
            market = Market(venue=self.venue, market_id=ticker, event_id=ticker,
                            title=ticker, outcome="YES")
            # Kalshi reports exposure in cents; approximate average entry probability.
            exposure = abs(int(mp.get("market_exposure", 0)))
            avg = (exposure / abs(count)) / 100.0 if count else 0.0
            out.append(Position(market=market, size=Decimal(count), avg_price=avg))
        return out
