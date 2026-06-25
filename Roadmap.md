# Roadmap

Where the bot is and where it's going. Effort is rough (S = hours, M = a day or
two, L = several days). "Validate" items need a funded/credentialed account to
finish — they can't be unit-tested.

See `Logic.md` for how each shipped piece works, `SETUP.md` to run it.

---

## ✅ Shipped

| # | Capability | Notes |
|---|---|---|
| 1 | Core engine, paper simulation, fills/PnL | same code path as live |
| 2 | Unified models + Exchange interface + Router | venue-agnostic |
| 3 | Arbitrage (dutch-book + cross-venue) | min-edge gated |
| 4 | Risk manager + daily-loss kill-switch | mandatory pre-trade gate |
| 5 | Stop-loss / take-profit exits | runs even while paused |
| 6 | Daily/weekly profit goals + lock-gains | persisted baselines |
| 7 | Market-making strategy + quote lifecycle | inventory-bounded |
| 8 | Claude agent (Telegram) + Quant skills | supervisor + analysis |
| 9 | Agent discretionary trades (`place_order`) | confirmation-gated |
| 10 | HTTP control API + LangGraph agent | bot is LLM-free; agent on-demand |
| 11 | Autopilot (reassess + briefings) | advisory only |
| 12 | WebSocket streaming — Polymarket | **live-validated, ~40 ms push** |
| 13 | Event-driven acting | **live-validated, ~11 ms react; edge→fill ~100–200 ms** |
| 14 | Kalshi live order signing (RSA) | signing tested |
| 15 | Universe curation (tradeable filter + volume rank + periodic refresh) | **live-validated**; Polymarket via Gamma API, Kalshi by volume |
| 16 | Persistence — event-sourced fills in SQLite | positions/cash/goals survive restart via replay; market registry so exits always resolve |
| 17 | Cross-venue event-mapping (linker) | user-curated link_id makes cross-venue arb actually fire (native ids never match) |

---

## ⏳ Needs live validation (blocked on credentials, not code)

| Item | What's left | Effort |
|---|---|---|
| **Polymarket live order placement** | Place one tiny real order via py-clob-client with a funded wallet; confirm fill/ack parsing | S + funds |
| **Kalshi live order placement** | Same, with Kalshi API creds | S + funds |
| **Kalshi WebSocket streaming** | The signed WS handshake — validate connect/subscribe with creds (parser already matches docs) | S + creds |

These are wired and gated behind `TB_LIVE` + the `go_live` confirmation. The
honest blocker is that order *acceptance/fill* timing and exact response shapes
can't be confirmed without a live account.

---

## 🔜 Next (highest value)

| # | Item | Why | Effort |
|---|---|---|---|
| 18 | **Signal / model strategy** | Turn the agent's `regime`/`alpha` Quant skills into live directional positions (sized fractional-Kelly, protected by stop-loss) — the third strategy | M–L |
| 19 | **Link auto-suggester (advisory)** | Fuzzy-match titles/outcomes across venues to *suggest* `link_id` groups for the user to confirm — speeds up building the cross-venue mapping (never auto-trades) | M |
| 20 | **Per-fill venue reconciliation (live)** | In live mode, reconcile our portfolio against venue-reported positions/fills each tick (paper drains locally today) | M |

---

## 🧠 Strategy & latency improvements

| Item | Why | Effort |
|---|---|---|
| **Micro-price fair value** for MM | Quote around a book-imbalance-weighted mid instead of raw mid — fewer adverse fills | S |
| **Price-skew (Avellaneda-Stoikov)** for MM | Skew quote *prices* by inventory + volatility, not just size — better inventory control + spread capture | M |
| **Queue-position modeling** in paper sim | Current sim fills resting orders optimistically (no queue priority); model it for realistic MM PnL | M |
| **Adaptive requote throttle** | Cap cancel/replace rate per market to avoid churn/rate-limits in fast markets | S |
| **Co-located / lower-RTT order path** | The ~50–150 ms POST is the remaining floor; a closer host or venue-native order WS would cut it | M (infra) |
| **Slippage & fee model per venue** | Replace the flat 0.1% paper fee with real per-venue maker/taker fees + expected slippage | S |

---

## 🤖 Agent / autonomy

| Item | Why | Effort |
|---|---|---|
| **Scheduled autopilot via LangGraph cron** | Run the agent's reassessment on a schedule in the cloud instead of the in-process loop | S |
| **Backtesting harness** | Replay historical book data through the strategies to tune params before risking capital (wire the `backtesting` Quant skill to real data) | L |
| **Strategy auto-tuning** | Let the agent propose param changes (min_edge, spread, sizing) from drawdown/regime analysis, gated by confirmation | M |
| **Alerting** | Push critical events (kill-switch tripped, big drawdown, WS down) to Telegram immediately, not just on autopilot cycles | S |

---

## 🏗️ Platform / hardening

| Item | Why | Effort |
|---|---|---|
| **Metrics + dashboard** (Prometheus/Grafana) | Watch PnL, fills/sec, latency, inventory live | M |
| **Structured trade log** | Every order/fill/cancel to a durable log for audit + post-trade analysis | S |
| **Reconnect/heartbeat hardening** | Detect a silently-dead WS (no updates for N s) and force reconnect; alert on prolonged outage | S |
| **`pmxt` evaluation** | Consider the unified Kalshi+Polymarket lib as one adapter behind the `Exchange` interface (reduce per-venue maintenance) | M |
| **Multi-account / capital allocation** | Split capital across strategies with per-strategy risk budgets | M |
| **Dockerize + deploy** | One container for the bot + API, one for the LangGraph agent; run on a small VPS near the venues | S–M |

---

## Guiding principles (don't regress these)

- **Paper-first, gated live.** Real money needs `TB_LIVE` + venue creds + a
  `go_live` confirmation. Never weaken this.
- **Rules execute, the LLM supervises.** Keep per-trade decisions in fast
  rule-based code; the agent advises, analyzes, and places discretionary orders.
- **Risk is mandatory.** Every order passes the risk manager; three loss controls
  (stop-loss → kill-switch → lock-gains) stay in place.
- **Fail-safe degradation.** Streaming falls back to REST; a dead component
  shouldn't take the bot down silently.

---

## How market selection works (shipped — item 15)

Two layers, so "which markets does the bot trade" has a precise answer:

**Layer 1 — Discovery / curation (`engine/universe.py`).** `discover()` pulls a
wide list from each venue, then `UniverseSelector` curates it:
- **Tradeable only.** Polymarket comes from the **Gamma API**
  (`active=true&closed=false`, sorted by `volume24hr`) — not the CLOB
  `active=true` list, which is mostly *closed* markets. Kalshi comes from
  `status=open` with 24h volume attached.
- **Filter** by `min_volume` (drop dust), `categories`, and an optional
  `watchlist`.
- **Rank by 24h volume** and keep the top `max_per_venue` per venue.
- **Re-curate** every `refresh_interval_min` (markets open/close/resolve); the
  WebSocket stream resubscribes to the new set automatically.

Config: `TB_UNIVERSE_*`. Live-validated: 400 discovered → 50 tracked, ranked by
volume (multi-$M 24h markets), no closed markets.

**Layer 2 — Selection (strategies + risk).** Within that universe each strategy
picks by its own edge: **arbitrage** acts only where a dutch-book or cross-venue
edge clears `min_edge`; the **market maker** quotes the widest-spread
`max_markets` above `min_spread`; the **event-driven reactor** concentrates on
markets that are actually moving; the **risk manager** bounds per-market and
gross exposure regardless.

Flow: **curate the universe (liquid, tradeable) → strategies pick by edge/spread
→ risk bounds exposure.**