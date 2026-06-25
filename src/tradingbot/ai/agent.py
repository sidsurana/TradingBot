"""TradingAgent — the Claude brain.

A Claude (Opus 4.8) agent with tools to read the bot's state, run the Quant.md
skills, and take gated actions. Drives a manual async tool-use loop so we keep
control of execution (sensitive actions are surfaced for confirmation rather
than auto-run). Conversation state is kept per chat so Telegram threads are
coherent across turns.

Uses the official `anthropic` SDK with adaptive thinking + the effort parameter,
per the project's Claude API guidance. Imported lazily so the rest of the bot
runs without the agent dependency installed.
"""

from __future__ import annotations

import json

import structlog

from tradingbot.ai import skills
from tradingbot.ai.controller import BotController
from tradingbot.config import AnthropicCreds

log = structlog.get_logger(__name__)

MAX_TOOL_ITERATIONS = 8

SYSTEM_PROMPT = """\
You are the operator-facing brain of an autonomous prediction-markets trading
bot (Kalshi + Polymarket). The user talks to you from Telegram about their PnL,
risk, open positions, and deploying capital. Be concise and direct — these are
phone messages. Lead with the answer, then a short supporting line.

How you work:
- Use tools to read LIVE state (portfolio, positions, risk, market snapshot)
  before answering questions about money or risk. Never guess numbers.
- Run a quant skill (run_quant_skill) when the user wants analysis: strategy
  ideas, regime read, risk review, alpha, drawdown, or a positions review.
- The user sets daily/weekly profit targets. Use get_goal_progress when they ask
  how they're doing, and factor pace into advice (behind pace -> what would help;
  target met -> note that lock-gains may have paused trading).
- You CAN place specific discretionary trades with place_order (sensitive ->
  confirm first). Use it when the user explicitly asks to buy/sell a market. The
  rule-based strategies handle routine edges automatically; place_order is for the
  user's own calls.
- The bot defaults to PAPER trading. Capital, risk-limit, kill-switch, and
  go-live actions are SENSITIVE: the tool returns a confirmation token and a
  summary. Relay the summary to the user and ask them to confirm; only call
  confirm_action with the token after they say yes. Never confirm on your own.
- Prices are probabilities in [0,1]. Mention amounts in dollars.

If something is paused or the kill-switch is tripped, say so plainly."""


def _tool_defs() -> list[dict]:
    skill_names = [s.name for s in skills.available_skills()]
    skill_help = "; ".join(f"{s.name}: {s.description}" for s in skills.available_skills())
    return [
        {"name": "get_portfolio", "description": "Cash, equity, session PnL, mode, paused state.",
         "input_schema": {"type": "object", "properties": {}}},
        {"name": "list_positions", "description": "Open positions with marks and unrealized PnL.",
         "input_schema": {"type": "object", "properties": {}}},
        {"name": "get_risk_status", "description": "Risk limits, kill-switch, gross notional.",
         "input_schema": {"type": "object", "properties": {}}},
        {"name": "get_goal_progress",
         "description": "Daily/weekly profit target, current PnL vs target, pace, and whether on track.",
         "input_schema": {"type": "object", "properties": {}}},
        {"name": "set_market_signal",
         "description": "Push a directional fair-value view for the signal strategy to act on "
                        "(when 'signal' is enabled). fair_value/confidence are 0-1. Use after "
                        "analysis when you have a genuine edge view; the strategy sizes it by Kelly.",
         "input_schema": {"type": "object", "properties": {
             "venue": {"type": "string", "enum": ["kalshi", "polymarket"]},
             "market_id": {"type": "string"},
             "fair_value": {"type": "number"},
             "confidence": {"type": "number"}},
             "required": ["venue", "market_id", "fair_value", "confidence"]}},
        {"name": "list_signals", "description": "Current directional fair-value signals.",
         "input_schema": {"type": "object", "properties": {}}},
        {"name": "get_market_snapshot",
         "description": "Most liquid tracked markets (mid, spread, sizes).",
         "input_schema": {"type": "object", "properties": {
             "limit": {"type": "integer", "description": "max rows (default 25)"}}}},
        {"name": "pause_trading", "description": "Halt order placement (data keeps flowing).",
         "input_schema": {"type": "object", "properties": {}}},
        {"name": "resume_trading", "description": "Resume order placement.",
         "input_schema": {"type": "object", "properties": {}}},
        {"name": "set_risk_limit",
         "description": "SENSITIVE. Stage a risk-limit change; returns a confirm token.",
         "input_schema": {"type": "object", "properties": {
             "name": {"type": "string"}, "value": {"type": "number"}},
             "required": ["name", "value"]}},
        {"name": "deploy_capital",
         "description": "SENSITIVE. Stage raising deployable capital; returns a confirm token.",
         "input_schema": {"type": "object", "properties": {
             "amount": {"type": "number"}}, "required": ["amount"]}},
        {"name": "place_order",
         "description": "SENSITIVE. Stage a specific discretionary trade; returns a confirm token. "
                        "Executes on the next engine tick after confirmation (even if paused).",
         "input_schema": {"type": "object", "properties": {
             "venue": {"type": "string", "enum": ["kalshi", "polymarket"]},
             "market_id": {"type": "string"},
             "side": {"type": "string", "enum": ["buy", "sell"]},
             "size": {"type": "number"},
             "price": {"type": "number", "description": "probability in (0,1)"}},
             "required": ["venue", "market_id", "side", "size", "price"]}},
        {"name": "trip_kill_switch",
         "description": "SENSITIVE. Stage tripping the kill-switch; returns a confirm token.",
         "input_schema": {"type": "object", "properties": {}}},
        {"name": "go_live",
         "description": "SENSITIVE. Stage switching from paper to real-money; returns a confirm token.",
         "input_schema": {"type": "object", "properties": {}}},
        {"name": "confirm_action", "description": "Execute a staged sensitive action by token.",
         "input_schema": {"type": "object", "properties": {
             "token": {"type": "string"}}, "required": ["token"]}},
        {"name": "cancel_action", "description": "Discard a staged sensitive action by token.",
         "input_schema": {"type": "object", "properties": {
             "token": {"type": "string"}}, "required": ["token"]}},
        {"name": "run_quant_skill",
         "description": f"Run a quant analysis skill against live state. Skills — {skill_help}",
         "input_schema": {"type": "object", "properties": {
             "skill_name": {"type": "string", "enum": skill_names},
             "capital": {"type": "string", "description": "optional, e.g. '$1,000'"},
             "risk_per_trade": {"type": "string", "description": "optional, e.g. '1-2%'"}},
             "required": ["skill_name"]}},
    ]


class TradingAgent:
    def __init__(self, controller: BotController, creds: AnthropicCreds):
        self.controller = controller
        self.creds = creds
        self._client = None
        self._history: dict[str, list] = {}  # chat_id -> messages
        self._tools = _tool_defs()

    @property
    def configured(self) -> bool:
        return self.creds.configured

    def _get_client(self):
        if self._client is None:
            from anthropic import AsyncAnthropic

            kwargs = {"api_key": self.creds.api_key} if self.creds.api_key else {}
            self._client = AsyncAnthropic(**kwargs)
        return self._client

    async def chat(self, chat_id: str, user_text: str) -> str:
        if not self.configured:
            return ("Agent not configured: set TB_ANTHROPIC_API_KEY (or ANTHROPIC_API_KEY) "
                    "to enable the Claude brain.")
        client = self._get_client()
        history = self._history.setdefault(chat_id, [])
        history.append({"role": "user", "content": user_text})

        try:
            reply = await self._run_loop(client, history)
        except Exception as exc:  # noqa: BLE001
            log.error("agent.error", error=str(exc))
            return f"Agent error: {exc}"
        history.append({"role": "assistant", "content": reply})
        # Bound memory: keep the last ~40 turns.
        if len(history) > 80:
            del history[:-80]
        return self._text_of(reply)

    async def _run_loop(self, client, history: list) -> list:
        messages = list(history)
        for _ in range(MAX_TOOL_ITERATIONS):
            resp = await client.messages.create(
                model=self.creds.model,
                max_tokens=self.creds.max_tokens,
                system=SYSTEM_PROMPT,
                tools=self._tools,
                thinking={"type": "adaptive"},
                output_config={"effort": self.creds.effort},
                messages=messages,
            )
            if resp.stop_reason != "tool_use":
                return resp.content

            messages.append({"role": "assistant", "content": resp.content})
            results = []
            for block in resp.content:
                if block.type != "tool_use":
                    continue
                out = await self._dispatch(client, block.name, block.input)
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(out, default=str),
                })
            messages.append({"role": "user", "content": results})
        return [{"type": "text",
                 "text": "Stopped after several tool calls without finishing. Try rephrasing."}]

    async def _dispatch(self, client, name: str, args: dict):
        c = self.controller
        try:
            if name == "get_portfolio":
                return c.portfolio_summary()
            if name == "list_positions":
                return c.list_positions()
            if name == "get_risk_status":
                return c.risk_status()
            if name == "get_goal_progress":
                return c.goal_progress()
            if name == "set_market_signal":
                return c.set_signal(args["venue"], args["market_id"],
                                    float(args["fair_value"]), float(args["confidence"]))
            if name == "list_signals":
                return c.list_signals()
            if name == "get_market_snapshot":
                return c.market_snapshot(int(args.get("limit", 25)))
            if name == "pause_trading":
                return c.pause()
            if name == "resume_trading":
                return c.resume()
            if name == "set_risk_limit":
                return c.request_set_risk_limit(args["name"], float(args["value"]))
            if name == "deploy_capital":
                return c.request_deploy_capital(float(args["amount"]))
            if name == "place_order":
                return c.request_place_order(args["venue"], args["market_id"], args["side"],
                                             float(args["size"]), float(args["price"]))
            if name == "trip_kill_switch":
                return c.request_trip_kill_switch()
            if name == "go_live":
                return c.request_go_live()
            if name == "confirm_action":
                return c.confirm(args["token"])
            if name == "cancel_action":
                return c.cancel(args["token"])
            if name == "run_quant_skill":
                return await self._run_skill(client, args)
            return {"error": f"unknown tool {name}"}
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc)}

    async def _run_skill(self, client, args: dict) -> dict:
        skill = skills.get_skill(args["skill_name"])
        if skill is None:
            return {"error": f"unknown skill {args['skill_name']}"}
        c = self.controller
        ctx = {
            "portfolio": json.dumps(c.portfolio_summary(), default=str),
            "positions": json.dumps(c.list_positions(), default=str),
            "risk_limits": json.dumps(c.risk_status()["limits"], default=str),
            "market_snapshot": json.dumps(c.market_snapshot(), default=str),
        }
        if args.get("capital"):
            ctx["capital"] = args["capital"]
        if args.get("risk_per_trade"):
            ctx["risk_per_trade"] = args["risk_per_trade"]

        prompt = skill.render(ctx)
        resp = await client.messages.create(
            model=self.creds.model,
            max_tokens=self.creds.max_tokens,
            thinking={"type": "adaptive"},
            output_config={"effort": self.creds.effort},
            messages=[{"role": "user", "content": prompt}],
        )
        return {"skill": skill.name, "analysis": self._text_of(resp.content)}

    @staticmethod
    def _text_of(content) -> str:
        if isinstance(content, str):
            return content
        parts = [b.text for b in content if getattr(b, "type", None) == "text"]
        return "\n".join(parts).strip() or "(no text response)"
