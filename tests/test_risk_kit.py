"""Focused tests for the risk-kit fixes: reducing-order bypasses, daily
re-baselining, exit cooldown, paper fill averaging, and the correlation filter."""

from decimal import Decimal

from tradingbot.config import ExitSettings, PersistenceSettings, RiskLimits, Settings
from tradingbot.engine import Engine, ExchangeRouter
from tradingbot.engine.exits import ExitManager
from tradingbot.engine.portfolio import Portfolio
from tradingbot.engine.risk import RiskManager
from tradingbot.exchanges.paper import PaperExchange
from tradingbot.models import Fill, Order, OrderType, PriceLevel, Side, Venue
from tradingbot.strategies import build
from tests.fake_exchange import FakeExchange, book, market


def _portfolio_with_long(m, size=10, price=0.40, cash=Decimal(1000)) -> Portfolio:
    pf = Portfolio(cash)
    pf.record_fill(m, Fill(market_key=m.key, side=Side.BUY,
                           size=Decimal(size), price=price), log_fill=False)
    return pf


# 1. Kill switch: reducing exit passes, increasing entry rejected.
def test_kill_switch_lets_reducing_exit_through_but_blocks_entries():
    m = market(Venue.KALSHI, "K1", "E", "YES")
    pf = _portfolio_with_long(m, size=10, price=0.40)
    risk = RiskManager(RiskLimits(), pf)
    risk.kill_switch = True
    marks = {m.key: book(m, bid=0.29, bid_sz=100, ask=0.31, ask_sz=100)}

    flatten = Order(market=m, side=Side.SELL, size=Decimal(10),
                    type=OrderType.LIMIT, price=0.29)
    assert risk.approve(flatten, marks) is True

    entry = Order(market=m, side=Side.BUY, size=Decimal(5),
                  type=OrderType.LIMIT, price=0.31)
    assert risk.approve(entry, marks) is False
    assert entry.reason == "kill_switch_active"


# 2. UTC day rollover re-baselines the session and re-arms the kill switch.
def test_day_rollover_rebaselines_and_resets_kill_switch():
    m = market(Venue.KALSHI, "K1", "E", "YES")
    pf = _portfolio_with_long(m, size=100, price=0.90)
    risk = RiskManager(RiskLimits(max_daily_loss=Decimal(50)), pf)
    marks = {m.key: book(m, bid=0.29, bid_sz=100, ask=0.31, ask_sz=100)}  # mid 0.30

    risk.update_kill_switch(marks)      # unrealized -60 <= -50 -> trips
    assert risk.kill_switch is True

    risk._day_key -= 1                  # simulate UTC day rollover
    risk.update_kill_switch(marks)
    assert risk.kill_switch is False    # re-armed for the new day
    assert pf.session_pnl(marks) == 0   # baseline reset to current equity


# 3. Persistence restore re-baselines the session (lifetime PnL doesn't brick today).
async def test_restore_rebaselines_session(tmp_path):
    db = str(tmp_path / "state.db")
    m = market(Venue.KALSHI, "K1", "E", "YES")

    def _engine() -> Engine:
        kx = FakeExchange(Venue.KALSHI, [m],
                          {m.key: book(m, bid=0.39, bid_sz=100, ask=0.41, ask_sz=100)})
        s = Settings(live=False, paper_starting_cash=Decimal(1000),
                     persistence=PersistenceSettings(enabled=True, path=db))
        return Engine(s, ExchangeRouter({Venue.KALSHI: kx}), [build("arbitrage")])

    e1 = _engine()
    await e1.router.connect()
    await e1.discover()
    e1.manual_orders.append(
        Order(market=m, side=Side.BUY, size=Decimal(10), type=OrderType.LIMIT, price=0.41))
    await e1._tick()
    assert e1.portfolio.position(m).size == Decimal(10)
    assert e1.portfolio.session_pnl({}) != 0   # fees/spread moved equity this session
    e1.store.close()

    e2 = _engine()                              # restart: replay + re-baseline
    assert e2.portfolio.position(m).size == Decimal(10)
    assert e2.portfolio.session_pnl({}) == 0    # restored PnL is not "today's loss"
    e2.store.close()


# 4. Gross-notional cap: flatten passes at the cap; new risk is still rejected.
def test_gross_cap_allows_flatten_when_book_at_cap():
    m = market(Venue.KALSHI, "K1", "E", "YES")
    m2 = market(Venue.KALSHI, "K2", "E2", "YES")
    pf = _portfolio_with_long(m, size=100, price=0.50)
    limits = RiskLimits(max_gross_notional=Decimal(50),
                        max_notional_per_market=Decimal(50))
    risk = RiskManager(limits, pf)
    marks = {m.key: book(m, bid=0.49, bid_sz=200, ask=0.51, ask_sz=200),
             m2.key: book(m2, bid=0.49, bid_sz=200, ask=0.51, ask_sz=200)}
    # gross = 100 * mid 0.50 = 50 -> exactly at the cap.

    flatten = Order(market=m, side=Side.SELL, size=Decimal(100),
                    type=OrderType.LIMIT, price=0.49)
    assert risk.approve(flatten, marks) is True

    entry = Order(market=m2, side=Side.BUY, size=Decimal(10),
                  type=OrderType.LIMIT, price=0.51)
    assert risk.approve(entry, marks) is False
    assert "gross_notional_cap" in entry.reason


# 5. Exit cooldown: no duplicate flatten within the window; re-allowed after;
#    entry cleared once flat.
def test_exit_cooldown_suppresses_duplicates_then_reallows():
    m = market(Venue.KALSHI, "K1", "E", "YES")
    from tradingbot.models import Position
    pos = Position(market=m)
    pos.apply(Fill(market_key=m.key, side=Side.BUY, size=Decimal(20), price=0.40))
    b = book(m, bid=0.29, bid_sz=50, ask=0.31, ask_sz=50)   # -27.5% -> stop
    em = ExitManager(ExitSettings(enabled=True, stop_loss_pct=0.20, emit_cooldown_s=30.0))

    assert len(em.evaluate({m.key: pos}, {m.key: b})) == 1  # first exit emits
    assert em.evaluate({m.key: pos}, {m.key: b}) == []      # duplicate suppressed

    em._last_emit[m.key] -= 31                              # cooldown elapsed
    assert len(em.evaluate({m.key: pos}, {m.key: b})) == 1  # re-allowed

    pos.apply(Fill(market_key=m.key, side=Side.SELL, size=Decimal(20), price=0.29))
    assert pos.size == 0
    em.evaluate({m.key: pos}, {m.key: b})                   # flat -> entry cleared
    assert m.key not in em._last_emit


# 6. Rate limit: quote churn can't starve exits; reducing bypasses but still counts.
def test_rate_limit_starved_exit_still_passes():
    import time
    m = market(Venue.KALSHI, "K1", "E", "YES")
    pf = _portfolio_with_long(m, size=10, price=0.40)
    risk = RiskManager(RiskLimits(max_orders_per_min=60), pf)
    now = time.time()
    risk._order_times.extend([now] * 60)                    # window exhausted
    marks = {m.key: book(m, bid=0.29, bid_sz=100, ask=0.31, ask_sz=100)}

    flatten = Order(market=m, side=Side.SELL, size=Decimal(10),
                    type=OrderType.LIMIT, price=0.29)
    assert risk.approve(flatten, marks) is True
    assert len(risk._order_times) == 61                     # still counted in window

    entry = Order(market=m, side=Side.BUY, size=Decimal(5),
                  type=OrderType.LIMIT, price=0.31)
    assert risk.approve(entry, marks) is False
    assert entry.reason == "rate_limit_exceeded"


# 7. Paper fills: avg_fill_price is the running average across partial fills.
def test_avg_fill_price_running_average_across_partial_fills():
    m = market(Venue.KALSHI, "K1", "E", "YES")
    paper = PaperExchange(FakeExchange(Venue.KALSHI, [m], {}))
    order = Order(market=m, side=Side.BUY, size=Decimal(20),
                  type=OrderType.LIMIT, price=0.50)

    paper._fill_against(order, [PriceLevel(price=0.40, size=Decimal(10))])
    assert order.filled_size == Decimal(10)
    assert order.avg_fill_price == 0.40

    paper._fill_against(order, [PriceLevel(price=0.50, size=Decimal(10))])
    assert order.filled_size == Decimal(20)
    assert order.avg_fill_price == 0.45     # (0.40*10 + 0.50*10) / 20, not 0.50


# 8. Correlation filter: same-direction second entry rejected; opposite direction
#    and reducing orders pass.
def test_correlation_filter():
    a = market(Venue.KALSHI, "A", "EA", "YES")
    b = market(Venue.KALSHI, "B", "EB", "YES")
    pf = _portfolio_with_long(a, size=10, price=0.40)       # long the first member
    limits = RiskLimits(correlation_groups=[[a.key, b.key]])
    risk = RiskManager(limits, pf)
    marks = {a.key: book(a, bid=0.39, bid_sz=100, ask=0.41, ask_sz=100),
             b.key: book(b, bid=0.39, bid_sz=100, ask=0.41, ask_sz=100)}

    same_dir = Order(market=b, side=Side.BUY, size=Decimal(10),
                     type=OrderType.LIMIT, price=0.41)
    assert risk.approve(same_dir, marks) is False
    assert same_dir.reason == "correlated_exposure"

    opposite = Order(market=b, side=Side.SELL, size=Decimal(10),
                     type=OrderType.LIMIT, price=0.39)
    assert risk.approve(opposite, marks) is True

    # Reducing the correlated position itself always passes, even after the
    # opposite-direction short in B exists.
    reduce_a = Order(market=a, side=Side.SELL, size=Decimal(5),
                     type=OrderType.LIMIT, price=0.39)
    assert risk.approve(reduce_a, marks) is True
