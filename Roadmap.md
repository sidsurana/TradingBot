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
| 15 | **Persistence** (positions / fills / audit / goals) to SQLite | So a restart doesn't lose positions or re-baseline goals mid-day; needed before unattended real-money runs | M |
| 16 | **Signal / model strategy** | Turn the agent's `regime`/`alpha` Quant skills into live directional positions (sized fractional-Kelly, protected by stop-loss) — the third strategy | M–L |
| 17 | **Cross-venue event-mapping table** | Today cross-venue arb relies on a shared `event_id`; a real mapping (the same World Cup match on Kalshi *and* Polymarket) unlocks far more arb | M |
| 18 | **Per-fill venue reconciliation (live)** | In live mode, reconcile our portfolio against venue-reported positions/fills each tick (paper drains locally today) | M |

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



---- 
My recommendation for what's next

  The fast path is now done and proven. Before you point real money at it unattended, the most
  important next piece is persistence (#15 in the roadmap) — right now a restart loses positions and
  re-baselines your daily goal mid-day, which is risky for a 24/7 bot. After that, the signal/Kelly 
  strategy gives you a third income stream. Want me to build persistence next, or go for the signal
  strategy?

  Layer 1 — Discovery: what gets tracked

  engine.discover() (engine.py:80) just aggregates each venue's list_markets() and takes the first
  200:
  - Kalshi → GET /markets?status=open&limit=200
  - Polymarket → GET /markets?active=true

  This is naive and it's the real gap. There's no liquidity, volume, or quality filter — it's "the
  first 200 the venue hands back." Worse, I found during the streaming validation that Polymarket's
  active=true list is full of closed/resolved markets (you have to use the Gamma API or filter
  accepting_orders + enable_order_book). So today the universe can include markets you can't actually
  trade. This works for paper/demo but is not what you'd point real money at.

  Layer 2 — Selection: which tracked markets actually get traded

  This part is genuinely sound — each strategy picks within the tracked universe by its own edge
  criteria:

  - Arbitrage scans the whole universe every evaluation and only acts where a real edge exists: a
  dutch-book (complete-set asks summing < 1.0) or a cross-venue dislocation, both gated by min_edge
  (0.02). It "picks" markets where there's risk-free money, ignores the rest.
  - Market maker ranks markets by spread (widest first) and quotes only the top max_markets (15)
  whose spread ≥ min_spread. So it picks the most-profitable-to-make books and ignores tight or
  illiquid ones.
  - Event-driven reactor concentrates compute/reactions on markets that are actually moving (the
  dirty set), so attention follows activity.
  - Risk manager then bounds everything — per-market position/notional caps + gross cap — so no
  single market gets over-weighted even if a strategy loves it.

  So the flow is: discovery casts a wide net → strategies filter by edge/spread → risk bounds 
  exposure.

  The fix (and why it matters)

  The selection logic is right; discovery needs to become real market curation. A proper universe
  layer would:
  1. Filter to genuinely tradeable markets — Polymarket via the Gamma API (closed=false,
  accepting_orders=true, sorted by 24h volume); Kalshi by open status + volume/liquidity.
  2. Rank by a tradeability score — volume + spread + time-to-resolution — and cap to the top N
  most-liquid.
  3. Refresh periodically — markets open, close, and resolve, so the universe should roll over (e.g.,
  re-curate every few minutes), not be fixed at startup.
  4. Optionally let you constrain by category (politics, sports, econ) or by a watchlist.

  This is the single highest-leverage improvement to actual profitability: the arb and MM strategies
  are only as good as the markets you point them at, and right now you're pointing them at an
  unfiltered list that may include dead markets.

  It's not yet a line item in Roadmap.md — it should be, near the top. Want me to build the
  universe-curation layer next (liquidity/volume-filtered, volume-ranked, periodically refreshed)?
  It's a clean, testable addition that makes everything downstream — arb, market-making, your
  daily-goal odds — materially better, and it's a prerequisite before live money. I'd put it ahead of
  persistence.