"""Portfolio: positions + PnL accounting across all venues.

Single source of truth for what the bot holds. The risk manager reads it before
approving orders; the engine updates it from fills.
"""

from __future__ import annotations

from decimal import Decimal

import structlog

from tradingbot.models import Fill, Market, OrderBook, Position

log = structlog.get_logger(__name__)


class Portfolio:
    def __init__(self, starting_cash: Decimal):
        self.starting_cash = starting_cash
        self.cash = starting_cash
        self.positions: dict[str, Position] = {}
        self._session_start_equity = starting_cash

    def rebaseline_session(self, equity: Decimal) -> None:
        """Reset the session-PnL baseline to the given equity.

        Called after persistence replay at startup (so restored lifetime PnL
        doesn't count against today's max_daily_loss) and by the risk manager
        on UTC day rollover (so 'daily loss' means daily)."""
        self._session_start_equity = equity

    def position(self, market: Market) -> Position:
        return self.positions.setdefault(market.key, Position(market=market))

    def record_fill(self, market: Market, fill: Fill, log_fill: bool = True) -> None:
        pos = self.position(market)
        pos.apply(fill)
        # Cash out/in for the traded notional + fees.
        notional = Decimal(str(fill.price)) * fill.size
        if fill.side.value == "buy":
            self.cash -= notional
        else:
            self.cash += notional
        self.cash -= fill.fee
        if log_fill:  # quiet during startup replay of persisted fills
            log.info("portfolio.fill", market=market.key, side=fill.side.value,
                     size=str(fill.size), price=fill.price, cash=str(round(self.cash, 4)))

    def gross_notional(self, marks: dict[str, OrderBook]) -> Decimal:
        total = Decimal(0)
        for key, pos in self.positions.items():
            if pos.size == 0:
                continue
            mark = self._mark(key, marks, pos.avg_price)
            total += Decimal(str(mark)) * abs(pos.size)
        return total

    def equity(self, marks: dict[str, OrderBook]) -> Decimal:
        eq = self.cash
        for key, pos in self.positions.items():
            if pos.size == 0:
                continue
            mark = self._mark(key, marks, pos.avg_price)
            eq += pos.unrealized_pnl(mark) + Decimal(str(pos.avg_price)) * pos.size
        return eq

    def session_pnl(self, marks: dict[str, OrderBook]) -> Decimal:
        return self.equity(marks) - self._session_start_equity

    @staticmethod
    def _mark(key: str, marks: dict[str, OrderBook], fallback: float) -> float:
        book = marks.get(key)
        if book and book.mid is not None:
            return book.mid
        return fallback
