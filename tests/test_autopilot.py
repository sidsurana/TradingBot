from decimal import Decimal

import pytest

from tradingbot.ai import BotController, TradingAgent
from tradingbot.ai.autopilot import Autopilot
from tradingbot.config import AnthropicCreds, AutopilotSettings, GoalSettings, Settings
from tradingbot.engine import Engine, ExchangeRouter
from tradingbot.models import Venue
from tradingbot.strategies import build
from tests.fake_exchange import FakeExchange, book, market


class FakeBridge:
    def __init__(self):
        self.sent: list[str] = []

    async def broadcast(self, text: str) -> None:
        self.sent.append(text)


def _engine(tmp_path, **goal_kw) -> Engine:
    k = market(Venue.KALSHI, "K1", "EVENT-A", "YES")
    kx = FakeExchange(Venue.KALSHI, [k], {k.key: book(k, bid=0.38, bid_sz=50, ask=0.40, ask_sz=50)})
    router = ExchangeRouter({Venue.KALSHI: kx})
    goals = GoalSettings(state_path=str(tmp_path / "g.json"), **goal_kw)
    s = Settings(live=False, paper_starting_cash=Decimal(1000), goals=goals)
    return Engine(s, router, [build("arbitrage")])


@pytest.mark.asyncio
async def test_autopilot_plain_briefing_without_agent(tmp_path):
    engine = _engine(tmp_path, daily_target=Decimal(50), weekly_target=Decimal(200))
    engine.goals.update(Decimal(1000))           # baseline
    controller = BotController(engine)
    agent = TradingAgent(controller, AnthropicCreds(api_key=""))  # not configured
    bridge = FakeBridge()
    ap = Autopilot(AutopilotSettings(enabled=True, briefing=True), controller, agent, bridge)

    await ap.cycle()
    assert len(bridge.sent) == 1
    assert "Daily:" in bridge.sent[0]
    assert "Weekly:" in bridge.sent[0]


@pytest.mark.asyncio
async def test_lock_gains_pauses_when_daily_target_met(tmp_path):
    engine = _engine(tmp_path, daily_target=Decimal(20), lock_gains=True)
    await engine.router.connect()
    await engine.discover()
    # Establish today's baseline at starting cash, then simulate profit.
    await engine._tick()
    assert engine.paused is False
    engine.portfolio.cash = Decimal(1030)        # +$30 > $20 target
    await engine._tick()
    assert engine.paused is True
    assert engine._paused_by_goals is True


@pytest.mark.asyncio
async def test_lock_gains_off_does_not_pause(tmp_path):
    engine = _engine(tmp_path, daily_target=Decimal(20), lock_gains=False)
    await engine.router.connect()
    await engine.discover()
    await engine._tick()
    engine.portfolio.cash = Decimal(1030)
    await engine._tick()
    assert engine.paused is False
