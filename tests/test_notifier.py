import pytest

from tradingbot.config import NotifierSettings
from tradingbot.interface.notifier import TradeNotifier
from tradingbot.models import Venue


def test_configured_per_venue():
    n = TradeNotifier(NotifierSettings(kalshi_token="t", kalshi_chat_id=1))
    assert n.configured_for(Venue.KALSHI) is True
    assert n.configured_for(Venue.POLYMARKET) is False   # PM bot not set
    assert n.any_configured is True


def test_unconfigured_is_fully_silent():
    n = TradeNotifier(NotifierSettings())
    assert n.any_configured is False
    assert n.configured_for(Venue.KALSHI) is False


def test_headers_label_each_agent():
    n = TradeNotifier(NotifierSettings())
    assert n._header(Venue.KALSHI) == "🟦 KALSHI AGENT"
    assert n._header(Venue.POLYMARKET) == "🟪 POLYMARKET AGENT"


@pytest.mark.asyncio
async def test_trade_noop_when_venue_unconfigured():
    # PM bot not configured -> sending a PM trade must not raise / not hit network.
    n = TradeNotifier(NotifierSettings(kalshi_token="t", kalshi_chat_id=1))
    await n.trade(Venue.POLYMARKET, "BUY", "x", 3, 0.9, 0.03, 250.0, 0.0)  # no-op, no error
