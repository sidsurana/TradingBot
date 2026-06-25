from decimal import Decimal

from tradingbot.models import Fill, Position, Side, Venue
from tests.fake_exchange import market


def test_position_average_price_blends_on_adds():
    m = market(Venue.PAPER, "x", "e")
    pos = Position(market=m)
    pos.apply(Fill(market_key=m.key, side=Side.BUY, size=Decimal(10), price=0.40))
    pos.apply(Fill(market_key=m.key, side=Side.BUY, size=Decimal(10), price=0.60))
    assert pos.size == Decimal(20)
    assert abs(pos.avg_price - 0.50) < 1e-9


def test_position_realizes_pnl_on_close():
    m = market(Venue.PAPER, "x", "e")
    pos = Position(market=m)
    pos.apply(Fill(market_key=m.key, side=Side.BUY, size=Decimal(10), price=0.40))
    pos.apply(Fill(market_key=m.key, side=Side.SELL, size=Decimal(10), price=0.55))
    assert pos.size == Decimal(0)
    # bought at .40, sold at .55 -> +0.15 * 10 = 1.5
    assert abs(float(pos.realized_pnl) - 1.5) < 1e-9


def test_unrealized_pnl_sign():
    m = market(Venue.PAPER, "x", "e")
    pos = Position(market=m)
    pos.apply(Fill(market_key=m.key, side=Side.BUY, size=Decimal(10), price=0.40))
    assert float(pos.unrealized_pnl(0.50)) > 0
    assert float(pos.unrealized_pnl(0.30)) < 0
