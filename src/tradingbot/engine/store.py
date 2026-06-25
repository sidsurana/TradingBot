"""Durable state — event-sourced fills in SQLite.

Every fill is appended here; on startup the engine replays them in order to
rebuild the portfolio (positions, average prices, realized PnL, cash) exactly as
it was. That means a crash or restart doesn't lose positions or reset the daily
goal baseline — the prerequisite for leaving the bot running unattended.

The fills table doubles as a durable audit/analysis log of everything that
traded. Uses stdlib sqlite3 (no dependency); writes are tiny and local.
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal

import structlog

from tradingbot.models import Fill, Market, Side, Venue

log = structlog.get_logger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS fills (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              REAL,
    venue           TEXT,
    market_id       TEXT,
    event_id        TEXT,
    title           TEXT,
    outcome         TEXT,
    side            TEXT,
    size            TEXT,
    price           REAL,
    fee             TEXT,
    order_client_id TEXT
);
"""


class Store:
    def __init__(self, path: str):
        self.path = path
        self._conn = sqlite3.connect(path)
        self._conn.execute(_SCHEMA)
        self._conn.commit()

    def record_fill(self, market: Market, fill: Fill) -> None:
        self._conn.execute(
            "INSERT INTO fills (ts, venue, market_id, event_id, title, outcome, "
            "side, size, price, fee, order_client_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (fill.ts, market.venue.value, market.market_id, market.event_id, market.title,
             market.outcome, fill.side.value, str(fill.size), fill.price, str(fill.fee),
             fill.order_client_id),
        )
        self._conn.commit()

    def load_fills(self) -> list[tuple[Market, Fill]]:
        """All fills in insertion order, as (Market, Fill) pairs for replay."""
        rows = self._conn.execute(
            "SELECT ts, venue, market_id, event_id, title, outcome, side, size, "
            "price, fee, order_client_id FROM fills ORDER BY id ASC"
        ).fetchall()
        out: list[tuple[Market, Fill]] = []
        for (ts, venue, market_id, event_id, title, outcome, side, size, price, fee,
             ocid) in rows:
            market = Market(venue=Venue(venue), market_id=market_id, event_id=event_id,
                            title=title, outcome=outcome)
            fill = Fill(market_key=market.key, side=Side(side), size=Decimal(size),
                        price=price, fee=Decimal(fee), order_client_id=ocid, ts=ts)
            out.append((market, fill))
        return out

    def close(self) -> None:
        self._conn.close()
