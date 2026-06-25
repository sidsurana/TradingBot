import json
from decimal import Decimal

from tradingbot.config import LinkSettings
from tradingbot.engine.linker import EventLinker
from tradingbot.models import Side, Venue
from tradingbot.strategies import build
from tradingbot.strategies.base import Context
from tests.fake_exchange import book, market


def _ctx(markets, books):
    return Context(markets, {b.market_key: b for b in books}, {})


def test_linker_annotates_members():
    links = [{"link_id": "wc-skorea", "members": [
        {"venue": "kalshi", "market_id": "KXSK"},
        {"venue": "polymarket", "market_id": "tok123"}]}]
    linker = EventLinker(LinkSettings(links=links))
    assert linker.count == 1
    k = market(Venue.KALSHI, "KXSK", "KX-EVENT", "YES")
    p = market(Venue.POLYMARKET, "tok123", "0xcond", "Yes")
    other = market(Venue.KALSHI, "NOPE", "E", "YES")
    linker.annotate([k, p, other])
    assert k.metadata["link_id"] == "wc-skorea"
    assert p.metadata["link_id"] == "wc-skorea"
    assert "link_id" not in other.metadata


def test_linker_loads_from_file(tmp_path):
    path = tmp_path / "links.json"
    path.write_text(json.dumps([{"link_id": "L1", "members": [
        {"venue": "kalshi", "market_id": "A"}]}]))
    linker = EventLinker(LinkSettings(map_path=str(path)))
    assert linker.count == 1


def test_cross_venue_arb_fires_on_linked_markets_with_different_event_ids():
    # Same real outcome, DIFFERENT native event_ids — only a link makes them group.
    k = market(Venue.KALSHI, "KXSK", "KX-EVENT-A", "YES")
    p = market(Venue.POLYMARKET, "tok123", "0xcondZZZ", "Yes")
    EventLinker(LinkSettings(links=[{"link_id": "L", "members": [
        {"venue": "kalshi", "market_id": "KXSK"},
        {"venue": "polymarket", "market_id": "tok123"}]}])).annotate([k, p])

    books = [
        book(k, bid=0.38, bid_sz=50, ask=0.40, ask_sz=50),   # buy K @ 0.40
        book(p, bid=0.50, bid_sz=50, ask=0.52, ask_sz=50),   # sell P @ 0.50
    ]
    orders = build("arbitrage", min_edge=0.02).generate(_ctx([k, p], books))
    pairs = [o for o in orders if "cross_venue" in o.reason]
    assert len(pairs) == 2
    buy = next(o for o in pairs if o.side is Side.BUY)
    sell = next(o for o in pairs if o.side is Side.SELL)
    assert buy.market.venue is Venue.KALSHI and sell.market.venue is Venue.POLYMARKET


def test_unlinked_different_events_do_not_fire():
    # Without a link, different event_ids must NOT be treated as the same outcome.
    k = market(Venue.KALSHI, "KXSK", "KX-EVENT-A", "YES")
    p = market(Venue.POLYMARKET, "tok123", "0xcondZZZ", "Yes")
    books = [
        book(k, bid=0.38, bid_sz=50, ask=0.40, ask_sz=50),
        book(p, bid=0.50, bid_sz=50, ask=0.52, ask_sz=50),
    ]
    orders = build("arbitrage", min_edge=0.02).generate(_ctx([k, p], books))
    assert [o for o in orders if "cross_venue" in o.reason] == []
