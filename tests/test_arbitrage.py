from decimal import Decimal

import pytest

from tests.fake_exchange import book, market
from tradingbot.fees import kalshi_taker_fee_per_share
from tradingbot.models import Market, Side, Venue
from tradingbot.strategies import build
from tradingbot.strategies.arbitrage import ArbitrageStrategy
from tradingbot.strategies.base import Context


def mkt(venue, mid, event, outcome="YES", *, category=None, num_outcomes=None):
    """Market with metadata (category drives the taker fee; num_outcomes is the
    ground-truth slate size for the dutch-book completeness check)."""
    meta = {}
    if category is not None:
        meta["category"] = category
    if num_outcomes is not None:
        meta["num_outcomes"] = num_outcomes
    return Market(venue=venue, market_id=mid, event_id=event, title=event,
                  outcome=outcome, metadata=meta)


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


def test_dutch_book_fee_makes_thin_edge_unprofitable():
    # Asks sum to 0.95: a fee-blind rule sees +5c and buys. But these are
    # mid-priced crypto legs (feeRate 0.07): per-share fee ~= 0.07*0.475*0.525
    # = 0.0175 each, ~3.5c total, so net edge ~1.5c. With a 2c buffer it must
    # NOT fire — the p*(1-p) taker fee eats most of the gross gap at mid-book.
    a = mkt(Venue.POLYMARKET, "A", "EV", "CAND_A", category="crypto")
    b = mkt(Venue.POLYMARKET, "B", "EV", "CAND_B", category="crypto")
    books = [book(a, bid=0.47, bid_sz=30, ask=0.475, ask_sz=30),
             book(b, bid=0.47, bid_sz=30, ask=0.475, ask_sz=30)]
    strat = build("arbitrage", min_edge=0.02)
    orders = strat.generate(_ctx([a, b], books))
    assert not [o for o in orders if "dutch_book" in o.reason]


def test_dutch_book_extreme_legs_nearly_fee_free():
    # Same gross edge (asks sum 0.95) but EXTREME-priced legs: fee ~= 0.07*0.05*0.95
    # = 0.0033 per share, tiny. Net edge ~4.3c > buffer, so it FIRES — the fee
    # model's p*(1-p) shape makes extreme-priced dutch books viable.
    a = mkt(Venue.POLYMARKET, "A", "EV2", "YES", category="crypto")
    b = mkt(Venue.POLYMARKET, "B", "EV2", "NO", category="crypto")
    books = [book(a, bid=0.90, bid_sz=30, ask=0.905, ask_sz=30),
             book(b, bid=0.04, bid_sz=30, ask=0.045, ask_sz=30)]
    strat = build("arbitrage", min_edge=0.02)
    dutch = [o for o in strat.generate(_ctx([a, b], books)) if "dutch_book" in o.reason]
    assert len(dutch) == 2


def test_dutch_book_geopolitics_is_fee_free():
    # Geopolitics has a 0% taker fee: a 0.985-sum set nets +1.5c and fires.
    a = mkt(Venue.POLYMARKET, "A", "EV3", "YES", category="geopolitics")
    b = mkt(Venue.POLYMARKET, "B", "EV3", "NO", category="geopolitics")
    books = [book(a, bid=0.49, bid_sz=30, ask=0.49, ask_sz=30),
             book(b, bid=0.49, bid_sz=30, ask=0.495, ask_sz=30)]
    strat = build("arbitrage", min_edge=0.01)
    dutch = [o for o in strat.generate(_ctx([a, b], books)) if "dutch_book" in o.reason]
    assert len(dutch) == 2


def test_dutch_book_rejects_curation_dropped_leg():
    # HIGH-severity regression: a 3-outcome event (num_outcomes=3) whose 3rd leg
    # was dropped by universe curation. The 2 survivors sum to 0.60 < 1 and look
    # like a locked set — but the dropped outcome could win. num_outcomes is the
    # ground truth: len(best)=2 != 3, so it must NOT fire.
    a = mkt(Venue.POLYMARKET, "A", "RACE3", "CAND_A", num_outcomes=3)
    b = mkt(Venue.POLYMARKET, "B", "RACE3", "CAND_B", num_outcomes=3)
    books = [book(a, bid=0.28, bid_sz=30, ask=0.30, ask_sz=30),
             book(b, bid=0.28, bid_sz=30, ask=0.30, ask_sz=30)]
    strat = build("arbitrage", min_edge=0.02)
    orders = strat.generate(_ctx([a, b], books))
    assert not [o for o in orders if "dutch_book" in o.reason]


def test_dutch_book_rejects_incomplete_set():
    # Event has THREE outcomes but one has no ask -> buying the other two for
    # < $1 is not a locked set (the missing one could win). Must not fire.
    a = market(Venue.POLYMARKET, "A", "RACE", "CAND_A")
    b = market(Venue.POLYMARKET, "B", "RACE", "CAND_B")
    c = market(Venue.POLYMARKET, "C", "RACE", "CAND_C")
    books = [book(a, bid=0.28, bid_sz=30, ask=0.30, ask_sz=30),
             book(b, bid=0.28, bid_sz=30, ask=0.30, ask_sz=30),
             book(c, bid=None, bid_sz=0, ask=None, ask_sz=0)]  # no market for CAND_C
    strat = build("arbitrage", min_edge=0.02)
    orders = strat.generate(_ctx([a, b, c], books))
    assert not [o for o in orders if "dutch_book" in o.reason]


def test_dutch_book_complete_three_way_fires():
    # All three outcomes priced, asks sum to 0.90 -> locked set, buy all three.
    a = market(Venue.POLYMARKET, "A", "RACE2", "CAND_A")
    b = market(Venue.POLYMARKET, "B", "RACE2", "CAND_B")
    c = market(Venue.POLYMARKET, "C", "RACE2", "CAND_C")
    books = [book(a, bid=0.28, bid_sz=40, ask=0.30, ask_sz=40),
             book(b, bid=0.28, bid_sz=40, ask=0.30, ask_sz=40),
             book(c, bid=0.28, bid_sz=40, ask=0.30, ask_sz=40)]
    strat = build("arbitrage", min_edge=0.02)
    dutch = [o for o in strat.generate(_ctx([a, b, c], books)) if "dutch_book" in o.reason]
    assert len(dutch) == 3
    assert all(o.size == Decimal(20) for o in dutch)


# --- fee models: Kalshi (0.07*p*(1-p)) vs Polymarket (category rate) ----------
def test_kalshi_taker_fee_formula():
    assert kalshi_taker_fee_per_share(0.5) == pytest.approx(0.0175)      # max at mid
    assert kalshi_taker_fee_per_share(0.9) == pytest.approx(0.07 * 0.9 * 0.1)
    assert kalshi_taker_fee_per_share(0.1) == pytest.approx(kalshi_taker_fee_per_share(0.9))
    assert kalshi_taker_fee_per_share(0.0) == 0.0
    assert kalshi_taker_fee_per_share(1.0) == 0.0


def test_leg_fee_dispatches_by_venue():
    k = mkt(Venue.KALSHI, "K", "E")
    p_crypto = mkt(Venue.POLYMARKET, "P", "E", category="crypto")     # 0.07
    p_pol = mkt(Venue.POLYMARKET, "P2", "E", category="politics")     # 0.04
    assert ArbitrageStrategy._leg_fee(k, 0.5) == pytest.approx(0.0175)          # Kalshi 0.07
    assert ArbitrageStrategy._leg_fee(p_crypto, 0.5) == pytest.approx(0.07 * 0.25)
    assert ArbitrageStrategy._leg_fee(p_pol, 0.5) == pytest.approx(0.04 * 0.25)


def test_kalshi_dutch_book_nets_fees():
    # YES 0.60 + NO 0.37 = 0.97 gross edge 0.03; Kalshi fee per leg 0.07*p(1-p)
    # = 0.0168 + 0.0155 = ~0.0323 > 0.03, so it must NOT fire at min_edge>=0.
    y = mkt(Venue.KALSHI, "Y", "GAME", "YES", num_outcomes=2)
    n = mkt(Venue.KALSHI, "N", "GAME", "NO", num_outcomes=2)
    books = [book(y, bid=0.59, bid_sz=30, ask=0.60, ask_sz=30),
             book(n, bid=0.36, bid_sz=30, ask=0.37, ask_sz=30)]
    s = build("arbitrage", min_edge=0.0, max_size=Decimal(10))
    dutch = [o for o in s.generate(_ctx([y, n], books)) if "dutch_book" in o.reason]
    assert dutch == []   # 3c gross gap is eaten by ~3.3c of Kalshi taker fees
