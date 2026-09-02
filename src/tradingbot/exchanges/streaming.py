"""WebSocket streaming order books.

Replaces per-tick REST polling (which is ~50 ms/market and gets rate-limited at
scale) with push updates: one WebSocket per venue maintains a live local order
book, so the engine reads top-of-book instantly and data is sub-second fresh.

Layers:
  - LocalBook: maintains one market's bid/ask ladder from snapshot + deltas, and
    renders the unified OrderBook on demand. Venue-agnostic (prices already in
    [0,1]); fully unit-tested.
  - parse_kalshi_message / parse_polymarket_message: translate each venue's wire
    messages into LocalBook updates. Unit-tested with synthetic messages.
  - StreamManager: holds the LocalBooks + runs the per-venue client tasks.
  - KalshiStream / PolymarketStream: connect, subscribe, and feed the books.
    The connect/subscribe wire details are best-effort and need live validation.
"""

from __future__ import annotations

import asyncio
import json
import time
from decimal import Decimal

import structlog

from tradingbot.models import Market, OrderBook, PriceLevel, Venue

log = structlog.get_logger(__name__)


class LocalBook:
    """A single market's order book, maintained incrementally."""

    def __init__(self, market_key: str):
        self.market_key = market_key
        self.bids: dict[float, Decimal] = {}   # price -> size
        self.asks: dict[float, Decimal] = {}
        self.ts = 0.0

    def set_snapshot(self, bids, asks) -> None:
        self.bids = {float(p): Decimal(str(s)) for p, s in bids if Decimal(str(s)) > 0}
        self.asks = {float(p): Decimal(str(s)) for p, s in asks if Decimal(str(s)) > 0}
        self.ts = time.time()

    def set_level(self, is_bid: bool, price: float, size) -> None:
        """Absolute set (Polymarket price_change): size at price; 0 removes it."""
        side = self.bids if is_bid else self.asks
        size = Decimal(str(size))
        if size <= 0:
            side.pop(float(price), None)
        else:
            side[float(price)] = size
        self.ts = time.time()

    def apply_delta(self, is_bid: bool, price: float, delta) -> None:
        """Additive change (Kalshi orderbook_delta): adjust size at price by delta."""
        side = self.bids if is_bid else self.asks
        new = side.get(float(price), Decimal(0)) + Decimal(str(delta))
        if new <= 0:
            side.pop(float(price), None)
        else:
            side[float(price)] = new
        self.ts = time.time()

    def to_order_book(self, depth: int = 10) -> OrderBook:
        bids = tuple(
            PriceLevel(price=p, size=s)
            for p, s in sorted(self.bids.items(), key=lambda x: x[0], reverse=True)[:depth]
        )
        asks = tuple(
            PriceLevel(price=p, size=s)
            for p, s in sorted(self.asks.items(), key=lambda x: x[0])[:depth]
        )
        return OrderBook(market_key=self.market_key, bids=bids, asks=asks, ts=self.ts)


def parse_kalshi_message(msg: dict, key_by_ticker: dict[str, str],
                         books: dict[str, LocalBook], dirty: set | None = None) -> None:
    """Apply a Kalshi orderbook_snapshot / orderbook_delta message. Adds any
    market whose book changed to `dirty` (for the event-driven reactor).

    2026 wire format (dollar-denominated, string-valued):
      snapshot: {type:"orderbook_snapshot", msg:{market_ticker, yes_dollars_fp:
          [["0.40","10.00"]], no_dollars_fp:[["0.55","7.00"]]}}
      delta:    {type:"orderbook_delta", msg:{market_ticker, price_dollars:"0.40",
          delta_fp:"5.00", side:"yes"|"no"}}
    Prices are already probabilities in [0,1]; sizes may be fractional. A NO level
    at price p is a YES ask at (1 - p), so both fold onto the unified YES book.
    The 1 - p conversion is rounded to Kalshi's 4-decimal price grid to keep the
    book keys off float dust. `price_dollars`/`delta_fp` can be absent on a
    heartbeat/partial delta, so a missing price is ignored rather than crashing
    (the old int-cents parser did `100 - None` and killed the socket task).
    """
    mtype = msg.get("type")
    data = msg.get("msg", msg)
    ticker = data.get("market_ticker") or data.get("ticker")
    key = key_by_ticker.get(ticker)
    if key is None:
        return
    book = books.setdefault(key, LocalBook(key))

    if mtype == "orderbook_snapshot":
        bids = [(float(p), s) for p, s in (data.get("yes_dollars_fp") or [])]
        asks = [(round(1.0 - float(p), 4), s) for p, s in (data.get("no_dollars_fp") or [])]
        book.set_snapshot(bids, asks)
    elif mtype == "orderbook_delta":
        price = data.get("price_dollars")
        if price is None:
            return
        delta = data.get("delta_fp", 0)
        p = float(price)
        if data.get("side") == "yes":
            book.apply_delta(True, p, delta)
        else:  # no side -> YES ask at (1 - price)
            book.apply_delta(False, round(1.0 - p, 4), delta)
    else:
        return
    if dirty is not None:
        dirty.add(key)


def parse_polymarket_message(msg: dict, key_by_token: dict[str, str],
                             books: dict[str, LocalBook], dirty: set | None = None) -> None:
    """Apply a Polymarket market-channel message. Confirmed live formats:

    book (snapshot): {event_type:"book", asset_id, bids:[{price,size}],
        asks:[{price,size}], ...}  — one asset.
    price_change:    {event_type:"price_change", market, price_changes:[
        {asset_id, price, size, side:"BUY"/"SELL", best_bid, best_ask}, ...]}
        — note the per-change asset_id, and the key is `price_changes`. `size` is
        the new absolute resting size at that price level.
    """
    event = msg.get("event_type") or msg.get("type")

    if event == "book":
        key = key_by_token.get(msg.get("asset_id") or msg.get("market"))
        if key is None:
            return
        book = books.setdefault(key, LocalBook(key))
        bids = [(b["price"], b["size"]) for b in msg.get("bids", [])]
        asks = [(a["price"], a["size"]) for a in msg.get("asks", [])]
        book.set_snapshot(bids, asks)
        if dirty is not None:
            dirty.add(key)

    elif event == "price_change":
        # One message can carry changes for several assets in the same market.
        for ch in msg.get("price_changes") or msg.get("changes") or []:
            key = key_by_token.get(ch.get("asset_id"))
            if key is None:
                continue
            book = books.setdefault(key, LocalBook(key))
            is_bid = ch.get("side", "").upper() == "BUY"
            book.set_level(is_bid, ch["price"], ch["size"])
            if dirty is not None:
                dirty.add(key)


class StreamManager:
    """Owns the live LocalBooks and the per-venue client tasks."""

    def __init__(self, clients: list["StreamClient"]):
        self.books: dict[str, LocalBook] = {}
        self.clients = clients
        self._tasks: list[asyncio.Task] = []
        self.active = False
        # Event-driven signalling: which markets changed since last drain.
        self.dirty: set[str] = set()
        self.updated = asyncio.Event()

    def book(self, market_key: str) -> OrderBook | None:
        lb = self.books.get(market_key)
        return lb.to_order_book() if lb else None

    def mark(self, keys: set[str]) -> None:
        """Called by clients when books change; wakes the engine's reactor."""
        if keys:
            self.dirty |= keys
            self.updated.set()

    def drain_dirty(self) -> set[str]:
        d = self.dirty
        self.dirty = set()
        return d

    async def start(self, markets: list[Market]) -> None:
        if not self.clients:
            return
        self.active = True
        for client in self.clients:
            subset = [m for m in markets if m.venue is client.venue]
            if subset:
                self._tasks.append(asyncio.create_task(client.run(subset, self.books, self.mark)))
        log.info("streaming.started", clients=len(self._tasks))

    async def stop(self) -> None:
        self.active = False
        await self._cancel_tasks()

    async def resubscribe(self, markets: list[Market]) -> None:
        """Re-point the live subscriptions at a new market set (after the universe
        re-curates). Cancels current client tasks and restarts them; existing
        LocalBooks for still-tracked markets are kept warm."""
        await self._cancel_tasks()
        for client in self.clients:
            subset = [m for m in markets if m.venue is client.venue]
            if subset:
                self._tasks.append(asyncio.create_task(client.run(subset, self.books, self.mark)))
        log.info("streaming.resubscribed", markets=len(markets))

    async def _cancel_tasks(self) -> None:
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._tasks.clear()


class StreamClient:
    venue: Venue

    async def run(self, markets: list[Market], books: dict[str, LocalBook], notify=None) -> None:
        raise NotImplementedError


class KalshiStream(StreamClient):
    venue = Venue.KALSHI

    def __init__(self, creds, signer=None):
        self._creds = creds
        self._signer = signer  # (method, path) -> auth headers; required for the WS handshake

    async def run(self, markets, books, notify=None) -> None:
        import websockets

        url = self._creds.base_url.replace("https://", "wss://") + "/trade-api/ws/v2"
        key_by_ticker = {m.market_id: m.key for m in markets}
        while True:
            try:
                headers = self._signer("GET", "/trade-api/ws/v2") if self._signer else {}
                async with websockets.connect(url, additional_headers=headers) as ws:
                    await ws.send(json.dumps({"id": 1, "cmd": "subscribe", "params": {
                        "channels": ["orderbook_delta"],
                        "market_tickers": list(key_by_ticker)}}))
                    log.info("kalshi_stream.subscribed", markets=len(key_by_ticker))
                    async for raw in ws:
                        dirty: set = set()
                        parse_kalshi_message(json.loads(raw), key_by_ticker, books, dirty)
                        if dirty and notify:
                            notify(dirty)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001  reconnect on any drop
                log.warning("kalshi_stream.reconnect", error=str(exc))
                await asyncio.sleep(3)


class PolymarketStream(StreamClient):
    venue = Venue.POLYMARKET
    URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

    def __init__(self, creds):
        self._creds = creds

    async def run(self, markets, books, notify=None) -> None:
        import websockets

        key_by_token = {m.market_id: m.key for m in markets}
        while True:
            try:
                # Manual PING/PONG keep-alive (the server drops idle conns; its
                # built-in ping is disabled so we control it). max_size=None: a
                # large universe's initial book snapshot exceeds the default 1 MB
                # frame limit (server sends it as one frame), which would 1009-drop.
                async with websockets.connect(self.URL, ping_interval=None, max_size=None) as ws:
                    await ws.send(json.dumps({"assets_ids": list(key_by_token),
                                              "type": "market"}))
                    log.info("polymarket_stream.subscribed", markets=len(key_by_token))
                    while True:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=5)
                        except asyncio.TimeoutError:
                            await ws.send("PING")
                            continue
                        if raw == "PONG":
                            continue
                        data = json.loads(raw)
                        dirty: set = set()
                        for m in (data if isinstance(data, list) else [data]):
                            parse_polymarket_message(m, key_by_token, books, dirty)
                        if dirty and notify:
                            notify(dirty)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("polymarket_stream.reconnect", error=str(exc))
                await asyncio.sleep(3)
