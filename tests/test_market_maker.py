from decimal import Decimal

import pytest

from tradingbot.config import Settings
from tradingbot.engine import Engine, ExchangeRouter
from tradingbot.models import Fill, Position, Side, Venue
from tradingbot.strategies import build
from tradingbot.strategies.base import Context
from tests.fake_exchange import FakeExchange, book, market


def _ctx(markets, books, positions=None):
    return Context(markets, {b.market_key: b for b in books}, positions or {})


def test_quotes_join_inside_when_spread_is_wide():
    m = market(Venue.KALSHI, "K1", "E", "YES")
    b = book(m, bid=0.40, bid_sz=100, ask=0.44, ask_sz=100)  # spread 0.04
    mm = build("market_maker", min_spread=0.02, quote_size=Decimal(10))
    qs = mm.quotes(_ctx([m], [b]))
    bid = next(o for o in qs if o.side is Side.BUY)
    ask = next(o for o in qs if o.side is Side.SELL)
    assert bid.price == 0.40 and bid.size == Decimal(10)
    assert ask.price == 0.44 and ask.size == Decimal(10)


def test_no_quotes_when_spread_too_tight():
    m = market(Venue.KALSHI, "K1", "E", "YES")
    b = book(m, bid=0.40, bid_sz=100, ask=0.41, ask_sz=100)  # spread 0.01 < 0.02
    mm = build("market_maker", min_spread=0.02)
    assert mm.quotes(_ctx([m], [b])) == []


def test_inventory_cap_skips_the_increasing_side():
    m = market(Venue.KALSHI, "K1", "E", "YES")
    b = book(m, bid=0.40, bid_sz=100, ask=0.44, ask_sz=100)
    mm = build("market_maker", min_spread=0.02, quote_size=Decimal(10), max_inventory=Decimal(40))

    long = Position(market=m)
    long.apply(Fill(market_key=m.key, side=Side.BUY, size=Decimal(40), price=0.42))  # at +cap
    qs = mm.quotes(_ctx([m], [b], {m.key: long}))
    assert all(o.side is Side.SELL for o in qs)   # no more buying at the long cap

    short = Position(market=m)
    short.apply(Fill(market_key=m.key, side=Side.SELL, size=Decimal(40), price=0.42))  # at -cap
    qs2 = mm.quotes(_ctx([m], [b], {m.key: short}))
    assert all(o.side is Side.BUY for o in qs2)    # no more selling at the short cap


def test_quotes_widest_markets_first_and_caps_count():
    markets, books = [], []
    for i, spr in enumerate([0.02, 0.10, 0.05]):
        m = market(Venue.KALSHI, f"K{i}", f"E{i}", "YES")
        markets.append(m)
        books.append(book(m, bid=0.40, bid_sz=50, ask=0.40 + spr, ask_sz=50))
    mm = build("market_maker", min_spread=0.02, max_markets=1)
    qs = mm.quotes(_ctx(markets, books))
    # Only the widest market (spread 0.10 -> K1) is quoted.
    assert {o.market.market_id for o in qs} == {"K1"}


@pytest.mark.asyncio
async def test_engine_mm_lifecycle_place_fill_requote():
    m = market(Venue.KALSHI, "K1", "E", "YES")
    b1 = book(m, bid=0.40, bid_sz=100, ask=0.44, ask_sz=100)
    kx = FakeExchange(Venue.KALSHI, [m], {m.key: b1})
    router = ExchangeRouter({Venue.KALSHI: kx})
    s = Settings(live=False, paper_starting_cash=Decimal(1000))
    engine = Engine(s, router, [build("market_maker", min_spread=0.02, quote_size=Decimal(10))])
    await router.connect()
    await engine.discover()

    await engine._tick()                       # places resting bid@0.40, ask@0.44
    assert len(engine._quotes) == 2

    # Market drops so our resting bid (0.40) becomes marketable and fills.
    kx._books[m.key] = book(m, bid=0.36, bid_sz=100, ask=0.40, ask_sz=100)
    await engine._tick()                       # match_resting fills the bid, then re-quotes

    assert engine.portfolio.position(m).size == Decimal(10)   # bought 10 at the bid
