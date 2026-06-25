from decimal import Decimal

import pytest

from tradingbot.config import PersistenceSettings, Settings
from tradingbot.engine import Engine, ExchangeRouter
from tradingbot.engine.store import Store
from tradingbot.models import Fill, Order, OrderType, Side, Venue
from tradingbot.strategies import build
from tests.fake_exchange import FakeExchange, book, market


def test_store_roundtrip(tmp_path):
    m = market(Venue.KALSHI, "K1", "E", "YES")
    store = Store(str(tmp_path / "s.db"))
    store.record_fill(m, Fill(market_key=m.key, side=Side.BUY, size=Decimal(10),
                              price=0.40, fee=Decimal("0.004"), order_client_id="c1"))
    store.close()

    store2 = Store(str(tmp_path / "s.db"))   # reopen (simulates restart)
    loaded = store2.load_fills()
    store2.close()
    assert len(loaded) == 1
    mk, fl = loaded[0]
    assert mk.key == m.key and mk.venue is Venue.KALSHI
    assert fl.side is Side.BUY and fl.size == Decimal(10) and fl.price == 0.40
    assert fl.fee == Decimal("0.004")


def _engine(db_path) -> Engine:
    m = market(Venue.KALSHI, "K1", "E", "YES")
    kx = FakeExchange(Venue.KALSHI, [m], {m.key: book(m, bid=0.39, bid_sz=100, ask=0.41, ask_sz=100)})
    router = ExchangeRouter({Venue.KALSHI: kx})
    s = Settings(live=False, paper_starting_cash=Decimal(1000),
                 persistence=PersistenceSettings(enabled=True, path=db_path))
    return Engine(s, router, [build("arbitrage")])


@pytest.mark.asyncio
async def test_position_and_cash_survive_restart(tmp_path):
    db = str(tmp_path / "state.db")
    m = market(Venue.KALSHI, "K1", "E", "YES")

    # First run: buy 10 @ 0.41 via a discretionary order, which fills + persists.
    e1 = _engine(db)
    await e1.router.connect()
    await e1.discover()
    e1.manual_orders.append(
        Order(market=m, side=Side.BUY, size=Decimal(10), type=OrderType.LIMIT, price=0.41))
    await e1._tick()
    pos1 = e1.portfolio.position(m).size
    cash1 = e1.portfolio.cash
    assert pos1 == Decimal(10)
    if e1.store:
        e1.store.close()

    # "Restart": a fresh engine on the same DB replays the fill.
    e2 = _engine(db)
    assert e2.portfolio.position(m).size == pos1          # position restored
    assert e2.portfolio.cash == cash1                     # cash restored
    e2.store.close()


def test_persistence_disabled_writes_nothing(tmp_path):
    db = str(tmp_path / "none.db")
    s = Settings(live=False, persistence=PersistenceSettings(enabled=False, path=db))
    m = market(Venue.KALSHI, "K1", "E", "YES")
    kx = FakeExchange(Venue.KALSHI, [m], {})
    engine = Engine(s, ExchangeRouter({Venue.KALSHI: kx}), [build("arbitrage")])
    assert engine.store is None
    import os
    assert not os.path.exists(db)
