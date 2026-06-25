from decimal import Decimal

import pytest

from tradingbot.config import Settings, SignalSettings
from tradingbot.engine import Engine, ExchangeRouter
from tradingbot.engine.signals import SignalStore
from tradingbot.models import Side, Venue
from tradingbot.strategies import build
from tradingbot.strategies.base import Context
from tests.fake_exchange import FakeExchange, book, market


def _ctx(markets, books, positions=None):
    return Context(markets, {b.market_key: b for b in books}, positions or {})


# --- SignalStore ----------------------------------------------------------

def test_store_set_get_and_staleness():
    clock = {"t": 1000.0}
    store = SignalStore(clock=lambda: clock["t"])
    store.set("k:1", fair_value=0.7, confidence=0.8)
    assert store.get("k:1").fair_value == 0.7
    assert "k:1" in store.active(max_age_s=60)
    clock["t"] = 1100.0                       # 100s later
    assert store.active(max_age_s=60) == {}   # stale
    assert store.active(max_age_s=600)        # still fresh under a longer window


def test_store_clamps_inputs():
    store = SignalStore()
    s = store.set("k:1", fair_value=1.5, confidence=2.0)
    assert s.fair_value == 1.0 and s.confidence == 1.0


# --- SignalStrategy -------------------------------------------------------

def _strategy_with(store, **kw):
    s = build("signal", **kw)
    s.set_store(store)
    return s


def test_buys_when_underpriced():
    store = SignalStore()
    store.set("kalshi:K1", fair_value=0.70, confidence=1.0)   # mkt ask 0.40 << 0.70
    s = _strategy_with(store, bankroll=Decimal(1000), kelly_fraction=0.25, min_edge=0.05,
                       max_position=Decimal(10000))           # raised so Kelly size isn't capped
    m = market(Venue.KALSHI, "K1", "E", "YES")
    b = book(m, bid=0.39, bid_sz=500, ask=0.40, ask_sz=500)
    orders = s.generate(_ctx([m], [b]))
    assert len(orders) == 1
    o = orders[0]
    assert o.side is Side.BUY and o.price == 0.40
    # quarter-Kelly: f=(0.7-0.4)/(1-0.4)=0.5; scaled=0.25*1*0.5=0.125;
    # notional=1000*0.125=125; contracts=floor(125/0.40)=312
    assert o.size == Decimal(312)


def test_sells_when_overpriced():
    store = SignalStore()
    store.set("kalshi:K1", fair_value=0.20, confidence=1.0)   # mkt bid 0.50 >> 0.20
    s = _strategy_with(store, bankroll=Decimal(1000), kelly_fraction=0.25, min_edge=0.05)
    m = market(Venue.KALSHI, "K1", "E", "YES")
    b = book(m, bid=0.50, bid_sz=500, ask=0.52, ask_sz=500)
    orders = s.generate(_ctx([m], [b]))
    assert orders and orders[0].side is Side.SELL and orders[0].price == 0.50


def test_no_trade_within_edge_band():
    store = SignalStore()
    store.set("kalshi:K1", fair_value=0.42, confidence=1.0)   # within min_edge of mid
    s = _strategy_with(store, min_edge=0.05)
    m = market(Venue.KALSHI, "K1", "E", "YES")
    b = book(m, bid=0.40, bid_sz=100, ask=0.41, ask_sz=100)
    assert s.generate(_ctx([m], [b])) == []


def test_low_confidence_ignored():
    store = SignalStore()
    store.set("kalshi:K1", fair_value=0.70, confidence=0.3)
    s = _strategy_with(store, min_confidence=0.5)
    m = market(Venue.KALSHI, "K1", "E", "YES")
    b = book(m, bid=0.39, bid_sz=100, ask=0.40, ask_sz=100)
    assert s.generate(_ctx([m], [b])) == []


def test_builds_toward_target_only_the_delta():
    store = SignalStore()
    store.set("kalshi:K1", fair_value=0.70, confidence=1.0)
    s = _strategy_with(store, bankroll=Decimal(1000), kelly_fraction=0.25, max_position=Decimal(50))
    m = market(Venue.KALSHI, "K1", "E", "YES")
    b = book(m, bid=0.39, bid_sz=500, ask=0.40, ask_sz=500)
    # target capped at max_position 50; already long 30 -> only buy the 20 delta.
    from tradingbot.models import Fill, Position
    pos = Position(market=m)
    pos.apply(Fill(market_key=m.key, side=Side.BUY, size=Decimal(30), price=0.40))
    orders = s.generate(_ctx([m], [b], {m.key: pos}))
    assert orders[0].size == Decimal(20)


@pytest.mark.asyncio
async def test_engine_injects_store_and_signal_trades():
    m = market(Venue.KALSHI, "K1", "E", "YES")
    kx = FakeExchange(Venue.KALSHI, [m], {m.key: book(m, bid=0.39, bid_sz=500, ask=0.40, ask_sz=500)})
    router = ExchangeRouter({Venue.KALSHI: kx})
    s = Settings(live=False, paper_starting_cash=Decimal(2000),
                 signal=SignalSettings(bankroll=Decimal(1000), kelly_fraction=0.25, max_position=Decimal(50)))
    engine = Engine(s, router, [build("signal")])
    await router.connect()
    await engine.discover()
    # Agent/operator pushes a fair-value view via the shared store.
    engine.signals.set(m.key, fair_value=0.70, confidence=1.0)
    await engine._tick()
    assert engine.portfolio.position(m).size == Decimal(50)   # built to the cap
