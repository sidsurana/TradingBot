"""Tests for the Telegram bridge's scheduled daily reports.

Covers the pure scheduling math (seconds_until, incl. DST boundaries), the
marker-file dedupe helpers, and the deterministic morning brief built from
canned Yahoo/Coinbase payloads via httpx.MockTransport — including the
per-symbol degradation path. Also proves the bridge module imports cleanly
with no environment configured (no crash, no network at import time).
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

AGENT_DIR = Path(__file__).resolve().parent.parent / "langgraph_agent"
sys.path.insert(0, str(AGENT_DIR))

# telegram_bridge deliberately runs load_dotenv() at import (the launchd service
# depends on it), which would leak the operator's real langgraph_agent/.env into
# os.environ and contaminate every other test in this session. Import it with
# the environment snapshotted and restored.
_env_before = dict(os.environ)
try:
    import telegram_bridge as tb  # noqa: E402
finally:
    os.environ.clear()
    os.environ.update(_env_before)

NY = "America/New_York"
NY_TZ = ZoneInfo(NY)


@pytest.fixture(autouse=True)
def _disable_yfinance(monkeypatch):
    """The bridge tries yfinance (real network) before the raw-httpx fallback;
    tests must exercise the mocked fallback path deterministically."""
    def _raise(symbol):
        raise RuntimeError("yfinance disabled in tests")
    monkeypatch.setattr(tb, "_yf_daily_closes", _raise)


# --------------------------------------------------------------------------- import
def test_module_imports_without_env():
    """The bridge must import with a bare environment — no raise, no connect.
    Run in a subprocess so nothing from this test session's env leaks in."""
    code = (
        f"import sys; sys.path.insert(0, {str(AGENT_DIR)!r}); "
        "import telegram_bridge; print('OK')"
    )
    env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "")}
    proc = subprocess.run(
        [sys.executable, "-c", code],
        env=env, cwd=os.getcwd(),  # conftest chdirs to a tmp dir: no .env visible
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert "OK" in proc.stdout


# --------------------------------------------------------------------------- seconds_until
def test_seconds_until_same_day():
    now = datetime(2026, 7, 8, 6, 0, tzinfo=NY_TZ)
    assert tb.seconds_until("07:30", NY, now) == pytest.approx(5400.0)


def test_seconds_until_rolls_to_next_day():
    now = datetime(2026, 7, 8, 8, 0, tzinfo=NY_TZ)
    assert tb.seconds_until("07:30", NY, now) == pytest.approx(84600.0)


def test_seconds_until_exactly_at_fire_time_is_tomorrow():
    now = datetime(2026, 7, 8, 7, 30, tzinfo=NY_TZ)
    assert tb.seconds_until("07:30", NY, now) == pytest.approx(86400.0)


def test_seconds_until_dst_spring_forward():
    # 2026-03-08 02:00 EST -> 03:00 EDT: 01:30 to 07:30 is 5 real hours, not 6.
    now = datetime(2026, 3, 8, 1, 30, tzinfo=NY_TZ)
    assert tb.seconds_until("07:30", NY, now) == pytest.approx(5 * 3600.0)


def test_seconds_until_dst_fall_back():
    # 2026-11-01 02:00 EDT -> 01:00 EST: 00:30 to 07:30 is 8 real hours, not 7.
    now = datetime(2026, 11, 1, 0, 30, tzinfo=NY_TZ)
    assert tb.seconds_until("07:30", NY, now) == pytest.approx(8 * 3600.0)


def test_seconds_until_naive_now_is_treated_as_tz_local():
    assert tb.seconds_until("07:30", NY, datetime(2026, 7, 8, 6, 0)) == pytest.approx(5400.0)


def test_seconds_until_empty_tz_uses_system_local():
    # Naive now + empty tz = pure wall-clock arithmetic in the system zone.
    assert tb.seconds_until("07:30", "", datetime(2026, 7, 8, 7, 0)) == pytest.approx(1800.0)


@pytest.mark.parametrize("bad", ["", "0730", "25:00", "12:60", "ab:cd"])
def test_seconds_until_rejects_malformed_times(bad):
    with pytest.raises(ValueError):
        tb.seconds_until(bad, NY, datetime(2026, 7, 8, 6, 0, tzinfo=NY_TZ))


# --------------------------------------------------------------------------- marker dedupe
def test_marker_missing_means_not_sent(tmp_path):
    marker = str(tmp_path / ".m")
    assert tb._marker_holds(marker, "2026-07-08") is False


def test_marker_write_then_holds_today_only(tmp_path):
    marker = str(tmp_path / ".m")
    tb._write_marker(marker, "2026-07-08")
    assert tb._marker_holds(marker, "2026-07-08") is True   # dedupe: skip resend
    assert tb._marker_holds(marker, "2026-07-09") is False  # next day fires again


def test_marker_overwrite_moves_the_date(tmp_path):
    marker = str(tmp_path / ".m")
    tb._write_marker(marker, "2026-07-08")
    tb._write_marker(marker, "2026-07-09")
    assert tb._marker_holds(marker, "2026-07-08") is False
    assert tb._marker_holds(marker, "2026-07-09") is True


# --------------------------------------------------------------------------- morning brief
def _yahoo_payload(closes):
    return {
        "chart": {
            "result": [
                {
                    "meta": {"regularMarketPrice": closes[-1]},
                    "timestamp": list(range(len(closes))),
                    "indicators": {"quote": [{"close": closes}]},
                }
            ],
            "error": None,
        }
    }


def _coinbase_payload(closes):
    """Coinbase rows are [time, low, high, open, close, volume], newest-first."""
    rows = [
        [86400 * (len(closes) - i), c - 1, c + 1, c, c, 10.0]
        for i, c in enumerate(reversed(closes))
    ]
    return rows


def _mock_client(yahoo_closes: dict, coinbase_closes, fail_symbols=(), captured=None):
    """MockTransport client serving canned Yahoo + Coinbase JSON."""

    def handler(request: httpx.Request) -> httpx.Response:
        if captured is not None:
            captured.append(request)
        if request.url.host == "api.exchange.coinbase.com":
            if coinbase_closes is None:
                return httpx.Response(500, text="down")
            return httpx.Response(200, json=_coinbase_payload(coinbase_closes))
        symbol = request.url.path.rsplit("/", 1)[-1]
        if symbol in fail_symbols:
            return httpx.Response(429, text="rate limited")
        if symbol not in yahoo_closes:
            return httpx.Response(404, text="unknown symbol")
        return httpx.Response(200, json=_yahoo_payload(yahoo_closes[symbol]))

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# 24 closes: 22 flat then a move — puts the last price clearly vs the 20d SMA.
UP = [100.0] * 22 + [100.0, 102.0]     # +2.00% 1d, above 20d avg
DOWN = [50.0] * 22 + [50.0, 48.0]      # -4.00% 1d, below 20d avg
YAHOO_ALL = {"SPY": UP, "QQQ": DOWN, "GC=F": UP, "CL=F": DOWN}


def test_morning_brief_happy_path():
    # A near-term and a far-term carry position; brief ranks soonest-first.
    now = datetime.now(tb._report_tz()).timestamp()
    pf = {"equity": 999.77, "cash": 900.11, "session_pnl": -0.23,
          "open_position_count": 2}
    pos = [
        {"title": "Will Norway win the 2026 World Cup?", "outcome": "No",
         "size": 53, "avg_price": 0.941, "mark": 0.9405, "unrealized_pnl": -0.03,
         "close_time": now + 32 * 86400},
        {"title": "Israel closes its airspace by July 15?", "outcome": "No",
         "size": 52, "avg_price": 0.96, "mark": 0.9595, "unrealized_pnl": -0.03,
         "close_time": now + 6 * 3600},
    ]

    async def go():
        async with _api_client(pf, pos) as client:
            return await tb.build_morning_brief(client)

    msg = asyncio.run(go())
    lines = msg.splitlines()
    assert "Morning brief" in lines[0]
    assert datetime.now(tb._report_tz()).strftime("%d %b %Y") in lines[0]
    assert "Certainty Carry book" in lines
    assert "Equity $999.77 | cash $900.11 | deployed $99.66 (2 positions)" in lines
    # soonest-to-resolve first: the 6h airspace market ranks above the 32d one
    airspace_i = next(i for i, l in enumerate(lines) if "airspace" in l)
    norway_i = next(i for i, l in enumerate(lines) if "Norway" in l)
    assert airspace_i < norway_i
    assert "~6h to resolve" in lines[airspace_i]
    assert "~32d to resolve" in lines[norway_i]
    assert "*" not in msg and "`" not in msg  # plain text for Telegram


def test_morning_brief_engine_offline_still_sends():
    async def go():
        async with _api_client(down=True) as client:
            return await tb.build_morning_brief(client)

    msg = asyncio.run(go())
    assert "Morning brief" in msg
    assert "Engine offline" in msg


def test_morning_brief_no_positions():
    pf = {"equity": 1000.0, "cash": 1000.0, "session_pnl": 0.0,
          "open_position_count": 0}

    async def go():
        async with _api_client(pf, []) as client:
            return await tb.build_morning_brief(client)

    msg = asyncio.run(go())
    assert "No open positions" in msg
    assert "deployed $0.00 (0 positions)" in msg


# --------------------------------------------------------------------------- night report
def _bridge() -> "tb.SupervisorTelegramBridge":
    return tb.SupervisorTelegramBridge(app=None, checkpointer=None, allowed=set())


def _api_client(pf=None, pos=None, down=False):
    def handler(request: httpx.Request) -> httpx.Response:
        if down:
            raise httpx.ConnectError("connection refused")
        if request.url.path == "/portfolio":
            return httpx.Response(200, json=pf)
        if request.url.path == "/positions":
            return httpx.Response(200, json=pos or [])
        return httpx.Response(404)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _write_history(rows):
    with open(tb.PNL_CSV, "w") as f:  # conftest chdir'd us to a tmp cwd
        f.write("epoch,equity,cash,session_pnl\n")
        for r in rows:
            f.write(",".join(str(x) for x in r) + "\n")


def test_night_report_online_with_positions():
    _write_history([[1751000000, "1000.00", "900.00", "0.00"],
                    [1751900000, "1040.00", "900.00", "5.00"]])
    pf = {"equity": 1050.0, "cash": 900.0, "session_pnl": 12.5}
    pos = [{"title": "Test market", "outcome": "YES", "size": 10,
            "avg_price": 0.5, "mark": 0.6, "unrealized_pnl": 1.0}]

    async def go():
        async with _api_client(pf, pos) as client:
            return await _bridge()._build_night_report(client)

    msg = asyncio.run(go())
    assert "Night report" in msg
    assert datetime.now(tb._report_tz()).strftime("%d %b %Y") in msg
    assert "Equity $1050.00" in msg          # equity-curve summary reused
    assert "Since start" in msg
    assert "Test market" in msg              # open positions via existing formatter
    assert "session PnL $12.5" in msg


def test_night_report_engine_offline_falls_back_to_csv():
    _write_history([[1751000000, "1000.00", "900.00", "0.00"],
                    [1751900000, "1040.00", "900.00", "5.00"]])

    async def go():
        async with _api_client(down=True) as client:
            return await _bridge()._build_night_report(client)

    msg = asyncio.run(go())
    assert "Engine offline" in msg
    assert "Equity $1040.00" in msg  # last CSV snapshot still reported


def test_night_report_offline_with_no_history_still_sends():
    async def go():
        async with _api_client(down=True) as client:
            return await _bridge()._build_night_report(client)

    msg = asyncio.run(go())
    assert "Engine offline" in msg
    assert "No P&L history yet." in msg


# --------------------------------------------------------------------------- night report gating
def test_evening_report_time_gates_old_24h_report():
    """When EVENING_REPORT_TIME is set the _pnl_tracker must not send its own
    24h report (the CSV snapshotting is separate and untouched). Verify the
    gate expression itself so we don't need a live tracker loop."""
    src = (AGENT_DIR / "telegram_bridge.py").read_text()
    assert "if not EVENING_REPORT_TIME and ts - last >= PNL_REPORT_INTERVAL_S:" in src


def test_brief_line_formatting_unit():
    assert tb._brief_line("SPY", UP) == "SPY: 102.00 (+2.00% 1d, above 20d avg)"
    assert tb._brief_line("QQQ", DOWN) == "QQQ: 48.00 (-4.00% 1d, below 20d avg)"
    # single close: no 1d change, still reports SMA side
    assert tb._brief_line("X", [10.0]) == "X: 10.00 (above 20d avg)"
