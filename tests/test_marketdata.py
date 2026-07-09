"""Offline tests for the DATA-venue market-data exchange.

All HTTP is served by httpx.MockTransport with canned Yahoo/Coinbase JSON;
handlers count requests so TTL / refresh-window behavior is observable.
"""

from __future__ import annotations

import time
from decimal import Decimal

import httpx
import pytest

from tradingbot.config import DataFeedSettings, UniverseSettings
from tradingbot.engine.universe import UniverseSelector
from tradingbot.exchanges.marketdata import (
    MarketDataExchange,
    parse_coinbase_candles,
    parse_yahoo_chart,
    resample,
)
from tradingbot.models import Candle, Market, Venue

NOW = int(time.time())


def yahoo_json(ts, opens, highs, lows, closes, volumes):
    return {"chart": {"result": [{
        "timestamp": list(ts),
        "indicators": {"quote": [{
            "open": list(opens), "high": list(highs), "low": list(lows),
            "close": list(closes), "volume": list(volumes),
        }]},
    }], "error": None}}


def yahoo_bars(n, interval_s, last_close=100.0, end_ts=None):
    """n synthetic bars ending at a bar whose open is `end_ts` (default: now,
    i.e. an in-progress bar so the cache reads as fresh)."""
    end = end_ts if end_ts is not None else (NOW // interval_s) * interval_s
    ts = [end - (n - 1 - i) * interval_s for i in range(n)]
    closes = [last_close - (n - 1 - i) for i in range(n)]
    return yahoo_json(ts, closes, [c + 1 for c in closes],
                      [c - 1 for c in closes], closes, [1000] * n)


def coinbase_rows(n, interval_s=3600, last_close=50_000.0, end_ts=None):
    """Newest-first rows: [time, low, high, open, close, volume]."""
    end = end_ts if end_ts is not None else (NOW // interval_s) * interval_s
    rows = []
    for i in range(n):  # i=0 is newest
        t = end - i * interval_s
        c = last_close - i
        rows.append([t, c - 1, c + 1, c - 0.5, c, 10.0 + i])
    return rows


class Counter:
    """Holds the requests seen by a mock transport, for TTL/refresh assertions."""

    def __init__(self):
        self.requests: list[httpx.Request] = []


def make_transport(counter, yahoo_by_symbol=None, coinbase_by_symbol=None, fail=False):
    yahoo_by_symbol = yahoo_by_symbol or {}
    coinbase_by_symbol = coinbase_by_symbol or {}

    def handler(request: httpx.Request) -> httpx.Response:
        counter.requests.append(request)
        if fail:
            return httpx.Response(500, json={"error": "boom"})
        if request.url.host in ("query1.finance.yahoo.com", "query2.finance.yahoo.com"):
            sym = request.url.path.rsplit("/", 1)[-1]
            return httpx.Response(200, json=yahoo_by_symbol[sym])
        if request.url.host == "api.exchange.coinbase.com":
            sym = request.url.path.split("/")[2]
            return httpx.Response(200, json=coinbase_by_symbol[sym])
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def settings(**kw):
    defaults = dict(enabled=True, equities=["SPY"], crypto=["BTC-USD"],
                    commodities=["GC=F"], quote_ttl_s=20.0, history_bars=200,
                    synthetic_spread_bps=2.0, request_timeout_s=10.0)
    defaults.update(kw)
    return DataFeedSettings(**defaults)


def full_transport(counter, **kw):
    return make_transport(
        counter,
        yahoo_by_symbol={"SPY": yahoo_bars(50, 900, last_close=500.0),
                         "GC=F": yahoo_bars(50, 3600, last_close=2400.0)},
        coinbase_by_symbol={"BTC-USD": coinbase_rows(50)},
        **kw,
    )


async def connected(cfg, transport):
    ex = MarketDataExchange(cfg, transport=transport)
    await ex.connect()
    return ex


# --- market listing -----------------------------------------------------------


@pytest.mark.asyncio
async def test_list_markets_and_metadata():
    ex = MarketDataExchange(settings(equities=["SPY", "QQQ"], crypto=["BTC-USD"],
                                     commodities=["GC=F", "CL=F"]))
    markets = await ex.list_markets()
    by_id = {m.market_id: m for m in markets}
    assert set(by_id) == {"SPY", "QQQ", "BTC-USD", "GC=F", "CL=F"}

    for m in markets:
        assert m.venue is Venue.DATA
        assert m.event_id == m.market_id
        assert m.outcome == "LONG"
        assert m.tick_size == 0.01
        assert m.min_size == Decimal("0.0001")
        assert m.metadata["synthetic"] is True
        assert m.metadata["volume"] >= 1e9

    assert by_id["SPY"].metadata["asset_class"] == "equity"
    assert by_id["SPY"].metadata["interval"] == "15m"
    assert by_id["SPY"].title == "S&P 500 ETF (SPY)"
    assert by_id["BTC-USD"].metadata["asset_class"] == "crypto"
    assert by_id["BTC-USD"].metadata["interval"] == "1h"
    assert by_id["GC=F"].metadata["asset_class"] == "commodity"
    assert by_id["GC=F"].metadata["interval"] == "4h"


@pytest.mark.asyncio
async def test_data_markets_survive_universe_selection():
    ex = MarketDataExchange(settings())
    data_markets = await ex.list_markets()
    other = Market(venue=Venue.KALSHI, market_id="K1", event_id="E1", title="k",
                   outcome="YES", metadata={"volume": 999, "category": "politics"})
    # Hostile settings: category filter, high min_volume, tiny per-venue cap,
    # and a watchlist that names none of the DATA symbols.
    sel = UniverseSelector(UniverseSettings(
        categories=["politics"], min_volume=1e15, max_per_venue=1,
        watchlist=["K1"],
    ))
    out = sel.select(data_markets + [other])
    kept = {m.market_id for m in out}
    assert {m.market_id for m in data_markets} <= kept


# --- synthetic book -------------------------------------------------------------


@pytest.mark.asyncio
async def test_synthetic_book_prices_and_sizes():
    counter = Counter()
    ex = await connected(settings(), full_transport(counter))
    (spy,) = [m for m in await ex.list_markets() if m.market_id == "SPY"]
    book = await ex.fetch_order_book(spy)
    last = 500.0
    half = 2.0 / 10_000
    assert book.market_key == "data:SPY"
    assert book.best_bid.price == pytest.approx(last * (1 - half))
    assert book.best_ask.price == pytest.approx(last * (1 + half))
    assert book.best_bid.size == Decimal("1000000")
    assert book.best_ask.size == Decimal("1000000")
    assert book.best_ask.price > book.best_bid.price
    await ex.close()


@pytest.mark.asyncio
async def test_ttl_caching_no_new_requests_within_ttl():
    counter = Counter()
    ex = await connected(settings(quote_ttl_s=60.0), full_transport(counter))
    primed = len(counter.requests)
    assert primed == 3  # one per symbol
    (spy,) = [m for m in await ex.list_markets() if m.market_id == "SPY"]
    b1 = await ex.fetch_order_book(spy)
    b2 = await ex.fetch_order_book(spy)
    assert b2 is b1                        # served from cache
    assert len(counter.requests) == primed  # no upstream traffic at all
    await ex.close()


@pytest.mark.asyncio
async def test_refresh_happens_when_ttl_expired_and_bar_stale():
    counter = Counter()
    # Stale candles (newest bar open 10 intervals ago) + zero TTL => every
    # fetch_order_book re-checks upstream, one request per call.
    stale = {"SPY": yahoo_bars(5, 900, last_close=500.0, end_ts=NOW - 9000)}
    transport = make_transport(counter, yahoo_by_symbol=stale)
    ex = await connected(settings(equities=["SPY"], crypto=[], commodities=[],
                                  quote_ttl_s=0.0), transport)
    (spy,) = await ex.list_markets()
    n0 = len(counter.requests)
    await ex.fetch_order_book(spy)
    assert len(counter.requests) == n0 + 1
    await ex.fetch_order_book(spy)
    assert len(counter.requests) == n0 + 2
    await ex.close()


@pytest.mark.asyncio
async def test_fresh_bar_skips_upstream_even_past_ttl():
    counter = Counter()
    ex = await connected(settings(equities=["SPY"], crypto=[], commodities=[],
                                  quote_ttl_s=0.0),
                         make_transport(counter, yahoo_by_symbol={
                             "SPY": yahoo_bars(5, 900, last_close=500.0)}))
    (spy,) = await ex.list_markets()
    n0 = len(counter.requests)
    await ex.fetch_order_book(spy)   # TTL expired but newest bar in-progress
    await ex.fetch_order_book(spy)
    assert len(counter.requests) == n0  # no refresh needed
    await ex.close()


# --- parsing / resampling --------------------------------------------------------


def test_resample_1h_to_4h_with_partial_final_bucket():
    bars = [
        Candle(ts=0, open=10, high=15, low=9, close=12, volume=100),
        Candle(ts=3600, open=12, high=20, low=11, close=18, volume=200),
        Candle(ts=7200, open=18, high=19, low=8, close=9, volume=300),
        Candle(ts=10800, open=9, high=13, low=9, close=13, volume=400),
        # partial next bucket (only 2 of 4 hours present)
        Candle(ts=14400, open=13, high=14, low=12, close=12.5, volume=50),
        Candle(ts=18000, open=12.5, high=16, low=12, close=15, volume=60),
    ]
    out = resample(bars, 14400)
    assert len(out) == 2
    full, partial = out
    assert (full.ts, full.open, full.high, full.low, full.close, full.volume) == \
        (0, 10, 20, 8, 13, 1000)
    assert (partial.ts, partial.open, partial.high, partial.low,
            partial.close, partial.volume) == (14400, 13, 16, 12, 15, 110)


def test_resample_buckets_by_floor_of_ts():
    # A bar not aligned to the bucket start still lands in ts//bucket*bucket.
    bars = [Candle(ts=15000, open=1, high=2, low=0.5, close=1.5, volume=7)]
    (out,) = resample(bars, 14400)
    assert out.ts == 14400


def test_yahoo_null_bars_skipped():
    data = yahoo_json(
        ts=[0, 900, 1800],
        opens=[10, None, 12], highs=[11, None, 13],
        lows=[9, None, 11], closes=[10.5, None, 12.5],
        volumes=[100, None, 300],
    )
    out = parse_yahoo_chart(data)
    assert [c.ts for c in out] == [0, 1800]
    assert out[1].close == 12.5


def test_yahoo_null_volume_kept_as_zero():
    data = yahoo_json(ts=[0], opens=[10], highs=[11], lows=[9], closes=[10.5],
                      volumes=[None])
    (c,) = parse_yahoo_chart(data)
    assert c.volume == 0.0


def test_coinbase_rows_reversed_and_mapped():
    # newest-first [time, low, high, open, close, volume]
    rows = [
        [7200, 90.0, 110.0, 95.0, 105.0, 3.0],   # newest
        [3600, 80.0, 100.0, 85.0, 95.0, 2.0],
        [0, 70.0, 90.0, 75.0, 85.0, 1.0],        # oldest
    ]
    out = parse_coinbase_candles(rows)
    assert [c.ts for c in out] == [0, 3600, 7200]  # oldest-first
    first = out[0]
    assert (first.low, first.high, first.open, first.close, first.volume) == \
        (70.0, 90.0, 75.0, 85.0, 1.0)


@pytest.mark.asyncio
async def test_commodity_fetches_1h_and_serves_4h():
    counter = Counter()
    ex = await connected(settings(equities=[], crypto=[], commodities=["GC=F"]),
                         make_transport(counter, yahoo_by_symbol={
                             "GC=F": yahoo_bars(12, 3600, last_close=2400.0)}))
    (req,) = counter.requests
    assert req.url.params["interval"] == "1h"       # Yahoo has no 4h
    snap = ex.candles_snapshot()
    series = snap["data:GC=F"]["4h"]
    assert all(c.ts % 14400 == 0 for c in series)   # bucket-aligned opens
    assert len(series) <= 4                          # 12 x 1h -> <= 4 x 4h buckets
    assert series[-1].close == 2400.0                # newest 1h close survives
    await ex.close()


# --- failure / cache behavior ------------------------------------------------------


@pytest.mark.asyncio
async def test_stale_cache_served_on_upstream_failure():
    counter = Counter()
    state = {"fail": False}
    good = yahoo_bars(5, 900, last_close=500.0, end_ts=NOW - 9000)  # stale bars

    def handler(request):
        counter.requests.append(request)
        if state["fail"]:
            raise httpx.ConnectError("network down", request=request)
        return httpx.Response(200, json=good)

    ex = MarketDataExchange(settings(equities=["SPY"], crypto=[], commodities=[],
                                     quote_ttl_s=0.0),
                            transport=httpx.MockTransport(handler))
    await ex.connect()
    (spy,) = await ex.list_markets()
    state["fail"] = True
    book = await ex.fetch_order_book(spy)  # stale bar forces a refresh, which fails
    assert book.best_bid.price == pytest.approx(500.0 * (1 - 0.0002))
    assert len(counter.requests) == 2      # prime + failed refresh attempt
    await ex.close()


@pytest.mark.asyncio
async def test_raises_only_when_no_cached_price_at_all():
    counter = Counter()
    ex = MarketDataExchange(settings(equities=["SPY"], crypto=[], commodities=[]),
                            transport=make_transport(counter, fail=True))
    await ex.connect()  # prime fails but must not raise
    (spy,) = await ex.list_markets()
    with pytest.raises(Exception):
        await ex.fetch_order_book(spy)
    await ex.close()


# --- candles_snapshot ------------------------------------------------------------


@pytest.mark.asyncio
async def test_candles_snapshot_shape_and_ordering():
    counter = Counter()
    ex = await connected(settings(), full_transport(counter))
    snap = ex.candles_snapshot()
    assert set(snap) == {"data:SPY", "data:BTC-USD", "data:GC=F"}
    assert set(snap["data:SPY"]) == {"15m"}
    assert set(snap["data:BTC-USD"]) == {"1h"}
    assert set(snap["data:GC=F"]) == {"4h"}
    for per_interval in snap.values():
        for series in per_interval.values():
            assert isinstance(series, tuple)
            assert all(isinstance(c, Candle) for c in series)
            ts = [c.ts for c in series]
            assert ts == sorted(ts)  # oldest-first
    assert snap["data:BTC-USD"]["1h"][-1].close == 50_000.0
    await ex.close()


@pytest.mark.asyncio
async def test_history_bars_trims_to_newest():
    counter = Counter()
    ex = await connected(
        settings(equities=["SPY"], crypto=[], commodities=[], history_bars=5),
        make_transport(counter, yahoo_by_symbol={
            "SPY": yahoo_bars(10, 900, last_close=500.0)}),
    )
    series = ex.candles_snapshot()["data:SPY"]["15m"]
    assert len(series) == 5
    assert series[-1].close == 500.0   # newest bars kept
    assert series[0].close == 496.0    # oldest 5 dropped
    await ex.close()


@pytest.mark.asyncio
async def test_read_only_venue_surface():
    ex = MarketDataExchange(settings())
    assert await ex.fetch_positions() == []
    with pytest.raises(NotImplementedError):
        await ex.place_order(None)
    with pytest.raises(NotImplementedError):
        await ex.cancel_order(None)
