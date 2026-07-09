"""DATA-venue adapter — directional instruments fed by free public endpoints.

Exposes equities/crypto/commodities as read-only "markets" on `Venue.DATA`.
Prices are dollars (not the [0,1] probability convention of the prediction
venues). Order books are synthetic: one level each side around the last
candle close, half-spread `synthetic_spread_bps`, deep enough that paper
fills never exhaust them.

Data sources:
  - equities + commodities: Yahoo v8 chart API (browser User-Agent required).
    Yahoo has no 4h interval, so commodities fetch 1h and resample locally.
  - crypto: Coinbase Exchange public REST (newest-first rows, reversed here).

Candle history per (symbol, interval) is kept to `history_bars` and served
synchronously via `candles_snapshot()` — the engine's `candle_source`.
Refreshes are lazy (during `fetch_order_book`) and rate-shielded by
`quote_ttl_s`; upstream failures degrade to the stale cache instead of
raising, as long as we have ever seen a price.
"""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal

import httpx
import structlog

from tradingbot.config import DataFeedSettings
from tradingbot.exchanges.base import Exchange
from tradingbot.models import Candle, Market, Order, OrderBook, Position, PriceLevel, Venue

log = structlog.get_logger(__name__)

# Yahoo rejects the default httpx UA; present as a browser.
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

_INTERVAL_S = {"15m": 900, "1h": 3600, "4h": 14400}
# Yahoo `range` per fetched interval — generous enough for 200 bars.
_YAHOO_RANGE = {"15m": "1mo", "1h": "3mo"}
# Interval per asset class (the strategy horizons).
_CLASS_INTERVAL = {"equity": "15m", "crypto": "1h", "commodity": "4h"}

_TITLES = {
    "SPY": "S&P 500 ETF (SPY)",
    "QQQ": "Nasdaq-100 ETF (QQQ)",
    "BTC-USD": "Bitcoin / USD (BTC-USD)",
    "ETH-USD": "Ethereum / USD (ETH-USD)",
    "GC=F": "Gold Futures (GC=F)",
    "CL=F": "Crude Oil Futures (CL=F)",
    "GLD": "Gold ETF (GLD)",
    "USO": "Crude Oil ETF (USO)",
}

# A huge nominal volume so universe volume-ranking never drops these.
_SYNTHETIC_VOLUME = 1e12
_BOOK_LEVEL_SIZE = Decimal("1000000")
# Extra grace beyond one interval before we consider the newest bar stale.
_REFRESH_SLACK_S = 60.0


def resample(candles: list[Candle], bucket_s: int) -> list[Candle]:
    """Aggregate oldest-first candles into `bucket_s`-second buckets
    (ts // bucket * bucket). open=first, high=max, low=min, close=last,
    volume=sum. The final bucket may be partial."""
    out: list[Candle] = []
    cur_key: float | None = None
    o = h = lo = c = v = 0.0
    for bar in candles:
        key = int(bar.ts) // bucket_s * bucket_s
        if key != cur_key:
            if cur_key is not None:
                out.append(Candle(ts=cur_key, open=o, high=h, low=lo, close=c, volume=v))
            cur_key, o, h, lo, c, v = key, bar.open, bar.high, bar.low, bar.close, bar.volume
        else:
            h = max(h, bar.high)
            lo = min(lo, bar.low)
            c = bar.close
            v += bar.volume
    if cur_key is not None:
        out.append(Candle(ts=cur_key, open=o, high=h, low=lo, close=c, volume=v))
    return out


def parse_yahoo_chart(data: dict) -> list[Candle]:
    """Yahoo v8 chart JSON -> oldest-first candles. Bars with null OHLC are
    skipped (Yahoo pads halts/closed sessions with nulls)."""
    result = data["chart"]["result"][0]
    timestamps = result.get("timestamp") or []
    quote = result["indicators"]["quote"][0]
    opens = quote.get("open") or []
    highs = quote.get("high") or []
    lows = quote.get("low") or []
    closes = quote.get("close") or []
    volumes = quote.get("volume") or []

    out: list[Candle] = []
    for i, ts in enumerate(timestamps):
        try:
            o, h, lo, c = opens[i], highs[i], lows[i], closes[i]
            v = volumes[i] if i < len(volumes) else 0
        except IndexError:
            continue
        if o is None or h is None or lo is None or c is None:
            continue
        out.append(Candle(ts=float(ts), open=float(o), high=float(h),
                          low=float(lo), close=float(c), volume=float(v or 0)))
    return out


def parse_coinbase_candles(rows: list) -> list[Candle]:
    """Coinbase Exchange candles: newest-first [time, low, high, open, close,
    volume] -> oldest-first Candle list."""
    out: list[Candle] = []
    for row in reversed(rows):
        t, lo, h, o, c, v = row[:6]
        out.append(Candle(ts=float(t), open=float(o), high=float(h),
                          low=float(lo), close=float(c), volume=float(v)))
    return out


class MarketDataExchange(Exchange):
    """Read-only market-data venue. Paper mode wraps this as the data source
    and only ever calls connect/close/list_markets/fetch_order_book."""

    venue = Venue.DATA

    def __init__(
        self,
        settings: DataFeedSettings,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._settings = settings
        self._transport = transport  # test hook (httpx.MockTransport)
        self._client: httpx.AsyncClient | None = None
        # symbol -> asset class ("equity"|"crypto"|"commodity"), insertion-ordered
        self._symbols: dict[str, str] = {}
        for sym in settings.equities:
            self._symbols[sym] = "equity"
        for sym in settings.crypto:
            self._symbols[sym] = "crypto"
        for sym in settings.commodities:
            self._symbols[sym] = "commodity"
        self._candles: dict[str, tuple[Candle, ...]] = {}   # symbol -> oldest-first
        self._books: dict[str, OrderBook] = {}              # symbol -> cached book
        # Upstream-failure backoff. Without it a symbol with no cache gets
        # re-fetched on EVERY engine tick (the book is only cached on success),
        # which keeps a 429'd rate-limit bucket exhausted forever.
        self._next_attempt: dict[str, float] = {}           # symbol -> earliest retry ts
        self._backoff_s: dict[str, float] = {}

    # --- lifecycle -----------------------------------------------------------

    async def connect(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self._settings.request_timeout_s,
                headers={"User-Agent": _USER_AGENT},
                transport=self._transport,
            )
        # Prime every symbol once; a failing symbol degrades (no price yet)
        # rather than blocking startup of the rest. Sequential on purpose — a
        # concurrent burst is exactly what trips Yahoo's per-IP bucket.
        for sym in self._symbols:
            try:
                await self._refresh(sym)
            except Exception as exc:  # noqa: BLE001
                log.warning("datafeed.prime_failed", symbol=sym, error=str(exc))

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # --- markets & books ------------------------------------------------------

    def _interval_for(self, symbol: str) -> str:
        return _CLASS_INTERVAL[self._symbols[symbol]]

    async def list_markets(self, *, event_filter: str | None = None) -> list[Market]:
        markets = []
        for sym, asset_class in self._symbols.items():
            if event_filter and event_filter not in sym:
                continue
            markets.append(Market(
                venue=Venue.DATA,
                market_id=sym,
                event_id=sym,
                title=_TITLES.get(sym, f"{sym} ({asset_class})"),
                outcome="LONG",
                tick_size=0.01,
                min_size=Decimal("0.0001"),
                metadata={
                    "asset_class": asset_class,
                    "interval": _CLASS_INTERVAL[asset_class],
                    "volume": _SYNTHETIC_VOLUME,
                    "synthetic": True,
                },
            ))
        return markets

    async def fetch_order_book(self, market: Market, depth: int = 10) -> OrderBook:
        sym = market.market_id
        now = time.time()
        cached = self._books.get(sym)
        if cached is not None and now - cached.ts < self._settings.quote_ttl_s:
            return cached

        # Past TTL: lazily refresh candles if the newest bar has rolled over.
        # At most one upstream request per symbol per TTL window — the book we
        # cache below (even one built from a stale price) shields until then.
        if self._candles_stale(sym):
            try:
                await self._refresh(sym)
            except Exception as exc:  # noqa: BLE001 — degrade to stale cache
                if not self._candles.get(sym):
                    raise
                log.warning("datafeed.refresh_failed", symbol=sym, error=str(exc))

        series = self._candles.get(sym)
        if not series:
            raise RuntimeError(f"no cached price for {sym!r} and refresh failed")
        last = series[-1].close
        half = self._settings.synthetic_spread_bps / 10_000.0
        book = OrderBook(
            market_key=market.key,
            bids=(PriceLevel(price=last * (1 - half), size=_BOOK_LEVEL_SIZE),),
            asks=(PriceLevel(price=last * (1 + half), size=_BOOK_LEVEL_SIZE),),
        )
        self._books[sym] = book
        return book

    # --- candles ---------------------------------------------------------------

    def candles_snapshot(self) -> dict[str, dict[str, tuple[Candle, ...]]]:
        """Synchronous snapshot: market_key -> interval -> oldest-first candles.
        This is what `engine.candle_source` calls each tick."""
        out: dict[str, dict[str, tuple[Candle, ...]]] = {}
        for sym, series in self._candles.items():
            key = f"{Venue.DATA.value}:{sym}"
            out[key] = {self._interval_for(sym): series}
        return out

    def _candles_stale(self, symbol: str) -> bool:
        series = self._candles.get(symbol)
        if not series:
            return True
        interval_s = _INTERVAL_S[self._interval_for(symbol)]
        return time.time() - series[-1].ts > interval_s + _REFRESH_SLACK_S

    async def _refresh(self, symbol: str) -> None:
        """One upstream request; replaces the cached series (trimmed)."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self._settings.request_timeout_s,
                headers={"User-Agent": _USER_AGENT},
                transport=self._transport,
            )
        now = time.time()
        if now < self._next_attempt.get(symbol, 0.0):
            return  # cooling down after an upstream failure; serve the stale cache
        asset_class = self._symbols[symbol]
        try:
            if asset_class == "crypto":
                candles = await self._fetch_coinbase(symbol)
            else:
                # Yahoo has no 4h interval: commodities fetch 1h and resample.
                fetch_interval = "15m" if asset_class == "equity" else "1h"
                candles = await self._fetch_yahoo(symbol, fetch_interval)
                if asset_class == "commodity":
                    candles = resample(candles, _INTERVAL_S["4h"])
        except Exception:
            backoff = min(self._backoff_s.get(symbol, 30.0) * 2, 900.0)
            self._backoff_s[symbol] = backoff
            self._next_attempt[symbol] = now + backoff
            raise
        self._backoff_s.pop(symbol, None)
        self._next_attempt.pop(symbol, None)
        if candles:
            self._candles[symbol] = tuple(candles[-self._settings.history_bars:])

    async def _fetch_yahoo(self, symbol: str, interval: str) -> list[Candle]:
        # Primary path: yfinance, which impersonates a browser TLS fingerprint
        # (curl_cffi). Yahoo 429-blocks plain python HTTP stacks per IP, and the
        # block is sticky for hours — raw httpx below is only the fallback.
        # Skipped when a test transport is injected (offline tests).
        if self._transport is None:
            try:
                candles = await asyncio.to_thread(self._fetch_yahoo_yf, symbol, interval)
                if candles:
                    return candles
            except Exception as exc:  # noqa: BLE001 — fall through to raw httpx
                log.debug("datafeed.yfinance_failed", symbol=symbol, error=str(exc))
        # Yahoo rate-limits per host per IP; query1 and query2 serve identical
        # data with independent buckets, so a 429 on one is retried on the other.
        last_exc: Exception | None = None
        for host in ("query2.finance.yahoo.com", "query1.finance.yahoo.com"):
            resp = await self._client.get(
                f"https://{host}/v8/finance/chart/{symbol}",
                params={"interval": interval, "range": _YAHOO_RANGE[interval]},
            )
            try:
                resp.raise_for_status()
                return parse_yahoo_chart(resp.json())
            except httpx.HTTPStatusError as exc:
                if resp.status_code != 429:
                    raise
                last_exc = exc
        raise last_exc

    def _fetch_yahoo_yf(self, symbol: str, interval: str) -> list[Candle]:
        """Blocking yfinance fetch — run via asyncio.to_thread."""
        import math

        import yfinance as yf  # optional dependency (pyproject extra "data")

        df = yf.Ticker(symbol).history(
            period=_YAHOO_RANGE[interval], interval=interval, auto_adjust=False)
        out: list[Candle] = []
        for ts, row in df.iterrows():
            o, h, lo, c = (float(row["Open"]), float(row["High"]),
                           float(row["Low"]), float(row["Close"]))
            if any(math.isnan(x) for x in (o, h, lo, c)):
                continue
            v = float(row.get("Volume", 0) or 0)
            out.append(Candle(ts=float(ts.timestamp()), open=o, high=h, low=lo,
                              close=c, volume=0.0 if math.isnan(v) else v))
        return out

    async def _fetch_coinbase(self, symbol: str) -> list[Candle]:
        resp = await self._client.get(
            f"https://api.exchange.coinbase.com/products/{symbol}/candles",
            params={"granularity": 3600},
        )
        resp.raise_for_status()
        return parse_coinbase_candles(resp.json())

    # --- read-only venue ---------------------------------------------------------

    async def place_order(self, order: Order) -> Order:
        raise NotImplementedError("data venue is read-only")

    async def cancel_order(self, order: Order) -> Order:
        raise NotImplementedError("data venue is read-only")

    async def fetch_positions(self) -> list[Position]:
        return []
