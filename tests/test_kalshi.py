from decimal import Decimal

import httpx
import pytest

from tradingbot.config import KalshiCreds
from tradingbot.exchanges.kalshi import (
    KalshiExchange,
    parse_kalshi_markets,
    parse_kalshi_orderbook,
)
from tradingbot.models import Venue


def test_parse_kalshi_orderbook_dollar_fp_folds_no_into_asks():
    # 2026 REST wire format: `orderbook_fp` with dollar-string prices (already
    # probabilities) and fixed-point string sizes. YES bids -> bids; a NO bid at
    # price p -> YES ask at (1 - p).
    ob = {
        "yes_dollars": [["0.62", "1200.00"], ["0.61", "300.00"]],
        "no_dollars": [["0.41", "800.00"]],
    }
    book = parse_kalshi_orderbook(ob, "kalshi:K1")
    assert book.best_bid.price == 0.62 and book.best_bid.size == Decimal("1200.00")
    # bids sorted high -> low
    assert [lvl.price for lvl in book.bids] == [0.62, 0.61]
    # NO at 0.41 -> YES ask at 0.59, rounded off float dust (1 - 0.41).
    assert book.best_ask.price == 0.59 and book.best_ask.size == Decimal("800.00")


def test_parse_kalshi_orderbook_empty():
    # Empty / drained book must yield an empty OrderBook, not raise.
    book = parse_kalshi_orderbook({"yes_dollars": [], "no_dollars": []}, "kalshi:K1")
    assert book.best_bid is None and book.best_ask is None
    # Missing keys entirely (e.g. wrong wrapper) also degrade gracefully.
    assert parse_kalshi_orderbook({}, "kalshi:K1").best_bid is None


def test_parse_kalshi_markets_reads_volume_fp():
    # volume was renamed volume_24h_fp / volume_fp (fixed-point strings).
    markets = parse_kalshi_markets(
        [{"ticker": "K1", "title": "t", "volume_24h_fp": "1500.00"}], Venue.KALSHI)
    assert markets[0].metadata["volume"] == 1500.0


def _mkt(ticker, series):
    return {"ticker": ticker, "event_ticker": series, "title": ticker,
            "volume_24h_fp": "100.00"}


@pytest.mark.asyncio
async def test_list_markets_sweeps_configured_series():
    # Discovery must query each configured series (not the MVE-flooded open list)
    # and dedupe across them.
    seen_series = []

    def handler(request: httpx.Request) -> httpx.Response:
        s = request.url.params.get("series_ticker")
        seen_series.append(s)
        return httpx.Response(200, json={"markets": [_mkt(f"{s}-1", s)], "cursor": ""})

    ex = KalshiExchange(KalshiCreds(series=["KXBTCD", "KXFED"]))
    ex._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://test")
    markets = await ex.list_markets()
    await ex._client.aclose()

    assert set(seen_series) == {"KXBTCD", "KXFED"}          # queried by series
    assert sorted(m.market_id for m in markets) == ["KXBTCD-1", "KXFED-1"]


@pytest.mark.asyncio
async def test_list_markets_event_filter_overrides_series():
    seen_series = []

    def handler(request: httpx.Request) -> httpx.Response:
        s = request.url.params.get("series_ticker")
        seen_series.append(s)
        return httpx.Response(200, json={"markets": [_mkt(f"{s}-1", s)], "cursor": ""})

    ex = KalshiExchange(KalshiCreds(series=["KXBTCD", "KXFED"]))
    ex._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://test")
    markets = await ex.list_markets(event_filter="KXNFLGAME")
    await ex._client.aclose()

    assert seen_series == ["KXNFLGAME"]                     # single series, ignores default set
    assert [m.market_id for m in markets] == ["KXNFLGAME-1"]


@pytest.mark.asyncio
async def test_list_markets_fallback_excludes_mve():
    # With no series configured, fall back to the open listing but drop the
    # auto-generated MVE parlay markets.
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params.get("series_ticker") is None
        return httpx.Response(200, json={"markets": [
            _mkt("KXMVECROSSCATEGORY-SHARD1-X", "KXMVECROSSCATEGORY"),
            _mkt("KXNFLGAME-26SEP-NYG", "KXNFLGAME"),
        ]})

    ex = KalshiExchange(KalshiCreds(series=[]))
    ex._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://test")
    markets = await ex.list_markets()
    await ex._client.aclose()

    assert [m.market_id for m in markets] == ["KXNFLGAME-26SEP-NYG"]
