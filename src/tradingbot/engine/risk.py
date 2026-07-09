"""Risk manager — the mandatory gate every order passes through.

For an unattended bot this is the most important component: it bounds the blast
radius of a buggy strategy or a bad market.

Order of checks (IMPORTANT — reducing vs increasing orders):
  A position-REDUCING order (signed size opposes the current position's sign and
  does not flip through zero) is an exit — typically a stop-loss flatten — and
  must never be blocked by the very limits it exists to protect. Reducing orders
  therefore SKIP the kill-switch, the rate limit, the gross-notional cap and the
  correlation filter, but still pass the per-market position-cap sanity check.
  Approved reducing orders still count toward the rate-limit window so the
  budget stays honest.

  Increasing orders go through everything, in order:
    1. Kill-switch: if tripped (daily loss breached), reject.
    2. Rate limit: cap orders/minute.
    3. Per-market position + notional caps.
    4. Gross book notional cap.
    5. Correlation filter: no same-direction exposure across a correlation group.
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
        self._day_key = self._utc_day()

    @staticmethod
    def _utc_day() -> int:
        # UTC day index (unix days). Derived from time.time() so tests can
        # simulate rollover by adjusting the stored key.
        return int(time.time() // 86400)

    def update_kill_switch(self, marks: dict[str, OrderBook]) -> None:
        # UTC day rollover: 'daily' loss means DAILY — re-baseline the session
        # PnL to current equity and re-arm (reset) the kill switch, so
        # yesterday's breach doesn't permanently brick the bot. Without this,
        # max_daily_loss would measure lifetime drawdown.
        day = self._utc_day()
        if day != self._day_key:
            self._day_key = day
            self.portfolio.rebaseline_session(self.portfolio.equity(marks))
            if self.kill_switch:
                log.info("risk.kill_switch_reset_new_day", day=day)
            self.kill_switch = False

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
        # Every approved order (reducing included) counts toward the rate-limit
        # window so the per-minute budget stays honest.
        self._order_times.append(time.time())
        return True

    def _is_reducing(self, order: Order) -> bool:
        """True if the order opposes the current position and does not flip it
        through zero — i.e. it strictly reduces (or flattens) exposure."""
        signed = order.size if order.side is Side.BUY else -order.size
        pos = self.portfolio.position(order.market)
        if pos.size == 0:
            return False
        return (pos.size > 0) != (signed > 0) and abs(signed) <= abs(pos.size)

    def _check(self, order: Order, marks: dict[str, OrderBook]) -> str | None:
        signed = order.size if order.side is Side.BUY else -order.size
        pos = self.portfolio.position(order.market)
        reducing = self._is_reducing(order)

        # Reducing orders (exits) bypass kill-switch / rate-limit / gross /
        # correlation — a stop-loss must always be able to flatten. See module
        # docstring for the full check-order contract.
        if not reducing:
            if self.kill_switch:
                return "kill_switch_active"

            # Rate limit (sliding 60s window).
            now = time.time()
            while self._order_times and now - self._order_times[0] > 60:
                self._order_times.popleft()
            if len(self._order_times) >= self.limits.max_orders_per_min:
                return "rate_limit_exceeded"

        # Per-market position cap: sanity check that applies to ALL orders.
        projected = abs(pos.size + signed)
        if projected > self.limits.max_position_per_market:
            return f"position_cap {projected} > {self.limits.max_position_per_market}"

        if reducing:
            # Notional/gross/correlation checks only bound NEW risk; a reducing
            # order shrinks exposure by construction, so it passes here. (It also
            # never needs a mark to be valued.)
            return None

        price = order.price if order.price is not None else self._mark(order, marks)
        if price is None:
            return "no_price_to_value_order"
        proj_notional = Decimal(str(price)) * projected
        if proj_notional > self.limits.max_notional_per_market:
            return f"market_notional_cap {proj_notional:.2f} > {self.limits.max_notional_per_market}"

        # Gross cap: only risk-increasing orders add to gross. (A reducing order
        # returned earlier — otherwise a stop-loss flatten would be rejected
        # exactly when the book sits at the cap.)
        gross = self.portfolio.gross_notional(marks) + Decimal(str(price)) * order.size
        if gross > self.limits.max_gross_notional:
            return f"gross_notional_cap {gross:.2f} > {self.limits.max_gross_notional}"

        # Correlation filter: reject a risk-increasing order whose resulting
        # direction matches an existing nonzero position in another member of
        # its correlation group.
        resulting = pos.size + signed
        for group in self.limits.correlation_groups:
            if order.market.key not in group:
                continue
            for other_key in group:
                if other_key == order.market.key:
                    continue
                other = self.portfolio.positions.get(other_key)
                if other is None or other.size == 0:
                    continue
                if (other.size > 0) == (resulting > 0):
                    return "correlated_exposure"

        return None

    @staticmethod
    def _mark(order: Order, marks: dict[str, OrderBook]) -> float | None:
        book = marks.get(order.market.key)
        return book.mid if book else None
