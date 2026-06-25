from datetime import datetime
from decimal import Decimal

from tradingbot.config import GoalSettings
from tradingbot.engine.goals import GoalTracker


def _ts(y, m, d, hh=12) -> float:
    return datetime(y, m, d, hh, 0, 0).timestamp()


def test_daily_progress_and_met(tmp_path):
    gs = GoalSettings(daily_target=Decimal(50), weekly_target=Decimal(200),
                      state_path=str(tmp_path / "g.json"))
    t0 = _ts(2026, 6, 24)
    gt = GoalTracker(gs, clock=lambda: t0)
    gt.update(Decimal(1000))  # baseline at start of day

    prog = gt.progress(Decimal(1030))
    assert prog["daily"]["pnl"] == "30.00"
    assert prog["daily"]["pct_of_target"] == 60.0
    assert prog["daily"]["met"] is False
    assert prog["daily_target_met"] is False

    prog2 = gt.progress(Decimal(1055))
    assert prog2["daily"]["met"] is True
    assert prog2["daily_target_met"] is True


def test_baseline_rolls_on_new_day(tmp_path):
    gs = GoalSettings(daily_target=Decimal(50), state_path=str(tmp_path / "g.json"))
    now = {"t": _ts(2026, 6, 24)}
    gt = GoalTracker(gs, clock=lambda: now["t"])
    gt.update(Decimal(1000))
    gt.update(Decimal(1040))  # same day, baseline unchanged
    assert gt.day_start_equity == Decimal(1000)

    now["t"] = _ts(2026, 6, 25)  # next day
    gt.update(Decimal(1040))     # re-baselines to current equity
    assert gt.day_start_equity == Decimal(1040)
    assert gt.progress(Decimal(1040))["daily"]["pnl"] == "0.00"


def test_persistence_across_instances(tmp_path):
    path = str(tmp_path / "g.json")
    gs = GoalSettings(daily_target=Decimal(50), state_path=path)
    t0 = _ts(2026, 6, 24)
    gt = GoalTracker(gs, clock=lambda: t0)
    gt.update(Decimal(1000))

    # New tracker, same day, loads the baseline from disk.
    gt2 = GoalTracker(gs, clock=lambda: t0)
    assert gt2.day_start_equity == Decimal(1000)
    assert gt2.progress(Decimal(1025))["daily"]["pnl"] == "25.00"


def test_weekly_pace(tmp_path):
    gs = GoalSettings(weekly_target=Decimal(700), state_path=str(tmp_path / "g.json"))
    # Wednesday (isoweekday 3) -> pace target = 700 * 3/7 = 300.
    wed = _ts(2026, 6, 24)
    assert datetime.fromtimestamp(wed).isoweekday() == 3
    gt = GoalTracker(gs, clock=lambda: wed)
    gt.update(Decimal(1000))
    prog = gt.progress(Decimal(1350))  # +350 weekly
    assert prog["weekly"]["pace_target_so_far"] == "300.00"
    assert prog["weekly"]["on_track"] is True
