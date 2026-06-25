"""The permanent TradingBot agent, as a LangGraph graph.

A ReAct agent (Claude + the HTTP tools in tools.py) that supervises the bot:
answers questions about PnL/risk/goals, runs Quant analyses, and takes gated
actions. Deploy it on LangGraph Platform (or run `langgraph dev` locally) so it
persists and is invoked on demand — the bot itself never calls a model, so you
only pay for tokens when you actually talk to this agent.

The exported `graph` object is what langgraph.json points at.
"""

from __future__ import annotations

import os

from langchain_anthropic import ChatAnthropic
from langgraph.prebuilt import create_react_agent

try:  # works both as a package and as a flat LangGraph project dir
    from .tools import TOOLS
except ImportError:  # pragma: no cover
    from tools import TOOLS

SYSTEM_PROMPT = """\
You are the permanent supervising agent for an autonomous prediction-markets
trading bot (Kalshi + Polymarket). You talk to the operator and act through
tools that call the bot's control API. Be concise and direct.

How you work:
- Use the read tools (get_portfolio, get_positions, get_risk, get_goals,
  get_markets) for any question about money, risk, or goals. Never guess numbers.
- For analysis, call run_quant_analysis(skill_name) — it returns a prompt
  pre-filled with live state; reason over it and give the answer in your reply.
- The bot defaults to PAPER trading. set_risk_limit, deploy_capital, go_live, and
  trip_kill_switch are SENSITIVE: they return a confirmation token + summary.
  Relay the summary, ask the operator to confirm, and only then call
  confirm_action(token). Never confirm on your own.
- Factor goal pace into advice: behind pace -> what would help; daily target met
  -> note that lock-gains may have paused trading.
Prices are probabilities in [0,1]; quote amounts in dollars."""

# Default to the most capable model. For cheap, frequent check-ins set
# LANGGRAPH_MODEL=claude-haiku-4-5 to cut cost; raise to claude-opus-4-8 for
# deep analysis. (Anthropic SDK key comes from ANTHROPIC_API_KEY.)
MODEL = os.getenv("LANGGRAPH_MODEL", "claude-opus-4-8")

model = ChatAnthropic(model=MODEL, max_tokens=4000)

graph = create_react_agent(model, TOOLS, prompt=SYSTEM_PROMPT)
