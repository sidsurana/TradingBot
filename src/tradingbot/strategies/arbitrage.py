"""Arbitrage strategy.

Two flavors, both venue-agnostic because they operate on the unified book:

1. **Mutually-exclusive (dutch book):** within one event, the cheapest asks of
   a complete set of outcomes should cost >= 1.0. If you can BUY every outcome
   for a combined price < 1.0, you lock a risk-free profit (one outcome resolves
   to 1.0). This naturally spans venues — outcome legs may live on Kalshi while
   others live on Polymarket, as long as they settle on the same real event.

2. **Cross-venue same-outcome:** the *same* outcome quoted on two venues. If
   venue A's best ask < venue B's best bid (beyond fees), buy A / sell B.

Edge is only acted on when it clears `min_edge` after a fee/slippage buffer.
Size is the min available depth across legs, capped by `max_size`.
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

import structlog

from tradingbot.fees import kalshi_taker_fee_per_share, taker_fee_per_share
from tradingbot.models import Market, Order, OrderType, Side, Venue
from tradingbot.strategies.base import Context, Strategy, register

log = structlog.get_logger(__name__)


@register
class ArbitrageStrategy(Strategy):
    name = "arbitrage"

    def __init__(self, *, min_edge: float = 0.02, max_size: Decimal = Decimal(20), **params):
        super().__init__(min_edge=min_edge, max_size=max_size, **params)
        self.min_edge = min_edge      # required NET profit per $1 set (leg-risk buffer)
        self.max_size = max_size

    def generate(self, ctx: Context) -> list[Order]:
        orders: list[Order] = []
        orders += self._dutch_book(ctx)
        orders += self._cross_venue(ctx)
        return orders

    @staticmethod
    def _leg_fee(market: Market, price: float) -> float:
        """Per-share taker fee for one leg, by venue (Kalshi vs Polymarket)."""
        if market.venue is Venue.KALSHI:
            return kalshi_taker_fee_per_share(price)
        return taker_fee_per_share(price, market.metadata.get("category"))

    # --- flavor 1: complete-set underpricing (dutch book) ------------------
    def _dutch_book(self, ctx: Context) -> list[Order]:
        """Buy one of every mutually-exclusive outcome when the cheapest asks
        sum to < $1 after fees. Correctness requires the COMPLETE set: buying a
        subset (e.g. 3 of 5 candidates) is NOT a locked profit, because a missing
        outcome could win and pay you zero. So we act only when every distinct
        outcome of the event currently has a tradeable ask.

        LEG RISK: the engine places the N legs sequentially. In paper mode they
        fill atomically against one book snapshot, so the set is always complete.
        LIVE, a partial fill (one leg fills, another's price moves) would leave an
        incomplete, un-arbitraged set. Mitigations here: (1) min_edge is required
        NET of fees, a cushion that absorbs small adverse moves between legs;
        (2) size is capped to the min depth across ALL legs, so every leg can
        fill at its observed ask. Residual risk (a leg vanishing mid-fire) needs
        either a venue atomic multi-leg order (Polymarket has none) or engine-
        level unwind of incomplete sets — required before live capital, and why
        size stays small. Loss is otherwise capped by full collateralization."""
        by_event: dict[str, list[Market]] = defaultdict(list)
        for m in ctx.markets:
            by_event[m.event_id].append(m)

        out: list[Order] = []
        for event_id, legs in by_event.items():
            # Cheapest ask per distinct outcome, and whether EVERY outcome of the
            # event is currently tradeable (has an ask).
            best: dict[str, tuple[Market, float, Decimal]] = {}
            outcomes_seen: set[str] = set()
            num_outcomes = 0
            for m in legs:
                outcomes_seen.add(m.outcome)
                num_outcomes = max(num_outcomes, int(m.metadata.get("num_outcomes") or 0))
                book = ctx.book(m)
                # A tradeable ask must exist and be strictly positive: a None or
                # 0.0 ask is a data glitch, not free money.
                if not book or not book.best_ask or not book.best_ask.price:
                    continue
                ask = book.best_ask
                cur = best.get(m.outcome)
                if cur is None or ask.price < cur[1]:
                    best[m.outcome] = (m, ask.price, ask.size)
            # Incomplete slate -> not a locked set. We must hold one of EVERY
            # outcome. `num_outcomes` (stamped per leg by the adapter) is the
            # ground truth: universe curation ranks/truncates individual legs and
            # can drop one leg of a multi-outcome event, after which the survivors
            # always sum to < $1 and would look like a locked set — but the dropped
            # outcome could win and pay us zero. Require every true outcome priced.
            # Fall back to "all present legs priced" only when the count is unknown.
            required = num_outcomes if num_outcomes else len(outcomes_seen)
            if len(best) < 2 or len(best) != required:
                continue
            cost = sum(p for _, p, _ in best.values())
            # Fee-aware per venue: Kalshi charges 0.07*p*(1-p)/contract; Polymarket
            # uses its category-dependent taker model. Both are near-zero at the
            # price extremes and largest mid-book. The winning leg redeems $1 free.
            fees = sum(self._leg_fee(m, p) for m, p, _ in best.values())
            net_cost = cost + fees
            edge = 1.0 - net_cost
            if edge < self.min_edge:
                continue
            size = min((s for _, _, s in best.values()), default=Decimal(0))
            size = min(size, self.max_size)
            if size <= 0:
                continue
            for m, price, _ in best.values():
                out.append(
                    Order(
                        market=m, side=Side.BUY, size=size, type=OrderType.LIMIT,
                        price=price, reason=f"dutch_book {event_id} edge={edge:.3f}",
                    )
                )
            log.info("arb.dutch_book", event_id=event_id, cost=round(cost, 4),
                     net_cost=round(net_cost, 4), edge=round(edge, 4), size=str(size))
        return out

    # --- flavor 2: same outcome, two venues --------------------------------
    def _cross_venue(self, ctx: Context) -> list[Order]:
        # Group markets that settle on the same real-world outcome. Prefer the
        # cross-venue `link_id` stamped by the EventLinker (the production path,
        # since venues' native ids never match); fall back to (event_id, outcome)
        # for same-venue groups and tests that share an event_id.
        groups: dict[str, list[Market]] = defaultdict(list)
        for m in ctx.markets:
            key = m.metadata.get("link_id") or f"{m.event_id}|{m.outcome.lower()}"
            groups[key].append(m)

        out: list[Order] = []
        for key, markets in groups.items():
            if len(markets) < 2:
                continue
            for i, buy_m in enumerate(markets):
                for sell_m in markets[i + 1 :]:
                    out += self._maybe_pair(ctx, buy_m, sell_m, key)
                    out += self._maybe_pair(ctx, sell_m, buy_m, key)
        return out

    def _maybe_pair(
        self, ctx: Context, buy_m: Market, sell_m: Market, group_key: str
    ) -> list[Order]:
        if buy_m.venue is sell_m.venue:
            return []  # cross-venue only; same-venue equivalents aren't an arb here
        bb, sb = ctx.book(buy_m), ctx.book(sell_m)
        if not bb or not sb or not bb.best_ask or not sb.best_bid:
            return []
        edge = sb.best_bid.price - bb.best_ask.price  # sell high, buy low
        if edge < self.min_edge:
            return []
        size = min(bb.best_ask.size, sb.best_bid.size, self.max_size)
        if size <= 0:
            return []
        log.info("arb.cross_venue", group=group_key,
                 buy=buy_m.venue.value, sell=sell_m.venue.value, edge=round(edge, 4))
        reason = f"cross_venue {buy_m.venue.value}->{sell_m.venue.value} edge={edge:.3f}"
        return [
            Order(market=buy_m, side=Side.BUY, size=size, type=OrderType.LIMIT,
                  price=bb.best_ask.price, reason=reason),
            Order(market=sell_m, side=Side.SELL, size=size, type=OrderType.LIMIT,
                  price=sb.best_bid.price, reason=reason),
        ]
