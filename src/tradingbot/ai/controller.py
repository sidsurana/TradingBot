"""BotController — the safe surface the AI agent (and Telegram) act through.

The agent never touches the engine internals directly. Every read and every
action goes through this facade, which:
  - returns plain JSON-able dicts (easy for the model to consume),
  - gates sensitive actions (capital, going live, raising limits) behind an
    explicit confirmation token, so a chat message can't move real money by
    accident,
  - keeps an audit log of actions taken.

Sensitive actions return a `needs_confirmation` payload with a token; the caller
must call `confirm(token)` to execute. This is the human-in-the-loop gate for an
otherwise-autonomous bot.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import structlog

from tradingbot.engine.engine import Engine
from tradingbot.models import Market, Order, OrderType, Side, Venue

log = structlog.get_logger(__name__)

SENSITIVE = {"set_risk_limit", "deploy_capital", "go_live", "trip_kill_switch", "place_order"}


@dataclass
class _Pending:
    action: str
    args: dict
    summary: str
    created_at: float = field(default_factory=time.time)


class BotController:
    def __init__(self, engine: Engine):
        self.engine = engine
        self._pending: dict[str, _Pending] = {}
        self.audit: list[dict] = []

    # --- reads ---------------------------------------------------------------

    def portfolio_summary(self) -> dict[str, Any]:
        p = self.engine.portfolio
        books = self.engine.last_books
        open_positions = [pos for pos in p.positions.values() if pos.size != 0]
        return {
            "mode": "live" if self.engine.settings.live else "paper",
            "paused": self.engine.paused,
            "cash": str(round(p.cash, 2)),
            "equity": str(round(p.equity(books), 2)),
            "session_pnl": str(round(p.session_pnl(books), 2)),
            "open_position_count": len(open_positions),
            "tracked_markets": len(self.engine.markets),
        }

    def list_positions(self) -> list[dict[str, Any]]:
        books = self.engine.last_books
        out = []
        for pos in self.engine.portfolio.positions.values():
            if pos.size == 0:
                continue
            mark = books.get(pos.market.key)
            mid = mark.mid if mark else None
            out.append({
                "market": pos.market.key,
                "title": pos.market.title,
                "outcome": pos.market.outcome,
                "size": str(pos.size),
                "avg_price": round(pos.avg_price, 4),
                "mark": round(mid, 4) if mid is not None else None,
                "unrealized_pnl": str(round(pos.unrealized_pnl(mid), 2)) if mid else None,
            })
        return out

    def risk_status(self) -> dict[str, Any]:
        r = self.engine.risk
        lim = r.limits
        ex = self.engine.settings.exits
        return {
            "kill_switch": r.kill_switch,
            "paused": self.engine.paused,
            "gross_notional": str(round(r.portfolio.gross_notional(self.engine.last_books), 2)),
            "limits": {
                "max_position_per_market": str(lim.max_position_per_market),
                "max_notional_per_market": str(lim.max_notional_per_market),
                "max_gross_notional": str(lim.max_gross_notional),
                "max_daily_loss": str(lim.max_daily_loss),
                "max_orders_per_min": lim.max_orders_per_min,
            },
            "exits": {
                "enabled": ex.enabled,
                "stop_loss_pct": ex.stop_loss_pct,
                "take_profit_pct": ex.take_profit_pct,
            },
        }

    def goal_progress(self) -> dict[str, Any]:
        equity = self.engine.portfolio.equity(self.engine.last_books)
        prog = self.engine.goals.progress(equity)
        prog["equity"] = str(round(equity, 2))
        prog["enabled"] = self.engine.goals.enabled
        return prog

    def market_snapshot(self, limit: int = 25) -> list[dict[str, Any]]:
        """Compact view of the most liquid tracked markets, for skill prompts."""
        rows = []
        for m in self.engine.markets:
            book = self.engine.last_books.get(m.key)
            if not book or book.mid is None:
                continue
            rows.append({
                "market": m.key,
                "title": m.title[:60],
                "outcome": m.outcome,
                "mid": round(book.mid, 3),
                "spread": round(book.spread, 3) if book.spread is not None else None,
                "bid_size": str(book.best_bid.size) if book.best_bid else "0",
                "ask_size": str(book.best_ask.size) if book.best_ask else "0",
            })
        # Tightest spreads first — the tradeable ones.
        rows.sort(key=lambda r: (r["spread"] is None, r["spread"] or 1.0))
        return rows[:limit]

    # --- non-sensitive actions ----------------------------------------------

    def pause(self) -> dict[str, Any]:
        self.engine.paused = True
        self._record("pause", {})
        return {"ok": True, "paused": True}

    def resume(self) -> dict[str, Any]:
        self.engine.paused = False
        self._record("resume", {})
        return {"ok": True, "paused": False}

    # --- sensitive actions (require confirm) ---------------------------------

    def request_set_risk_limit(self, name: str, value: float) -> dict[str, Any]:
        valid = {
            "max_position_per_market", "max_notional_per_market",
            "max_gross_notional", "max_daily_loss", "max_orders_per_min",
        }
        if name not in valid:
            return {"ok": False, "error": f"unknown limit {name!r}; valid: {sorted(valid)}"}
        return self._stage("set_risk_limit", {"name": name, "value": value},
                           f"Set risk limit {name} -> {value}")

    def request_deploy_capital(self, amount: float) -> dict[str, Any]:
        return self._stage("deploy_capital", {"amount": amount},
                           f"Deploy ${amount} of capital (raises gross-notional cap)")

    def request_place_order(self, venue: str, market_id: str, side: str,
                            size: float, price: float) -> dict[str, Any]:
        venue = venue.lower()
        side = side.lower()
        if venue not in {v.value for v in Venue}:
            return {"ok": False, "error": f"unknown venue {venue!r}"}
        if side not in {"buy", "sell"}:
            return {"ok": False, "error": "side must be 'buy' or 'sell'"}
        if not (0 < price < 1):
            return {"ok": False, "error": "price must be a probability in (0,1)"}
        if size <= 0:
            return {"ok": False, "error": "size must be positive"}
        return self._stage(
            "place_order",
            {"venue": venue, "market_id": market_id, "side": side, "size": size, "price": price},
            f"{side.upper()} {size} {venue}:{market_id} @ {price}",
        )

    def request_trip_kill_switch(self) -> dict[str, Any]:
        return self._stage("trip_kill_switch", {},
                           "TRIP KILL-SWITCH: reject all further orders this session")

    def request_go_live(self) -> dict[str, Any]:
        return self._stage("go_live", {},
                           "GO LIVE: switch from paper to real-money trading")

    def confirm(self, token: str) -> dict[str, Any]:
        pending = self._pending.pop(token, None)
        if pending is None:
            return {"ok": False, "error": "no pending action for that token (expired?)"}
        result = self._execute(pending.action, pending.args)
        self._record(pending.action, pending.args, confirmed=True)
        return {"ok": True, "executed": pending.summary, **result}

    def cancel(self, token: str) -> dict[str, Any]:
        existed = self._pending.pop(token, None) is not None
        return {"ok": True, "cancelled": existed}

    # --- internals -----------------------------------------------------------

    def _stage(self, action: str, args: dict, summary: str) -> dict[str, Any]:
        token = uuid.uuid4().hex[:8]
        self._pending[token] = _Pending(action=action, args=args, summary=summary)
        log.info("controller.staged", action=action, token=token, summary=summary)
        return {"needs_confirmation": True, "token": token, "summary": summary}

    def _execute(self, action: str, args: dict) -> dict[str, Any]:
        lim = self.engine.risk.limits
        if action == "set_risk_limit":
            name, value = args["name"], args["value"]
            cur = getattr(lim, name)
            newval = int(value) if name == "max_orders_per_min" else Decimal(str(value))
            setattr(lim, name, newval)
            return {"changed": name, "from": str(cur), "to": str(newval)}
        if action == "deploy_capital":
            amount = Decimal(str(args["amount"]))
            lim.max_gross_notional += amount
            return {"new_max_gross_notional": str(lim.max_gross_notional)}
        if action == "place_order":
            return self._queue_manual_order(args)
        if action == "trip_kill_switch":
            self.engine.risk.kill_switch = True
            return {"kill_switch": True}
        if action == "go_live":
            # Intentionally refuses unless venue creds exist; flips the engine flag.
            s = self.engine.settings
            if not (s.kalshi.configured or s.polymarket.configured):
                return {"ok": False, "error": "no venue credentials configured; cannot go live"}
            s.live = True
            return {"live": True, "warning": "real orders will now be placed"}
        return {"ok": False, "error": f"unknown action {action}"}

    def _queue_manual_order(self, args: dict) -> dict[str, Any]:
        key = f"{args['venue']}:{args['market_id']}"
        market = next((m for m in self.engine.markets if m.key == key), None)
        if market is None:
            market = Market(venue=Venue(args["venue"]), market_id=args["market_id"],
                            event_id=args["market_id"], title=args["market_id"], outcome="YES")
        order = Order(
            market=market,
            side=Side(args["side"]),
            size=Decimal(str(args["size"])),
            type=OrderType.LIMIT,
            price=float(args["price"]),
            reason="manual",
        )
        self.engine.manual_orders.append(order)
        log.info("controller.manual_order_queued", market=key, side=args["side"],
                 size=args["size"], price=args["price"])
        return {"queued": True, "market": key, "side": args["side"],
                "size": args["size"], "price": args["price"]}

    def _record(self, action: str, args: dict, confirmed: bool = False) -> None:
        self.audit.append({"ts": time.time(), "action": action, "args": args,
                           "confirmed": confirmed})
