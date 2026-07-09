"""Telegram front-end for the multi-agent supervisor.

Long-polls the Telegram Bot API (httpx, no extra dep) and routes each message to
the supervisor graph (graph.py), which delegates to the research/risk/execution/
portfolio specialists. Each Telegram chat is its own persistent thread (SqliteSaver),
so the conversation — and any staged-action confirmations — survive restarts.

Only chat IDs in the allowlist may command the desk; everyone else is ignored, so a
stranger who finds the bot can't drive it.

    cd langgraph_agent
    ../.venv/bin/python telegram_bridge.py

Env (langgraph_agent/.env):
  TELEGRAM_BOT_TOKEN          from @BotFather
  TELEGRAM_ALLOWED_CHAT_IDS   JSON list or comma-separated ints, e.g. [123456789]
  ANTHROPIC_API_KEY           the model calls
  TRADINGBOT_API_URL / _TOKEN the bot's control API (already configured)
  LANGGRAPH_CHECKPOINT_DB      thread store (default agent_threads.db)
  MORNING_BRIEF_TIME          daily deterministic market brief, HH:MM local
                              (default 07:30; empty disables)
  EVENING_REPORT_TIME         daily performance report, HH:MM local (default
                              21:00; empty disables and re-enables the old
                              rolling-24h P&L report)
  REPORT_TZ                   IANA tz for the above (empty = system local)
"""

from __future__ import annotations

import asyncio
import csv
import json
import os
import re
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv
from langgraph.checkpoint.sqlite import SqliteSaver

# MUST run before importing graph/specialists: those build their ChatAnthropic
# clients at import time, so the key has to be in the environment first. (When
# started via nohup with no shell export, this load_dotenv is the only source.)
load_dotenv()

try:
    from .graph import build_supervisor
    from .specialists import research_agent
except ImportError:
    from graph import build_supervisor
    from specialists import research_agent

API = "https://api.telegram.org/bot{token}/{method}"
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHECKPOINT_DB = os.getenv("LANGGRAPH_CHECKPOINT_DB", "agent_threads.db")

# Bot control API — for proactive position updates (no model call, just reads).
BOT_API_URL = os.getenv("TRADINGBOT_API_URL", "http://127.0.0.1:8787").rstrip("/")
BOT_API_TOKEN = os.getenv("TRADINGBOT_API_TOKEN", "")
# Push an open-positions summary this often (seconds); 0 disables.
POSITION_UPDATE_INTERVAL_S = int(os.getenv("POSITION_UPDATE_INTERVAL_S", "120"))

# Autonomous trading: every AUTOTRADE_INTERVAL_S the RESEARCH agent (read tools +
# set_market_signal only — it CANNOT deploy capital, change limits, or go live)
# assesses markets and pushes/updates fair-value signals. The signal strategy
# executes them within the risk caps; exits manage protective closes.
AUTOTRADE_ENABLED = os.getenv("AUTOTRADE_ENABLED", "false").lower() in ("1", "true", "yes")
AUTOTRADE_INTERVAL_S = int(os.getenv("AUTOTRADE_INTERVAL_S", "900"))

# P&L tracking: snapshot equity to a CSV (durable equity curve) and send a daily
# summary so we can measure the real sustained daily return rate.
PNL_SNAPSHOT_INTERVAL_S = int(os.getenv("PNL_SNAPSHOT_INTERVAL_S", "3600"))   # hourly
PNL_REPORT_INTERVAL_S = int(os.getenv("PNL_REPORT_INTERVAL_S", "86400"))      # daily
PNL_CSV = os.getenv("PNL_HISTORY_FILE", "pnl_history.csv")
PNL_MARKER = ".pnl_last_report"

# Daily scheduled reports (local time HH:MM; empty string disables either one).
# The morning brief is fully deterministic (public market data, no LLM call);
# the night report reuses the control API + pnl_history.csv machinery. While
# EVENING_REPORT_TIME is set, the old rolling-24h P&L report is suppressed
# (the hourly CSV snapshots continue).
MORNING_BRIEF_TIME = os.getenv("MORNING_BRIEF_TIME", "07:30").strip()
EVENING_REPORT_TIME = os.getenv("EVENING_REPORT_TIME", "21:00").strip()
REPORT_TZ = os.getenv("REPORT_TZ", "").strip()  # IANA name; empty = system local
MORNING_MARKER = ".morning_last_brief"
EVENING_MARKER = ".evening_last_report"

# Morning-brief data sources. Yahoo requires a browser User-Agent or it 429s.
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
COINBASE_CANDLES_URL = "https://api.exchange.coinbase.com/products/BTC-USD/candles"
BRIEF_YAHOO_SYMBOLS = ("SPY", "QQQ", "GC=F", "CL=F")
_BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")
}

AUTOTRADE_PROMPT = (
    "Autonomous trading cycle — assess the live markets and act, staying selective:\n"
    "1. Call get_markets and get_positions for current prices and what we hold.\n"
    "2. For any market where you have a genuine, quantifiable fair-value edge of at "
    "least 5% vs the price, push it with set_market_signal (fair_value and confidence "
    "in 0-1). The signal strategy sizes it by Kelly within the risk caps.\n"
    "3. Review existing positions and signals with list_signals: if your view has "
    "changed, update the signal; if a holding now looks overpriced, push a bearish "
    "fair_value to reduce or reverse it. Leave holdings that still match your view.\n"
    "Only act on real edges — doing nothing is fine. Reply with a one-line summary of "
    "the actions you took, or 'no action'."
)


def _parse_allowed(raw: str) -> set[int]:
    """Accept a JSON list ([123, 456]) or a comma-separated string (123,456)."""
    raw = (raw or "").strip()
    if not raw:
        return set()
    try:
        return {int(x) for x in json.loads(raw)}
    except (json.JSONDecodeError, ValueError, TypeError):
        return {int(x) for x in raw.split(",") if x.strip()}


ALLOWED = _parse_allowed(os.getenv("TELEGRAM_ALLOWED_CHAT_IDS", ""))


def _report_tz(tz_name: str | None = None):
    """Resolve an IANA tz name (default REPORT_TZ) to a tzinfo; empty/invalid
    falls back to the system local timezone."""
    name = REPORT_TZ if tz_name is None else tz_name
    if name:
        try:
            return ZoneInfo(name)
        except Exception:  # noqa: BLE001 — bad tz name: fall back to local
            print(f"unknown REPORT_TZ {name!r}, using system local time")
    return datetime.now().astimezone().tzinfo


def seconds_until(hhmm: str, tz_name: str, now: datetime | None = None) -> float:
    """Seconds until the next occurrence of local wall-clock HH:MM in tz_name
    (empty = system local). DST-correct: the difference is taken between epoch
    timestamps (same-tzinfo datetime subtraction would be naive wall-clock
    math and miss spring-forward/fall-back hours), so a fire time across a DST
    gap is the true elapsed seconds. Raises ValueError on a malformed hhmm."""
    hh_s, _, mm_s = hhmm.partition(":")
    hh, mm = int(hh_s), int(mm_s)
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise ValueError(f"bad HH:MM time {hhmm!r}")
    tz = _report_tz(tz_name)
    if now is None:
        now = datetime.now(tz)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=tz)
    local = now.astimezone(tz)
    target = local.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if target.timestamp() <= local.timestamp():
        target = (local + timedelta(days=1)).replace(hour=hh, minute=mm,
                                                     second=0, microsecond=0)
    return max(target.timestamp() - local.timestamp(), 0.0)


def _marker_holds(marker_path: str, today: str) -> bool:
    """True if the marker file already records `today` (report already sent —
    same restart-safe dedupe idea as .pnl_last_report, keyed by local date)."""
    try:
        with open(marker_path) as f:
            return f.read().strip() == today
    except OSError:
        return False


def _write_marker(marker_path: str, today: str) -> None:
    try:
        with open(marker_path, "w") as f:
            f.write(today)
    except OSError as exc:
        print(f"marker write error ({marker_path}):", exc)


async def _yahoo_daily_closes(client: httpx.AsyncClient, symbol: str) -> list[float]:
    """Daily closes (oldest-first, ~1 month). yfinance first (browser TLS
    fingerprint — Yahoo 429-blocks plain python HTTP per IP for hours), raw
    httpx as the fallback."""
    try:
        closes = await asyncio.to_thread(_yf_daily_closes, symbol)
        if closes:
            return closes
    except Exception as exc:  # noqa: BLE001 — fall through to raw httpx
        print(f"yfinance daily closes {symbol} error:", exc)
    resp = await client.get(
        YAHOO_CHART_URL.format(symbol=symbol),
        params={"range": "1mo", "interval": "1d"},
        headers=_BROWSER_HEADERS, timeout=15,
    )
    resp.raise_for_status()
    result = resp.json()["chart"]["result"][0]
    closes = [float(c) for c in result["indicators"]["quote"][0]["close"] if c is not None]
    if not closes:
        raise ValueError(f"no closes for {symbol}")
    return closes


def _yf_daily_closes(symbol: str) -> list[float]:
    """Blocking yfinance fetch — run via asyncio.to_thread."""
    import math

    import yfinance as yf

    df = yf.Ticker(symbol).history(period="1mo", interval="1d", auto_adjust=False)
    return [float(c) for c in df["Close"].tolist() if not math.isnan(float(c))]


async def _coinbase_daily_closes(client: httpx.AsyncClient) -> list[float]:
    """Daily BTC-USD closes (oldest-first) from Coinbase Exchange candles.
    Rows are [time, low, high, open, close, volume], newest-first."""
    resp = await client.get(COINBASE_CANDLES_URL, params={"granularity": 86400}, timeout=15)
    resp.raise_for_status()
    rows = sorted(resp.json(), key=lambda r: r[0])
    closes = [float(r[4]) for r in rows]
    if not closes:
        raise ValueError("no BTC candles")
    return closes


def _brief_line(symbol: str, closes: list[float]) -> str:
    """One compact morning-brief line: last price, 1-day % change, and the
    price's side of the 20-day SMA. `closes` is oldest-first."""
    px = closes[-1]
    window = closes[-20:]
    rel = "above" if px >= sum(window) / len(window) else "below"
    if len(closes) >= 2 and closes[-2]:
        chg = (px - closes[-2]) / closes[-2] * 100
        return f"{symbol}: {px:,.2f} ({chg:+.2f}% 1d, {rel} 20d avg)"
    return f"{symbol}: {px:,.2f} ({rel} 20d avg)"


def _hours_to(close_time, now_ts: float) -> float | None:
    """Hours from now until a position's resolution, or None if unknown."""
    try:
        return (float(close_time) - now_ts) / 3600.0
    except (TypeError, ValueError):
        return None


def _fmt_horizon(hours: float | None) -> str:
    if hours is None:
        return "resolution TBD"
    if hours < 0:
        return "resolving now"
    if hours < 48:
        return f"~{hours:.0f}h to resolve"
    return f"~{hours / 24:.0f}d to resolve"


async def build_morning_brief(client: httpx.AsyncClient) -> str:
    """Certainty-carry book snapshot to start the day — no LLM call. Reads the
    live control API (/portfolio + /positions) and lists open carry positions
    soonest-to-resolve first, so you can see what's about to pay out. Degrades
    to a clear 'engine offline' line if the API is unreachable; always sends."""
    tz = _report_tz()
    now_ts = datetime.now(tz).timestamp()
    lines = [f"🌅 Morning brief — {datetime.now(tz).strftime('%a %d %b %Y')}",
             "Certainty Carry book"]
    headers = {"Authorization": f"Bearer {BOT_API_TOKEN}"}
    pf, pos, pos_ok = None, None, False
    try:
        pf = (await client.get(f"{BOT_API_URL}/portfolio", headers=headers, timeout=15)).json()
    except Exception as exc:  # noqa: BLE001 — engine may be down at 07:30
        print("morning-brief portfolio fetch error:", exc)
    try:
        pos = (await client.get(f"{BOT_API_URL}/positions", headers=headers, timeout=15)).json()
        pos_ok = isinstance(pos, list)
    except Exception as exc:  # noqa: BLE001
        print("morning-brief positions fetch error:", exc)

    if not (isinstance(pf, dict) and pf):
        lines.append("⚠️ Engine offline — control API unreachable.")
        return "\n".join(lines)

    equity = float(pf.get("equity", 0) or 0)
    cash = float(pf.get("cash", 0) or 0)
    deployed = equity - cash
    lines.append(f"Equity ${equity:.2f} | cash ${cash:.2f} | "
                 f"deployed ${deployed:.2f} ({pf.get('open_position_count', 0)} positions)")

    if not pos_ok:
        lines.append("⚠️ Positions unavailable (API error).")
        return "\n".join(lines)
    if not pos:
        lines.append("No open positions — scanning for in-band markets.")
        return "\n".join(lines)

    ranked = sorted(pos, key=lambda p: (_hours_to(p.get("close_time"), now_ts)
                                        if _hours_to(p.get("close_time"), now_ts) is not None
                                        else float("inf")))
    lines.append("Positions (soonest to resolve first):")
    for p in ranked:
        title = (p.get("title") or p.get("market", ""))[:48]
        horizon = _fmt_horizon(_hours_to(p.get("close_time"), now_ts))
        lines.append(f"• {title} [{p.get('outcome', '')}]: {p.get('size')} @ "
                     f"{p.get('avg_price')}, now {p.get('mark')} — {horizon}")
    return "\n".join(lines)


def _strip_markdown(text: str) -> str:
    """Telegram messages are sent as plain text, so Markdown shows literally (e.g.
    '**bold**' renders as asterisks). Strip it. Keep single underscores — market
    tickers use them (KXBTC_..., etc.)."""
    text = re.sub(r"^(\s*)[-*]\s+", r"\1• ", text, flags=re.M)  # bullets -> •
    text = re.sub(r"^\s{0,3}#{1,6}\s+", "", text, flags=re.M)   # ## headings
    text = re.sub(r"__(.+?)__", r"\1", text)                    # __bold__ (lone _ kept)
    text = text.replace("`", "")                               # `code`
    text = text.replace("*", "")                               # **bold** / *italic*
    return text


def _format_positions(pf: dict, pos: list) -> str:
    """Plain-text open-positions summary for the periodic push."""
    head = (f"📊 Positions — equity ${pf.get('equity', '?')}, "
            f"cash ${pf.get('cash', '?')}, session PnL ${pf.get('session_pnl', '?')}")
    lines = [head]
    for p in pos:
        title = (p.get("title") or p.get("market", ""))[:40]
        lines.append(
            f"• {title} ({p.get('outcome', '')}): {p.get('size')} @ {p.get('avg_price')}, "
            f"mark {p.get('mark')}, uPnL ${p.get('unrealized_pnl')}"
        )
    return "\n".join(lines)


def _reply_text(result: dict) -> str:
    """Coerce the supervisor's final message into a plain string for Telegram."""
    content = result["messages"][-1].content
    if isinstance(content, str):
        text = content
    else:
        # Anthropic may return a list of content blocks; keep the text parts.
        text = "".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in content)
    return _strip_markdown(text.strip()) or "(no reply)"


class SupervisorTelegramBridge:
    def __init__(self, app, checkpointer, allowed: set[int]):
        self.app = app
        self.checkpointer = checkpointer
        self.allowed = allowed
        self._offset = 0

    async def run(self) -> None:
        if not BOT_TOKEN or not self.allowed:
            raise SystemExit(
                "Set TELEGRAM_BOT_TOKEN and TELEGRAM_ALLOWED_CHAT_IDS in langgraph_agent/.env"
            )
        print(f"Supervisor Telegram bridge online — {len(self.allowed)} allowed chat(s).")
        async with httpx.AsyncClient(timeout=40.0) as client:
            await self._send(client, next(iter(self.allowed)),
                             "🤖 Trading desk online. Ask about PnL, risk, regime, or say "
                             "'deploy $200' / 'pause'. Sensitive actions ask you to confirm first.")
            # Background: proactive open-positions updates (no model call) and,
            # if enabled, the autonomous assess-and-trade loop.
            bg = [asyncio.create_task(self._position_updates(client)),
                  asyncio.create_task(self._pnl_tracker(client))]
            if AUTOTRADE_ENABLED:
                bg.append(asyncio.create_task(self._autotrade_loop(client)))
                print(f"Autonomous trading ON — assessing every {AUTOTRADE_INTERVAL_S}s.")
            if MORNING_BRIEF_TIME:
                bg.append(asyncio.create_task(self._daily_at(
                    client, MORNING_BRIEF_TIME, MORNING_MARKER, build_morning_brief)))
                print(f"Morning brief scheduled daily at {MORNING_BRIEF_TIME}.")
            if EVENING_REPORT_TIME:
                bg.append(asyncio.create_task(self._daily_at(
                    client, EVENING_REPORT_TIME, EVENING_MARKER, self._build_night_report)))
                print(f"Night report scheduled daily at {EVENING_REPORT_TIME} "
                      "(rolling-24h P&L report suppressed).")
            try:
                while True:
                    try:
                        updates = await self._get_updates(client)
                    except Exception as exc:  # noqa: BLE001
                        print("poll error:", exc)
                        await asyncio.sleep(3)
                        continue
                    for upd in updates:
                        await self._handle(client, upd)
            finally:
                for t in bg:
                    t.cancel()

    async def _autotrade_loop(self, client: httpx.AsyncClient) -> None:
        """Periodically have the research agent assess markets and push/update signals.
        Scoped to read tools + set_market_signal, so every action is Kelly-sized within
        the risk caps — it cannot deploy capital, change limits, or go live."""
        if not AUTOTRADE_ENABLED or AUTOTRADE_INTERVAL_S <= 0:
            return
        await asyncio.sleep(60)  # warmup: let books settle after start
        while True:
            try:
                summary = await asyncio.to_thread(research_agent.invoke, {"request": AUTOTRADE_PROMPT})
                text = _strip_markdown(str(summary).strip())
                if text and "no action" not in text.lower():
                    for chat_id in self.allowed:
                        await self._send(client, chat_id, "🤖 Auto-assess: " + text[:1500])
            except Exception as exc:  # noqa: BLE001
                print("autotrade error:", exc)
            await asyncio.sleep(AUTOTRADE_INTERVAL_S)

    async def _daily_at(self, client: httpx.AsyncClient, hh_mm: str,
                        marker_path: str, build_msg) -> None:
        """Generic once-a-day scheduler: sleep until the next local hh_mm
        (REPORT_TZ), skip if the marker already holds today's date (restart-safe,
        same pattern as .pnl_last_report), build the message via the async
        `build_msg(client)` callable, broadcast, write the marker, repeat."""
        try:
            seconds_until(hh_mm, REPORT_TZ)  # validate the schedule up front
        except (ValueError, TypeError) as exc:
            print(f"daily report disabled — bad time {hh_mm!r}: {exc}")
            return
        while True:
            await asyncio.sleep(seconds_until(hh_mm, REPORT_TZ))
            today = datetime.now(_report_tz()).date().isoformat()
            if _marker_holds(marker_path, today):
                continue  # already sent today; reschedule for tomorrow
            try:
                msg = await build_msg(client)
            except Exception as exc:  # noqa: BLE001 — never kill the loop
                print(f"daily report build error ({marker_path}):", exc)
                continue
            delivered = False
            for chat_id in self.allowed:
                delivered = await self._send(client, chat_id, msg) or delivered
            # Only mark the day done when someone actually received it — a
            # network blip at fire time must leave the restart-recovery path
            # open instead of silently losing the day's report.
            if delivered:
                _write_marker(marker_path, today)

    async def _build_night_report(self, client: httpx.AsyncClient) -> str:
        """Nightly performance report: live equity/cash/session PnL from the
        control API plus the equity-curve summary and open positions. Falls back
        to whatever pnl_history.csv offers when the engine is offline."""
        headers = {"Authorization": f"Bearer {BOT_API_TOKEN}"}
        pf, pos, pos_ok = None, None, False
        try:
            pf = (await client.get(f"{BOT_API_URL}/portfolio", headers=headers, timeout=15)).json()
        except Exception as exc:  # noqa: BLE001 — engine may be down at 21:00
            print("night-report fetch error:", exc)
        # Separate try: a /positions failure must not be reported as "flat".
        try:
            pos = (await client.get(f"{BOT_API_URL}/positions", headers=headers, timeout=15)).json()
            pos_ok = isinstance(pos, list)
        except Exception as exc:  # noqa: BLE001
            print("night-report positions fetch error:", exc)
        tz = _report_tz()
        lines = [f"🌙 Night report — {datetime.now(tz).strftime('%a %d %b %Y')}"]
        if isinstance(pf, dict) and pf:
            eq = float(pf.get("equity", 0) or 0)
            lines.append(self._daily_report(eq))
            if pos_ok and pos:
                lines.append(_format_positions(pf, pos))
            elif pos_ok:
                lines.append("No open positions.")
            else:
                lines.append("⚠️ Positions unavailable (API error).")
        else:
            lines.append("⚠️ Engine offline — control API unreachable.")
            rows = []
            try:
                with open(PNL_CSV) as f:
                    rows = [r for r in csv.DictReader(f)]
            except OSError:
                pass
            if rows:
                lines.append(self._daily_report(float(rows[-1]["equity"])))
            else:
                lines.append("No P&L history yet.")
        return "\n".join(lines)

    async def _position_updates(self, client: httpx.AsyncClient) -> None:
        """Every POSITION_UPDATE_INTERVAL_S, push a summary of open positions to the
        allowed chats. Pure reads from the bot's control API — no model call, so it's
        free and deterministic. Stays quiet when flat to avoid spam."""
        if POSITION_UPDATE_INTERVAL_S <= 0:
            return
        headers = {"Authorization": f"Bearer {BOT_API_TOKEN}"}
        while True:
            await asyncio.sleep(POSITION_UPDATE_INTERVAL_S)
            try:
                pf = (await client.get(f"{BOT_API_URL}/portfolio", headers=headers, timeout=15)).json()
                pos = (await client.get(f"{BOT_API_URL}/positions", headers=headers, timeout=15)).json()
            except Exception as exc:  # noqa: BLE001
                print("position-update fetch error:", exc)
                continue
            if not isinstance(pos, list) or not pos:
                continue  # flat: nothing to report
            msg = _format_positions(pf, pos)
            for chat_id in self.allowed:
                await self._send(client, chat_id, msg)

    async def _pnl_tracker(self, client: httpx.AsyncClient) -> None:
        """Snapshot equity to a CSV every hour (durable equity curve, survives
        restarts) and send a daily P&L summary so we can measure the real sustained
        daily return rate — the number that says how much capital a $/day goal needs."""
        if PNL_SNAPSHOT_INTERVAL_S <= 0:
            return
        headers = {"Authorization": f"Bearer {BOT_API_TOKEN}"}
        # Anchor the daily-report clock now so the first report fires ~24h in, not
        # immediately, even across restarts (the marker persists the last send).
        if not os.path.exists(PNL_MARKER):
            try:
                with open(PNL_MARKER, "w") as f:
                    f.write(str(time.time()))
            except OSError:
                pass
        while True:
            try:
                pf = (await client.get(f"{BOT_API_URL}/portfolio", headers=headers, timeout=15)).json()
                eq = float(pf.get("equity", 0) or 0)
                cash = float(pf.get("cash", 0) or 0)
                pnl = float(pf.get("session_pnl", 0) or 0)
            except Exception as exc:  # noqa: BLE001
                print("pnl-tracker fetch error:", exc)
                await asyncio.sleep(PNL_SNAPSHOT_INTERVAL_S)
                continue
            ts = time.time()
            try:
                new = not os.path.exists(PNL_CSV)
                with open(PNL_CSV, "a", newline="") as f:
                    w = csv.writer(f)
                    if new:
                        w.writerow(["epoch", "equity", "cash", "session_pnl"])
                    w.writerow([int(ts), f"{eq:.2f}", f"{cash:.2f}", f"{pnl:.2f}"])
            except OSError as exc:
                print("pnl-tracker csv error:", exc)
            # Daily report when due (marker-based so restarts don't double-send).
            # Superseded by the scheduled night report when EVENING_REPORT_TIME is
            # set — the hourly CSV snapshots above always continue regardless.
            try:
                last = float(open(PNL_MARKER).read().strip())
            except (OSError, ValueError):
                last = 0.0
            if not EVENING_REPORT_TIME and ts - last >= PNL_REPORT_INTERVAL_S:
                msg = self._daily_report(eq)
                for chat_id in self.allowed:
                    await self._send(client, chat_id, msg)
                try:
                    with open(PNL_MARKER, "w") as f:
                        f.write(str(ts))
                except OSError:
                    pass
            await asyncio.sleep(PNL_SNAPSHOT_INTERVAL_S)

    def _daily_report(self, eq_now: float) -> str:
        """Build the daily P&L summary from the equity-curve CSV."""
        rows = []
        try:
            with open(PNL_CSV) as f:
                rows = [r for r in csv.DictReader(f)]
        except OSError:
            pass
        if not rows:
            return f"📅 Daily P&L\nEquity ${eq_now:.2f} (no history yet)"
        start_eq = float(rows[0]["equity"])
        start_ts = float(rows[0]["epoch"])
        now = time.time()
        day_ago = now - 86400
        prior = min(rows, key=lambda r: abs(float(r["epoch"]) - day_ago))
        day_eq = float(prior["equity"])
        d_chg, d_pct = eq_now - day_eq, (eq_now - day_eq) / day_eq * 100 if day_eq else 0
        t_chg = eq_now - start_eq
        t_pct = (t_chg / start_eq * 100) if start_eq else 0
        days = max((now - start_ts) / 86400, 1e-9)
        return (f"📅 Daily P&L\n"
                f"Equity ${eq_now:.2f}\n"
                f"24h: {d_chg:+.2f} ({d_pct:+.2f}%)\n"
                f"Since start ({days:.1f}d): {t_chg:+.2f} ({t_pct:+.2f}%)\n"
                f"Avg/day: {t_pct / days:+.2f}%  ← the rate that matters")

    async def _get_updates(self, client: httpx.AsyncClient) -> list[dict]:
        resp = await client.get(
            API.format(token=BOT_TOKEN, method="getUpdates"),
            params={"offset": self._offset, "timeout": 30},
        )
        resp.raise_for_status()
        return resp.json().get("result", [])

    async def _handle(self, client: httpx.AsyncClient, update: dict) -> None:
        self._offset = max(self._offset, update["update_id"] + 1)
        msg = update.get("message") or update.get("edited_message")
        if not msg or "text" not in msg:
            return
        chat_id = msg["chat"]["id"]
        text = msg["text"].strip()
        if chat_id not in self.allowed:
            print(f"unauthorized chat {chat_id}: {text[:40]!r}")
            return
        if text in ("/start", "/help"):
            await self._send(client, chat_id,
                             "Ask me: 'what's my PnL?', 'is my book too concentrated?', "
                             "'run regime detection', 'deploy $200', 'pause'. I route to the "
                             "right specialist and confirm sensitive actions with you first.")
            return

        await self._send_action(client, chat_id, "typing")
        # The graph invoke is synchronous and can be slow (model + tool calls);
        # run it off the event loop so polling/typing stays responsive.
        config = {"configurable": {"thread_id": str(chat_id)}}
        payload = {"messages": [("user", text)]}
        try:
            result = await asyncio.to_thread(self.app.invoke, payload, config)
            reply = _reply_text(result)
        except Exception as exc:  # noqa: BLE001
            # A crash mid-tool-call can leave a dangling tool request in this
            # thread's saved history (INVALID_CHAT_HISTORY) and wedge the chat.
            # Self-heal: wipe just this chat's thread and retry once on a clean
            # slate so one bad turn can't break the conversation permanently.
            print(f"agent error on chat {chat_id}: {exc} — resetting thread and retrying")
            try:
                await asyncio.to_thread(self.checkpointer.delete_thread, str(chat_id))
                result = await asyncio.to_thread(self.app.invoke, payload, config)
                reply = _reply_text(result)
            except Exception as exc2:  # noqa: BLE001
                print(f"retry failed on chat {chat_id}: {exc2}")
                reply = ("⚠️ Hit an error and reset our conversation history to recover. "
                         "Please send that again.")
        await self._send(client, chat_id, reply)

    async def _send(self, client: httpx.AsyncClient, chat_id: int, text: str) -> bool:
        """Returns True only if every chunk was accepted by Telegram — callers
        that must not lose a message (the daily reports) key their sent-marker
        off this instead of assuming delivery."""
        ok = True
        for chunk in (text[i:i + 3800] for i in range(0, len(text), 3800)) or [text]:
            try:
                resp = await client.post(
                    API.format(token=BOT_TOKEN, method="sendMessage"),
                    json={"chat_id": chat_id, "text": chunk},
                )
                resp.raise_for_status()
            except Exception as exc:  # noqa: BLE001
                print("send error:", exc)
                ok = False
        return ok

    async def _send_action(self, client: httpx.AsyncClient, chat_id: int, action: str) -> None:
        try:
            await client.post(
                API.format(token=BOT_TOKEN, method="sendChatAction"),
                json={"chat_id": chat_id, "action": action},
            )
        except Exception:  # noqa: BLE001
            pass


def main() -> None:
    with SqliteSaver.from_conn_string(CHECKPOINT_DB) as checkpointer:
        app = build_supervisor(checkpointer=checkpointer)
        bridge = SupervisorTelegramBridge(app, checkpointer, ALLOWED)
        try:
            asyncio.run(bridge.run())
        except KeyboardInterrupt:
            print("\nbridge stopped.")


if __name__ == "__main__":
    main()
