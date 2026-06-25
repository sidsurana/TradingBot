# Setup Guide

Step-by-step setup for the prediction-markets trading bot: install, every API
key/credential, the Claude brain, Telegram chat, profit goals, and the
paper → real-money progression.

> **Golden rule:** the bot defaults to **paper trading** (simulated fills over
> live market data). It cannot place a real order until you (1) wire venue
> credentials, (2) set `TB_LIVE=true`, and (3) confirm `go_live` in chat. Run in
> paper for a few days first — the guide assumes that.

---

## 0. Prerequisites

- **Python 3.11+** (`python3 --version`)
- A phone with **Telegram** installed
- An **Anthropic API key** (for the Claude brain)
- Later, for real money: a **Kalshi** account and/or a **Polymarket** wallet

---

## 1. Install

```bash
cd ~/Desktop/TradingBot
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[agent]"      # core + Claude brain
# add ".[live]" later when you go to real money (Kalshi/Polymarket signing libs)
pytest                          # sanity check — should pass
tradingbot                      # paper mode, no creds needed (Ctrl-C to stop)
```

`tradingbot` will connect to Kalshi + Polymarket **public** market data, discover
markets, and paper-trade. No keys required for this.

Copy the env template and edit it as you go:

```bash
cp .env.example .env
```

Everything below is a block of `.env`.

---

## 2. Anthropic API key (the brain)

The agent that you chat with and that runs the Quant skills is Claude.

1. Go to **https://console.anthropic.com** → sign in.
2. **Settings → API Keys → Create Key**. Copy it (starts with `sk-ant-...`).
3. Add billing (Plans & Billing) — agent calls cost a few cents each.
4. In `.env`:

```ini
TB_ANTHROPIC_API_KEY=sk-ant-xxxxxxxx
TB_ANTHROPIC_MODEL=claude-opus-4-8     # default; the most capable model
TB_ANTHROPIC_EFFORT=high               # low|medium|high|xhigh|max
```

(If you prefer, you can instead export `ANTHROPIC_API_KEY` in your shell — the
bot falls back to it.)

---

## 3. Telegram bot (chat from your phone)

1. In Telegram, message **@BotFather** → `/newbot` → follow prompts → it gives
   you a **bot token** like `123456:ABC-DEF...`.
2. Open a chat with your new bot and send it any message (e.g. "hi").
3. Find **your chat id**: message **@userinfobot**, it replies with your numeric
   id. (Alternatively, start the bot once with just the token set and read the
   logged `telegram.unauthorized chat_id=...` when you message it.)
4. In `.env`:

```ini
TB_TELEGRAM_BOT_TOKEN=123456:ABC-DEF...
TB_TELEGRAM_ALLOWED_CHAT_IDS=[7654321]   # JSON list; only these chats can command it
```

> Only chat IDs in this list can drive the bot — everyone else is ignored. Keep
> the token secret; anyone with it can message *as* the bot.

Restart `tradingbot`. It should message you "🤖 TradingBot online." Now try:

- "what's my PnL?"
- "show risk"
- "run regime detection"
- "pause" / "resume"
- "deploy $200"  → it replies with a summary and asks you to confirm; reply "yes".

---

## 4. Profit goals (daily / weekly targets)

Set how much you want to make. The bot tracks PnL against these, the agent
reports on pace, and (optionally) it **locks gains** by pausing trading once the
daily target is hit, resuming the next day.

```ini
TB_GOAL_DAILY_TARGET=25       # $/day (0 disables)
TB_GOAL_WEEKLY_TARGET=125     # $/week (0 disables)
TB_GOAL_LOCK_GAINS=true       # pause once daily target met; resume next day
```

Then on Telegram: *"how am I tracking against my goal?"* → the agent calls
`get_goal_progress` and tells you daily/weekly PnL, % of target, and whether
you're on weekly pace.

**Picking realistic targets.** Targets should scale to deployed capital, not
ambition. With a $1,000 paper book and thin prediction-market edges, $10–30/day
is a sane starting goal. Run paper for a few days, see what the strategies
actually clear, then set the target slightly above the paper average. Don't set
a target so high it pushes the bot past its risk caps — those caps win.

> Goal baselines persist to `.tradingbot/goals.json`. In paper mode positions
> don't yet persist across restarts, so restarting mid-day re-baselines from
> fresh paper cash. Within a continuous run, tracking is exact.

---

## 5. Autopilot (let it run on its own)

Autopilot periodically reassesses goals + market regime and sends you a short
Telegram briefing — so the bot "runs on its own" and keeps you informed.

```ini
TB_AUTOPILOT_ENABLED=true
TB_AUTOPILOT_INTERVAL_MIN=30    # reassess every 30 min
TB_AUTOPILOT_BRIEFING=true      # push a briefing to Telegram each cycle
```

Autopilot is **advisory + safe**: it never deploys capital or raises limits on
its own (those always need your confirmation). The only automatic action is
lock-gains. Each cycle you'll get something like:
*"Daily: $18/$25 (72%). Regime: tight spreads, mild cross-venue dislocation —
favor arbitrage. Consider holding posture."*

---

## 6. Recommended rollout: paper first, then real dollars

**Days 1–3 — paper.** Just run it (Steps 1–5, `TB_LIVE` unset/false). Watch the
Telegram briefings and the logs. Confirm: goals track correctly, the agent
answers accurately, the strategies actually find edges, no errors. Tune your
risk limits (`TB_RISK_*` in `.env`) and goal targets to what you observe.

**Day 4+ — small real money.** Only after paper looks good:

1. Wire a venue (Step 7).
2. `pip install -e ".[live]"` (adds the signing libraries).
3. Set conservative caps, e.g. `TB_RISK_MAX_GROSS_NOTIONAL=50`,
   `TB_RISK_MAX_DAILY_LOSS=10`.
4. Set `TB_LIVE=true` and restart.
5. On Telegram: say *"go live"* → confirm. Start tiny. Scale only after it
   behaves with real fills.

---

## 7. Venue credentials (only when going live)

### Kalshi (US-regulated; easiest legal path for US users)

1. Create/verify an account at **https://kalshi.com** and fund it.
2. **Account → Settings → API Keys → Create** (this is the trading API, not the
   website login). Kalshi gives you an **API Key ID** and downloads an **RSA
   private key** file (`.pem`/`.key`). Save the file somewhere safe.
3. In `.env`:

```ini
TB_KALSHI_API_KEY_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
TB_KALSHI_PRIVATE_KEY_PATH=/absolute/path/to/kalshi_private_key.pem
TB_KALSHI_BASE_URL=https://api.elections.kalshi.com
```

The bot signs each trading request with that key (RSA-PSS). Market data already
works without it; the key is only needed to place/cancel orders and read
positions.

### Polymarket (crypto; Polygon + USDC)

1. Create a wallet (e.g. MetaMask) on **Polygon**; fund it with **USDC.e** and a
   little **MATIC** for gas.
2. Deposit into Polymarket and approve trading at **https://polymarket.com**.
3. Export the wallet's **private key** (MetaMask → Account details → Export
   private key). **This key controls real funds — guard it.**
4. In `.env`:

```ini
TB_POLY_PRIVATE_KEY=0xyourprivatekey
TB_POLY_FUNDER_ADDRESS=0xyourwalletaddress   # the funded address, if proxied
TB_POLY_CLOB_URL=https://clob.polymarket.com
```

> Polymarket order placement uses `py-clob-client` (installed by `.[live]`) for
> EIP-712 signing. **Validate it with one tiny manual order before trusting the
> autopilot with it** — this path can't be unit-tested without a funded wallet.

> US persons: check Polymarket's current availability/terms for your
> jurisdiction before funding.

---

## 8. Going live — the switch

Live trading requires **all** of:

```ini
TB_LIVE=true
```

…**plus** configured venue creds (Step 7) **plus** an in-chat `go_live`
confirmation. Any one missing → it stays in paper. The `go_live` action refuses
if no venue is configured. This triple gate is intentional: it's hard to start
risking real money by accident.

To stop trading instantly at any time: say *"trip the kill switch"* (and
confirm) on Telegram, or just *"pause"*.

---

## 9. Quick reference — what to say on Telegram

| You say | The bot does |
|---|---|
| "what's my PnL?" / "show portfolio" | reads live cash, equity, session PnL |
| "show positions" | lists open positions with marks |
| "show risk" | limits, kill-switch, gross exposure |
| "how am I tracking to goal?" | daily/weekly target vs PnL + pace |
| "run regime detection" / "find alpha" | runs that Quant skill on live data |
| "pause" / "resume" | halts / resumes order placement |
| "deploy $200" | stages it → asks you to confirm |
| "set max daily loss to 20" | stages a limit change → confirm |
| "go live" | stages the switch to real money → confirm |
| "trip the kill switch" | stages emergency stop → confirm |

Sensitive actions (capital, limits, go-live, kill-switch) always require a
"yes". Reads and analysis never do.
