"""Quant skills — the playbooks from Quant.md, as structured Claude prompts.

Each skill is a reusable analysis the agent can invoke by name (the "Claude
Skills" idea from TradingBot.md). The agent calls `run_quant_skill(name, ...)`,
which renders the skill's prompt with live context and asks Claude for the
analysis. Keeping them here (vs inline) means they're versioned, testable, and
reusable across the autonomous loop and the Telegram chat.

A skill is just: a name, a one-line description (shown to the agent so it knows
when to reach for it), and a `render(context)` that returns the prompt text.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

# Shared framing so every skill answers in the prediction-markets context, where
# "price" is a probability in [0, 1] and edges are small.
PREDICTION_MARKET_CONTEXT = """\
You are a quant analyst for an autonomous prediction-markets trading bot that
trades Kalshi and Polymarket. Prices are probabilities in [0, 1]. Edges are thin
and fees/slippage matter. Markets resolve to 0 or 1 at a known event time. Be
concrete and quantitative; prefer rules a program can execute over prose."""


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    render: Callable[[dict], str]


def _strategy_generation(ctx: dict) -> str:
    market = ctx.get("market", "prediction markets (Kalshi + Polymarket)")
    capital = ctx.get("capital", "$1,000")
    risk = ctx.get("risk_per_trade", "1-2%")
    return f"""{PREDICTION_MARKET_CONTEXT}

Act as a hedge-fund quant. Propose 3 strategies suited to {market}.
Constraints: capital {capital}; risk per trade {risk}; positions must respect a
hard per-market notional cap and a daily-loss kill-switch.

For each strategy give:
1. Signal/edge (what inefficiency it captures) and why most traders miss it
2. Entry & exit rules, step by step, in probability terms
3. Position sizing + stop/again-resolution handling
4. Which market regimes it works/fails in
5. Concrete params a program could plug in"""


def _regime_detection(ctx: dict) -> str:
    snapshot = ctx.get("market_snapshot", "(no live snapshot provided)")
    return f"""{PREDICTION_MARKET_CONTEXT}

Assess the current trading environment from this live snapshot of tracked
markets (spreads, mids, sizes):

{snapshot}

Identify: overall liquidity/spread regime, how much cross-venue dislocation is
present, and event-time clustering risk. Then recommend which of the bot's
strategies (arbitrage, market-making, signal) to favor right now and which to
throttle, with a one-line justification each."""


def _risk_analysis(ctx: dict) -> str:
    portfolio = ctx.get("portfolio", "(no portfolio provided)")
    limits = ctx.get("risk_limits", "(no limits provided)")
    return f"""{PREDICTION_MARKET_CONTEXT}

Analyze the risk profile of the current book.

Portfolio: {portfolio}
Risk limits: {limits}

Break down: largest single-market exposure, concentration, distance to the
daily-loss kill-switch, and any leg/settlement risk (e.g. one arb leg filled,
the other not). Then give: 3 concrete ways to reduce risk now, and 2 ways to
raise expected return WITHOUT raising risk. Be specific about which limit to
change and to what value."""


def _alpha_detection(ctx: dict) -> str:
    snapshot = ctx.get("market_snapshot", "(no live snapshot provided)")
    return f"""{PREDICTION_MARKET_CONTEXT}

Find underexploited opportunities given this snapshot:

{snapshot}

Focus on behavioral inefficiencies (favorite-longshot bias, round-number
anchoring, slow reaction to news) and market-structure gaps (cross-venue price
divergence on the same real event, complete-set mispricing). Propose 2 concrete,
executable plays with entry/exit and the specific markets to watch."""


def _drawdown_analysis(ctx: dict) -> str:
    portfolio = ctx.get("portfolio", "(no portfolio provided)")
    return f"""{PREDICTION_MARKET_CONTEXT}

Given the current book and session PnL:

{portfolio}

Estimate drawdown exposure and recovery characteristics, then suggest 3 ways to
reduce drawdowns and a position-sizing rule (e.g. fractional-Kelly on edge) the
bot could adopt. Keep it implementable."""


def _trade_review(ctx: dict) -> str:
    positions = ctx.get("positions", "(no open positions)")
    return f"""{PREDICTION_MARKET_CONTEXT}

Review the bot's open positions for soundness:

{positions}

For each: is the thesis still valid, is it near resolution, and should it be
held, trimmed, or closed? Flag any unhedged arbitrage legs explicitly. End with
a prioritized action list."""


_SKILLS: dict[str, Skill] = {
    s.name: s
    for s in [
        Skill("strategy_generation",
              "Propose new strategies for given capital/risk constraints.",
              _strategy_generation),
        Skill("regime_detection",
              "Read current liquidity/dislocation regime and recommend which strategies to favor.",
              _regime_detection),
        Skill("risk_analysis",
              "Analyze book risk and recommend specific limit changes.",
              _risk_analysis),
        Skill("alpha_detection",
              "Surface underexploited edges from the live market snapshot.",
              _alpha_detection),
        Skill("drawdown_analysis",
              "Estimate drawdown exposure and propose position-sizing rules.",
              _drawdown_analysis),
        Skill("trade_review",
              "Review open positions and produce a hold/trim/close action list.",
              _trade_review),
    ]
}


def get_skill(name: str) -> Skill | None:
    return _SKILLS.get(name)


def available_skills() -> list[Skill]:
    return list(_SKILLS.values())
