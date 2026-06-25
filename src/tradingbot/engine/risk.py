"""Risk manager — the mandatory gate every order passes through.

For an unattended bot this is the most important component: it bounds the blast
radius of a buggy strategy or a bad market. Checks, in order:
  1. Kill-switch: if tripped (daily loss breached), reject everything.
  2. Rate limit: cap orders/minute.
  3. Per-market position + notional caps.
  4. Gross book notional cap.
A rejected order is annotated with the reason and never sent to a venue.
"""

from __future__ import annotations

import time
from collections import deque
from decimal import Decimal

import structlog

from tradingbot.config import RiskLimits
from tradingbot.engine.portfolio import Portfolio
from tradingbot.models import Order, OrderBook, OrderStatus, Side

log = structlog.get_logger(__name__)


class RiskManager:
    def __init__(self, limits: RiskLimits, portfolio: Portfolio):
        self.limits = limits
        self.portfolio = portfolio
        self.kill_switch = False
        self._order_times: deque[float] = deque()

    def update_kill_switch(self, marks: dict[str, OrderBook]) -> None:
        pnl = self.portfolio.session_pnl(marks)
        if pnl <= -self.limits.max_daily_loss and not self.kill_switch:
            self.kill_switch = True
            log.error("risk.kill_switch_tripped", session_pnl=str(round(pnl, 2)),
                      limit=str(self.limits.max_daily_loss))

    def approve(self, order: Order, marks: dict[str, OrderBook]) -> bool:
        reason = self._check(order, marks)
        if reason:
            order.status = OrderStatus.REJECTED
            order.reason = reason
            log.warning("risk.rejected", market=order.market.key, reason=reason)
            return False
        self._order_times.append(time.time())
        return True

    def _check(self, order: Order, marks: dict[str, OrderBook]) -> str | None:
        if self.kill_switch:
            return "kill_switch_active"

        # Rate limit (sliding 60s window).
        now = time.time()
        while self._order_times and now - self._order_times[0] > 60:
            self._order_times.popleft()
        if len(self._order_times) >= self.limits.max_orders_per_min:
            return "rate_limit_exceeded"

        signed = order.size if order.side is Side.BUY else -order.size
        pos = self.portfolio.position(order.market)
        projected = abs(pos.size + signed)
        if projected > self.limits.max_position_per_market:
            return f"position_cap {projected} > {self.limits.max_position_per_market}"

        price = order.price if order.price is not None else self._mark(order, marks)
        if price is None:
            return "no_price_to_value_order"
        proj_notional = Decimal(str(price)) * projected
        if proj_notional > self.limits.max_notional_per_market:
            return f"market_notional_cap {proj_notional:.2f} > {self.limits.max_notional_per_market}"

        gross = self.portfolio.gross_notional(marks) + Decimal(str(price)) * order.size
        if gross > self.limits.max_gross_notional:
            return f"gross_notional_cap {gross:.2f} > {self.limits.max_gross_notional}"

        return None

    @staticmethod
    def _mark(order: Order, marks: dict[str, OrderBook]) -> float | None:
        book = marks.get(order.market.key)
        return book.mid if book else None
