import asyncio
import json
from decimal import Decimal

import pytest

from tradingbot.exchanges.streaming import StreamClient, StreamManager

from tradingbot.config import Settings, UniverseSettings
from tradingbot.engine import Engine, ExchangeRouter
from tradingbot.engine.universe import UniverseSelector
from tradingbot.exchanges.kalshi import parse_kalshi_markets
from tradingbot.exchanges.polymarket import parse_gamma_markets
from tradingbot.models import Market, Venue
from tradingbot.strategies import build
from tests.fake_exchange import FakeExchange, book, market


def _mkt(venue, mid, vol, category="", event=None):
    return Market(venue=venue, market_id=mid, event_id=event or mid, title=mid,
                  outcome="YES", metadata={"volume": vol, "category": category})


def test_ranks_by_volume_and_caps_per_venue():
    markets = [
        _mkt(Venue.KALSHI, "K_lo", 10),
        _mkt(Venue.KALSHI, "K_hi", 9000),
        _mkt(Venue.KALSHI, "K_mid", 500),
        _mkt(Venue.POLYMARKET, "P_hi", 8000),
    ]
    sel = UniverseSelector(UniverseSettings(max_per_venue=2))
    out = sel.select(markets)
    kalshi = [m.market_id for m in out if m.venue is Venue.KALSHI]
    assert kalshi == ["K_hi", "K_mid"]                  # top-2 by volume, ordered
    assert any(m.market_id == "P_hi" for m in out)      # other venue kept


def test_min_volume_filter():
    markets = [_mkt(Venue.KALSHI, "A", 50), _mkt(Venue.KALSHI, "B", 5000)]
    out = UniverseSelector(UniverseSettings(min_volume=1000)).select(markets)
    assert [m.market_id for m in out] == ["B"]


def test_category_filter():
    markets = [_mkt(Venue.KALSHI, "A", 100, "politics"),
               _mkt(Venue.KALSHI, "B", 100, "sports")]
    out = UniverseSelector(UniverseSettings(categories=["Politics"])).select(markets)
    assert [m.market_id for m in out] == ["A"]


def test_watchlist_filter():
    markets = [_mkt(Venue.KALSHI, "A", 100, event="EVT-1"),
               _mkt(Venue.KALSHI, "B", 100, event="EVT-2")]
    # Watchlist matches market_id OR event_id.
    out = UniverseSelector(UniverseSettings(watchlist=["EVT-2"])).select(markets)
    assert [m.market_id for m in out] == ["B"]


def test_parse_gamma_markets():
    data = [{
        "question": "Will X win?", "conditionId": "0xabc",
        "clobTokenIds": json.dumps(["tokA", "tokB"]),
        "outcomes": json.dumps(["Yes", "No"]),
        "volume24hr": 42000, "category": "Politics",
    }]
    out = parse_gamma_markets(data, Venue.POLYMARKET)
    assert len(out) == 2
    assert {m.outcome for m in out} == {"Yes", "No"}
    assert out[0].metadata["volume"] == 42000.0
    assert out[0].metadata["category"] == "politics"
    assert out[0].event_id == "0xabc"


def test_parse_gamma_skips_malformed():
    data = [{"question": "bad", "clobTokenIds": "not-json"}]
    assert parse_gamma_markets(data, Venue.POLYMARKET) == []


def test_parse_kalshi_markets():
    raw = [{"ticker": "K1", "event_ticker": "E1", "title": "T", "volume": 1234, "category": "Econ"}]
    out = parse_kalshi_markets(raw, Venue.KALSHI)
    assert out[0].market_id == "K1"
    assert out[0].metadata["volume"] == 1234.0
    assert out[0].metadata["category"] == "econ"


class _FakeStreamClient(StreamClient):
    def __init__(self, venue):
        self.venue = venue
        self.runs: list[list[str]] = []

    async def run(self, markets, books, notify=None):
        self.runs.append([m.market_id for m in markets])
        await asyncio.sleep(3600)  # block until cancelled


@pytest.mark.asyncio
async def test_stream_resubscribe_swaps_market_set():
    fc = _FakeStreamClient(Venue.KALSHI)
    mgr = StreamManager([fc])
    m1 = _mkt(Venue.KALSHI, "M1", 1)
    m2 = _mkt(Venue.KALSHI, "M2", 1)
    await mgr.start([m1])
    await asyncio.sleep(0)                     # let the task start
    await mgr.resubscribe([m2])
    await asyncio.sleep(0)
    await mgr.stop()
    assert fc.runs == [["M1"], ["M2"]]         # restarted on the new universe


@pytest.mark.asyncio
async def test_engine_discover_applies_selector():
    # Two fake markets with volume metadata; min_volume drops the small one.
    big = Market(venue=Venue.KALSHI, market_id="BIG", event_id="E", title="big",
                 outcome="YES", metadata={"volume": 5000})
    small = Market(venue=Venue.KALSHI, market_id="SMALL", event_id="E2", title="small",
                   outcome="YES", metadata={"volume": 10})
    kx = FakeExchange(Venue.KALSHI, [big, small], {})
    router = ExchangeRouter({Venue.KALSHI: kx})
    s = Settings(live=False, paper_starting_cash=Decimal(1000),
                 universe=UniverseSettings(min_volume=1000))
    engine = Engine(s, router, [build("arbitrage")])
    await engine.discover()
    assert [m.market_id for m in engine.markets] == ["BIG"]
