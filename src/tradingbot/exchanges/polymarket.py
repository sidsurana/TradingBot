"""Polymarket adapter.

Polymarket runs a central-limit order book (CLOB) off-chain with on-chain
settlement on Polygon in USDC. Market data is public via the CLOB REST API;
order placement requires EIP-712 signing with a wallet key and is subject to
on-chain allowances. Prices are decimals in [0,1] already — no conversion.

STATUS: market-data reads are structured against the public CLOB endpoints.
Order placement raises until wallet signing (py-clob-client / EIP-712) is wired.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

import httpx
import structlog

from tradingbot.config import PolymarketCreds
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


class PolymarketExchange(Exchange):
    venue = Venue.POLYMARKET

    def __init__(self, creds: PolymarketCreds):
        self._creds = creds
        self._client: httpx.AsyncClient | None = None
        self._clob = None  # py-clob-client signed client, set on connect if configured

    async def connect(self) -> None:
        self._client = httpx.AsyncClient(base_url=self._creds.clob_url, timeout=10.0)
        self._clob = None
        if self._creds.configured:
            self._clob = await asyncio.to_thread(self._build_clob)
        log.info("polymarket.connected", clob_url=self._creds.clob_url,
                 authenticated=self._creds.configured)

    def _build_clob(self):
        """Construct the signed CLOB client (py-clob-client). Blocking; run in a thread."""
        try:
            from py_clob_client.client import ClobClient
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "Polymarket live trading needs py-clob-client: pip install -e '.[live]'"
            ) from exc
        client = ClobClient(
            self._creds.clob_url,
            key=self._creds.private_key,
            chain_id=137,  # Polygon mainnet
            funder=self._creds.funder_address or None,
        )
        client.set_api_creds(client.create_or_derive_api_creds())
        return client

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def list_markets(self, *, event_filter: str | None = None) -> list[Market]:
        assert self._client is not None, "connect() first"
        resp = await self._client.get("/markets", params={"active": "true"})
        resp.raise_for_status()
        out: list[Market] = []
        for m in resp.json().get("data", []):
            if event_filter and event_filter not in m.get("question", ""):
                continue
            condition_id = m.get("condition_id", "")
            for tok in m.get("tokens", []):
                out.append(
                    Market(
                        venue=self.venue,
                        market_id=tok["token_id"],
                        event_id=condition_id,
                        title=m.get("question", condition_id),
                        outcome=tok.get("outcome", "YES"),
                        tick_size=float(m.get("minimum_tick_size", 0.01)),
                        min_size=Decimal(str(m.get("minimum_order_size", 1))),
                        metadata={"raw": m},
                    )
                )
        return out

    async def fetch_order_book(self, market: Market, depth: int = 10) -> OrderBook:
        assert self._client is not None, "connect() first"
        resp = await self._client.get("/book", params={"token_id": market.market_id})
        resp.raise_for_status()
        data = resp.json()
        bids = tuple(
            PriceLevel(price=float(b["price"]), size=Decimal(str(b["size"])))
            for b in sorted(data.get("bids", []), key=lambda x: float(x["price"]), reverse=True)
        )[:depth]
        asks = tuple(
            PriceLevel(price=float(a["price"]), size=Decimal(str(a["size"])))
            for a in sorted(data.get("asks", []), key=lambda x: float(x["price"]))
        )[:depth]
        return OrderBook(market_key=market.key, bids=bids, asks=asks)

    async def place_order(self, order: Order) -> Order:
        if self._clob is None:
            raise RuntimeError("Polymarket not authenticated: set TB_POLY_PRIVATE_KEY and "
                               "install '.[live]' to place live orders.")
        if order.price is None:
            raise ValueError("Polymarket limit order requires a price")
        order.status = await asyncio.to_thread(self._post_order_blocking, order)
        return order

    def _post_order_blocking(self, order: Order) -> OrderStatus:
        """EIP-712 sign + post via py-clob-client. Blocking; run in a thread."""
        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY, SELL

        args = OrderArgs(
            token_id=order.market.market_id,
            price=round(order.price, 3),
            size=float(order.size),
            side=BUY if order.side is Side.BUY else SELL,
        )
        signed = self._clob.create_order(args)
        resp = self._clob.post_order(signed, OrderType.GTC)
        order.venue_id = resp.get("orderID") or resp.get("orderId")
        success = bool(resp.get("success", order.venue_id))
        if not success:
            order.reason = f"polymarket: {resp}"
            return OrderStatus.REJECTED
        log.info("polymarket.order_placed", token=order.market.market_id,
                 venue_id=order.venue_id)
        return OrderStatus.OPEN

    async def cancel_order(self, order: Order) -> Order:
        if self._clob is None or not order.venue_id:
            return order
        await asyncio.to_thread(self._clob.cancel, order.venue_id)
        if not order.is_terminal:
            order.status = OrderStatus.CANCELED
        return order

    async def fetch_positions(self) -> list[Position]:
        # Polymarket positions are derived from on-chain ERC-1155 balances; the
        # portfolio tracker is the source of truth in-process. Left as a no-op
        # until on-chain balance reconciliation is added.
        return []
