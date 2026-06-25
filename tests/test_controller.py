from decimal import Decimal

import pytest

from tradingbot.ai import BotController
from tradingbot.config import Settings
from tradingbot.engine import Engine, ExchangeRouter
from tradingbot.models import Venue
from tradingbot.strategies import build
from tests.fake_exchange import FakeExchange, book, market


def _engine() -> Engine:
    k = market(Venue.KALSHI, "K1", "EVENT-A", "YES")
    p = market(Venue.POLYMARKET, "P1", "EVENT-A", "YES")
    kx = FakeExchange(Venue.KALSHI, [k], {k.key: book(k, bid=0.38, bid_sz=50, ask=0.40, ask_sz=50)})
    px = FakeExchange(Venue.POLYMARKET, [p], {p.key: book(p, bid=0.50, bid_sz=50, ask=0.52, ask_sz=50)})
    router = ExchangeRouter({Venue.KALSHI: kx, Venue.POLYMARKET: px})
    return Engine(Settings(live=False, paper_starting_cash=Decimal(1000)), router,
                  [build("arbitrage", min_edge=0.02)])


@pytest.mark.asyncio
async def test_controller_reads_after_tick():
    engine = _engine()
    await engine.router.connect()
    await engine.discover()
    await engine._tick()
    c = BotController(engine)

    summary = c.portfolio_summary()
    assert summary["mode"] == "paper"
    assert summary["open_position_count"] == 2
    assert len(c.list_positions()) == 2
    assert c.market_snapshot()  # non-empty


@pytest.mark.asyncio
async def test_pause_resume_controls_engine():
    engine = _engine()
    c = BotController(engine)
    assert c.pause()["paused"] is True
    assert engine.paused is True
    assert c.resume()["paused"] is False
    assert engine.paused is False


@pytest.mark.asyncio
async def test_sensitive_action_requires_confirmation():
    engine = _engine()
    c = BotController(engine)
    before = engine.risk.limits.max_gross_notional

    staged = c.request_set_risk_limit("max_gross_notional", 750)
    assert staged["needs_confirmation"] is True
    # Not applied until confirmed.
    assert engine.risk.limits.max_gross_notional == before

    done = c.confirm(staged["token"])
    assert done["ok"] is True
    assert engine.risk.limits.max_gross_notional == Decimal("750")
    # Token is single-use.
    assert c.confirm(staged["token"])["ok"] is False


@pytest.mark.asyncio
async def test_go_live_refused_without_credentials():
    engine = _engine()
    c = BotController(engine)
    staged = c.request_go_live()
    result = c.confirm(staged["token"])
    # go_live refused (no creds), so its ok:False propagates through confirm.
    assert result["ok"] is False
    assert result.get("error")
    assert engine.settings.live is False
