"""LangChain tools that drive the TradingBot over its HTTP control API.

Each tool is a thin call to the bot's FastAPI control API (see
`src/tradingbot/api.py`). The bot makes NO LLM calls itself — this agent is the
only thing that talks to a model, so you only pay for tokens when you invoke it.

Config via env:
  TRADINGBOT_API_URL    default http://127.0.0.1:8787
  TRADINGBOT_API_TOKEN  must match the bot's TB_API_TOKEN
"""

from __future__ import annotations

import os

import httpx
from langchain_core.tools import tool

API_URL = os.getenv("TRADINGBOT_API_URL", "http://127.0.0.1:8787").rstrip("/")
API_TOKEN = os.getenv("TRADINGBOT_API_TOKEN", "")


def _headers() -> dict:
    return {"Authorization": f"Bearer {API_TOKEN}"}


def _get(path: str, params: dict | None = None):
    try:
        r = httpx.get(f"{API_URL}{path}", headers=_headers(), params=params, timeout=20)
        r.raise_for_status()
        return r.json()
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


def _post(path: str, body: dict | None = None):
    try:
        r = httpx.post(f"{API_URL}{path}", headers=_headers(), json=body or {}, timeout=20)
        r.raise_for_status()
        return r.json()
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


@tool
def get_portfolio() -> dict:
    """Cash, equity, session PnL, mode (paper/live), and paused state."""
    return _get("/portfolio")


@tool
def get_positions() -> list:
    """Open positions with marks and unrealized PnL."""
    return _get("/positions")


@tool
def get_risk() -> dict:
    """Risk limits, kill-switch state, gross notional, and stop-loss/take-profit config."""
    return _get("/risk")


@tool
def get_goals() -> dict:
    """Daily/weekly profit target progress, pace, and whether on track."""
    return _get("/goals")


@tool
def get_markets(limit: int = 25) -> list:
    """Snapshot of the most liquid tracked markets (mid, spread, sizes)."""
    return _get("/markets", {"limit": limit})


@tool
def run_quant_analysis(skill_name: str) -> str:
    """Fetch a Quant analysis prompt pre-filled with LIVE bot state, then ANSWER it
    yourself in this turn. skill_name is one of: strategy_generation,
    regime_detection, risk_analysis, alpha_detection, drawdown_analysis,
    trade_review. Returns the prompt to reason over."""
    data = _get(f"/skill_prompt/{skill_name}")
    if "error" in data:
        return f"error: {data['error']}"
    return data["prompt"]


@tool
def set_market_signal(venue: str, market_id: str, fair_value: float, confidence: float) -> dict:
    """Push a directional fair-value view for the signal strategy to act on (when
    'signal' is enabled). venue is 'kalshi'/'polymarket'; fair_value and confidence
    are probabilities in 0-1. Use after analysis when you have a genuine edge view;
    the strategy sizes the position by fractional Kelly within the risk caps."""
    return _post("/signals", {"venue": venue, "market_id": market_id,
                              "fair_value": fair_value, "confidence": confidence})


@tool
def list_signals() -> list:
    """Current directional fair-value signals the signal strategy is acting on."""
    return _get("/signals")


@tool
def pause_trading() -> dict:
    """Halt new order placement (market data and stop-loss exits keep running)."""
    return _post("/pause")


@tool
def resume_trading() -> dict:
    """Resume order placement."""
    return _post("/resume")


@tool
def set_risk_limit(name: str, value: float) -> dict:
    """SENSITIVE. Stage a risk-limit change; returns a confirmation token. Relay the
    summary to the user and only call confirm_action after they say yes. Valid names:
    max_position_per_market, max_notional_per_market, max_gross_notional,
    max_daily_loss, max_orders_per_min."""
    return _post("/actions/set_risk_limit", {"name": name, "value": value})


@tool
def deploy_capital(amount: float) -> dict:
    """SENSITIVE. Stage raising deployable capital; returns a confirmation token.
    Requires user confirmation via confirm_action."""
    return _post("/actions/deploy_capital", {"amount": amount})


@tool
def place_order(venue: str, market_id: str, side: str, size: float, price: float) -> dict:
    """SENSITIVE. Stage a specific discretionary trade; returns a confirmation token.
    venue is 'kalshi' or 'polymarket'; side is 'buy' or 'sell'; price is a
    probability in (0,1). Executes on the next engine tick after confirm_action.
    Use only when the user explicitly asks to buy/sell a market."""
    return _post("/actions/place_order", {"venue": venue, "market_id": market_id,
                                          "side": side, "size": size, "price": price})


@tool
def go_live() -> dict:
    """SENSITIVE. Stage switching from paper to real-money trading; returns a token.
    Requires user confirmation. Refuses if no venue credentials are configured."""
    return _post("/actions/go_live")


@tool
def trip_kill_switch() -> dict:
    """SENSITIVE. Stage the emergency stop (reject all further orders); returns a token.
    Requires user confirmation."""
    return _post("/actions/kill_switch")


@tool
def confirm_action(token: str) -> dict:
    """Execute a previously staged sensitive action, using its confirmation token.
    Only call after the user has explicitly approved."""
    return _post("/confirm", {"token": token})


@tool
def cancel_action(token: str) -> dict:
    """Discard a staged sensitive action by its token."""
    return _post("/cancel", {"token": token})


TOOLS = [
    get_portfolio, get_positions, get_risk, get_goals, get_markets,
    run_quant_analysis, pause_trading, resume_trading,
    set_risk_limit, deploy_capital, place_order, go_live, trip_kill_switch,
    confirm_action, cancel_action,
]
