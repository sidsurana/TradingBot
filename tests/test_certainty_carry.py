"""Certainty Carry strategy — unit tests.

All tests build a Context directly from fabricated Polymarket Markets, OrderBooks
and Positions. No engine, no network. A fixed clock is injected so
time-to-resolution and cooldown are deterministic.
"""

from decimal import Decimal

from tradingbot.config import CertaintyCarrySettings
from tradingbot.models import (
    Market, OrderBook, OrderType, Position, PriceLevel, Side, Venue,
)
from tradingbot.strategies import build
from tradingbot.strategies.base import Context

NOW = 1_000_000.0
GOOD_END = NOW + 7 * 86400          # 7 days out: inside [48h, 14d]
DAY = 86400.0


def strat(cfg=None, cooldown_s=60.0):
    return build("certainty_carry", cfg=cfg or CertaintyCarrySettings(),
                 clock=lambda: NOW, cooldown_s=cooldown_s)


def pm(mid, event, *, title="Bitcoin above 100k", outcome="YES",
       volume=10000.0, category="crypto", is_sports=False, end_ts=GOOD_END,
       uma_status="resolved"):
    return Market(
        venue=Venue.POLYMARKET, market_id=mid, event_id=event, title=title,
        outcome=outcome,
        metadata={"volume": volume, "category": category, "is_sports": is_sports,
                  "end_ts": end_ts, "uma_status": uma_status},
    )


def bk(m, *, bid, ask, bid_sz=1000, ask_sz=1000):
    return OrderBook(
        market_key=m.key,
        bids=(PriceLevel(price=bid, size=Decimal(str(bid_sz))),),
        asks=(PriceLevel(price=ask, size=Decimal(str(ask_sz))),),
    )


def ctx(markets, books, positions=None, equity=1_000_000.0):
    return Context(markets, {b.market_key: b for b in books}, positions or {},
                   equity=equity)


def pos(m, size, avg=0.95):
    return Position(market=m, size=Decimal(str(size)), avg_price=avg)


# --- registration ---------------------------------------------------------

def test_registered_with_name():
    s = strat()
    assert s.name == "certainty_carry"


# --- entry: happy path ----------------------------------------------------

def test_entry_fires_in_band():
    m = pm("T1", "E1")
    b = bk(m, bid=0.945, ask=0.95)
    orders = strat().generate(ctx([m], [b]))
    assert len(orders) == 1
    o = orders[0]
    assert o.side is Side.BUY and o.type is OrderType.LIMIT
    assert o.price == 0.95 and o.market.key == m.key
    assert "carry ask=0.950" in o.reason


# --- entry: universe rejections ------------------------------------------

def test_rejects_ask_below_band():
    m = pm("T1", "E1")
    b = bk(m, bid=0.895, ask=0.90)          # below price_min 0.94
    assert strat().generate(ctx([m], [b])) == []


def test_rejects_ask_above_band():
    m = pm("T1", "E1")
    b = bk(m, bid=0.965, ask=0.97)          # above price_max 0.96
    assert strat().generate(ctx([m], [b])) == []


def test_rejects_wide_spread():
    m = pm("T1", "E1")
    b = bk(m, bid=0.93, ask=0.95)           # spread 0.02 > max_spread 0.015
    assert strat().generate(ctx([m], [b])) == []


def test_rejects_low_volume():
    m = pm("T1", "E1", volume=100.0)        # < min_volume_24h 5000
    b = bk(m, bid=0.945, ask=0.95)
    assert strat().generate(ctx([m], [b])) == []


def test_rejects_sports():
    m = pm("T1", "E1", is_sports=True)
    b = bk(m, bid=0.945, ask=0.95)
    assert strat().generate(ctx([m], [b])) == []


def test_rejects_too_near_resolution():
    m = pm("T1", "E1", end_ts=NOW + 10 * 3600)   # 10h < min_hours 48
    b = bk(m, bid=0.945, ask=0.95)
    assert strat().generate(ctx([m], [b])) == []


def test_rejects_too_far_resolution():
    m = pm("T1", "E1", end_ts=NOW + 30 * DAY)    # 30d > max_days 14
    b = bk(m, bid=0.945, ask=0.95)
    assert strat().generate(ctx([m], [b])) == []


def test_rejects_missing_end_ts():
    m = pm("T1", "E1", end_ts=0.0)
    b = bk(m, bid=0.945, ask=0.95)
    assert strat().generate(ctx([m], [b])) == []


def test_rejects_disputed_uma():
    m = pm("T1", "E1", uma_status="disputed")
    b = bk(m, bid=0.945, ask=0.95)
    assert strat().generate(ctx([m], [b])) == []


def test_disputed_uma_allowed_when_gate_off():
    m = pm("T1", "E1", uma_status="disputed")
    b = bk(m, bid=0.945, ask=0.95)
    cfg = CertaintyCarrySettings(require_uma_ok=False)
    assert len(strat(cfg).generate(ctx([m], [b]))) == 1


def test_rejects_vetoed_keyword():
    m = pm("T1", "E1", title="Will Elon tweet before Friday")   # "tweet" vetoed
    b = bk(m, bid=0.945, ask=0.95)
    assert strat().generate(ctx([m], [b])) == []


def test_rejects_failed_annualized_carry():
    # In-band but demand an absurd annualized carry the ~7-day trade can't meet.
    m = pm("T1", "E1")
    b = bk(m, bid=0.945, ask=0.95)
    cfg = CertaintyCarrySettings(min_annualized_carry=10.0)
    assert strat(cfg).generate(ctx([m], [b])) == []


def test_category_gate_blocks_and_allows():
    m = pm("T1", "E1", category="entertainment")
    b = bk(m, bid=0.945, ask=0.95)
    cfg = CertaintyCarrySettings(allowed_categories=["crypto", "politics"])
    assert strat(cfg).generate(ctx([m], [b])) == []           # not allowed
    m2 = pm("T2", "E2", category="crypto", title="Ether above 5k")
    b2 = bk(m2, bid=0.945, ask=0.95)
    assert len(strat(cfg).generate(ctx([m2], [b2]))) == 1     # allowed


# --- concentration --------------------------------------------------------

def test_max_positions_cap():
    cfg = CertaintyCarrySettings(max_positions=1)
    held = pos(pm("H1", "EH", title="Ether above 5k"), 10)   # 1 existing open
    m = pm("T1", "E1")
    b = bk(m, bid=0.945, ask=0.95)
    orders = strat(cfg).generate(ctx([m], [b], {held.market.key: held}))
    assert orders == []


def test_max_per_group_cap_existing():
    cfg = CertaintyCarrySettings(max_per_group=1)
    # existing open position on the same subject/category => group full
    held = pos(pm("H1", "EH", title="Bitcoin under 90k"), 10)
    m = pm("T1", "E1", title="Bitcoin above 100k")
    b = bk(m, bid=0.945, ask=0.95)
    orders = strat(cfg).generate(ctx([m], [b], {held.market.key: held}))
    assert orders == []


def test_max_per_group_cap_same_tick():
    cfg = CertaintyCarrySettings(max_per_group=1)
    m1 = pm("T1", "E1", title="Bitcoin above 100k")
    m2 = pm("T2", "E2", title="Bitcoin above 120k")   # same group (crypto/Bitcoin)
    b1 = bk(m1, bid=0.945, ask=0.95)
    b2 = bk(m2, bid=0.945, ask=0.95)
    orders = strat(cfg).generate(ctx([m1, m2], [b1, b2]))
    assert len(orders) == 1     # only one correlated buy this tick


def test_sleeve_cap_rejects():
    # equity 100 -> sleeve budget 40; an existing ~38 notional leaves no room.
    held = pos(pm("H1", "EH", title="Ether above 5k"), 40, avg=0.95)   # 38 notional
    m = pm("T1", "E1")
    b = bk(m, bid=0.945, ask=0.95)
    orders = strat().generate(ctx([m], [b], {held.market.key: held}, equity=100.0))
    assert orders == []


# --- sizing ---------------------------------------------------------------

def test_sizing_notional_floor():
    m = pm("T1", "E1")
    b = bk(m, bid=0.945, ask=0.95, ask_sz=1000)
    o = strat().generate(ctx([m], [b]))[0]
    assert o.size == Decimal(52)     # floor(50/0.95)=52, sleeve & book not binding


def test_sizing_capped_by_book_size():
    m = pm("T1", "E1")
    b = bk(m, bid=0.945, ask=0.95, ask_sz=10)
    o = strat().generate(ctx([m], [b]))[0]
    assert o.size == Decimal(10)     # book resting size caps it


def test_sizing_capped_by_sleeve():
    # equity 30 -> sleeve budget 12; floor(0.4*30/0.95)=12 shares.
    m = pm("T1", "E1")
    b = bk(m, bid=0.945, ask=0.95, ask_sz=1000)
    o = strat().generate(ctx([m], [b], equity=30.0))[0]
    assert o.size == Decimal(12)


# --- exits ----------------------------------------------------------------

def test_no_exit_for_healthy_holding():
    m = pm("T1", "E1")
    b = bk(m, bid=0.96, ask=0.98)        # mid 0.97 >= complement_exit_below
    held = pos(m, 10)
    assert strat().generate(ctx([m], [b], {m.key: held})) == []


def test_complement_lock_chosen():
    # held token deteriorated (mid 0.81); complement cheap so 1-comp_ask > bid.
    m = pm("T1", "E1", title="Bitcoin above 100k")
    comp = pm("T1N", "E1", title="Bitcoin above 100k", outcome="NO")
    b = bk(m, bid=0.80, ask=0.82)
    cb = bk(comp, bid=0.13, ask=0.15)    # 1-0.15=0.85 > bid 0.80
    held = pos(m, 10, avg=0.95)
    orders = strat().generate(ctx([m, comp], [b, cb], {m.key: held}))
    assert len(orders) == 1
    o = orders[0]
    assert o.market.key == comp.key and o.side is Side.BUY
    assert o.price == 0.15 and o.size == Decimal(10)
    assert o.reason == "carry_complement_lock"


def test_sell_at_bid_chosen_when_complement_worse():
    m = pm("T1", "E1", title="Bitcoin above 100k")
    comp = pm("T1N", "E1", title="Bitcoin above 100k", outcome="NO")
    b = bk(m, bid=0.80, ask=0.82)
    cb = bk(comp, bid=0.23, ask=0.25)    # 1-0.25=0.75 < bid 0.80 -> sell wins
    held = pos(m, 10, avg=0.95)
    orders = strat().generate(ctx([m, comp], [b, cb], {m.key: held}))
    o = orders[0]
    assert o.market.key == m.key and o.side is Side.SELL
    assert o.price == 0.80 and o.size == Decimal(10)
    assert o.reason == "carry_stop_bid"


def test_sell_at_bid_when_no_complement():
    m = pm("T1", "E1")
    b = bk(m, bid=0.80, ask=0.82)        # mid 0.81, no complement in ctx
    held = pos(m, 10, avg=0.95)
    o = strat().generate(ctx([m], [b], {m.key: held}))[0]
    assert o.side is Side.SELL and o.reason == "carry_stop_bid"


# --- dedupe / cooldown ----------------------------------------------------

def test_no_double_emit_within_cooldown():
    s = strat()
    m = pm("T1", "E1")
    b = bk(m, bid=0.945, ask=0.95)
    c = ctx([m], [b])
    assert len(s.generate(c)) == 1      # entry fires, records last-acted
    assert s.generate(c) == []          # in-flight; not re-emitted within cooldown


def test_exit_not_re_emitted_within_cooldown():
    s = strat()
    m = pm("T1", "E1")
    b = bk(m, bid=0.80, ask=0.82)
    held = pos(m, 10, avg=0.95)
    c = ctx([m], [b], {m.key: held})
    assert len(s.generate(c)) == 1
    assert s.generate(c) == []


# --- issue 3: group-key normalization -------------------------------------

class _Clock:
    """Mutable clock so multi-tick tests can advance past the cooldown."""

    def __init__(self, t=NOW):
        self.t = t

    def __call__(self):
        return self.t


def test_group_key_normalization():
    s = strat()
    # Leading stopword "Will" is stripped so the subject is the real underlying,
    # and the ticker alias collapses BTC<->Bitcoin, Ethereum<->ETH.
    assert s._group_key(pm("a", "e", title="Will Bitcoin hit 100k")) == ("crypto", "btc")
    assert s._group_key(pm("a", "e", title="BTC above 120k")) == ("crypto", "btc")
    assert s._group_key(pm("a", "e", title="Will Ethereum moon")) == ("crypto", "eth")
    # No meaningful token left -> fall back to category.
    assert s._group_key(pm("a", "e", title="Will the")) == ("crypto", "crypto")


def test_stopword_no_longer_collapses_distinct_subjects():
    # Before the fix both titles collapsed to "Will" and shared a group, so the
    # per-group cap wrongly blocked the second. Now Bitcoin vs Ethereum differ.
    cfg = CertaintyCarrySettings(max_per_group=1)
    m1 = pm("T1", "E1", title="Will Bitcoin hit 100k")
    m2 = pm("T2", "E2", title="Will Ethereum hit 5k")
    b1 = bk(m1, bid=0.945, ask=0.95)
    b2 = bk(m2, bid=0.945, ask=0.95)
    orders = strat(cfg).generate(ctx([m1, m2], [b1, b2]))
    assert len(orders) == 2      # distinct underlyings, both fire


def test_alias_collapses_bitcoin_and_btc():
    # "Bitcoin" and "BTC" are the same underlying: they must share a group so
    # the per-group cap fires (previously they split and both slipped through).
    cfg = CertaintyCarrySettings(max_per_group=1)
    m1 = pm("T1", "E1", title="Bitcoin above 100k")
    m2 = pm("T2", "E2", title="BTC above 120k")
    b1 = bk(m1, bid=0.945, ask=0.95)
    b2 = bk(m2, bid=0.945, ask=0.95)
    orders = strat(cfg).generate(ctx([m1, m2], [b1, b2]))
    assert len(orders) == 1      # same group -> only one correlated buy


# --- issue 1: complement-lock idempotency ---------------------------------

def _pair(bid_m=0.80, ask_m=0.82, bid_c=0.13, ask_c=0.15, ask_c_sz=1000):
    m = pm("T1", "E1", title="Bitcoin above 100k")
    comp = pm("T1N", "E1", title="Bitcoin above 100k", outcome="NO")
    b = bk(m, bid=bid_m, ask=ask_m)
    cb = bk(comp, bid=bid_c, ask=ask_c, ask_sz=ask_c_sz)
    return m, comp, b, cb


def test_complete_set_emits_nothing_across_ticks():
    # BOTH legs of the event are held at equal size -> a complete $1 set. Even
    # though both legs' mids look "deteriorated", the strategy must emit ZERO
    # orders for either leg over many ticks (leave it for settlement). Before
    # the fix each leg re-bought the other and both doubled every cooldown.
    m, comp, b, cb = _pair()
    positions = {m.key: pos(m, 10), comp.key: pos(comp, 10)}
    clk = _Clock()
    s = build("certainty_carry", cfg=CertaintyCarrySettings(),
              clock=clk, cooldown_s=60.0)
    c = ctx([m, comp], [b, cb], positions)
    for _ in range(4):
        assert s.generate(c) == []
        clk.t += 120.0            # advance well past the cooldown each tick


def test_partial_set_locks_only_the_remainder_once():
    # Held 10 of the leg, only 3 of the complement -> unhedged remainder 7.
    # Lock exactly 7 once, then stop (the in-flight lock covers the rest).
    m, comp, b, cb = _pair()
    positions = {m.key: pos(m, 10), comp.key: pos(comp, 3)}
    clk = _Clock()
    s = build("certainty_carry", cfg=CertaintyCarrySettings(),
              clock=clk, cooldown_s=60.0)
    c = ctx([m, comp], [b, cb], positions)

    o1 = s.generate(c)
    assert len(o1) == 1
    assert o1[0].market.key == comp.key and o1[0].side is Side.BUY
    assert o1[0].size == Decimal(7)      # only the unhedged remainder
    assert o1[0].reason == "carry_complement_lock"

    clk.t += 120.0                        # past cooldown
    assert s.generate(c) == []            # remainder already in-flight -> stop


# --- issue 2: exit respects book depth / risk caps ------------------------

def test_lock_capped_by_thin_complement_book():
    # Complement book only offers 5 -> lock 5, not the full position size 10.
    m, comp, b, cb = _pair(ask_c_sz=5)
    positions = {m.key: pos(m, 10)}       # complement not held
    o = strat().generate(ctx([m, comp], [b, cb], positions))[0]
    assert o.market.key == comp.key and o.side is Side.BUY
    assert o.size == Decimal(5)           # book depth caps the lock


# --- issue 4: exit not doubled once one is outstanding --------------------

def test_exit_not_doubled_past_cooldown():
    # A sell-at-bid exit fires; a later tick PAST the cooldown must not re-emit
    # it (the outstanding-exit marker, not the time cooldown, suppresses it).
    m = pm("T1", "E1")                    # no complement in ctx -> sell path
    b = bk(m, bid=0.80, ask=0.82)
    clk = _Clock()
    s = build("certainty_carry", cfg=CertaintyCarrySettings(),
              clock=clk, cooldown_s=60.0)
    c = ctx([m], [b], {m.key: pos(m, 10)})
    o1 = s.generate(c)
    assert len(o1) == 1 and o1[0].side is Side.SELL and o1[0].size == Decimal(10)
    clk.t += 120.0                        # past cooldown -> no longer gated by time
    assert s.generate(c) == []            # outstanding marker prevents doubling
