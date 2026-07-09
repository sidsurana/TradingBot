# Strategy Playbook — Fable 5 Strategy Lab, 2026-07-09

13 strategies were proposed by five specialist lenses (Polymarket structure, hedged
stat-arb, regime/momentum, event/calendar, ML) and each was attacked by an
adversarial quant agent with explicit cost arithmetic against THIS stack ($1,000
paper, 24bp stock round-trip, real Polymarket spreads + 10bp fee). Several
attackers ran real backtests. 9 survived with required modifications, 4 were killed.

## Cross-cutting findings (read first)

1. **The paper engine cannot redeem resolved Polymarket tokens.** Gamma discovery
   filters `closed=false`, so resolved markets silently leave the universe and held
   tokens never pay out $1/$0. Every hold-to-resolution strategy (including the
   EXISTING dutch-book arb) strands its profit. **Prerequisite for all Polymarket
   strategies: on universe refresh, poll Gamma for held-but-closed conditionIds and
   inject a synthetic redemption fill at outcomePrices (~30–50 lines).**
2. **The running BTC breakout backtests at −56%/2y** (17,511 real Coinbase 1h bars,
   2024-07→2026-07, exact engine mechanics + live .env): −$560 on $1k; even with
   zero costs it loses. Hit rate 20–28%, max losing streak 16. Recommend disabling
   or replacing — there is no gross edge at 1h candle granularity.
3. **The running 15m SPY/QQQ mean reversion is structurally cost-bound**: in the
   quiet regimes where fading is safe, a winner captures ~19–31bp vs a fixed 24bp
   round trip → ≈ −$1.20/trade EV. Harm-reduction patches if kept: force flat at
   15:45 ET, 12-bar time stop, regime-flip exit. Entry gates can't fix the math.
4. **The 4h gold/oil trend is the only current strategy the panel endorsed** (as
   upgraded below).
5. **Start archiving candles now** (daily append of 15m/1h bars to SQLite/parquet).
   Yahoo serves only ~60 days of 15m history; every future backtest/ML effort is
   starved without an archive. Tiny effort, compounding value.

## Build list (ranked by panel score × effort)

### Tier 0 — prerequisites (build before anything below)
- **P0. Polymarket redemption settlement** in the paper engine (fixes existing arb too).
- **P1. Candle archiver** (daily 15m/1h append per symbol).
- **P2. UMA/dispute status + gameStartTime parsed into market metadata** (one line
  in the Gamma parser; three strategies below consume it).

### Tier 1 — Polymarket structural carry/arb (the strongest family; score 6 each)

**S1. Certainty Carry (modified)** — buy near-certain tokens at 0.93–0.96 and hold
to redemption. Edge: favorite-longshot bias + zero-yield collateral discount (a
structural premium that can't be arbed to zero). Rules after modification: ask ≤
0.96, spread ≤ 0.015, volume ≥ $5k, **48h–14d to resolution** (never <48h — the
repo's own adverse-selection lesson), only markets with continuously observable
underlyings (crypto/index thresholds, poll-tracked politics — no discrete
announcement markets), keyword-veto on subjective resolution wording, UMA-dispute
force-exit, stop via `max(sell at bid, buy complement to lock $1 set)` sized as if
every loss is −100%. Caps: 8 positions, 2 per correlation group, ≤40% of equity.
Honest expectation: +1–2c EV/trade, ~$10–30/month gross on a $400 sleeve, punctuated
by rare 40–80c stop-outs.

**S2. Implication Arb / Conditional Dominance (modified)** — across threshold
ladders ("BTC ≥ $120k" implies "BTC ≥ $110k"), buy the dominated combo when
P(A) > P(B) + costs; the two-leg set pays ≥ $1 in every state. Edge: fragmented
retail attention + no cross-market margining (arbitrageurs must lock full zero-yield
collateral on both legs). Mods: verb-class parser (TOUCH vs SNAPSHOT markets never
paired; unit-test against real Gamma titles), position-based leg reconciliation,
partial-fill handling, redemption prerequisite.

**S3. Settlement-Lag Sweep (modified)** — buy tokens of effectively-decided
markets (0.97–0.99) in the window between real-world resolution and on-chain
settlement. Edge: sellers pay for immediacy to unlock collateral. Mods: mandatory
UMA `proposed/resolved` status gate (kills the in-play adverse-selection leak),
sports require mid ≥ 0.97 sustained 60 min after game start.

**S4. Scheduled-Macro Certainty (modified)** — FOMC-only at launch: buy the
consensus rate-decision outcome at T-36h when priced 0.90–0.96 (CME FedWatch-style
consensus is nearly always right by T-36h). Gate CPI/NFP categories behind a
calibration study on ≥100 resolved markets. Size as if losses are −100%.

*Family caveat: S1–S4 share one edge family (prediction-market carry) and one tail
(a chaotic macro/news day). The correlation-group caps and the ≤40% sleeve are
load-bearing.*

### Tier 2 — stocks/commodities upgrades

**S5. Trend upgrade (single-unit)** — add to the running 4h trend: efficiency-ratio
entry gate (ER(20) ≥ 0.30), delayed entry on 20-bar Donchian extreme, exit on
15-bar Donchian trail OR opposite EMA cross. NO pyramiding (risk caps make it
impossible at $1k anyway). Small effort, keeps the one endorsed strategy and cuts
its whipsaw losses.

**S6. Risk-off regime gate (engine/regime.py)** — shared module: risk-off = SPY
below 200-bar 1h EMA AND realized vol > 1.4× its 2y median; when risk-off, block
NEW mean-reversion entries (exits untouched). Captures the "stop dip-buying a
crash" benefit of a hedge overlay with zero transaction cost. (The short-SPY
overlay itself was rejected: the netted position book breaks it — engine positions
are keyed by market, so a hedge short would net into MR's long and confuse both.)

**S7. Post-Selloff Overnight Reversal (backtest-gated)** — buy SPY at close after
an intraday selloff ≤ −1.5%, sell next open (liquidity-provision premium to
end-of-day de-riskers). BUILD GATE: offline backtest on 20y of free daily data must
show ≥ +15bp/trade net with t > 2 in the vol-filtered subset, including 2018–2025
alone. If it fails, don't build. Needs a small feed change (equities daily bars or
longer history) either way.

### Tier 3 — the model (build infrastructure now, gate later)

**S8. Meta-labeling gate, shadow-first** — walk-forward logistic regression that
scores every entry signal of the live strategies (features: vol regime, spread z,
time-of-day, streak stats; label: realized round-trip PnL via bar-by-bar replay of
each strategy's OWN exit logic, not a synthetic take-profit). v1 ships as
**shadow mode only**: log features + probability for every signal, block nothing.
Enable live gating only after ≥3 months / ≥100 logged decisions AND the shadow
top-half vs bottom-half hit-rate spread ≥ 8pp. Depends on P1 (archiver).
This is the honest version of "develop a model": there are not yet enough closed
trades to train on, so the model earns its way in.

### Killed (do not build)
- 15m MR entry-gating (winner capture < round-trip cost — arithmetic, not tuning)
- BTC compression breakout (backtested: no gross edge at 1h granularity)
- Pre-FOMC drift (post-2015 drift ≈ retail costs; 14 years to statistical significance)
- XLE/USO oil-lag pairs (t-stat needs ~59 years of trades; hedge misses SPY factor)

## Suggested build order
P0+P2 (one PR) → S1 → S3 → S5+S6 (one PR) → P1 → S2 → S4 → S7 (if backtest passes) → S8 shadow.
