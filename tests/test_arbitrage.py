from decimal import Decimal

from tradingbot.models import Side, Venue
from tradingbot.strategies import build
from tradingbot.strategies.base import Context
from tests.fake_exchange import book, market


def _ctx(markets, books):
    return Context(markets, {b.market_key: b for b in books}, {})


def test_cross_venue_arbitrage_detected():
    # Same outcome on two venues: Kalshi ask 0.40, Polymarket bid 0.50 -> buy K, sell P.
    k = market(Venue.KALSHI, "K1", "EVENT-A", "YES")
    p = market(Venue.POLYMARKET, "P1", "EVENT-A", "YES")
    books = [
        book(k, bid=0.38, bid_sz=50, ask=0.40, ask_sz=50),
        book(p, bid=0.50, bid_sz=50, ask=0.52, ask_sz=50),
    ]
    strat = build("arbitrage", min_edge=0.02)
    orders = strat.generate(_ctx([k, p], books))

    pairs = [o for o in orders if "cross_venue" in o.reason]
    assert len(pairs) == 2
    buy = next(o for o in pairs if o.side is Side.BUY)
    sell = next(o for o in pairs if o.side is Side.SELL)
    assert buy.market.venue is Venue.KALSHI and buy.price == 0.40
    assert sell.market.venue is Venue.POLYMARKET and sell.price == 0.50


def test_no_arbitrage_when_edge_below_threshold():
    k = market(Venue.KALSHI, "K1", "EVENT-A", "YES")
    p = market(Venue.POLYMARKET, "P1", "EVENT-A", "YES")
    books = [
        book(k, bid=0.49, bid_sz=50, ask=0.50, ask_sz=50),
        book(p, bid=0.505, bid_sz=50, ask=0.515, ask_sz=50),
    ]
    strat = build("arbitrage", min_edge=0.02)
    orders = strat.generate(_ctx([k, p], books))
    assert not [o for o in orders if "cross_venue" in o.reason]


def test_dutch_book_complete_set_underpriced():
    # Two mutually-exclusive outcomes whose asks sum to 0.90 < 1.0 -> buy both.
    a = market(Venue.KALSHI, "A", "ELECTION", "CAND_A")
    b = market(Venue.POLYMARKET, "B", "ELECTION", "CAND_B")
    books = [
        book(a, bid=0.43, bid_sz=30, ask=0.45, ask_sz=30),
        book(b, bid=0.43, bid_sz=30, ask=0.45, ask_sz=30),
    ]
    strat = build("arbitrage", min_edge=0.02)
    orders = strat.generate(_ctx([a, b], books))
    dutch = [o for o in orders if "dutch_book" in o.reason]
    assert len(dutch) == 2
    assert all(o.side is Side.BUY for o in dutch)
    assert all(o.size == Decimal(20) for o in dutch)  # capped by default max_size
