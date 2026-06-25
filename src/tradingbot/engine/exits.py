"""Stop-loss / take-profit exit manager.

Runs every tick (including while the bot is paused — risk exits must never be
skipped). For each open position it computes the move from the average entry
price toward the current mark and, if that move breaches the configured
stop-loss or take-profit threshold, emits a marketable order to flatten the
position.

Move is measured in entry-relative terms so the thresholds mean the same thing
regardless of the entry price:

    pnl_fraction = direction * (mark - avg_price) / avg_price

  - long YES at 0.40, mark 0.30  -> -0.25  (down 25%)
  - long YES at 0.40, mark 0.60  -> +0.50  (up 50%)

Close when pnl_fraction <= -stop_loss_pct, or pnl_fraction >= take_profit_pct.
"""

from __future__ import annotations

from decimal import Decimal

import structlog

from tradingbot.config import ExitSettings
from tradingbot.models import Order, OrderBook, OrderType, Position, Side

log = structlog.get_logger(__name__)


class ExitManager:
    def __init__(self, settings: ExitSettings):
        self.settings = settings

    @property
    def active(self) -> bool:
        s = self.settings
        return s.enabled and (s.stop_loss_pct > 0 or s.take_profit_pct > 0)

    def evaluate(
        self, positions: dict[str, Position], books: dict[str, OrderBook]
    ) -> list[Order]:
        if not self.active:
            return []
        orders: list[Order] = []
        for key, pos in positions.items():
            if abs(pos.size) < self.settings.min_size_to_exit or pos.avg_price <= 0:
                continue
            book = books.get(key)
            mark = book.mid if book else None
            if mark is None:
                continue
            order = self._maybe_exit(pos, book, mark)
            if order is not None:
                orders.append(order)
        return orders

    def _maybe_exit(self, pos: Position, book: OrderBook, mark: float) -> Order | None:
        direction = 1 if pos.size > 0 else -1
        pnl_fraction = direction * (mark - pos.avg_price) / pos.avg_price

        reason = None
        sl, tp = self.settings.stop_loss_pct, self.settings.take_profit_pct
        if sl > 0 and pnl_fraction <= -sl:
            reason = f"stop_loss {pnl_fraction:.1%}"
        elif tp > 0 and pnl_fraction >= tp:
            reason = f"take_profit {pnl_fraction:.1%}"
        if reason is None:
            return None

        # Flatten: long -> SELL into the bid; short -> BUY from the ask.
        if pos.size > 0:
            side, level = Side.SELL, book.best_bid
        else:
            side, level = Side.BUY, book.best_ask
        if level is None:
            return None

        log.info("exit.triggered", market=pos.market.key, reason=reason,
                 size=str(abs(pos.size)), avg=round(pos.avg_price, 4), mark=round(mark, 4))
        return Order(
            market=pos.market,
            side=side,
            size=abs(pos.size),
            type=OrderType.LIMIT,
            price=level.price,
            reason=reason,
        )
