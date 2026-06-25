"""Profit goal tracking.

Tracks PnL against daily and weekly dollar targets by anchoring an equity
baseline at the start of each day/week and measuring how far above it you are
now. Baselines persist to a small JSON file so they survive restarts within the
same day/week.

Note: in paper mode positions don't yet persist across restarts (roadmap item:
persistence), so a mid-day restart re-baselines from the fresh paper cash. The
targets, pace math, and live in-process tracking are exact; cross-restart
fidelity arrives with position persistence.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal

import structlog

from tradingbot.config import GoalSettings

log = structlog.get_logger(__name__)


def _day_key(now: float) -> str:
    return datetime.fromtimestamp(now).strftime("%Y-%m-%d")


def _week_key(now: float) -> str:
    iso = datetime.fromtimestamp(now).isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


class GoalTracker:
    def __init__(self, settings: GoalSettings, clock: Callable[[], float] = time.time):
        self.settings = settings
        self._clock = clock
        self.day_key = ""
        self.week_key = ""
        self.day_start_equity = Decimal(0)
        self.week_start_equity = Decimal(0)
        self._loaded = False
        self._load()

    @property
    def enabled(self) -> bool:
        return self.settings.daily_target > 0 or self.settings.weekly_target > 0

    def update(self, equity: Decimal, now: float | None = None) -> None:
        """Roll the day/week baselines when the calendar advances."""
        now = self._clock() if now is None else now
        dk, wk = _day_key(now), _week_key(now)
        changed = False
        if not self._loaded or dk != self.day_key:
            self.day_key, self.day_start_equity = dk, equity
            changed = True
        if not self._loaded or wk != self.week_key:
            self.week_key, self.week_start_equity = wk, equity
            changed = True
        self._loaded = True
        if changed:
            self._save()

    def progress(self, equity: Decimal) -> dict:
        now = self._clock()
        daily_pnl = equity - self.day_start_equity
        weekly_pnl = equity - self.week_start_equity
        dt = self.settings.daily_target
        wt = self.settings.weekly_target

        # Weekly pace: how much we'd want by now if earning the target evenly.
        weekday = datetime.fromtimestamp(now).isoweekday()  # 1=Mon..7=Sun
        elapsed_frac = weekday / 7
        weekly_pace = wt * Decimal(str(elapsed_frac)) if wt > 0 else Decimal(0)

        return {
            "daily": self._leg(daily_pnl, dt),
            "weekly": self._leg(weekly_pnl, wt, pace=weekly_pace),
            "daily_target_met": dt > 0 and daily_pnl >= dt,
        }

    @staticmethod
    def _leg(pnl: Decimal, target: Decimal, pace: Decimal | None = None) -> dict:
        if target <= 0:
            return {"target": "0", "pnl": str(round(pnl, 2)), "enabled": False}
        pct = float(pnl / target * 100)
        leg = {
            "enabled": True,
            "target": str(target),
            "pnl": str(round(pnl, 2)),
            "remaining": str(round(max(Decimal(0), target - pnl), 2)),
            "pct_of_target": round(pct, 1),
            "met": pnl >= target,
        }
        if pace is not None:
            leg["on_track"] = pnl >= pace
            leg["pace_target_so_far"] = str(round(pace, 2))
        return leg

    # --- persistence ---------------------------------------------------------

    def _load(self) -> None:
        try:
            with open(self.settings.state_path) as f:
                data = json.load(f)
            self.day_key = data.get("day_key", "")
            self.week_key = data.get("week_key", "")
            self.day_start_equity = Decimal(str(data.get("day_start_equity", "0")))
            self.week_start_equity = Decimal(str(data.get("week_start_equity", "0")))
            self._loaded = bool(self.day_key)
        except (FileNotFoundError, json.JSONDecodeError, KeyError):
            pass

    def _save(self) -> None:
        try:
            path = self.settings.state_path
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(path, "w") as f:
                json.dump({
                    "day_key": self.day_key,
                    "week_key": self.week_key,
                    "day_start_equity": str(self.day_start_equity),
                    "week_start_equity": str(self.week_start_equity),
                }, f)
        except OSError as exc:  # non-fatal: tracking still works in-process
            log.debug("goals.save_failed", error=str(exc))
