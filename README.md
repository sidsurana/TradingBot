# TradingBot

Autonomous, multi-strategy trading bot for prediction markets, spanning
**Kalshi** and **Polymarket** behind one venue-agnostic core.

## Status

Foundation / paper-trading skeleton. The full engine loop, risk layer, portfolio
accounting, router, and a working **arbitrage** strategy run end-to-end in paper
mode. Live order placement on each venue is stubbed and clearly marked — the bot
is safe to run unconfigured (public market data + simulated fills only).

## Design

```
  Telegram (your phone)  ⇄  Claude Agent (Opus 4.8 + tools)  ⇄  BotController
   "what's my PnL?"           reasons, runs Quant skills,         (safe facade,
   "deploy $200"              gates sensitive actions             confirms actions)
                                       │                                 │
Strategies (arb / MM / signal)  ──emit──>  Orders                       │
        │  read                                │                         │
        ▼                                       ▼                         ▼
   Market data  <──  Exchange adapters  ──>  Risk Manager ──> Portfolio ─┘
   (unified models)  (Kalshi, Polymarket,        │  (limits, kill-switch)
                      Paper, Router)               └─ Engine event loop (async)
```

**The brain.** A Claude agent (`ai/agent.py`) talks to you over Telegram
(`interface/telegram.py`) and acts through a single `BotController` facade
(`ai/controller.py`). It reads live PnL/positions/risk via tools, runs **Quant
skills** (`ai/skills.py` — the playbooks from `Quant.md`: strategy generation,
regime detection, risk/alpha/drawdown analysis, trade review), and can
pause/resume or adjust limits. Capital, limit, kill-switch, and go-live actions
are **sensitive**: the tool stages them and returns a confirmation token, so a
chat message can't move real money without an explicit "yes".

- **Unified models** (`models.py`): everything is a binary outcome priced as a
  probability in `[0, 1]`. Adapters convert native units (Kalshi cents,
  Polymarket decimals) to this convention.
- **Exchange interface** (`exchanges/base.py`): strategies/engine depend only on
  this ABC. `Router` dispatches per-market; `PaperExchange` simulates fills over
  live data.
- **Risk manager** (`engine/risk.py`): mandatory pre-trade gate — position /
  notional / gross / rate limits + daily-loss kill-switch.
- **Strategies** (`strategies/`): pure `state -> orders`. `arbitrage` ships
  (dutch-book + cross-venue). Market-making and signal are the next two.

## Quickstart

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest                       # run the suite
tradingbot                   # paper mode (public data + simulated fills)
```

Enable the chat brain:

```bash
pip install -e ".[agent]"    # adds the Claude SDK
# in .env: set TB_ANTHROPIC_API_KEY (or ANTHROPIC_API_KEY),
#          TB_TELEGRAM_BOT_TOKEN, TB_TELEGRAM_ALLOWED_CHAT_IDS=[<your chat id>]
tradingbot                   # engine + Telegram run together
```

Then message your bot: *"what's my PnL?"*, *"show risk"*, *"run regime
detection"*, *"deploy $200"* (it'll ask you to confirm), *"pause"*.

Live trading: copy `.env.example` -> `.env`, fill venue credentials, set
`TB_LIVE=true`. Do **not** do this until you've run paper mode and wired the
adapters' `place_order` (RSA signing for Kalshi, EIP-712 for Polymarket).

## Roadmap

1. ✅ Core: models, config, exchange interface, paper sim
2. ✅ Kalshi + Polymarket read-only adapters
3. ✅ Risk manager + portfolio
4. ✅ Strategy framework + arbitrage
5. ✅ Engine loop + entrypoint + tests
6. ✅ Claude agent + BotController + Quant skills + Telegram chat
7. ✅ Daily/weekly profit goals + lock-gains
8. ✅ Autopilot (autonomous reassess + Telegram briefings)
9. ✅ Live order placement — Kalshi RSA signing (tested), Polymarket EIP-712 via
   py-clob-client (needs live validation with a funded wallet)
10. ✅ Stop-loss / take-profit exit manager
11. ✅ HTTP control API + deployable LangGraph agent (`langgraph_agent/`)
12. ✅ Agent discretionary trading (`place_order`, confirmation-gated)
13. ✅ WebSocket streaming books — Polymarket **live-validated (~40ms push latency)**;
    Kalshi wired (signed handshake needs creds to validate); fail-safe REST fallback;
    engine ticks at ~250ms when streaming
14. ✅ Market-making strategy (passive two-sided, inventory-bounded, quote lifecycle)
15. ✅ Event-driven acting (react ~11ms on book update; edge→fill ~100–200ms)
```

Full forward-looking plan with effort estimates is in **[Roadmap.md](Roadmap.md)**.

**Docs:** **[SETUP.md](SETUP.md)** (credentials + run walkthrough) ·
**[Logic.md](Logic.md)** (strategy/risk/stop-loss/goal/latency logic in detail) ·
**[Roadmap.md](Roadmap.md)** (what's next) ·
**[langgraph_agent/](langgraph_agent/)** (deployable permanent agent).
