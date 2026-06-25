from decimal import Decimal

import pytest

from tradingbot.config import ExitSettings, Settings
from tradingbot.engine import Engine, ExchangeRouter
from tradingbot.engine.exits import ExitManager
from tradingbot.models import Fill, OrderBook, Position, PriceLevel, Side, Venue
from tradingbot.strategies import build
from tests.fake_exchange import FakeExchange, book, market


def _pos(m, size, avg):
    p = Position(market=m)
    p.apply(Fill(market_key=m.key, side=Side.BUY if size > 0 else Side.SELL,
                 size=Decimal(abs(size)), price=avg))
    return p


def test_stop_loss_triggers_when_underwater():
    m = market(Venue.KALSHI, "K1", "E", "YES")
    pos = _pos(m, 20, 0.40)                       # long 20 @ 0.40
    b = book(m, bid=0.29, bid_sz=50, ask=0.31, ask_sz=50)  # mid 0.30 -> -25%
    em = ExitManager(ExitSettings(enabled=True, stop_loss_pct=0.20))
    orders = em.evaluate({m.key: pos}, {m.key: b})
    assert len(orders) == 1
    assert orders[0].side is Side.SELL
    assert "stop_loss" in orders[0].reason
    assert orders[0].price == 0.29               # sells into the bid


def test_take_profit_triggers_when_up():
    m = market(Venue.KALSHI, "K1", "E", "YES")
    pos = _pos(m, 20, 0.40)
    b = book(m, bid=0.61, bid_sz=50, ask=0.63, ask_sz=50)  # mid 0.62 -> +55%
    em = ExitManager(ExitSettings(enabled=True, take_profit_pct=0.50))
    orders = em.evaluate({m.key: pos}, {m.key: b})
    assert len(orders) == 1
    assert "take_profit" in orders[0].reason


def test_no_exit_within_band():
    m = market(Venue.KALSHI, "K1", "E", "YES")
    pos = _pos(m, 20, 0.40)
    b = book(m, bid=0.41, bid_sz=50, ask=0.43, ask_sz=50)  # mid 0.42 -> +5%
    em = ExitManager(ExitSettings(enabled=True, stop_loss_pct=0.20, take_profit_pct=0.50))
    assert em.evaluate({m.key: pos}, {m.key: b}) == []


def test_disabled_does_nothing():
    m = market(Venue.KALSHI, "K1", "E", "YES")
    pos = _pos(m, 20, 0.40)
    b = book(m, bid=0.10, bid_sz=50, ask=0.12, ask_sz=50)
    em = ExitManager(ExitSettings(enabled=False, stop_loss_pct=0.20))
    assert em.evaluate({m.key: pos}, {m.key: b}) == []


@pytest.mark.asyncio
async def test_engine_closes_position_on_stop_loss():
    # A short market built so the engine holds a long position, then drops.
    m = market(Venue.KALSHI, "K1", "E", "YES")
    books = {m.key: book(m, bid=0.28, bid_sz=100, ask=0.30, ask_sz=100)}
    kx = FakeExchange(Venue.KALSHI, [m], books)
    router = ExchangeRouter({Venue.KALSHI: kx})
    s = Settings(live=False, paper_starting_cash=Decimal(1000),
                 exits=ExitSettings(enabled=True, stop_loss_pct=0.20))
    engine = Engine(s, router, [build("arbitrage")])
    await router.connect()
    await engine.discover()

    # Seed a long position at 0.40 in the portfolio (the paper sim keeps its own
    # book; the drained closing fill brings the portfolio back to flat).
    engine.portfolio.position(m).apply(
        Fill(market_key=m.key, side=Side.BUY, size=Decimal(20), price=0.40))

    await engine._tick()  # mark 0.29 -> -27.5% -> stop-loss flattens it
    assert engine.portfolio.position(m).size == Decimal(0)
