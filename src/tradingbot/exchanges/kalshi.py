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

import asyncio
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

# V2 single-book orders endpoint (the legacy /portfolio/orders now 410s).
ORDERS_V2_PATH = "/trade-api/v2/portfolio/events/orders"
POSITIONS_PATH = "/trade-api/v2/portfolio/positions"

# Kalshi order status -> our status.
_STATUS = {
    "resting": OrderStatus.OPEN,
    "open": OrderStatus.OPEN,
    "executed": OrderStatus.FILLED,
    "filled": OrderStatus.FILLED,
    "canceled": OrderStatus.CANCELED,
    "cancelled": OrderStatus.CANCELED,
    "pending": OrderStatus.PENDING,
}


def parse_kalshi_markets(markets: list, venue: Venue) -> list[Market]:
    """Turn Kalshi market objects into Market legs, carrying 24h volume and
    category in metadata for the universe selector. Pure (no network)."""
    out: list[Market] = []
    for m in markets:
        out.append(Market(
            venue=venue,
            market_id=m["ticker"],
            event_id=m.get("event_ticker", m["ticker"]),
            title=m.get("title", m["ticker"]),
            outcome="YES",
            tick_size=0.01,
            min_size=Decimal(1),
            metadata={
                # 2026 wire format renamed volume -> volume_24h_fp / volume_fp
                # (fixed-point strings); keep the legacy key as a fallback.
                "volume": float(m.get("volume_24h_fp")
                                or m.get("volume_fp")
                                or m.get("volume") or 0),
                "category": (m.get("category") or "").lower(),
                "raw": m,
            },
        ))
    return out


# Strike types whose markets tile a numeric line into a partition. A
# mutually-exclusive event built from ONLY these is collectively exhaustive —
# exactly one bucket resolves YES — so buying one of every outcome for < $1 is a
# genuine locked set. Named-candidate events (strike_type "custom"/None) are NOT
# exhaustive: an unlisted outcome can win, so their YES legs summing < $1 is a
# false dutch book that can lose the whole stake. We only stamp dutch-book
# outcomes for events that pass this gate.
_RANGE_STRIKE_TYPES = {"between", "greater", "less",
                       "greater_or_equal", "less_or_equal"}


def event_is_exhaustive(event: dict) -> bool:
    """True iff this event is a safe dutch-book set: mutually exclusive AND
    collectively exhaustive (every leg is a numeric range/bucket)."""
    if not event.get("mutually_exclusive"):
        return False
    markets = event.get("markets") or []
    if len(markets) < 2:
        return False
    types = {str(m.get("strike_type")) for m in markets}
    return bool(types) and types.issubset(_RANGE_STRIKE_TYPES)


def parse_kalshi_event_markets(event: dict, venue: Venue) -> list[Market]:
    """Turn one Kalshi event (with nested markets) into Market legs. For an
    exhaustive mutually-exclusive event, each nested market is a DISTINCT outcome
    of the same event: give it a unique `outcome` and stamp `num_outcomes` so the
    arbitrage strategy can require the COMPLETE set before locking a dutch book.
    For every other event, keep the single-YES modeling (outcome="YES", no
    `num_outcomes`) so those legs can never form a (false) dutch-book set."""
    markets = event.get("markets") or []
    event_ticker = event.get("event_ticker", "")
    category = (event.get("category") or "").lower()
    exhaustive = event_is_exhaustive(event)
    n = len(markets)
    out: list[Market] = []
    for m in markets:
        ticker = m.get("ticker")
        if not ticker:
            continue
        meta: dict = {
            "volume": float(m.get("volume_24h_fp")
                            or m.get("volume_fp")
                            or m.get("volume") or 0),
            "category": category,
            "raw": m,
        }
        if exhaustive:
            # Ticker suffix is unique within the event -> guarantees distinct
            # outcome keys (a collision would just make the set look incomplete
            # and skip, never a false lock).
            outcome = ticker.rsplit("-", 1)[-1]
            meta["num_outcomes"] = n
        else:
            outcome = "YES"
        out.append(Market(
            venue=venue,
            market_id=ticker,
            event_id=event_ticker or ticker,
            title=m.get("title") or event.get("title") or ticker,
            outcome=str(outcome),
            tick_size=0.01,
            min_size=Decimal(1),
            metadata=meta,
        ))
    return out


def cents_to_prob(cents: int) -> float:
    return cents / 100.0


def prob_to_cents(prob: float) -> int:
    return max(1, min(99, round(prob * 100)))


def parse_kalshi_orderbook(ob: dict, market_key: str) -> OrderBook:
    """Build a unified YES-probability OrderBook from Kalshi's `orderbook_fp`.

    The 2026 wire format sends dollar-denominated prices (already in [0,1]) and
    fixed-point, possibly fractional, sizes — both as strings:

        {"yes_dollars": [["0.62", "1200.00"], ...],   # resting YES bids
         "no_dollars":  [["0.41", "800.00"], ...]}    # resting NO bids

    A NO bid at price p is a YES ask at (1 - p), so both fold onto the YES book.
    (Superseded the old integer-cents `{"yes": [[62, 1200]], "no": [...]}` shape.)
    The 1 - p conversion is rounded to Kalshi's 4-decimal price grid to keep the
    result off float dust (1 - 0.55 would otherwise be 0.44999999999999996).
    """
    yes = ob.get("yes_dollars") or []
    no = ob.get("no_dollars") or []
    bids = tuple(sorted(
        (PriceLevel(price=float(p), size=Decimal(str(s))) for p, s in yes),
        key=lambda lvl: lvl.price, reverse=True))
    asks = tuple(sorted(
        (PriceLevel(price=round(1.0 - float(p), 4), size=Decimal(str(s)))
         for p, s in no),
        key=lambda lvl: lvl.price))
    return OrderBook(market_key=market_key, bids=bids, asks=asks)


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
        # Kalshi's status=open listing is ~all auto-generated KXMVECROSSCATEGORY
        # parlay markets (empty books, no volume), so real markets are pulled by
        # series. An explicit event_filter selects one series; otherwise the
        # configured set (self._creds.series) is swept. With neither, fall back to
        # the raw open listing with the MVE junk filtered out.
        series = [event_filter] if event_filter else list(self._creds.series)
        if not series:
            return await self._list_open(exclude_mve=True)
        cap = self._creds.discovery_max
        out: dict[str, Market] = {}
        for s in series:
            if len(out) >= cap:
                break
            for m in await self._list_series(s, cap - len(out)):
                out[m.market_id] = m
            await asyncio.sleep(0.1)  # gentle spacing to stay under the rate limit
        return list(out.values())

    async def _list_series(self, series_ticker: str, cap: int) -> list[Market]:
        """Fetch open markets for one series via the EVENTS endpoint (nested
        markets), so event grouping + mutual-exclusivity is preserved and the
        dutch-book can see complete outcome sets. Paginates by cursor up to `cap`;
        bounded pages/attempts so a persistent 429 can't wedge discovery."""
        markets: list[Market] = []
        cursor: str | None = None
        pages = attempts = 0
        while len(markets) < cap and pages < 6 and attempts < 12:
            attempts += 1
            params: dict = {"series_ticker": series_ticker, "status": "open",
                            "with_nested_markets": "true", "limit": 200}
            if cursor:
                params["cursor"] = cursor
            resp = await self._client.get("/trade-api/v2/events", params=params)
            if resp.status_code == 429:
                await asyncio.sleep(1.0)
                continue
            resp.raise_for_status()
            body = resp.json()
            for event in body.get("events", []):
                markets += parse_kalshi_event_markets(event, self.venue)
            cursor = body.get("cursor")
            pages += 1
            if not cursor:
                break
        return markets[:cap]

    async def _list_open(self, exclude_mve: bool = True) -> list[Market]:
        """Fallback discovery: the raw open listing, minus MVE parlay junk."""
        resp = await self._client.get(
            "/trade-api/v2/markets", params={"status": "open", "limit": 200})
        resp.raise_for_status()
        raw = resp.json().get("markets", [])
        if exclude_mve:
            raw = [m for m in raw if not m.get("ticker", "").startswith("KXMVE")]
        return parse_kalshi_markets(raw, self.venue)

    async def fetch_order_book(self, market: Market, depth: int = 10) -> OrderBook:
        assert self._client is not None, "connect() first"
        resp = await self._client.get(
            f"/trade-api/v2/markets/{market.market_id}/orderbook",
            params={"depth": depth},
        )
        resp.raise_for_status()
        ob = resp.json().get("orderbook_fp", {})
        return parse_kalshi_orderbook(ob, market.key)

    async def place_order(self, order: Order) -> Order:
        assert self._client is not None, "connect() first"
        if self._sign is None:
            raise RuntimeError("Kalshi not authenticated: set TB_KALSHI_API_KEY_ID + "
                               "TB_KALSHI_PRIVATE_KEY_PATH to place live orders.")
        if order.price is None:
            raise ValueError("Kalshi limit order requires a price")
        order.client_id = order.client_id or uuid.uuid4().hex
        tif = {"FOK": "fill_or_kill", "IOC": "immediate_or_cancel"}.get(
            order.time_in_force, "good_till_canceled")
        # V2 single-book model: side bid = buy YES, ask = sell YES; price is a
        # fixed-point DOLLAR string in [0,1] (not integer cents); count is
        # fixed-point too.
        payload = {
            "ticker": order.market.market_id,
            "client_order_id": order.client_id,
            "side": "bid" if order.side is Side.BUY else "ask",
            "count": f"{float(order.size):.2f}",
            "type": "limit",
            "price": f"{order.price:.4f}",
            "time_in_force": tif,
            "self_trade_prevention_type": "taker_at_cross",
            # Kalshi shards matching across exchanges; -1 auto-routes to the shard
            # holding this ticker (a fixed 0 hit the wrong shard -> user_not_found).
            "exchange_index": -1,
        }
        resp = await self._client.post(
            ORDERS_V2_PATH, json=payload, headers=self._sign("POST", ORDERS_V2_PATH)
        )
        if resp.status_code >= 400:
            order.status = OrderStatus.REJECTED
            order.reason = f"kalshi {resp.status_code}: {resp.text[:200]}"
            log.warning("kalshi.order_rejected", reason=order.reason)
            return order
        # V2 create response is FLAT (not nested under "order") and carries no
        # status string — infer it from fill/remaining counts:
        #   {order_id, fill_count, remaining_count, average_fill_price, ...}
        data = resp.json()
        order.venue_id = data.get("order_id") or data.get("id")

        def _dec(x) -> Decimal:
            try:
                return Decimal(str(x))
            except (ValueError, TypeError):
                return Decimal(0)

        fill = _dec(data.get("fill_count") or data.get("filled_count") or 0)
        remaining = _dec(data.get("remaining_count")) if data.get("remaining_count") is not None \
            else (order.size - fill)
        if fill > 0:
            order.filled_size = fill
            avg = data.get("average_fill_price")
            order.avg_fill_price = float(avg) if avg not in (None, "") else order.price
        if remaining <= 0 and fill > 0:
            order.status = OrderStatus.FILLED
        elif fill > 0:
            order.status = OrderStatus.PARTIAL
        elif order.time_in_force in ("FOK", "IOC"):
            order.status = OrderStatus.CANCELED   # marketable-only, nothing filled
        elif order.venue_id:
            order.status = OrderStatus.OPEN       # accepted, resting
        else:
            order.status = OrderStatus.PENDING
        log.info("kalshi.order_placed", ticker=order.market.market_id, venue_id=order.venue_id,
                 status=order.status.value, filled=str(order.filled_size))
        return order

    def _shard(self, order: Order):
        """The exchange shard a market's orders live on (from market metadata),
        so cancels target the right instance; -1 lets Kalshi auto-route."""
        idx = (order.market.metadata.get("raw") or {}).get("exchange_index")
        return idx if idx is not None else -1

    async def cancel_order(self, order: Order) -> Order:
        assert self._client is not None and self._sign is not None
        if not order.venue_id:
            return order
        path = f"{ORDERS_V2_PATH}/{order.venue_id}"
        resp = await self._client.request(
            "DELETE", path, params={"exchange_index": self._shard(order)},
            headers=self._sign("DELETE", path))
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

    async def fetch_balance(self) -> float:
        """Total cash balance in USD across shards (0.0 if unavailable). Never raises."""
        if self._sign is None or self._client is None:
            return 0.0
        path = "/trade-api/v2/portfolio/balance"
        try:
            resp = await self._client.get(path, headers=self._sign("GET", path))
            resp.raise_for_status()
            return float(resp.json().get("balance_dollars") or 0.0)
        except Exception as exc:  # noqa: BLE001
            log.warning("kalshi.balance_error", error=str(exc))
            return 0.0
