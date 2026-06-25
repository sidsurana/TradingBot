"""Run the multi-agent supervisor locally with persistent threads.

Uses a SqliteSaver checkpointer so a conversation thread survives restarts
(LangGraph Platform provides its own persistence, so the deployed `graph` in
graph.py omits the checkpointer — this script is for self-hosting).

    python run_local.py            # REPL on thread "local"
    THREAD_ID=alice python run_local.py

Env: ANTHROPIC_API_KEY, TRADINGBOT_API_URL, TRADINGBOT_API_TOKEN (see .env).
"""

from __future__ import annotations

import os

from langgraph.checkpoint.sqlite import SqliteSaver

try:
    from .graph import build_supervisor
except ImportError:
    from graph import build_supervisor

CHECKPOINT_DB = os.getenv("LANGGRAPH_CHECKPOINT_DB", "agent_threads.db")
THREAD_ID = os.getenv("THREAD_ID", "local")


def main() -> None:
    with SqliteSaver.from_conn_string(CHECKPOINT_DB) as checkpointer:
        app = build_supervisor(checkpointer=checkpointer)
        config = {"configurable": {"thread_id": THREAD_ID}}
        print(f"TradingBot multi-agent supervisor — thread '{THREAD_ID}'. Ctrl-C to exit.")
        while True:
            try:
                message = input("\n> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not message:
                continue
            result = app.invoke({"messages": [("user", message)]}, config)
            print(result["messages"][-1].content)


if __name__ == "__main__":
    main()
