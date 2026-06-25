from decimal import Decimal

import pytest

from tradingbot.config import Settings, StreamingSettings
from tradingbot.engine import Engine, ExchangeRouter
from tradingbot.exchanges.streaming import LocalBook, StreamManager, parse_polymarket_message
from tradingbot.models import Venue
from tradingbot.strategies import build
from tests.fake_exchange import FakeExchange, book, market


def test_parse_marks_dirty():
    books, dirty = {}, set()
    parse_polymarket_message(
        {"event_type": "book", "asset_id": "tok1",
         "bids": [{"price": "0.4", "size": "10"}], "asks": [{"price": "0.6", "size": "10"}]},
        {"tok1": "polymarket:tok1"}, books, dirty)
    assert dirty == {"polymarket:tok1"}


def test_stream_manager_mark_and_drain():
    mgr = StreamManager(clients=[])
    mgr.mark({"a", "b"})
    assert mgr.updated.is_set()
    assert mgr.drain_dirty() == {"a", "b"}
    assert mgr.drain_dirty() == set()   # drained


def _arb_stream():
    k = market(Venue.KALSHI, "K1", "EVENT-A", "YES")
    p = market(Venue.POLYMARKET, "P1", "EVENT-A", "YES")
    mgr = StreamManager(clients=[])
    mgr.active = True
    lbk = LocalBook(k.key); lbk.set_snapshot(bids=[(0.38, 50)], asks=[(0.40, 50)])
    lbp = LocalBook(p.key); lbp.set_snapshot(bids=[(0.50, 50)], asks=[(0.52, 50)])
    mgr.books[k.key], mgr.books[p.key] = lbk, lbp
    return mgr, k, p


@pytest.mark.asyncio
async def test_reactor_fires_arb_immediately_on_update():
    mgr, k, p = _arb_stream()
    # Router only needed for adapter plumbing; paper fills use the stream cache.
    router = ExchangeRouter({Venue.KALSHI: FakeExchange(Venue.KALSHI, [k], {k.key: book(k, bid=0.38, bid_sz=1, ask=0.40, ask_sz=1)}),
                             Venue.POLYMARKET: FakeExchange(Venue.POLYMARKET, [p], {p.key: book(p, bid=0.50, bid_sz=1, ask=0.52, ask_sz=1)})})
    s = Settings(live=False, paper_starting_cash=Decimal(1000),
                 streaming=StreamingSettings(enabled=True))
    engine = Engine(s, router, [build("arbitrage", min_edge=0.02)], stream=mgr)
    engine.markets = [k, p]

    # No periodic tick — react directly to a book update, as the reactor would.
    await engine._on_update({k.key, p.key})

    assert engine.portfolio.position(k).size == Decimal(20)    # bought the cheap leg
    assert engine.portfolio.position(p).size == Decimal(-20)   # sold the rich leg


@pytest.mark.asyncio
async def test_reactor_noop_when_paused():
    mgr, k, p = _arb_stream()
    router = ExchangeRouter({Venue.KALSHI: FakeExchange(Venue.KALSHI, [k], {}),
                             Venue.POLYMARKET: FakeExchange(Venue.POLYMARKET, [p], {})})
    s = Settings(live=False, paper_starting_cash=Decimal(1000),
                 streaming=StreamingSettings(enabled=True))
    engine = Engine(s, router, [build("arbitrage", min_edge=0.02)], stream=mgr)
    engine.markets = [k, p]
    engine.paused = True
    await engine._on_update({k.key, p.key})
    assert engine.portfolio.position(k).size == Decimal(0)     # paused: no entry


@pytest.mark.asyncio
async def test_paper_uses_book_source_not_rest():
    # data_source returns a far book; book_source (the "stream") returns the real one.
    m = market(Venue.KALSHI, "K1", "E", "YES")
    data = FakeExchange(Venue.KALSHI, [m], {m.key: book(m, bid=0.01, bid_sz=1, ask=0.99, ask_sz=1)})
    from tradingbot.exchanges.paper import PaperExchange
    from tradingbot.models import Order, OrderType, Side
    paper = PaperExchange(data)
    paper.set_book_source(lambda mk: book(mk, bid=0.39, bid_sz=50, ask=0.41, ask_sz=50))
    order = Order(market=m, side=Side.BUY, size=Decimal(10), type=OrderType.MARKET, price=None)
    await paper.place_order(order)
    # Filled at the stream's ask (0.41), not the data source's 0.99.
    assert order.avg_fill_price == 0.41
