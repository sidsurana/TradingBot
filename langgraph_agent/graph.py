"""The permanent TradingBot agent — a multi-agent supervisor.

A supervisor ReAct agent delegates to four specialist agents (research/quant,
risk, execution, portfolio reporter), each wrapped as a tool. The supervisor
decides who handles a request, can chain them (e.g. research -> risk -> execute),
and synthesizes the final reply. Specialists are defined in specialists.py.

Deploy on LangGraph Platform (which provides persistence) by pointing
langgraph.json at the exported `graph`. To run it yourself with persistent
threads, use run_local.py, which compiles the same graph with a SqliteSaver.
"""

from __future__ import annotations

from langchain_anthropic import ChatAnthropic
from langgraph.prebuilt import create_react_agent

try:  # works as a package and as a flat LangGraph project dir
    from .specialists import MODEL, SPECIALISTS
except ImportError:  # pragma: no cover
    from specialists import MODEL, SPECIALISTS

SUPERVISOR_PROMPT = """\
You are the supervising agent for an autonomous prediction-markets trading bot
(Kalshi + Polymarket). You coordinate a desk of specialists by delegating to
them as tools; you do not call the bot's API directly.

Delegate by intent:
- research_agent — analysis: regime, alpha/edges, strategy ideas, drawdown,
  positions review.
- risk_agent — exposure, distance to the kill-switch, risk-limit changes.
- execution_agent — pause/resume, placing a specific order, deploying capital,
  going live.
- portfolio_agent — read-only PnL / positions / goal-pace check-ins.

You may chain them (e.g. ask research for a read, then risk to size it, then
execution to act). Sensitive actions (limit changes, capital, go-live,
kill-switch) come back from a specialist with a confirmation summary — relay it
to the operator, get an explicit "yes", then tell the specialist to confirm.
Never confirm on the operator's behalf. Be concise; these are phone messages —
lead with the answer. Write plain text only: no Markdown, asterisks, bold,
headings, or backticks (they render as literal characters in the chat)."""


def build_supervisor(checkpointer=None):
    """Build the supervisor graph. Pass a checkpointer (e.g. SqliteSaver) for
    persistent threads when self-hosting; leave None for LangGraph Platform,
    which injects its own persistence."""
    return create_react_agent(
        ChatAnthropic(model=MODEL, max_tokens=4000),
        SPECIALISTS,
        prompt=SUPERVISOR_PROMPT,
        checkpointer=checkpointer,
    )


# Exported for langgraph.json / LangGraph Platform (no explicit checkpointer —
# the platform provides persistence).
graph = build_supervisor()
