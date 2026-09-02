from decimal import Decimal

import pytest

from tradingbot.config import RiskLimits, Settings
from tradingbot.engine import Engine, ExchangeRouter
from tradingbot.models import Order, OrderStatus, OrderType, Side, Venue
from tradingbot.strategies import build
from tests.fake_exchange import FakeExchange, book, market


class _ScriptedExec:
    """Fills every leg except `fail_market` (FOK-killed), recording placements."""

    def __init__(self, fail_market: str | None = None):
        self.fail_market = fail_market
        self.placed: list[Order] = []

    async def place_order(self, order: Order) -> Order:
        self.placed.append(order)
        if order.market.market_id == self.fail_market:
            order.status = OrderStatus.CANCELED
        else:
            order.status = OrderStatus.FILLED
            order.filled_size = order.size
            order.avg_fill_price = order.price
        return order


def _db_legs(mids, event="EVT"):
    return [Order(market=market(Venue.KALSHI, mid, event), side=Side.BUY,
                  size=Decimal(5), type=OrderType.LIMIT, price=0.30,
                  time_in_force="FOK", set_id=f"db:{event}") for mid in mids]


@pytest.mark.asyncio
async def test_dutch_book_unwinds_on_incomplete_fill():
    engine = Engine(_settings(), _router_with_arb(), [build("arbitrage")])
    ex = _ScriptedExec(fail_market="C")
    engine.exec = ex
    legs = _db_legs(["A", "B", "C"])
    books = {l.market.key: book(l.market, bid=0.29, bid_sz=50, ask=0.30, ask_sz=50) for l in legs}
    await engine._place_locked_set(legs, books)

    buys = [o for o in ex.placed if o.side is Side.BUY]
    sells = [o for o in ex.placed if o.side is Side.SELL]
    assert [o.market.market_id for o in buys] == ["A", "B", "C"]   # legged A,B then C failed
    assert {o.market.market_id for o in sells} == {"A", "B"}       # filled legs unwound
    assert all(o.time_in_force == "FOK" and "unwind" in o.reason for o in sells)
    assert all(o.price == 0.29 for o in sells)                     # sold to bid


@pytest.mark.asyncio
async def test_dutch_book_all_fill_no_unwind():
    engine = Engine(_settings(), _router_with_arb(), [build("arbitrage")])
    ex = _ScriptedExec(fail_market=None)
    engine.exec = ex
    legs = _db_legs(["A", "B", "C"])
    books = {l.market.key: book(l.market, bid=0.29, bid_sz=50, ask=0.30, ask_sz=50) for l in legs}
    await engine._place_locked_set(legs, books)
    assert len(ex.placed) == 3 and all(o.side is Side.BUY for o in ex.placed)  # no unwind


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
