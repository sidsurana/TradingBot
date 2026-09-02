"""Polymarket US adapter (api.polymarket.us).

A separate, CFTC-regulated platform from polymarket.com with a different API:
Ed25519 API-key auth (see polymarket_us_auth), REST market data + orders, prices
already decimals in [0,1]. Each market is one binary Yes/No *outcome* of an event
(e.g. one team of "World Series Champion"); the event's mutually-exclusive
outcomes share the same `question`. Order placement uses the /v1/orders endpoint.

Dutch-book safety: an event is only a locked set if it is collectively
exhaustive. Unlike a single polymarket.com conditionId (whose outcome array is
complete by construction), the .us question-group is inferred, so we gate on the
market-implied completeness signal — the group's fair YES prices summing to ~1.
Award/"MVP"-style open fields (a handful of a large field) sum well below 1 and
are excluded; a naive "buy every listed YES for <$1" there is a false arb that
can lose the whole stake.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from decimal import Decimal

import httpx
import structlog

from tradingbot.config import PolymarketUSCreds
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

# A question-group is treated as a complete/exhaustive mutually-exclusive set
# only when its per-outcome fair YES prices sum into this band. The LOWER bound
# is the safety-critical one: an INCOMPLETE field (missing outcomes) sums well
# below 1, and buying every listed YES for <$1 there is a false arb that can lose
# the stake. The upper bound is generous — a complete set's fair probs sum to 1,
# and anything above that is just vig/wide spreads (a wide but complete market),
# which never produces a Σ(ask)<1 fire anyway; the ceiling only rejects glitches.
_EXHAUSTIVE_LO = 0.95
_EXHAUSTIVE_HI = 1.60

_STATUS = {
    "ORDER_STATE_NEW": OrderStatus.OPEN,
    "ORDER_STATE_PENDING_NEW": OrderStatus.PENDING,
    "ORDER_STATE_PARTIALLY_FILLED": OrderStatus.OPEN,
    "ORDER_STATE_FILLED": OrderStatus.FILLED,
    "ORDER_STATE_CANCELED": OrderStatus.CANCELED,
    "ORDER_STATE_REJECTED": OrderStatus.REJECTED,
    "ORDER_STATE_EXPIRED": OrderStatus.CANCELED,
}


def _amount(a) -> float:
    """A Polymarket US `Amount` is {value, currency} (or a bare string)."""
    if isinstance(a, dict):
        a = a.get("value")
    try:
        return float(a)
    except (TypeError, ValueError):
        return 0.0


def _event_key(m: dict) -> str:
    """Group mutually-exclusive outcomes: same question + end date = one event."""
    return f"{m.get('question','')}|{m.get('endDate','')}"


def _fair_yes(m: dict) -> float:
    """Best estimate of this outcome's fair YES probability: the marked
    outcomePrice when present, else the live BBO mid, else whatever side quotes."""
    op = m.get("outcomePrices") or []
    v = _amount(op[0]) if op else 0.0
    if v > 0:
        return v
    bid, ask = _amount(m.get("bestBidQuote")), _amount(m.get("bestAskQuote"))
    if bid and ask:
        return (bid + ask) / 2
    return ask or bid


def parse_polymarket_us_markets(data: list, venue: Venue) -> list[Market]:
    """Turn a page of Polymarket US markets into YES-outcome Market legs, grouped
    into mutually-exclusive events. For an exhaustive event (fair YES prices sum
    ~1) each leg gets a distinct outcome + `num_outcomes` so the dutch-book can
    require the complete set; other groups stay single-outcome and never form a
    (false) set. Pure (no network)."""
    groups: dict[str, list[dict]] = defaultdict(list)
    for m in data:
        if m.get("slug"):
            groups[_event_key(m)].append(m)

    out: list[Market] = []
    for _, members in groups.items():
        fair_sum = sum(_fair_yes(m) for m in members)
        exhaustive = (len(members) >= 2 and _EXHAUSTIVE_LO <= fair_sum <= _EXHAUSTIVE_HI)
        n = len(members)
        for m in members:
            slug = m["slug"]
            meta: dict = {
                "volume": _amount(m.get("volume24hr") or m.get("volume")),
                "category": (m.get("category") or "").lower(),
                "fee_coefficient": _amount(m.get("feeCoefficient")),
                "raw": m,
            }
            if exhaustive:
                outcome = m.get("title") or slug
                meta["num_outcomes"] = n
            else:
                outcome = "YES"
            out.append(Market(
                venue=venue,
                market_id=slug,
                event_id=_event_key(m),
                title=m.get("question") or m.get("title") or slug,
                outcome=str(outcome),
                tick_size=float(m.get("orderPriceMinTickSize") or 0.01),
                min_size=Decimal(str(m.get("minimumTradeQty") or 1)),
                metadata=meta,
            ))
    return out


class PolymarketUSExchange(Exchange):
    venue = Venue.POLYMARKET

    def __init__(self, creds: PolymarketUSCreds):
        self._creds = creds
        self._client: httpx.AsyncClient | None = None
        self._sign = None  # (method, path) -> auth headers

    async def connect(self) -> None:
        self._client = httpx.AsyncClient(base_url=self._creds.base_url, timeout=15.0)
        if self._creds.configured:
            from tradingbot.exchanges.polymarket_us_auth import load_signer

            self._sign = load_signer(self._creds.key_id, self._creds.secret_key)
        log.info("polymarket_us.connected", base_url=self._creds.base_url,
                 authenticated=self._creds.configured)

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    def _headers(self, method: str, path: str) -> dict:
        if self._sign is None:
            raise RuntimeError("Polymarket US not authenticated: set TB_POLYUS_KEY_ID "
                               "+ TB_POLYUS_SECRET_KEY.")
        return self._sign(method, path)

    async def list_markets(self, *, event_filter: str | None = None) -> list[Market]:
        assert self._client is not None, "connect() first"
        path = "/v1/markets"
        cap = self._creds.discovery_max
        # Collect ALL raw markets first, THEN group once — a mutually-exclusive
        # event's outcomes can straddle page boundaries, and grouping per-page
        # would fragment the set (and defeat the exhaustiveness check).
        raw_all: list[dict] = []
        offset = 0
        pages = 0
        while len(raw_all) < cap and pages < 20:
            params = {"active": "true", "closed": "false", "limit": 100, "offset": offset}
            resp = await self._client.get(path, params=params, headers=self._headers("GET", path))
            resp.raise_for_status()
            raw = resp.json().get("markets", [])
            if not raw:
                break
            raw_all += raw
            offset += len(raw)
            pages += 1
        out = parse_polymarket_us_markets(raw_all, self.venue)
        if event_filter:
            ef = event_filter.lower()
            out = [m for m in out if ef in m.title.lower()]
        return out

    async def fetch_order_book(self, market: Market, depth: int = 10) -> OrderBook:
        assert self._client is not None, "connect() first"
        path = f"/v1/markets/{market.market_id}/book"
        resp = await self._client.get(path, headers=self._headers("GET", path))
        resp.raise_for_status()
        md = resp.json().get("marketData", {})
        bids = tuple(
            PriceLevel(price=_amount(b["px"]), size=Decimal(str(b["qty"])))
            for b in md.get("bids", [])
        )[:depth]
        asks = tuple(
            PriceLevel(price=_amount(a["px"]), size=Decimal(str(a["qty"])))
            for a in md.get("offers", [])
        )[:depth]
        return OrderBook(market_key=market.key, bids=bids, asks=asks)

    async def place_order(self, order: Order) -> Order:
        assert self._client is not None, "connect() first"
        if self._sign is None:
            raise RuntimeError("Polymarket US not authenticated.")
        if order.price is None:
            raise ValueError("Polymarket US limit order requires a price")
        path = "/v1/orders"
        tick = order.market.tick_size or 0.01
        price = round(round(order.price / tick) * tick, 6)
        # FOK legs (dutch-book) execute synchronously and fill-or-kill so the
        # caller learns the definitive outcome inline and can unwind on failure.
        fok = order.time_in_force == "FOK"
        payload = {
            "marketSlug": order.market.market_id,
            "type": "ORDER_TYPE_LIMIT",
            "outcomeSide": "OUTCOME_SIDE_YES",  # legs are modeled as YES exposure
            "action": "ORDER_ACTION_BUY" if order.side is Side.BUY else "ORDER_ACTION_SELL",
            "price": {"value": f"{price:.6f}", "currency": "USD"},
            "quantity": float(order.size),
            "tif": "TIME_IN_FORCE_FILL_OR_KILL" if fok else "TIME_IN_FORCE_GOOD_TILL_CANCEL",
        }
        if fok:
            payload["synchronousExecution"] = True
        resp = await self._client.post(path, json=payload, headers=self._headers("POST", path))
        if resp.status_code >= 400:
            order.status = OrderStatus.REJECTED
            order.reason = f"polymarket_us {resp.status_code}: {resp.text[:200]}"
            log.warning("polymarket_us.order_rejected", reason=order.reason)
            return order
        data = resp.json()
        order.venue_id = data.get("id")
        execs = data.get("executions") or []
        filled = sum(_amount(e.get("quantity") or e.get("fillQty") or 0) for e in execs)
        if filled > 0:
            order.filled_size = Decimal(str(filled))
            order.avg_fill_price = price
        state = execs[-1].get("orderState") if execs else None
        if state:
            order.status = _STATUS.get(state, OrderStatus.OPEN)
        elif fok:
            # Synchronous FOK with no execution reported -> it was killed unfilled.
            order.status = OrderStatus.FILLED if order.filled_size >= order.size else OrderStatus.CANCELED
        else:
            order.status = OrderStatus.OPEN
        log.info("polymarket_us.order_placed", slug=order.market.market_id,
                 venue_id=order.venue_id, status=order.status.value,
                 filled=str(order.filled_size))
        return order

    async def cancel_order(self, order: Order) -> Order:
        if self._sign is None or self._client is None or not order.venue_id:
            return order
        # Cancel is POST /v1/order/{id}/cancel (singular "order") with a
        # marketSlug body — NOT DELETE /v1/orders/{id}.
        path = f"/v1/order/{order.venue_id}/cancel"
        resp = await self._client.post(
            path, json={"marketSlug": order.market.market_id},
            headers=self._headers("POST", path))
        if resp.status_code < 400 and not order.is_terminal:
            order.status = OrderStatus.CANCELED
        return order

    async def fetch_positions(self) -> list[Position]:
        if self._sign is None or self._client is None:
            return []
        path = "/v1/portfolio/positions"
        resp = await self._client.get(path, headers=self._headers("GET", path))
        resp.raise_for_status()
        out: list[Position] = []
        positions = resp.json().get("positions", {})
        for slug, p in positions.items():
            qty = int(float(p.get("netPosition", 0) or 0))
            if qty == 0:
                continue
            cost = _amount(p.get("cost"))
            avg = (cost / abs(qty)) if qty else 0.0
            meta = p.get("marketMetadata", {})
            market = Market(venue=self.venue, market_id=slug, event_id=slug,
                            title=meta.get("title", slug), outcome=meta.get("outcome", "YES"))
            out.append(Position(market=market, size=Decimal(qty), avg_price=avg))
        return out

    async def fetch_balance(self) -> float:
        """Cash buying power in USD (0.0 if unavailable). Never raises."""
        if self._sign is None or self._client is None:
            return 0.0
        path = "/v1/account/balances"
        try:
            resp = await self._client.get(path, headers=self._headers("GET", path))
            resp.raise_for_status()
            bals = resp.json().get("balances", [])
            return float(bals[0].get("buyingPower", 0.0)) if bals else 0.0
        except Exception as exc:  # noqa: BLE001
            log.warning("polymarket_us.balance_error", error=str(exc))
            return 0.0
