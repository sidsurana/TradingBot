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
import json
from datetime import datetime
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

# Gamma is Polymarket's market-metadata API. Unlike the CLOB /markets endpoint
# (which returns mostly closed markets under active=true), Gamma lets us filter
# to genuinely tradeable markets and sort by 24h volume.
GAMMA_URL = "https://gamma-api.polymarket.com/markets"


def parse_gamma_markets(data: list, venue: Venue, event_filter: str | None = None) -> list[Market]:
    """Turn Gamma market objects into tradeable Market legs, carrying 24h volume
    and category in metadata for the universe selector. Pure (no network)."""
    out: list[Market] = []
    for m in data:
        question = m.get("question", "")
        if event_filter and event_filter.lower() not in question.lower():
            continue
        ids = m.get("clobTokenIds")
        outcomes = m.get("outcomes")
        try:
            ids = json.loads(ids) if isinstance(ids, str) else (ids or [])
            outcomes = json.loads(outcomes) if isinstance(outcomes, str) else (outcomes or [])
        except (json.JSONDecodeError, TypeError):
            continue
        if not ids:
            continue
        condition = m.get("conditionId") or m.get("condition_id") or ""
        volume = float(m.get("volume24hr") or m.get("volume") or 0)
        category = (m.get("category") or "").lower()
        tick = float(m.get("orderPriceMinTickSize") or m.get("minimum_tick_size") or 0.01)
        # Sports / resolution flags so strategies can avoid event-driven, in-play, or
        # about-to-resolve markets (adverse-selection traps for a market maker).
        is_sports = bool(m.get("sportsMarketType") or m.get("gameStartTime"))
        uma_status = str(m.get("umaResolutionStatus") or "").lower()
        end_iso = m.get("endDateIso") or m.get("endDate") or ""
        end_ts = 0.0
        if end_iso:
            try:
                end_ts = datetime.fromisoformat(str(end_iso).replace("Z", "+00:00")).timestamp()
            except (ValueError, TypeError):
                end_ts = 0.0
        for i, token_id in enumerate(ids):
            outcome = outcomes[i] if i < len(outcomes) else "YES"
            out.append(Market(
                venue=venue, market_id=str(token_id), event_id=condition or str(token_id),
                title=question or condition, outcome=str(outcome), tick_size=tick,
                metadata={"volume": volume, "category": category,
                          "is_sports": is_sports, "end_ts": end_ts,
                          "uma_status": uma_status},
            ))
    return out


def parse_gamma_resolutions(data: list, held_keys: set[str]) -> dict[str, float]:
    """Map held Polymarket token keys to their redemption value (1.0 winner,
    0.0 loser) for markets that have FINALLY resolved. Pure (no network).

    A market counts as resolved only when umaResolutionStatus == "resolved"
    (empty / "proposed" / "disputed" are non-final and OMITTED) and its
    outcomePrices array — aligned to outcomes/clobTokenIds — is present. The
    winning outcome carries a price of ~1.0, the loser ~0.0."""
    out: dict[str, float] = {}
    for m in data:
        uma = str(m.get("umaResolutionStatus") or "").lower()
        if uma != "resolved":
            continue  # non-final (proposed / disputed / open) -> not redeemable yet
        ids = m.get("clobTokenIds")
        prices = m.get("outcomePrices")
        try:
            ids = json.loads(ids) if isinstance(ids, str) else (ids or [])
            prices = json.loads(prices) if isinstance(prices, str) else (prices or [])
        except (json.JSONDecodeError, TypeError):
            continue
        if not ids or not prices or len(prices) < len(ids):
            continue  # missing outcome prices -> can't determine the winner
        for i, token_id in enumerate(ids):
            key = f"{Venue.POLYMARKET.value}:{token_id}"
            if key not in held_keys:
                continue
            try:
                p = float(prices[i])
            except (ValueError, TypeError):
                continue
            out[key] = 1.0 if p >= 0.5 else 0.0
    return out


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
        # Gamma, server-side filtered to tradeable markets, sorted by 24h volume.
        # Gamma caps a page at 100; paginate by offset up to discovery_max so broad
        # coverage is possible. The universe selector then keeps the most liquid N.
        target = self._creds.discovery_max
        out: list[Market] = []
        offset = 0
        while len(out) < target:
            resp = await self._client.get(GAMMA_URL, params={
                "active": "true", "closed": "false", "archived": "false",
                "order": "volume24hr", "ascending": "false",
                "limit": 100, "offset": offset,
            })
            resp.raise_for_status()
            raw = resp.json()
            if not raw:
                break  # ran out of markets
            out += parse_gamma_markets(raw, self.venue, event_filter)
            offset += len(raw)  # advance by the raw page size Gamma returned
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

    async def fetch_resolutions(self, markets: list[Market]) -> dict[str, float]:
        """For the given held tokens, return {market.key: 1.0|0.0} ONLY for
        markets that have finally resolved on-chain. Resolved markets leave the
        active (closed=false) universe, so we must query Gamma by their
        conditionIds INCLUDING closed markets. Never raises: network / parse
        failures are logged and yield a partial (or empty) result so the
        settlement poller keeps running."""
        if self._client is None or not markets:
            return {}
        held_keys = {m.key for m in markets}
        condition_ids = sorted({m.event_id for m in markets if m.event_id})
        out: dict[str, float] = {}
        # Gamma caps query params; chunk the conditionIds defensively.
        for i in range(0, len(condition_ids), 20):
            chunk = condition_ids[i : i + 20]
            params: list[tuple[str, object]] = [("condition_ids", c) for c in chunk]
            params += [("closed", "true"), ("limit", 100)]
            try:
                resp = await self._client.get(GAMMA_URL, params=params)
                resp.raise_for_status()
                raw = resp.json()
            except Exception as exc:  # noqa: BLE001 — never break the poll loop
                log.warning("polymarket.resolutions_error", error=str(exc))
                continue
            try:
                out.update(parse_gamma_resolutions(raw, held_keys))
            except Exception as exc:  # noqa: BLE001 — defensive against odd payloads
                log.warning("polymarket.resolutions_parse_error", error=str(exc))
        return out

    async def fetch_positions(self) -> list[Position]:
        # Polymarket positions are derived from on-chain ERC-1155 balances; the
        # portfolio tracker is the source of truth in-process. Left as a no-op
        # until on-chain balance reconciliation is added.
        return []
