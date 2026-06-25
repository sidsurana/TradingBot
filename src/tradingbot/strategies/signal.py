"""Signal/model strategy — directional positions sized by fractional Kelly.

Reads fair-value views from a SignalStore (pushed by the agent's alpha/regime
skills or any model) and takes a directional position when the market price
diverges from the believed fair value by at least `min_edge`. Position size is
**fractional Kelly × confidence** of an allocated bankroll, capped, and built
toward the target without churning. Exits are handled by the stop-loss /
take-profit ExitManager (enable `TB_EXIT_*` when running this strategy).

Kelly for a binary contract bought at price c that pays 1 with true prob p:
    f* = (p - c) / (1 - c)          # buy YES when p > c
For the short side (sell YES at price c, i.e. you think p < c):
    f* = (c - p) / c                # buy NO equivalent
We scale by `kelly_fraction` (e.g. 0.25 = quarter-Kelly) and `confidence`.
"""

from __future__ import annotations

import time
from decimal import Decimal

import structlog

from tradingbot.models import Order, OrderType, Side
from tradingbot.strategies.base import Context, Strategy, register

log = structlog.get_logger(__name__)


@register
class SignalStrategy(Strategy):
    name = "signal"

    def __init__(self, *, kelly_fraction: float = 0.25, bankroll: Decimal = Decimal(500),
                 min_edge: float = 0.05, min_confidence: float = 0.5,
                 max_signal_age_s: float = 3600.0, max_position: Decimal = Decimal(50),
                 **params):
        super().__init__(kelly_fraction=kelly_fraction, bankroll=bankroll, min_edge=min_edge,
                         min_confidence=min_confidence, max_signal_age_s=max_signal_age_s,
                         max_position=max_position, **params)
        self.kelly_fraction = kelly_fraction
        self.bankroll = Decimal(bankroll)
        self.min_edge = min_edge
        self.min_confidence = min_confidence
        self.max_signal_age_s = max_signal_age_s
        self.max_position = Decimal(max_position)
        self.store = None  # injected by the engine (a SignalStore)

    def set_store(self, store) -> None:
        self.store = store

    def generate(self, ctx: Context) -> list[Order]:
        if self.store is None:
            return []
        now = time.time()
        by_key = {m.key: m for m in ctx.markets}
        orders: list[Order] = []
        for key, sig in self.store.active(self.max_signal_age_s, now).items():
            if sig.confidence < self.min_confidence:
                continue
            market = by_key.get(key)
            book = ctx.book(market) if market else None
            if not market or not book or not book.best_bid or not book.best_ask:
                continue
            order = self._target_order(market, book, sig, ctx)
            if order is not None:
                orders.append(order)
        return orders

    def _target_order(self, market, book, sig, ctx: Context) -> Order | None:
        p = sig.fair_value
        ask, bid = book.best_ask.price, book.best_bid.price

        if p > ask + self.min_edge and ask < 1.0:        # underpriced -> buy YES
            direction, side, price = 1, Side.BUY, ask
            f = (p - ask) / (1.0 - ask)
        elif p < bid - self.min_edge and bid > 0.0:      # overpriced -> sell YES
            direction, side, price = -1, Side.SELL, bid
            f = (bid - p) / bid
        else:
            return None

        f_scaled = max(0.0, min(1.0, self.kelly_fraction * sig.confidence * f))
        contracts = int(float(self.bankroll) * f_scaled / price) if price > 0 else 0
        target = Decimal(contracts) * direction
        if abs(target) > self.max_position:
            target = self.max_position * direction
        if target == 0:
            return None

        pos = ctx.positions.get(market.key)
        current = pos.size if pos else Decimal(0)
        delta = target - current
        # Only build toward the target in the signal's direction (no churn;
        # exits/flips unwind). delta must point the same way as the signal.
        if direction > 0 and delta > 0:
            size = delta
        elif direction < 0 and delta < 0:
            size = -delta
        else:
            return None

        log.info("signal.order", market=market.key, side=side.value, size=str(size),
                 fair_value=round(p, 3), price=price, kelly=round(f_scaled, 3))
        return Order(market=market, side=side, size=size, type=OrderType.LIMIT,
                     price=price, reason=f"signal fv={p:.2f} conf={sig.confidence:.2f} "
                                         f"f={f_scaled:.3f}")
