# Trading Logic

How the bot decides, simulates, sizes, and protects positions — the math and the
mechanics. Sections marked **[implemented]** are live and tested; **[planned]**
are designed but not yet built. Every threshold named here is a config knob (see
the Parameters table at the end).

---

## 1. Who actually trades (autonomy model)

There are two layers, deliberately separated:

- **The engine** trades autonomously and continuously, with **no LLM in the
  loop**. Each tick it refreshes order books, runs the strategies, passes every
  proposed order through the risk manager, and executes the survivors. This is
  what runs 24/7 and what places real orders.
- **The agent** (Claude, via Telegram or the LangGraph deployment) is the
  *supervisor*. It reads state, runs analyses, adjusts posture, and gates capital
  — but it does **not** place individual trades. That keeps trading fast and
  cheap (no per-trade model calls) and keeps the LLM's role to judgment, not
  execution.

So "the AI trades autonomously" is true at the system level: rule-based
strategies execute on their own; the AI oversees and can intervene. Real-money
trading additionally requires `TB_LIVE=true` + venue credentials + an explicit
`go_live` confirmation.

Price convention everywhere: **probability in [0, 1]**. A "price" of 0.40 means
a 40% implied chance, costing $0.40 per contract, paying $1.00 if it resolves
YES. Each venue adapter converts its native unit (Kalshi cents, Polymarket
decimals) to this.

---

## 2. The engine tick **[implemented]**

Every `TB_LOOP_INTERVAL_S` seconds:

1. **Refresh books** for the tracked market universe (top-of-book per market).
2. **Update kill-switch** from session PnL.
3. **Update goals** (roll daily/week baselines) and apply **lock-gains**.
4. **Run risk exits** (stop-loss / take-profit) — *always*, even while paused.
5. If not paused: **run strategies** → collect proposed orders.
6. **Risk-check** each order; **execute** the approved ones.
7. **Reconcile fills** into the portfolio (PnL, positions, cash).

The loop is defensive: a strategy or venue error is logged and the tick
continues. Pausing (operator or lock-gains) stops step 5 only — data, marks,
protective exits, and discretionary orders keep running.

**Latency (detection → execution).** Order *placement* is fast: strategy
evaluation ~0.1 ms, plus one signed REST POST ~50–100 ms. The real constraint is
**data freshness**. With REST polling, refreshing books costs ~50 ms/market
*sequentially* — ~10 s for a 200-market universe — and starts returning HTTP 429
(rate-limited). So a market's book can be up to ~10 s stale before a strategy
even sees the edge. **WebSocket streaming (Section 2a)** removes this: books are
pushed and read from a local cache in microseconds, so freshness drops to
sub-second and there's no per-market polling to rate-limit.

### 2a. WebSocket streaming **[implemented; Polymarket live-validated]**

When `TB_STREAMING_ENABLED=true`, each venue maintains a pushed local order book
(`LocalBook`): a full snapshot then incremental updates (Kalshi sends additive
deltas; Polymarket sends absolute level sets). The engine reads top-of-book from
this cache instantly; any market not yet warmed up is REST-filled (capped, to
avoid 429s). And because reads are now in-memory, the engine ticks at
`TB_STREAMING_LOOP_INTERVAL_S` (default 0.25 s) instead of the slow REST cadence
— so it *acts* on an edge in ~250 ms, not seconds.

**Measured (Polymarket, live):** push latency from server timestamp to local
receipt is **~40 ms median** (min ~35 ms), with ~15 order-book updates/sec across
40 active markets. End-to-end edge→execution becomes ~40 ms data + ~250 ms tick +
~50–100 ms order ≈ **a few hundred ms**, vs ~10 s on REST.

#### Full latency budget (data → fill/ack)

For a **marketable** order (arbitrage / discretionary — the latency-critical
case). ✅ = measured here; ◇ = estimate (network/venue-dependent; not measurable
without funded live creds):

| Stage | Time | Notes |
|---|---|---|
| Market data received (venue → our process) | **~40 ms** ✅ | WS push, server ts → local recv (Polymarket) |
| Parse + update local book | **<0.1 ms** ✅ | in-memory dict ops |
| React (event-driven debounce + schedule) | **~11 ms** ✅ | measured live; coalesces update bursts |
| Decision (strategy eval) | **~0.1 ms** ✅ | arb/MM math is trivial |
| Build + sign order | **~1–3 ms** ◇ | Kalshi RSA-PSS ~1–2 ms; Polymarket EIP-712 a few ms |
| API request → venue accept/ack | **~50–150 ms** ◇ | one REST POST round-trip to the venue |
| Fill/ack returned | included above (marketable) ◇ | Kalshi returns order status on the POST; else confirmed via WS |
| **Total edge → fill** | **~100–200 ms** | now network-bound (the tick wait is gone) |

**Event-driven acting [implemented].** The engine no longer waits for the loop to
act. WS clients mark each changed market dirty and wake a **reactor** (debounced
~10 ms to coalesce bursts) that evaluates the affected markets and places/cancels
orders immediately — sharing a trade-lock with the periodic loop so the two never
interleave. The loop stays on purely as the backstop: risk, exits, full quote
reconciliation, lock-gains, and heartbeat recovery. This removed the ~125 ms
average tick wait; measured live the reactor fires within ~11 ms of an update.

The remaining floor is the network: ~40 ms in + ~50–150 ms out ≈ **~100–200 ms**,
which a retail connection cannot beat (co-location would, but that's out of
scope). `TB_STREAMING_EVENT_DRIVEN=true` (default) enables the reactor;
`TB_STREAMING_REACT_DEBOUNCE_S` (0.01) tunes the coalescing window.

For a **resting market-maker quote**, "accept/ack" (the quote resting on the
book) is the ~50–150 ms above; the **fill** then happens whenever someone trades
against it — that's the nature of making (could be milliseconds or never), which
is exactly why the quote must stay fresh.

The Polymarket market channel (public) is validated, including the real
`price_change` wire format (`price_changes` with a per-change `asset_id`) and the
PING/PONG keep-alive. The Kalshi WS path matches the documented format but needs
live validation with credentials (its handshake is signed). It's **fail-safe**:
if a WS yields nothing, the engine falls back to REST, so streaming can only help.

---

## 3. Simulation / paper-fill model **[implemented]**

Paper mode runs the **same code path** as live; only execution is simulated, by
`PaperExchange`, against the *real* observed order book:

- **Marketable orders** (market orders, or limits that cross the spread) fill by
  walking the opposite side of the book, paying the spread. Size beyond
  available depth is left unfilled — no fantasy liquidity.
- **Resting limit orders** (non-marketable) are acknowledged `OPEN` and fill on a
  later tick if the market trades through them.
- **Fees:** a synthetic `0.1%` of notional per fill, so strategies can't profit
  on sub-fee dust edges. (Real venues differ; live fees come from the venue.)

This is intentionally conservative — paper PnL should *understate*, not
overstate, live results, so a strategy that looks good on paper has margin.

---

## 4. Strategy logic

### 4a. Arbitrage **[implemented]**

Two flavors, both venue-agnostic (they operate on the unified book, so legs can
span Kalshi and Polymarket):

**Dutch-book (complete-set underpricing).** Within one event, a complete set of
mutually-exclusive outcomes must cost ≥ 1.0 (exactly one resolves to $1). If the
cheapest asks across the set sum to `C < 1`, buying one of each locks a risk-free
profit of `1 − C` per set:

```
edge = 1 − Σ(best_ask_price for each distinct outcome)
trade if edge ≥ TB (min_edge), size = min(depth across legs), capped by max_size
```

**Cross-venue (same outcome, two venues).** If the same real outcome is quoted on
two venues and venue A's best ask < venue B's best bid beyond the edge buffer,
buy A / sell B:

```
edge = best_bid(B) − best_ask(A)
trade if edge ≥ min_edge, size = min(ask_size_A, bid_size_B, max_size)
```

The `min_edge` buffer absorbs fees + slippage so only real edges trade. Today
cross-venue linkage uses a shared `event_id`; a production **event-mapping
table** (roadmap) will link the same real event across venues robustly.

### 4b. Market-making **[implemented]**

A passive two-sided maker that earns the bid/ask spread. Each tick, for the
widest-spread markets (most profit per round-trip, capped to `max_markets`):

```
if (best_ask − best_bid) < min_spread:  skip   # not enough to clear fees + edge
quote BUY  size at best_bid   (join the inside bid)
quote SELL size at best_ask   (join the inside ask)
```

**Inventory control by size-skew:** the buy quote's size is capped at
`max_inventory − position` and the sell quote's at `max_inventory + position`. So
as the maker accumulates a long position, its buy size shrinks toward zero at the
cap while it keeps selling — the book mechanically mean-reverts inventory back to
flat, no manual unwinding needed.

**Quote lifecycle** is managed by the engine (not fire-and-forget like arb): it
places each quote once, leaves it resting, and **cancels/replaces only when the
target price moves or the quote fills**. Filled quotes are detected next tick and
re-quoted. This is why streaming matters most here — a maker with stale quotes
gets *picked off* (someone lifts your now-mispriced quote); fresh quotes (~40 ms)
earn the spread. Market-making is the **high-frequency income stream** toward a
daily target: many small spread captures rather than rare arbitrage windows.

Knobs: `TB_MM_MIN_SPREAD` (0.02), `TB_MM_QUOTE_SIZE` (10), `TB_MM_MAX_INVENTORY`
(40), `TB_MM_MAX_MARKETS` (15). Enable with `TB_ENABLED_STRATEGIES=["arbitrage","market_maker"]`.

### 4c. Signal / model-driven **[planned]**

Take directional positions from a predictive signal. The hook already exists: the
agent's **Quant skills** (regime, alpha, strategy generation) produce structured
views; the planned signal strategy will consume those (and external data) to size
directional bets — then stop-loss / take-profit protect them.

---

## 5. Position sizing **[implemented, basic]**

Current sizing is **depth-and-cap bounded**: take the most you can at the target
price up to the lesser of available book size and the strategy's `max_size`, then
the risk manager clamps further (Section 6). This is appropriate for arbitrage,
where edge is realized at fill.

**[planned] Fractional-Kelly** for directional strategies: size ∝ edge / variance,
scaled by a fraction (e.g. 0.25) to control drawdown. The `drawdown_analysis`
Quant skill is designed to recommend the sizing rule.

---

## 6. Risk management **[implemented]**

Every proposed order passes through the risk manager *before* it can reach a
venue. Checks, in order — any failure rejects the order with a logged reason:

1. **Kill-switch** — if tripped, reject everything.
2. **Rate limit** — at most `max_orders_per_min` (sliding 60s window).
3. **Per-market position cap** — projected `|position|` ≤ `max_position_per_market`.
4. **Per-market notional cap** — projected `price × size` ≤ `max_notional_per_market`.
5. **Gross notional cap** — book-wide exposure ≤ `max_gross_notional`.

**Kill-switch (daily-loss stop).** Session PnL is marked each tick; if it falls to
`−max_daily_loss`, the kill-switch trips and **all** further orders are rejected
for the session. This is the hard floor on how much a bad day can cost. It's the
account-level stop-loss; Section 7 is the per-position stop-loss.

---

## 7. Stop-loss / take-profit (exits) **[implemented]**

Per-position protective exits, evaluated every tick — **including while paused**,
so a lock-gains or operator pause never strands a losing position. For each open
position, the move from entry toward the current mark is measured in
entry-relative terms:

```
pnl_fraction = direction × (mark − avg_entry_price) / avg_entry_price
   long YES @ 0.40, mark 0.30  →  −0.25  (−25%)
   long YES @ 0.40, mark 0.60  →  +0.50  (+50%)
```

- **Stop-loss:** if `pnl_fraction ≤ −stop_loss_pct`, flatten the position
  (long → sell into the bid; short → buy from the ask).
- **Take-profit:** if `pnl_fraction ≥ take_profit_pct`, flatten the same way.

Closing orders still pass through the risk manager and the paper/live executor
like any other order. **Defaults OFF** (`stop_loss_pct = take_profit_pct = 0`),
because pure arbitrage legs are meant to be held to resolution — turn them on for
directional (signal/MM) exposure. Recommended starting points once directional
strategies are live: stop-loss `0.20–0.30`, take-profit `0.40–0.60`.

There are therefore **three** loss controls, at widening scope:
per-position (stop-loss) → account-day (kill-switch) → goal-driven (lock-gains).

---

## 8. Profit goals **[implemented]**

You set dollar targets; the bot tracks PnL against them.

- **Baselines:** equity at the start of the current day and ISO-week are anchored
  and persisted (`.tradingbot/goals.json`). Daily PnL = equity − day baseline;
  weekly PnL = equity − week baseline. Baselines roll automatically at
  day/week boundaries.
- **Weekly pace:** the bot computes how much you'd want by now if earning the
  weekly target evenly: `pace = weekly_target × (weekday / 7)`. "On track" means
  weekly PnL ≥ pace — this is what the agent uses to tell you if you're ahead or
  behind.
- **Lock-gains:** if `lock_gains` is on and the **daily** target is hit, the
  engine pauses new trading for the rest of the day (protective exits keep
  running), and auto-resumes at the next day's rollover. This stops the bot from
  giving back a good day chasing more.

Set realistic targets — they should scale to deployed capital, not ambition. With
a $1,000 book and thin edges, $10–30/day is sane. Goals never override risk caps;
the caps always win.

---

## 9. Autopilot (autonomous optimization) **[implemented]**

On an interval (`TB_AUTOPILOT_INTERVAL_MIN`), the bot reassesses itself and pushes
a short Telegram briefing:

1. Read goal progress (where you stand vs daily/weekly target + pace).
2. Run the `regime_detection` Quant skill on the live snapshot.
3. Produce one concrete recommendation.

Autopilot is **advisory and safe**: it never deploys capital or raises limits on
its own. The only automatic action in the whole system is lock-gains (Section 8).
Everything else — capital, limit changes, going live, the kill-switch — requires
an explicit operator confirmation. This is the human-in-the-loop gate that makes
an otherwise-autonomous bot safe to leave running.

---

## 10. Quant skills (the analytical playbooks) **[implemented]**

Structured Claude prompts (from `Quant.md`), each pre-filled with live bot state
and run on demand by the agent:

| Skill | What it produces |
|---|---|
| `strategy_generation` | New strategy ideas for given capital/risk constraints |
| `regime_detection` | Liquidity/dislocation regime → which strategies to favor |
| `risk_analysis` | Book risk breakdown + specific limit-change suggestions |
| `alpha_detection` | Underexploited edges from the live snapshot |
| `drawdown_analysis` | Drawdown exposure + a position-sizing rule |
| `trade_review` | Hold/trim/close action list for open positions |

These inform decisions; they don't place trades. The planned signal strategy will
turn selected skill outputs into live directional positions.

---

## 11. Paper → live **[implemented gates]**

Real-money trading requires **all** of: `TB_LIVE=true` **and** configured venue
credentials **and** an in-chat `go_live` confirmation (which itself refuses if no
venue is configured). Any one missing → it stays in paper. To stop instantly:
`pause`, or trip the kill-switch. Recommended rollout is in `SETUP.md` §6 — paper
for several days, then small real caps, then scale.

---

## 12. Implemented vs planned — at a glance

| Capability | Status |
|---|---|
| Engine loop, paper simulation, fills/PnL | ✅ implemented |
| Arbitrage (dutch-book + cross-venue) | ✅ implemented |
| Risk caps + daily-loss kill-switch | ✅ implemented |
| Stop-loss / take-profit exits | ✅ implemented |
| Daily/weekly goals + pace + lock-gains | ✅ implemented |
| Autopilot briefings | ✅ implemented |
| Quant skills + agent (Telegram + LangGraph) | ✅ implemented |
| Agent discretionary trades (`place_order`) | ✅ implemented |
| HTTP control API + LangGraph agent | ✅ implemented |
| Live execution — Kalshi (signed) | ✅ implemented (signing tested) |
| Live execution — Polymarket (py-clob-client) | ⚠️ wired; needs live validation |
| WebSocket streaming — Polymarket | ✅ implemented + live-validated (~40ms push) |
| WebSocket streaming — Kalshi | ⚠️ implemented; needs live validation (signed handshake) |
| Market-making strategy + quote lifecycle | ✅ implemented |
| Event-driven acting (react on update, ~11ms) | ✅ implemented + live-validated |
| Signal/model strategy + Kelly sizing | ⬜ planned |
| Cross-venue event-mapping table | ⬜ planned |

---

## 13. Parameters reference

| Env var | Meaning | Default |
|---|---|---|
| `TB_LOOP_INTERVAL_S` | Engine tick cadence (seconds) | 2.0 |
| `TB_LIVE` | Master switch: real orders | false |
| `TB_PAPER_STARTING_CASH` | Paper starting bankroll | 1000 |
| `TB_ENABLED_STRATEGIES` | Strategies to run | ["arbitrage"] |
| `TB_RISK_MAX_POSITION_PER_MARKET` | Max abs contracts per market | 100 |
| `TB_RISK_MAX_NOTIONAL_PER_MARKET` | Max $ at risk per market | 50 |
| `TB_RISK_MAX_GROSS_NOTIONAL` | Max $ across the book | 500 |
| `TB_RISK_MAX_DAILY_LOSS` | Kill-switch threshold ($) | 50 |
| `TB_RISK_MAX_ORDERS_PER_MIN` | Order rate guard | 60 |
| `TB_EXIT_ENABLED` | Enable stop-loss/take-profit | false |
| `TB_EXIT_STOP_LOSS_PCT` | Close at −X% from entry (0=off) | 0.0 |
| `TB_EXIT_TAKE_PROFIT_PCT` | Close at +X% from entry (0=off) | 0.0 |
| `TB_GOAL_DAILY_TARGET` | Daily profit target ($) | 0 |
| `TB_GOAL_WEEKLY_TARGET` | Weekly profit target ($) | 0 |
| `TB_GOAL_LOCK_GAINS` | Pause once daily target met | true |
| `TB_AUTOPILOT_ENABLED` | Run the autopilot loop | false |
| `TB_AUTOPILOT_INTERVAL_MIN` | Reassess cadence (minutes) | 30 |

Arbitrage `min_edge` (default 0.02) and `max_size` (default 20) are strategy
params, tunable where strategies are built.
