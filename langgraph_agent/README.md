# TradingBot LangGraph Agent

The **permanent supervising agent** for the trading bot, as a LangGraph graph.
It's the only component that calls an LLM — the bot itself runs LLM-free, so
keeping it on 24/7 costs nothing in model tokens. You pay only when you talk to
this agent.

```
  You ──▶ LangGraph agent (Claude + tools) ──HTTP──▶ Bot control API ──▶ engine
          (the only thing that calls a model)        (LLM-free, always on)
```

## Why this design (the cost answer)

Running Claude on every bot tick or every 30-min autopilot cycle racks up a
bill. Instead, the **bot exposes its state and actions over HTTP** (`TB_API_*`),
and this agent — deployed once on LangGraph Platform — calls those endpoints as
tools. The agent only runs when you send it a message (or you schedule it). To
cut cost further, set `LANGGRAPH_MODEL=claude-haiku-4-5` for routine check-ins
and only switch to `claude-opus-4-8` for deep analysis.

## 1. Turn on the bot's control API

In the bot's `.env`:

```ini
TB_API_ENABLED=true
TB_API_HOST=127.0.0.1
TB_API_PORT=8787
TB_API_TOKEN=choose-a-long-random-string
```

Install the serve extra and run the bot:

```bash
pip install -e ".[serve]"        # from the repo root
tradingbot                        # now also serves the control API on :8787
```

Smoke-test it:

```bash
curl -s localhost:8787/health
curl -s localhost:8787/portfolio -H "Authorization: Bearer choose-a-long-random-string"
```

## 2. Run the agent locally

```bash
cd langgraph_agent
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt "langgraph-cli[inmem]"
cp .env.example .env              # fill ANTHROPIC_API_KEY + TRADINGBOT_API_TOKEN
langgraph dev                     # opens LangGraph Studio; chat with the agent
```

Ask it: *"what's my PnL?"*, *"how am I tracking to my daily goal?"*,
*"run regime detection"*, *"deploy $200"* (it stages + asks you to confirm).

## 3. Deploy it permanently (LangGraph Platform)

1. Push this repo to GitHub.
2. In **LangSmith → LangGraph Platform → New Deployment**, point it at this
   repo and the `langgraph_agent/` directory (it reads `langgraph.json`).
3. Set the deployment env vars: `ANTHROPIC_API_KEY`, `LANGGRAPH_MODEL`,
   `TRADINGBOT_API_URL`, `TRADINGBOT_API_TOKEN`.
4. Because the agent runs in the cloud, it must reach your bot's API. Either:
   - run the bot on a small VPS with the API port reachable (lock it down to the
     LangGraph egress IPs + the bearer token), or
   - expose your local bot with a tunnel: `cloudflared tunnel --url
     http://localhost:8787` (or `ngrok http 8787`) and put that public URL in
     `TRADINGBOT_API_URL`.

The deployment gives you a persistent agent with its own URL/API you can call
from anything (a cron, a webhook, a phone shortcut). That's your permanent brain.

## Files

| File | What it is |
|---|---|
| `graph.py` | The ReAct agent graph (`graph` is the deployed object). |
| `tools.py` | LangChain tools — thin HTTP calls to the bot's control API. |
| `langgraph.json` | Deployment manifest (graph path, deps, env). |
| `requirements.txt` | Agent dependencies. |
| `.env.example` | Env template. |

## Note on the two brains

The repo also ships an in-process agent (`src/tradingbot/ai/agent.py`) used by
the Telegram bridge — handy for local/paper use. This LangGraph agent is the
**deployable, permanent** version that talks to the bot over HTTP. Use whichever
fits: Telegram-only and local → in-process; hosted/always-available → LangGraph.
Both expose the same capabilities and the same safety gates.
