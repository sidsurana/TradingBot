"""Certainty Carry — harvest the near-certain-outcome carry on Polymarket.

Thesis: outcome tokens trading in a narrow high band (~0.94-0.96) are
structurally *underpriced* relative to their true resolution probability. Two
well-documented forces push them there: the favorite-longshot bias (punters
overpay for longshots, so favorites are left cheap) and the zero-yield of the
USDC collateral locked until resolution (holders demand a discount for the
dead time). Buying the favorite token and holding to on-chain resolution
collects the gap to $1.

HONEST GUARDRAILS (read before trusting this):
  * The edge is THIN. At ask=0.95 the gross edge to $1 is +5c, but a token this
    priced only clears our filters when the *annualized* carry is real; per
    trade the expected value after the fee is roughly +1-2c. It is a carry
    harvest, not a mispricing you can flip.
  * Every position must be sized to survive a full -100% loss. A "near-certain"
    token still resolves to $0 sometimes, and it does so as a GAP — you cannot
    price-stop your way out through a resolution headline. Hence
    max_notional_per_position and the sleeve cap.
  * The ONLY managed exit is the complement lock: when a held token
    deteriorates (mid below complement_exit_below) we buy the OTHER outcome to
    complete a $1 set, which is a hedge, not a stop. If selling our token at the
    bid recovers more than the set lock, we do that instead. We never place a
    naive mid/price stop (it whipsaws and can't fill through the gap). Winners
    that simply drift toward 1.0 are left for settlement to redeem at $1.

The strategy is a pure function of Context; the engine handles execution.
"""

from __future__ import annotations

import math
import time
from collections import defaultdict
from decimal import Decimal

import structlog

from tradingbot.config import CertaintyCarrySettings
from tradingbot.models import Market, Order, OrderBook, OrderType, Side, Venue
from tradingbot.strategies.base import Context, Strategy, register

log = structlog.get_logger(__name__)

_FEE_RATE = 0.001   # taker fee assumption, charged on notional (ask * size)

# Leading filler words that carry no subject information ("Will BITCOIN…" must
# group on BITCOIN, not on "Will"). Stripped case-insensitively before we pick
# the subject token.
_STOPWORDS = frozenset({"will", "the", "is", "are", "does", "do", "can",
                        "a", "an", "to"})
# Canonicalize the same underlying spelled different ways so "Bitcoin" and
# "BTC" share one correlation group.
_ALIAS = {
    "bitcoin": "btc", "btc": "btc",
    "ethereum": "eth", "eth": "eth", "ether": "eth",
    "solana": "sol", "sol": "sol",
}
_PUNCT = ".,!?:;\"'()[]"


@register
class CertaintyCarryStrategy(Strategy):
    name = "certainty_carry"

    def __init__(self, *, cfg: CertaintyCarrySettings | None = None,
                 clock=time.time, cooldown_s: float = 60.0, **params):
        super().__init__(**params)
        self.cfg = cfg or CertaintyCarrySettings()
        self._clock = clock
        self._cooldown_s = cooldown_s
        # market.key -> last time we emitted an order for it (dedupe in-flight
        # re-emits of the same entry/exit while a fill is pending).
        self._last_acted: dict[str, float] = {}
        # market.key -> cumulative exit size already emitted (lock or sell) that
        # has not yet reduced/covered the position. Suppresses re-emitting an
        # exit for shares an in-flight order already covers — independent of the
        # time cooldown.
        self._exit_outstanding: dict[str, Decimal] = {}

    # --- helpers ----------------------------------------------------------

    def _group_key(self, market: Market) -> tuple[str, str]:
        """Correlation group: (category, subject). Subject is the first
        meaningful title token after stripping leading stopwords, normalized
        through a small ticker/alias map so the same underlying collapses to
        one canonical subject. Falls back to the category when no meaningful
        token remains."""
        cat = str(market.metadata.get("category", "") or "")
        subject: str | None = None
        for raw in market.title.split():
            w = raw.strip(_PUNCT).lower()
            if not w or w in _STOPWORDS:
                continue
            subject = _ALIAS.get(w, w)
            break
        if subject is None:
            subject = cat.lower()
        return (cat, subject)

    def _complement(self, market: Market, ctx: Context) -> tuple[Market | None, OrderBook | None]:
        """The other outcome token of the same event (shares event_id, different
        market_id) and its book, if present."""
        for m in ctx.markets:
            if (m.venue is Venue.POLYMARKET and m.event_id == market.event_id
                    and m.market_id != market.market_id):
                return m, ctx.books.get(m.key)
        return None, None

    def _cooling_down(self, key: str, now: float) -> bool:
        last = self._last_acted.get(key)
        return last is not None and (now - last) < self._cooldown_s

    def _act(self, key: str, now: float, order: Order) -> Order:
        self._last_acted[key] = now
        return order

    # --- main -------------------------------------------------------------

    def generate(self, ctx: Context) -> list[Order]:
        now = self._clock()
        cfg = self.cfg
        orders: list[Order] = []

        # Snapshot existing open Polymarket exposure for concentration/sleeve.
        open_positions = [p for p in ctx.positions.values()
                          if p.market.venue is Venue.POLYMARKET and p.size != 0]
        open_count = len(open_positions)
        group_counts: dict[tuple[str, str], int] = defaultdict(int)
        sleeve_used = 0.0
        for p in open_positions:
            group_counts[self._group_key(p.market)] += 1
            sleeve_used += abs(float(p.size)) * p.avg_price

        for market in ctx.markets:
            if market.venue is not Venue.POLYMARKET:
                continue
            if self._cooling_down(market.key, now):
                continue

            pos = ctx.positions.get(market.key)
            pos_size = pos.size if pos else Decimal(0)

            if pos_size > 0:
                exit_order = self._maybe_exit(market, pos_size, ctx, now,
                                              sleeve_used)
                if exit_order is not None:
                    orders.append(exit_order)
                continue
            if pos_size < 0:
                continue  # this strategy never carries short exposure

            entry = self._maybe_enter(market, ctx, now, open_count,
                                      group_counts, sleeve_used)
            if entry is not None:
                order, notional, group = entry
                orders.append(order)
                open_count += 1
                group_counts[group] += 1
                sleeve_used += notional

        return orders

    # --- entry ------------------------------------------------------------

    def _maybe_enter(self, market: Market, ctx: Context, now: float,
                     open_count: int, group_counts: dict, sleeve_used: float):
        cfg = self.cfg
        book = ctx.books.get(market.key)
        if not book or not book.best_bid or not book.best_ask:
            return None
        ask = book.best_ask.price
        bid = book.best_bid.price

        if (ask - bid) > cfg.max_spread:
            return None
        if not (cfg.price_min <= ask <= cfg.price_max):
            return None

        md = market.metadata
        if float(md.get("volume", 0) or 0) < cfg.min_volume_24h:
            return None
        if md.get("is_sports"):
            return None

        end_ts = float(md.get("end_ts", 0) or 0)
        if end_ts <= 0:
            return None  # can't assess time-to-resolution -> skip
        seconds = end_ts - now
        hours = seconds / 3600.0
        if hours < cfg.min_hours_to_resolution:
            return None
        if seconds > cfg.max_days_to_resolution * 86400.0:
            return None

        if cfg.require_uma_ok and md.get("uma_status") == "disputed":
            return None

        title_l = market.title.lower()
        if any(kw in title_l for kw in cfg.veto_keywords):
            return None

        if cfg.allowed_categories and md.get("category") not in cfg.allowed_categories:
            return None

        # Annualized carry gate. net_edge = gap to $1 net of the taker fee.
        days = max(hours / 24.0, 1e-9)
        net_edge = (1.0 - ask) - _FEE_RATE * ask
        annualized = (net_edge / ask) * (365.0 / days)
        if net_edge <= 0 or annualized < cfg.min_annualized_carry:
            return None

        # Concentration.
        if open_count >= cfg.max_positions:
            return None
        group = self._group_key(market)
        if group_counts.get(group, 0) >= cfg.max_per_group:
            return None

        # Sizing: notional cap, sleeve cap, and never more than resting size.
        shares = min(
            math.floor(float(cfg.max_notional_per_position) / ask),
            math.floor(cfg.sleeve_fraction * ctx.equity / ask) if ctx.equity > 0 else 0,
            int(book.best_ask.size),
        )
        if shares < 1:
            return None

        notional = shares * ask
        if sleeve_used + notional > cfg.sleeve_fraction * ctx.equity:
            return None

        log.info("carry.entry", market=market.key, ask=round(ask, 3),
                 annualized=round(annualized, 2), shares=shares)
        order = Order(market=market, side=Side.BUY, size=Decimal(shares),
                      type=OrderType.LIMIT, price=ask,
                      reason=f"carry ask={ask:.3f} ann={annualized:.2f}")
        return self._act(market.key, now, order), notional, group

    # --- exit / hedge -----------------------------------------------------

    def _maybe_exit(self, market: Market, pos_size: Decimal, ctx: Context,
                    now: float, sleeve_used: float) -> Order | None:
        cfg = self.cfg
        book = ctx.books.get(market.key)
        if not book or not book.best_bid or not book.best_ask:
            return None

        comp_market, comp_book = self._complement(market, ctx)
        comp_pos = ctx.positions.get(comp_market.key) if comp_market is not None else None
        comp_held = comp_pos.size if comp_pos and comp_pos.size > 0 else Decimal(0)

        # (a)/(c) The complement is already held in >= this leg's size: the two
        # legs form a COMPLETE $1 set (this leg is fully hedged, or this leg IS
        # the hedge of the other). Emit nothing for either leg — leave it for
        # settlement/redemption. This is what makes the lock idempotent: without
        # it, the cheap hedge leg would look "deteriorated" every tick and each
        # side would keep re-buying the other, doubling both legs each cooldown.
        if comp_held >= pos_size:
            return None

        mid = book.mid
        if mid is None or mid >= cfg.complement_exit_below:
            return None  # healthy holding — leave winners for settlement to redeem

        # Only act on the UNHEDGED remainder, and subtract any exit already
        # in-flight so a second tick (even past the cooldown) never doubles it.
        outstanding = self._exit_outstanding.get(market.key, Decimal(0))
        remainder = pos_size - comp_held - outstanding
        if remainder <= 0:
            return None

        bid = book.best_bid.price
        # (b) complement lock: buy the OTHER outcome at its ask to complete a
        #     $1 set -> effective value 1 - comp_ask (the hedge, not a stop).
        # (else) sell our token at the bid -> recovers `bid`.
        comp_ask = comp_book.best_ask.price if (comp_book and comp_book.best_ask) else None
        comp_value = (1.0 - comp_ask) if comp_ask is not None else None

        if comp_value is not None and comp_value > bid:
            lock = self._lock_size(remainder, comp_ask, comp_book,
                                   sleeve_used, ctx.equity)
            if lock >= 1:
                self._exit_outstanding[market.key] = outstanding + Decimal(lock)
                self._last_acted[market.key] = now
                log.info("carry.complement_lock", market=market.key,
                         comp=comp_market.key, comp_ask=round(comp_ask, 3),
                         size=lock)
                return Order(market=comp_market, side=Side.BUY, size=Decimal(lock),
                             type=OrderType.LIMIT, price=comp_ask,
                             reason="carry_complement_lock")
            # Complement book/budget can't fill the lock — fall through and
            # recover what the bid can take instead of overshooting.

        sell = min(int(remainder), int(book.best_bid.size))
        if sell >= 1:
            self._exit_outstanding[market.key] = outstanding + Decimal(sell)
            self._last_acted[market.key] = now
            log.info("carry.stop_bid", market=market.key, bid=round(bid, 3),
                     size=sell)
            return Order(market=market, side=Side.SELL, size=Decimal(sell),
                         type=OrderType.LIMIT, price=bid,
                         reason="carry_stop_bid")
        return None

    def _lock_size(self, remainder: Decimal, comp_ask: float,
                   comp_book: OrderBook, sleeve_used: float,
                   equity: float) -> int:
        """Largest complement BUY that completes part of the $1 set without
        breaching book depth, the per-position notional cap, or the remaining
        sleeve budget. Downsizes (partial lock) rather than overshooting."""
        cfg = self.cfg
        caps = [
            int(remainder),
            int(comp_book.best_ask.size) if comp_book.best_ask else 0,
            math.floor(float(cfg.max_notional_per_position) / comp_ask),
        ]
        if equity > 0:
            budget = cfg.sleeve_fraction * equity - sleeve_used
            caps.append(math.floor(budget / comp_ask))
        return max(0, min(caps))
