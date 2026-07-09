"""Specialist agents — the trading-desk roles the supervisor delegates to.

Each specialist is a ReAct agent with a scoped slice of the bot's control-API
tools and its own system prompt, then wrapped as a delegation tool the
supervisor can call (the "agents-as-tools" pattern). This keeps each agent
focused (a risk agent can't place orders; a reporter is read-only) and lets the
supervisor compose them.

All tools hit the bot's HTTP control API (see tools.py), so the bot itself makes
no LLM calls — only this agent system does, on demand.
"""

from __future__ import annotations

import os

from langchain_anthropic import ChatAnthropic
from langchain_core.tools import tool
from langgraph.prebuilt import create_react_agent

try:  # works as a package and as a flat LangGraph project dir
    from .tools import (
        cancel_action, confirm_action, deploy_capital, get_goals, get_markets,
        get_portfolio, get_positions, get_risk, go_live, list_signals, pause_trading,
        place_order, resume_trading, run_quant_analysis, set_market_signal,
        set_risk_limit, trip_kill_switch,
    )
except ImportError:  # pragma: no cover
    from tools import (
        cancel_action, confirm_action, deploy_capital, get_goals, get_markets,
        get_portfolio, get_positions, get_risk, go_live, list_signals, pause_trading,
        place_order, resume_trading, run_quant_analysis, set_market_signal,
        set_risk_limit, trip_kill_switch,
    )

# One model, shared by all agents. claude-opus-4-8 by default; set
# LANGGRAPH_MODEL=claude-haiku-4-5 for cheap, frequent runs.
MODEL = os.getenv("LANGGRAPH_MODEL", "claude-haiku-4-5")


def _model() -> ChatAnthropic:
    return ChatAnthropic(model=MODEL, max_tokens=4000)


READ_TOOLS = [get_portfolio, get_positions, get_risk, get_goals, get_markets]

# --- the four specialists -------------------------------------------------

_research = create_react_agent(
    _model(),
    READ_TOOLS + [run_quant_analysis, set_market_signal, list_signals],
    prompt=(
        "You are the Research/Quant analyst for a prediction-markets trading bot. "
        "Use the read tools for live state and run_quant_analysis(skill_name) for "
        "regime_detection, alpha_detection, strategy_generation, drawdown_analysis, "
        "or trade_review. Reason over what it returns and give a concrete, "
        "quantitative answer. You do NOT place orders. When you reach a genuine "
        "fair-value view on a market, you MAY push it with set_market_signal "
        "(fair_value + confidence, 0-1) — the signal strategy sizes it by Kelly "
        "within the risk caps. Only signal when you have a real edge view."
    ),
)

_risk = create_react_agent(
    _model(),
    READ_TOOLS + [set_risk_limit, trip_kill_switch, confirm_action, cancel_action],
    prompt=(
        "You are the Risk manager for a prediction-markets trading bot. Read the "
        "portfolio, risk status, and goals; flag concentration, distance to the "
        "daily-loss kill-switch, and unhedged legs. set_risk_limit and "
        "trip_kill_switch are SENSITIVE: they return a confirmation token + summary "
        "— relay the summary, ask the operator to confirm, and only then call "
        "confirm_action(token). Never confirm on your own."
    ),
)

_execution = create_react_agent(
    _model(),
    [get_portfolio, get_positions, get_markets, pause_trading, resume_trading,
     place_order, deploy_capital, go_live, confirm_action, cancel_action],
    prompt=(
        "You are the Execution trader for a prediction-markets trading bot. You can "
        "pause/resume trading and, for explicit operator requests, place specific "
        "orders, deploy capital, or go live. place_order, deploy_capital, and "
        "go_live are SENSITIVE: they return a confirmation token + summary — relay "
        "it, ask the operator to confirm, and only then call confirm_action(token). "
        "Never confirm on your own. Prices are probabilities in (0,1)."
    ),
)

_portfolio = create_react_agent(
    _model(),
    READ_TOOLS,
    prompt=(
        "You are the Portfolio/PnL reporter for a prediction-markets trading bot. "
        "Read-only: summarize cash, equity, session PnL, open positions, and "
        "daily/weekly goal pace clearly and concisely. You do not trade or change "
        "anything."
    ),
)


def _delegate(agent, request: str) -> str:
    result = agent.invoke({"messages": [("user", request)]})
    return result["messages"][-1].content


@tool
def research_agent(request: str) -> str:
    """Delegate market analysis to the Research/Quant specialist: regime reads,
    alpha/edge ideas, strategy generation, drawdown analysis, or a positions
    review. Pass the full question."""
    return _delegate(_research, request)


@tool
def risk_agent(request: str) -> str:
    """Delegate risk questions to the Risk specialist: exposure/concentration,
    distance to the kill-switch, and risk-limit changes (which it will stage for
    your confirmation). Pass the full question."""
    return _delegate(_risk, request)


@tool
def execution_agent(request: str) -> str:
    """Delegate trading actions to the Execution specialist: pause/resume, place a
    specific order, deploy capital, or go live (sensitive actions are staged for
    confirmation). Pass the full instruction."""
    return _delegate(_execution, request)


@tool
def portfolio_agent(request: str) -> str:
    """Delegate read-only PnL/positions/goal-pace check-ins to the Portfolio
    reporter. Pass the question."""
    return _delegate(_portfolio, request)


SPECIALISTS = [research_agent, risk_agent, execution_agent, portfolio_agent]
