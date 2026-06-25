from decimal import Decimal

import pytest

from tradingbot.config import RiskLimits, Settings
from tradingbot.engine import Engine, ExchangeRouter
from tradingbot.models import Venue
from tradingbot.strategies import build
from tests.fake_exchange import FakeExchange, book, market


def _settings(**over) -> Settings:
    s = Settings(live=False, paper_starting_cash=Decimal(1000))
    for k, v in over.items():
        setattr(s, k, v)
    return s


def _router_with_arb():
    k = market(Venue.KALSHI, "K1", "EVENT-A", "YES")
    p = market(Venue.POLYMARKET, "P1", "EVENT-A", "YES")
    kbook = book(k, bid=0.38, bid_sz=50, ask=0.40, ask_sz=50)
    pbook = book(p, bid=0.50, bid_sz=50, ask=0.52, ask_sz=50)
    kx = FakeExchange(Venue.KALSHI, [k], {k.key: kbook})
    px = FakeExchange(Venue.POLYMARKET, [p], {p.key: pbook})
    return ExchangeRouter({Venue.KALSHI: kx, Venue.POLYMARKET: px})


@pytest.mark.asyncio
async def test_engine_executes_paper_arbitrage_and_books_fills():
    router = _router_with_arb()
    engine = Engine(_settings(), router, [build("arbitrage", min_edge=0.02)])
    await router.connect()
    await engine.discover()
    await engine._tick()

    # Both legs should have filled in paper mode and moved the portfolio.
    positions = [p for p in engine.portfolio.positions.values() if p.size != 0]
    assert len(positions) == 2
    venues = {p.market.venue for p in positions}
    assert venues == {Venue.KALSHI, Venue.POLYMARKET}


@pytest.mark.asyncio
async def test_risk_kill_switch_blocks_orders():
    router = _router_with_arb()
    engine = Engine(_settings(), router, [build("arbitrage", min_edge=0.02)])
    engine.risk.kill_switch = True
    await router.connect()
    await engine.discover()
    await engine._tick()
    assert all(p.size == 0 for p in engine.portfolio.positions.values())


@pytest.mark.asyncio
async def test_risk_position_cap_enforced():
    router = _router_with_arb()
    tight = RiskLimits(max_position_per_market=Decimal(5))
    engine = Engine(_settings(risk=tight), router, [build("arbitrage", min_edge=0.02)])
    await router.connect()
    await engine.discover()
    await engine._tick()
    # Arb wants size 20 but cap is 5 -> rejected, nothing fills.
    assert all(p.size == 0 for p in engine.portfolio.positions.values())
