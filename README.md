# TradingBot

Autonomous **dutch-book arbitrage** bot for prediction markets, running live on
**Kalshi** and **Polymarket US** behind one venue-agnostic core. Two independent
single-venue bots trade in parallel — no cross-venue coupling — each with its own
capital, and each texting a dedicated Telegram bot on every fill.

## Status

**Live.** The engine runs an `arbitrage` strategy on both venues with real
capital, driven by streaming order books, with per-venue risk caps and a
kill-switch. Data feeds, order placement, cancellation, and partial-fill unwind
are all validated live on each venue. It can also run fully in **paper mode**
(public/market data + simulated fills) with no credentials.

> Trades real money when `TB_LIVE=true` and a strategy is enabled. Prediction
> markets are risky and can go to zero; run paper mode first.

## How it works

```
   WebSocket books ──►  LocalBook cache  ──►  Engine event loop (async, ~250ms)
   (Kalshi + PM US)                                │
                                                   ▼
                             Arbitrage strategy  ── emit ──►  atomic order sets
                                                   │              (fill-or-kill)
                                                   ▼                   │
   Risk manager (caps + kill-switch) ──►  Router ──►  Venue adapters ──┘
                                                   │        (Kalshi V2, PM US)
                                                   ▼
                       Portfolio  ──►  per-venue Telegram bots (alerts + P&L + chat)
```

- **Unified models** (`models.py`): every market is a binary outcome priced as a
  probability in `[0, 1]`; adapters convert native venue units to this.
- **Dutch-book arbitrage** (`strategies/arbitrage.py`): within one event, buy the
  cheapest ask of *every* mutually-exclusive outcome when the complete set costs
  **< $1 after fees** — one outcome always redeems to $1, so the set is a locked
  profit. It only fires on **collectively-exhaustive** events (so an unlisted
  outcome can't win and leave you short): on Kalshi, numeric range/bucket events;
  on Polymarket US, complete fields whose fair prices sum to ~1. Named-candidate
  fields are excluded.
- **Atomic sets + unwind** (`engine/engine.py`): the N legs of a set are placed
  fill-or-kill; if any leg fails to fully fill, the legs that did fill are
  immediately flattened, so a broken set costs spread+fees rather than leaving
  naked directional risk.
- **Risk manager** (`engine/risk.py`): mandatory pre-trade gate — per-market /
  gross-notional / order-rate caps and a daily-loss kill-switch.

## Venue adapters

- **Kalshi** (`exchanges/kalshi.py`, `kalshi_auth.py`): RSA-signed requests;
  2026 dollar/fixed-point wire format; discovery via the events endpoint (with an
  exhaustiveness gate); V2 single-book orders (`bid`/`ask`, fixed-point dollar
  prices) with exchange-shard auto-routing.
- **Polymarket US** (`exchanges/polymarket_us.py`, `polymarket_us_auth.py`): the
  CFTC-regulated `api.polymarket.us` platform (distinct from `.com`) — Ed25519
  API-key auth, REST market data + orders, one binary Yes/No market per outcome
  grouped into events by question.
- **Streaming** (`exchanges/streaming.py`): a signed WebSocket per venue keeps a
  live local book so the engine reads top-of-book instantly; REST fallback fills
  any gaps.

## Telegram trade bots

Each venue texts its **own** bot (`interface/notifier.py`):

- **On every trade** — a labeled BUY (a locked set) or SELL (an unwind), with
  leg count, cost, edge, and that venue's live equity + P&L.
- **Daily report** — equity/cash/P&L and each open position with mark + uPnL.
- **Natural-language replies** (`interface/llm.py`) — ask "how am I doing?" or
  "what did you buy today?" and the bot answers, grounded strictly in that
  venue's live data (OpenAI; falls back to a fixed report if unavailable).

## Quickstart

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest                       # run the suite
tradingbot                   # paper mode (public data + simulated fills)
```

Going live — copy `.env.example` → `.env` and set, at minimum:

```bash
TB_LIVE=true
TB_ENABLED_STRATEGIES=["arbitrage"]

# Kalshi (RSA key file)
TB_KALSHI_API_KEY_ID=...
TB_KALSHI_PRIVATE_KEY_PATH=/path/to/kalshi_key.pem

# Polymarket US (dev-portal Ed25519 key)
TB_POLYUS_KEY_ID=...
TB_POLYUS_SECRET_KEY=...

# Per-venue Telegram bots (one per agent) + P&L baselines
TB_NOTIFY_KALSHI_TOKEN=...   ;  TB_NOTIFY_KALSHI_CHAT_ID=...
TB_NOTIFY_PM_TOKEN=...       ;  TB_NOTIFY_PM_CHAT_ID=...
TB_NOTIFY_OPENAI_KEY=...     # optional: natural-language replies
```

Run `tradingbot`; each bot pings "online" with its balance, then trades and
reports autonomously. Risk caps live under `TB_RISK_*`; kill via
`TB_ENABLED_STRATEGIES=[]` (restart) or the control API's kill-switch.

## Docs

**[SETUP.md](SETUP.md)** (credentials + run walkthrough) ·
**[Logic.md](Logic.md)** (strategy / risk / latency detail) ·
**[Roadmap.md](Roadmap.md)** (what's next).
