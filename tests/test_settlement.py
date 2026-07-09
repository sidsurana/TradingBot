"""Paper redemption settlement: PaperExchange.settle cash math + engine poll."""

from __future__ import annotations

from decimal import Decimal

import pytest

from tradingbot.config import PersistenceSettings, Settings, SettlementSettings
from tradingbot.engine import Engine, ExchangeRouter
from tradingbot.engine.portfolio import Portfolio
from tradingbot.exchanges.paper import PaperExchange
from tradingbot.models import Order, OrderType, Side, Venue
from tests.fake_exchange import FakeExchange, book, market


def _settings(**over) -> Settings:
    s = Settings(live=False, paper_starting_cash=Decimal(1000))
    for k, v in over.items():
        setattr(s, k, v)
    return s


def _drain(paper: PaperExchange, portfolio: Portfolio, registry: dict) -> None:
    for fill in paper.fills:
        portfolio.record_fill(registry[fill.market_key], fill)
    paper.fills.clear()


async def _fill_buy(paper: PaperExchange, mkt, size: int) -> None:
    order = Order(market=mkt, side=Side.BUY, size=Decimal(size), type=OrderType.MARKET)
    await paper.place_order(order)


# --- 3) PaperExchange.settle end-to-end cash math ----------------------------

@pytest.mark.asyncio
async def test_settle_winner_realizes_gain():
    poly = market(Venue.POLYMARKET, "P1", "cond1", "Yes")
    pbook = book(poly, bid=0.94, bid_sz=100, ask=0.95, ask_sz=100)
    px = FakeExchange(Venue.POLYMARKET, [poly], {poly.key: pbook})
    paper = PaperExchange(px)
    portfolio = Portfolio(Decimal(1000))
    registry = {poly.key: poly}

    await _fill_buy(paper, poly, 100)          # buy 100 @0.95
    _drain(paper, portfolio, registry)
    entry_fee = Decimal("0.95") * Decimal(100) * Decimal("0.001")  # 0.095
    assert portfolio.position(poly).size == Decimal(100)

    paper.settle(poly, 1.0)                     # redeem winner at $1
    _drain(paper, portfolio, registry)

    pos = portfolio.position(poly)
    assert pos.size == 0
    # +0.05/share on 100 shares, minus the entry fee (redemption fee is zero).
    assert pos.realized_pnl == pytest.approx(Decimal(5) - entry_fee)
    # Flat, so equity == cash; realized ~ +$5 minus entry fee.
    assert portfolio.cash == pytest.approx(Decimal(1000) + Decimal(5) - entry_fee)


@pytest.mark.asyncio
async def test_settle_loser_realizes_loss():
    poly = market(Venue.POLYMARKET, "P2", "cond2", "Yes")
    pbook = book(poly, bid=0.94, bid_sz=100, ask=0.95, ask_sz=100)
    px = FakeExchange(Venue.POLYMARKET, [poly], {poly.key: pbook})
    paper = PaperExchange(px)
    portfolio = Portfolio(Decimal(1000))
    registry = {poly.key: poly}

    await _fill_buy(paper, poly, 100)          # buy 100 @0.95
    _drain(paper, portfolio, registry)
    entry_fee = Decimal("0.95") * Decimal(100) * Decimal("0.001")

    paper.settle(poly, 0.0)                     # redeem loser at $0
    _drain(paper, portfolio, registry)

    pos = portfolio.position(poly)
    assert pos.size == 0
    assert pos.realized_pnl == pytest.approx(Decimal(-95) - entry_fee)
    assert portfolio.cash == pytest.approx(Decimal(1000) - Decimal(95) - entry_fee)


@pytest.mark.asyncio
async def test_settle_noop_when_flat():
    poly = market(Venue.POLYMARKET, "P3", "cond3", "Yes")
    px = FakeExchange(Venue.POLYMARKET, [poly], {})
    paper = PaperExchange(px)
    paper.settle(poly, 1.0)                     # no position -> no fill
    assert paper.fills == []


# --- 4) Engine settlement poll cycle -----------------------------------------

class FakeResolvingPoly(FakeExchange):
    """Fake Polymarket data source that also answers resolution queries."""

    def __init__(self, markets, books, resolutions):
        super().__init__(Venue.POLYMARKET, markets, books)
        self._resolutions = resolutions

    async def fetch_resolutions(self, markets):
        held = {m.key for m in markets}
        return {k: v for k, v in self._resolutions.items() if k in held}


@pytest.mark.asyncio
async def test_engine_poll_redeems_resolved_position():
    poly = market(Venue.POLYMARKET, "P1", "cond1", "Yes")
    pbook = book(poly, bid=0.94, bid_sz=100, ask=0.95, ask_sz=100)
    px = FakeResolvingPoly([poly], {poly.key: pbook}, {poly.key: 1.0})
    router = ExchangeRouter({Venue.POLYMARKET: px})
    engine = Engine(_settings(settlement=SettlementSettings(enabled=True)), router, [])
    await router.connect()

    # Seed a held position: buy 100 @0.95 through the paper layer, book it.
    engine._market_registry[poly.key] = poly
    order = Order(market=poly, side=Side.BUY, size=Decimal(100), type=OrderType.MARKET)
    await engine._paper.place_order(order)
    engine._drain_fills()
    assert engine.portfolio.position(poly).size == Decimal(100)

    # One poll cycle: the token resolved YES -> redeemed at $1, position flat.
    await engine._settle_once()

    pos = engine.portfolio.position(poly)
    assert pos.size == 0
    entry_fee = Decimal("0.95") * Decimal(100) * Decimal("0.001")
    assert pos.realized_pnl == pytest.approx(Decimal(5) - entry_fee)


@pytest.mark.asyncio
async def test_engine_poll_skips_unresolved_and_missing_venue():
    poly = market(Venue.POLYMARKET, "P1", "cond1", "Yes")
    pbook = book(poly, bid=0.94, bid_sz=100, ask=0.95, ask_sz=100)
    # Nothing resolved yet.
    px = FakeResolvingPoly([poly], {poly.key: pbook}, {})
    router = ExchangeRouter({Venue.POLYMARKET: px})
    engine = Engine(_settings(settlement=SettlementSettings(enabled=True)), router, [])
    await router.connect()

    engine._market_registry[poly.key] = poly
    order = Order(market=poly, side=Side.BUY, size=Decimal(100), type=OrderType.MARKET)
    await engine._paper.place_order(order)
    engine._drain_fills()

    await engine._settle_once()                # no resolution -> untouched
    assert engine.portfolio.position(poly).size == Decimal(100)


# --- 5) Restart: restored positions must rehydrate the paper layer & settle ---

@pytest.mark.asyncio
async def test_restart_restores_paper_position_and_settles(tmp_path):
    """Two-session restart: a held position persisted in session 1 must survive
    into session 2's paper book, otherwise settlement finds the portfolio
    position but paper._positions is empty and the $1 redemption is silently
    lost. Regression for the restore path skipping the paper layer."""
    poly = market(Venue.POLYMARKET, "P9", "cond9", "Yes")
    pbook = book(poly, bid=0.94, bid_sz=100, ask=0.95, ask_sz=100)
    db = str(tmp_path / "state.db")
    persist = PersistenceSettings(enabled=True, path=db)

    # --- Session 1: buy 100 @0.95, book + persist to the Store, shut down. ---
    px1 = FakeResolvingPoly([poly], {poly.key: pbook}, {poly.key: 1.0})
    router1 = ExchangeRouter({Venue.POLYMARKET: px1})
    engine1 = Engine(_settings(persistence=persist), router1, [])
    await router1.connect()
    engine1._market_registry[poly.key] = poly
    order = Order(market=poly, side=Side.BUY, size=Decimal(100), type=OrderType.MARKET)
    await engine1._paper.place_order(order)
    engine1._drain_fills()                      # books into portfolio AND persists
    assert engine1.portfolio.position(poly).size == Decimal(100)
    engine1.store.close()

    # --- Session 2: a fresh engine from the SAME store -> restore. ---
    px2 = FakeResolvingPoly([poly], {poly.key: pbook}, {poly.key: 1.0})
    router2 = ExchangeRouter({Venue.POLYMARKET: px2})
    engine2 = Engine(
        _settings(settlement=SettlementSettings(enabled=True), persistence=persist),
        router2, [],
    )
    await router2.connect()

    # Portfolio restored from replayed fills...
    assert engine2.portfolio.position(poly).size == Decimal(100)
    # ...and the paper layer now MIRRORS it (the bug: it used to start empty).
    assert engine2._paper._positions[poly.key].size == Decimal(100)

    # One settlement cycle: the token resolved YES -> redeemed at $1, now flat.
    await engine2._settle_once()
    pos = engine2.portfolio.position(poly)
    assert pos.size == 0
    entry_fee = Decimal("0.95") * Decimal(100) * Decimal("0.001")  # 0.095
    assert pos.realized_pnl == pytest.approx(Decimal(5) - entry_fee)
    assert engine2.portfolio.cash == pytest.approx(Decimal(1000) + Decimal(5) - entry_fee)

    # Idempotent: a second poll finds nothing held -> emits no new fill.
    fills_before = len(engine2._paper.fills)
    await engine2._settle_once()
    assert len(engine2._paper.fills) == fills_before
    assert engine2.portfolio.position(poly).size == 0
