from decimal import Decimal

import pytest

from tradingbot.ai import BotController
from tradingbot.config import Settings
from tradingbot.engine import Engine, ExchangeRouter
from tradingbot.models import Side, Venue
from tradingbot.strategies import build
from tests.fake_exchange import FakeExchange, book, market


def _engine() -> Engine:
    m = market(Venue.KALSHI, "K1", "E", "YES")
    kx = FakeExchange(Venue.KALSHI, [m], {m.key: book(m, bid=0.39, bid_sz=100, ask=0.41, ask_sz=100)})
    router = ExchangeRouter({Venue.KALSHI: kx})
    return Engine(Settings(live=False, paper_starting_cash=Decimal(1000)), router, [build("arbitrage")])


def test_place_order_validation():
    c = BotController(_engine())
    assert c.request_place_order("kalshi", "K1", "buy", 10, 1.5).get("error")   # bad price
    assert c.request_place_order("kalshi", "K1", "hold", 10, 0.4).get("error")  # bad side
    assert c.request_place_order("nasdaq", "K1", "buy", 10, 0.4).get("error")   # bad venue
    ok = c.request_place_order("kalshi", "K1", "buy", 10, 0.41)
    assert ok["needs_confirmation"] is True


@pytest.mark.asyncio
async def test_manual_order_executes_on_tick():
    engine = _engine()
    c = BotController(engine)
    await engine.router.connect()
    await engine.discover()

    staged = c.request_place_order("kalshi", "K1", "buy", 10, 0.41)
    c.confirm(staged["token"])
    assert len(engine.manual_orders) == 1   # queued

    await engine._tick()                     # drains + executes
    assert engine.manual_orders == []
    pos = engine.portfolio.position(market(Venue.KALSHI, "K1", "E", "YES"))
    assert pos.size == Decimal(10)           # filled long 10


@pytest.mark.asyncio
async def test_manual_order_executes_even_when_paused():
    engine = _engine()
    c = BotController(engine)
    await engine.router.connect()
    await engine.discover()
    engine.paused = True

    staged = c.request_place_order("kalshi", "K1", "buy", 5, 0.41)
    c.confirm(staged["token"])
    await engine._tick()
    pos = engine.portfolio.position(market(Venue.KALSHI, "K1", "E", "YES"))
    assert pos.size == Decimal(5)            # discretionary trade runs despite pause
