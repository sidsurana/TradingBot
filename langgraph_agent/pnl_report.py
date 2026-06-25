"""On-demand P&L / equity-curve report from the tracker's CSV.

    cd langgraph_agent && ../.venv/bin/python pnl_report.py

Reads pnl_history.csv (written hourly by the bridge's _pnl_tracker) and prints the
equity curve summary + the sustained average daily return rate — the number that
tells you how much capital a given $/day goal actually needs.
"""

from __future__ import annotations

import csv
import os
import time

PNL_CSV = os.getenv("PNL_HISTORY_FILE", "pnl_history.csv")


def main() -> None:
    try:
        with open(PNL_CSV) as f:
            rows = [r for r in csv.DictReader(f)]
    except OSError:
        print(f"no history yet ({PNL_CSV} not found) — let the bot run a while")
        return
    if not rows:
        print("no snapshots recorded yet")
        return

    start_eq = float(rows[0]["equity"])
    start_ts = float(rows[0]["epoch"])
    now_eq = float(rows[-1]["equity"])
    now_ts = float(rows[-1]["epoch"])
    days = max((now_ts - start_ts) / 86400, 1e-9)
    t_chg = now_eq - start_eq
    t_pct = (t_chg / start_eq * 100) if start_eq else 0.0
    avg_day = t_pct / days

    print(f"snapshots:     {len(rows)}")
    print(f"window:        {days:.2f} days")
    print(f"start equity:  ${start_eq:.2f}")
    print(f"current:       ${now_eq:.2f}")
    print(f"total:         {t_chg:+.2f} ({t_pct:+.2f}%)")
    print(f"avg/day:       {avg_day:+.3f}%/day   <-- the rate that matters")
    if avg_day > 0:
        for tgt in (150, 200):
            cap = tgt / (avg_day / 100) if avg_day else 0
            print(f"  -> ${tgt}/day at this rate needs ~${cap:,.0f} capital")
    # last few points of the curve
    print("recent curve:")
    for r in rows[-6:]:
        t = time.strftime("%m-%d %H:%M", time.localtime(float(r["epoch"])))
        print(f"  {t}  equity ${float(r['equity']):.2f}  pnl {r['session_pnl']}")


if __name__ == "__main__":
    main()
