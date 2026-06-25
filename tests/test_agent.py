"""Agent tests that don't hit the network — tool dispatch + skill rendering.

Includes a full loop test driven by a scripted fake Claude client, which
exercises exactly the code path a real Anthropic call would once a key is set.
"""

from decimal import Decimal
from types import SimpleNamespace

import pytest

from tradingbot.ai import BotController, TradingAgent
from tradingbot.ai import skills
from tradingbot.config import AnthropicCreds, Settings
from tradingbot.engine import Engine, ExchangeRouter
from tradingbot.models import Venue
from tradingbot.strategies import build
from tests.fake_exchange import FakeExchange, book, market


class _FakeMessages:
    def __init__(self, outer):
        self._outer = outer

    async def create(self, **kwargs):
        self._outer.calls.append(kwargs)
        return self._outer.scripted.pop(0)


class FakeAnthropic:
    """Returns pre-scripted responses, mimicking AsyncAnthropic.messages.create."""

    def __init__(self, scripted):
        self.scripted = list(scripted)
        self.calls = []
        self.messages = _FakeMessages(self)


def _text(t):
    return SimpleNamespace(type="text", text=t)


def _tool_use(name, tool_id, inp):
    return SimpleNamespace(type="tool_use", id=tool_id, name=name, input=inp)


def _resp(stop_reason, content):
    return SimpleNamespace(stop_reason=stop_reason, content=content)


def _agent() -> tuple[TradingAgent, Engine]:
    k = market(Venue.KALSHI, "K1", "EVENT-A", "YES")
    kx = FakeExchange(Venue.KALSHI, [k], {k.key: book(k, bid=0.38, bid_sz=50, ask=0.40, ask_sz=50)})
    router = ExchangeRouter({Venue.KALSHI: kx})
    engine = Engine(Settings(live=False, paper_starting_cash=Decimal(1000)), router,
                    [build("arbitrage")])
    controller = BotController(engine)
    return TradingAgent(controller, AnthropicCreds(api_key="")), engine


def test_agent_not_configured_without_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    agent, _ = _agent()
    assert agent.configured is False


@pytest.mark.asyncio
async def test_dispatch_reads_and_controls(monkeypatch):
    agent, engine = _agent()
    # No client needed for non-skill tools.
    assert (await agent._dispatch(None, "get_portfolio", {}))["mode"] == "paper"
    assert (await agent._dispatch(None, "pause_trading", {}))["paused"] is True
    assert engine.paused is True
    assert (await agent._dispatch(None, "resume_trading", {}))["paused"] is False

    staged = await agent._dispatch(None, "deploy_capital", {"amount": 200})
    assert staged["needs_confirmation"] is True
    confirmed = await agent._dispatch(None, "confirm_action", {"token": staged["token"]})
    assert confirmed["ok"] is True


@pytest.mark.asyncio
async def test_dispatch_unknown_tool():
    agent, _ = _agent()
    assert "error" in await agent._dispatch(None, "bogus", {})


@pytest.mark.asyncio
async def test_full_loop_calls_tool_then_answers():
    # Model: first turn calls get_portfolio, second turn answers with text.
    agent, _ = _agent()
    agent.creds = AnthropicCreds(api_key="test-key")  # mark configured
    agent._client = FakeAnthropic([
        _resp("tool_use", [_tool_use("get_portfolio", "t1", {})]),
        _resp("end_turn", [_text("You have $1000 cash, flat, in paper mode.")]),
    ])

    reply = await agent.chat("user-1", "what's my PnL?")
    assert "1000" in reply
    # Two model calls: the tool-use turn and the final answer.
    assert len(agent._client.calls) == 2
    # The tool result was fed back as a user message on the second call.
    second_messages = agent._client.calls[1]["messages"]
    assert any(m["role"] == "user" and isinstance(m["content"], list)
               and m["content"][0].get("type") == "tool_result"
               for m in second_messages)


def test_skill_renders_with_context():
    s = skills.get_skill("regime_detection")
    assert s is not None
    prompt = s.render({"market_snapshot": "[some json]"})
    assert "regime" in prompt.lower()
    assert "[some json]" in prompt
    assert {sk.name for sk in skills.available_skills()} >= {
        "strategy_generation", "regime_detection", "risk_analysis", "alpha_detection",
    }
