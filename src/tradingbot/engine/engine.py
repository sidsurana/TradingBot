"""Engine — the autonomous event loop.

Each tick:
  1. Refresh order books for the tracked market universe.
  2. Build a read-only Context and run every enabled strategy.
  3. Pass each desired order through the risk manager.
  4. Execute approved orders (paper sim or live venue).
  5. Drain resulting fills into the portfolio; update the kill-switch.

The loop is defensive: a strategy or venue exception is logged and the tick
continues. It runs until cancelled (Ctrl-C / SIGTERM).
"""

from __future__ import annotations

import asyncio
import re
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal

import structlog

from tradingbot.config import Settings
from tradingbot.engine.exits import ExitManager
from tradingbot.engine.goals import GoalTracker
from tradingbot.engine.portfolio import Portfolio
from tradingbot.engine.risk import RiskManager
from tradingbot.engine.router import ExchangeRouter
from tradingbot.engine.linker import EventLinker
from tradingbot.engine.signals import SignalStore
from tradingbot.engine.universe import UniverseSelector
from tradingbot.exchanges.base import Exchange
from tradingbot.exchanges.paper import PaperExchange
from tradingbot.models import (
    Fill,
    Market,
    Order,
    OrderBook,
    OrderStatus,
    OrderType,
    Side,
    Venue,
)
from tradingbot.strategies.base import Context, Strategy

log = structlog.get_logger(__name__)


def _parse_edge(reason: str) -> float | None:
    """Pull the fractional edge out of a dutch_book reason ('… edge=0.031')."""
    m = re.search(r"edge[=\s]([0-9.]+)", reason or "")
    return float(m.group(1)) if m else None


class Engine:
    def __init__(
        self,
        settings: Settings,
        router: ExchangeRouter,
        strategies: list[Strategy],
        stream=None,
        notifier=None,
    ):
        self.settings = settings
        self.router = router
        self.strategies = strategies
        self.stream = stream   # optional StreamManager for WebSocket books
        self.notifier = notifier  # optional per-venue TradeNotifier (Telegram)
        self.portfolio = Portfolio(settings.paper_starting_cash)
        self.risk = RiskManager(settings.risk, self.portfolio)
        self.goals = GoalTracker(settings.goals)
        self.exits = ExitManager(settings.exits)
        self.universe = UniverseSelector(settings.universe)
        self.linker = EventLinker(settings.links)
        self.signals = SignalStore()
        # Hand the shared signal store to any signal strategy (decouples the
        # signal source — agent/model — from the executing strategy).
        for strat in self.strategies:
            if hasattr(strat, "set_store"):
                strat.set_store(self.signals)
        self._locked_day = ""        # day on which lock-gains already fired
        self._paused_by_goals = False
        # Per-venue trade tally for the daily summary (reset each summary).
        self._daily_trades: dict = defaultdict(lambda: {"buys": 0, "sells": 0})

        # In paper mode the execution layer simulates fills; in live mode the
        # router places real orders on the venues.
        self.exec: Exchange
        self._paper: PaperExchange | None = None
        if settings.live:
            self.exec = router
            log.warning("engine.LIVE_MODE", msg="real orders will be placed")
        else:
            self._paper = PaperExchange(router)
            self.exec = self._paper
            log.info("engine.paper_mode", starting_cash=str(settings.paper_starting_cash))

        # Paper fills simulate against the live stream cache (no REST per order).
        if self._paper is not None and self.stream is not None:
            self._paper.set_book_source(lambda m: self.stream.book(m.key))

        self.markets: list[Market] = []
        # Every market we've ever tracked or traded, so a fill still resolves even
        # after its market leaves the curated universe (e.g. an exit closing it).
        self._market_registry: dict[str, Market] = {}
        self._fill_cursor = 0
        self._stop = asyncio.Event()
        self._trade_lock = asyncio.Lock()  # serializes the periodic loop vs the reactor

        # Durable state: replay persisted fills to rebuild the portfolio exactly.
        self.store = None
        if settings.persistence.enabled:
            from tradingbot.engine.store import Store

            self.store = Store(settings.persistence.path)
            restored = self.store.load_fills()
            for market, fill in restored:
                self._market_registry[market.key] = market
                self.portfolio.record_fill(market, fill, log_fill=False)
                # Rehydrate the paper layer's own book so held positions can
                # still settle after a restart. restore_fill mirrors the fill
                # into paper._positions WITHOUT touching paper.fills, so the
                # fill cursor stays put and _drain_fills never double-counts.
                if self._paper is not None:
                    self._paper.restore_fill(market, fill)
            open_positions = [p for p in self.portfolio.positions.values() if p.size != 0]
            if restored:
                # Replay restored LIFETIME PnL; re-baseline so max_daily_loss
                # measures this session, not all history since first launch.
                self.portfolio.rebaseline_session(self.portfolio.equity({}))
                log.info("persistence.restored", fills=len(restored),
                         open_positions=len(open_positions),
                         cash=str(round(self.portfolio.cash, 2)))
        # Optional candle provider for directional strategies: a zero-arg
        # callable returning market_key -> interval -> tuple[Candle, ...]
        # (a cheap snapshot of the data venue's in-memory cache). Set by
        # main.py when the DATA venue is wired.
        self.candle_source = None
        self.paused = False          # agent/operator can halt order placement live
        self.last_books: dict[str, OrderBook] = {}  # latest snapshot, for introspection
        self.manual_orders: list = []  # discretionary orders queued by the agent/operator
        self._quotes: dict[tuple, object] = {}  # (market_key, side) -> live MM resting quote

    async def discover(self, event_filter: str | None = None, limit: int | None = None) -> None:
        raw = await self.router.list_markets(event_filter=event_filter)
        selected = self.universe.select(raw)
        if limit is not None:
            selected = selected[:limit]
        # Always keep markets we hold a position in, even if they fell out of the
        # volume-ranked universe. Otherwise they get no fresh book and the exit
        # manager silently skips them (no stop-loss/take-profit) — a position could
        # then run past its stop unprotected, which is exactly what must never happen.
        have = {m.key for m in selected}
        raw_by_key = {m.key: m for m in raw}
        for key, pos in self.portfolio.positions.items():
            if key not in have and abs(pos.size) > 0:
                mkt = raw_by_key.get(key) or self._market_registry.get(key)
                if mkt is not None:
                    selected.append(mkt)
                    have.add(key)
        self.linker.annotate(selected)   # stamp cross-venue link_ids for arbitrage
        self.markets = selected
        self._market_registry.update({m.key: m for m in selected})
        log.info("engine.universe", discovered=len(raw), tracked=len(self.markets),
                 cross_venue_links=self.linker.count)

    def stop(self) -> None:
        self._stop.set()

    async def _announce_online(self) -> None:
        """Ping each configured venue bot on startup with its current equity."""
        if self.notifier is None or not self.notifier.any_configured:
            return
        for venue in (Venue.KALSHI, Venue.POLYMARKET):
            if not self.notifier.configured_for(venue):
                continue
            eq, pnl = await self._venue_equity_pnl(venue)
            sign = "+" if pnl >= 0 else "−"
            await self.notifier.announce(
                venue, f"online — watching for arbs.\nEquity ${eq:.2f}  |  P&L {sign}${abs(pnl):.2f}")

    async def _daily_summary(self) -> None:
        """Once a day at the configured local hour, text each venue bot its P&L
        and today's trade counts — a check-in even on days with no trades."""
        hour = self.settings.notifier.summary_hour
        if self.notifier is None or not self.notifier.any_configured or not 0 <= hour <= 23:
            return
        while not self._stop.is_set():
            now = datetime.now()
            target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=(target - now).total_seconds())
                return  # stopped during the wait
            except asyncio.TimeoutError:
                pass    # reached the summary hour
            date_str = datetime.now().strftime("%d %b %Y")
            for venue in (Venue.KALSHI, Venue.POLYMARKET):
                if not self.notifier.configured_for(venue):
                    continue
                snap = await self._venue_snapshot(venue)
                t = self._daily_trades[venue]
                await self.notifier.daily_report(venue, snap, t["buys"], t["sells"], date_str)
            self._daily_trades.clear()

    async def _bot_responder(self, venue) -> None:
        """Reply to messages sent to this venue's bot with its live report, so the
        two trade bots are interactive (they were send-only). Only the owner chat
        is answered; other senders are ignored."""
        if self.notifier is None or not self.notifier.configured_for(venue):
            return
        owner = self.notifier.owner_chat(venue)
        offset = 0
        try:                       # skip any backlog so we don't reply to old msgs
            offset, _ = await self.notifier.poll(venue, offset)
        except Exception:          # noqa: BLE001
            pass
        while not self._stop.is_set():
            try:
                offset, msgs = await self.notifier.poll(venue, offset)
            except Exception as exc:  # noqa: BLE001
                log.warning("responder.poll_error", venue=venue.value, error=str(exc))
                await asyncio.sleep(3)
                continue
            for chat_id, text in msgs:
                if chat_id != owner:
                    continue
                snap = await self._venue_snapshot(venue)
                t = self._daily_trades[venue]
                reply = None
                key = self.settings.notifier.openai_key
                if key:                       # natural-language answer, grounded in live data
                    from tradingbot.interface import llm
                    reply = await llm.chat(key, self.settings.notifier.openai_model,
                                           self._agent_prompt(venue, snap, t), text)
                if reply:
                    await self.notifier.announce(venue, reply)
                else:                         # no LLM (or it failed) -> fixed report
                    await self.notifier.report(venue, snap, t["buys"], t["sells"], "STATUS")

    def _agent_prompt(self, venue, snap: dict, trades: dict) -> str:
        """System prompt: scope the bot to ONE venue and ground it in live data."""
        label = "Kalshi" if venue is Venue.KALSHI else "Polymarket"
        pos = "\n".join(snap["positions"]) if snap["positions"] else "none"
        pct = snap["pnl"] / max(snap["baseline"], 1e-9) * 100
        return (
            f"You are the {label} trading assistant on Telegram. You speak ONLY about the "
            f"user's {label} account and its autonomous dutch-book arbitrage bot — it buys "
            f"a complete set of an event's mutually-exclusive outcomes when they cost under "
            f"$1 after fees (locking risk-free profit) and unwinds a set it can't complete. "
            f"Never discuss any other venue. Be concise (1-4 short lines), plain, and honest. "
            f"Use ONLY the numbers below — never invent prices, trades, or P&L. If nothing "
            f"has traded or P&L is ~0, say so plainly.\n\n"
            f"Live {label} account (now):\n"
            f"Equity ${snap['equity']:.2f}, cash ${snap['cash']:.2f}\n"
            f"P&L since go-live: ${snap['pnl']:+.2f} ({pct:+.2f}%)\n"
            f"Today: {trades['buys']} buys, {trades['sells']} sells\n"
            f"Open positions:\n{pos}"
        )

    async def run(self) -> None:
        if not self.markets:
            await self.discover()
        await self._announce_online()
        summary_task = asyncio.create_task(self._daily_summary())
        responder_tasks = [
            asyncio.create_task(self._bot_responder(v))
            for v in (Venue.KALSHI, Venue.POLYMARKET)
            if self.notifier is not None and self.notifier.configured_for(v)
        ]
        reactor_task = None
        if self.stream is not None:
            await self.stream.start(self.markets)
            if self.settings.streaming.event_driven:
                reactor_task = asyncio.create_task(self._reactor())
                log.info("engine.event_driven", debounce_s=self.settings.streaming.react_debounce_s)
        curator_task = None
        if self.settings.universe.refresh_interval_min > 0:
            curator_task = asyncio.create_task(self._curator())
        settlement_task = None
        if self.settings.settlement.enabled:
            settlement_task = asyncio.create_task(self._settlement())
        try:
            while not self._stop.is_set():
                await self._tick()
                # When streaming, reads are in-memory so we tick fast (act on
                # edges in ~250ms instead of waiting on the REST cadence).
                interval = (
                    self.settings.streaming.loop_interval_s
                    if (self.stream is not None and self.stream.active)
                    else self.settings.loop_interval_s
                )
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=interval)
                except asyncio.TimeoutError:
                    pass
        finally:
            for task in (reactor_task, curator_task, settlement_task, summary_task,
                         *responder_tasks):
                if task is not None:
                    task.cancel()
                    try:
                        await task
                    except (asyncio.CancelledError, Exception):  # noqa: BLE001
                        pass
            if self.stream is not None:
                await self.stream.stop()
            if self.store is not None:
                self.store.close()
            log.info("engine.stopped",
                     session_pnl=str(round(self.portfolio.session_pnl({}), 4)))

    async def _tick(self) -> None:
        """Periodic loop (backstop + slow path): risk, goals/lock-gains, exits,
        manual orders, a FULL strategy + quote pass, and fill draining. Runs even
        without streaming; with streaming the reactor handles the fast entry and
        this is the heartbeat that catches anything missed."""
        books = await self._refresh_books()
        async with self._trade_lock:
            self.last_books = books
            # Fill any resting market-maker quotes the fresh books traded through.
            if self._paper is not None:
                self._paper.match_resting(books)
            self.risk.update_kill_switch(books)

            # Goal tracking + lock-gains: once today's profit target is hit, stop
            # opening new risk for the rest of the day (if enabled).
            equity = self.portfolio.equity(books)
            self.goals.update(equity)
            if self.settings.goals.lock_gains and self.goals.enabled:
                day = self.goals.day_key
                # New day rolled over: release a lock we set yesterday.
                if self._paused_by_goals and self._locked_day and self._locked_day != day:
                    self.paused = False
                    self._paused_by_goals = False
                    self._locked_day = ""
                    log.info("goals.new_day_unlock", day=day)
                prog = self.goals.progress(equity)
                if prog["daily_target_met"] and self._locked_day != day:
                    self._locked_day = day
                    self.paused = True
                    self._paused_by_goals = True
                    log.info("goals.daily_target_met_locking_gains",
                             day=day, equity=str(round(equity, 2)))

            # Risk exits (stop-loss / take-profit) ALWAYS run — even while paused,
            # so a locked-gains or operator pause never strands a losing position.
            await self._place_orders(self.exits.evaluate(self.portfolio.positions, books), books)

            # Discretionary orders queued by the agent/operator ALSO run regardless
            # of pause — an explicit "buy X" is intentional and should execute now.
            if self.manual_orders:
                queued, self.manual_orders = self.manual_orders, []
                await self._place_orders(queued, books)

            # Paused: keep data flowing (marks stay fresh) and keep exits live, but
            # open no new strategy positions and pull all quotes.
            if not self.paused:
                await self._run_strategies(books)
                await self._reconcile_quotes(self._ctx(books), books)
            else:
                await self._cancel_all_quotes()

            self._drain_fills()

    async def _on_update(self, dirty: set[str]) -> None:
        """Event-driven fast path: react the instant books change. Evaluates
        strategies and refreshes the dirty markets' quotes immediately, instead of
        waiting for the next loop. Shares the trade lock with the periodic tick."""
        async with self._trade_lock:
            if self.paused or self.risk.kill_switch:
                return
            books = self._books_from_stream()
            if not books:
                return
            self.last_books = books
            if self._paper is not None:
                self._paper.match_resting(books)
            await self._run_strategies(books)
            await self._reconcile_quotes(self._ctx(books), books, only_keys=dirty)
            self._drain_fills()

    def _ctx(self, books: dict[str, OrderBook]) -> Context:
        candles = self.candle_source() if self.candle_source is not None else None
        return Context(self.markets, books, self.portfolio.positions,
                       candles=candles, equity=float(self.portfolio.equity(books)))

    async def _run_strategies(self, books: dict[str, OrderBook]) -> None:
        ctx = self._ctx(books)
        desired = []
        for strat in self.strategies:
            try:
                desired += strat.generate(ctx)
            except Exception as exc:  # noqa: BLE001
                log.error("strategy.error", strategy=strat.name, error=str(exc))
        await self._place_orders(desired, books)

    def _books_from_stream(self) -> dict[str, OrderBook]:
        books: dict[str, OrderBook] = {}
        if self.stream is None:
            return books
        for m in self.markets:
            b = self.stream.book(m.key)
            if b is not None and (b.best_bid or b.best_ask):
                books[m.key] = b
        return books

    async def _curator(self) -> None:
        """Periodically re-curate the universe (markets open / close / resolve) and
        re-point the stream at the new set. The selector is cheap; this just keeps
        the tracked universe live without a restart."""
        interval = self.settings.universe.refresh_interval_min * 60
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
                return  # stop requested
            except asyncio.TimeoutError:
                pass
            try:
                old = {m.key for m in self.markets}
                await self.discover()
                new = {m.key for m in self.markets}
                if new != old:
                    if self.stream is not None and self.stream.active:
                        await self.stream.resubscribe(self.markets)
                    log.info("universe.recurated",
                             added=len(new - old), removed=len(old - new), tracked=len(new))
            except Exception as exc:  # noqa: BLE001
                log.error("curator.error", error=str(exc))

    async def _settlement(self) -> None:
        """Periodically redeem resolved Polymarket positions. Resolved markets
        leave the active universe, so a held token would otherwise strand at its
        last mark forever; this polls Gamma for resolution and books a synthetic
        redemption fill at $1 (winner) or $0 (loser). Paper mode only — same
        start/cancel lifecycle as the curator."""
        interval = self.settings.settlement.poll_min * 60
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
                return  # stop requested
            except asyncio.TimeoutError:
                pass
            try:
                await self._settle_once()
            except Exception as exc:  # noqa: BLE001
                log.error("settlement.error", error=str(exc))

    async def _settle_once(self) -> None:
        """One settlement poll cycle: find held Polymarket positions, ask the
        Polymarket adapter which have resolved, and redeem those in the paper
        book. No-op in live mode or when no Polymarket venue is wired."""
        if self._paper is None:
            return  # live venues redeem on-chain; nothing to simulate
        held: list[Market] = []
        for key, pos in self.portfolio.positions.items():
            if pos.size == 0:
                continue
            market = self._market_registry.get(key)
            if market is None or market.venue is not Venue.POLYMARKET:
                continue
            held.append(market)
        if not held:
            return
        # Reach the Polymarket adapter through the router, the same routing
        # discover() uses. Absent venue -> no-op.
        try:
            poly = self.router._for(held[0])
        except KeyError:
            return
        fetch = getattr(poly, "fetch_resolutions", None)
        if fetch is None:
            return
        resolutions = await fetch(held)
        if not resolutions:
            return
        for market in held:
            price = resolutions.get(market.key)
            if price is None:
                continue
            before = self.portfolio.position(market).realized_pnl
            if not self._paper.settle(market, price):
                continue  # nothing to redeem — don't log a false success
            self._drain_fills()
            realized = self.portfolio.position(market).realized_pnl - before
            log.info("settlement.redeemed", market=market.key, price=price,
                     realized=str(round(realized, 4)))

    async def _reactor(self) -> None:
        """Waits on stream updates and fires the fast path, debounced to coalesce
        bursts. The periodic loop continues independently as the backstop."""
        debounce = self.settings.streaming.react_debounce_s
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self.stream.updated.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            self.stream.updated.clear()
            if debounce > 0:
                await asyncio.sleep(debounce)
            dirty = self.stream.drain_dirty()
            if not dirty:
                continue
            try:
                await self._on_update(dirty)
            except Exception as exc:  # noqa: BLE001
                log.error("reactor.error", error=str(exc))

    async def _reconcile_quotes(self, ctx: Context, books: dict[str, OrderBook],
                                only_keys: set[str] | None = None) -> None:
        """Maintain market-maker resting quotes: place new ones, and cancel/replace
        any whose target price moved or that already filled. When `only_keys` is
        given (the reactor's dirty markets), only those markets are touched."""
        desired: dict[tuple, "object"] = {}
        for strat in self.strategies:
            if not getattr(strat, "is_market_maker", False):
                continue
            try:
                for o in strat.quotes(ctx):
                    if only_keys is not None and o.market.key not in only_keys:
                        continue
                    desired[(o.market.key, o.side)] = o
            except Exception as exc:  # noqa: BLE001
                log.error("strategy.error", strategy=strat.name, error=str(exc))

        # Cancel quotes that filled, vanished from the target set, or moved price by
        # more than the requote tolerance. The tolerance is what stops churn: the MM
        # recomputes target prices from the live book every tick and they jitter by
        # sub-tick amounts, which would otherwise trigger a cancel/replace every tick
        # and blow through the order rate limit.
        tol = self.settings.market_maker.requote_tolerance
        for key, order in list(self._quotes.items()):
            if only_keys is not None and key[0] not in only_keys:
                continue
            want = desired.get(key)
            moved = want is not None and abs(float(want.price) - float(order.price)) > tol
            if order.is_terminal or want is None or moved:
                if not order.is_terminal:
                    try:
                        await self.exec.cancel_order(order)
                    except Exception:  # noqa: BLE001
                        pass
                del self._quotes[key]

        # Place quotes we want but don't yet have resting.
        for key, o in desired.items():
            if key in self._quotes:
                continue
            if not self.risk.approve(o, books):
                continue
            try:
                await self.exec.place_order(o)
                if not o.is_terminal:
                    self._quotes[key] = o
            except NotImplementedError as exc:
                log.error("order.unsupported", market=o.market.key, error=str(exc))
            except Exception as exc:  # noqa: BLE001
                log.error("order.error", market=o.market.key, error=str(exc))

    async def _cancel_all_quotes(self) -> None:
        for order in list(self._quotes.values()):
            if not order.is_terminal:
                try:
                    await self.exec.cancel_order(order)
                except Exception:  # noqa: BLE001
                    pass
        self._quotes.clear()

    async def _place_orders(self, orders: list, books: dict[str, OrderBook]) -> None:
        # Atomic sets (dutch-book legs sharing a set_id) must fill completely or be
        # unwound; place them separately from ordinary independent orders.
        sets: dict[str, list] = {}
        singles: list = []
        for order in orders:
            if order.set_id:
                sets.setdefault(order.set_id, []).append(order)
            else:
                singles.append(order)
        for legs in sets.values():
            await self._place_locked_set(legs, books)
        for order in singles:
            await self._place_single(order, books)

    async def _place_single(self, order, books: dict[str, OrderBook]) -> None:
        self._market_registry.setdefault(order.market.key, order.market)
        if not self.risk.approve(order, books):
            return
        try:
            await self.exec.place_order(order)
            if order.status is OrderStatus.REJECTED:
                log.warning("order.rejected", market=order.market.key, reason=order.reason)
        except NotImplementedError as exc:
            log.error("order.unsupported", market=order.market.key, error=str(exc))
        except Exception as exc:  # noqa: BLE001
            log.error("order.error", market=order.market.key, error=str(exc))

    async def _place_locked_set(self, legs: list, books: dict[str, OrderBook]) -> None:
        """Place a complete dutch-book set all-or-nothing. Legs are FOK, so each
        either fully fills or dies. If any leg fails to fully fill, the legs that
        DID fill are immediately flattened (sold to bid) — a partially-filled set
        is directional exposure, not a locked arb. Risk-approve every leg up front
        so we never start a set we're not cleared to complete."""
        for leg in legs:
            self._market_registry.setdefault(leg.market.key, leg.market)
            if not self.risk.approve(leg, books):
                log.warning("dutch_book.set_skipped_by_risk", set_id=legs[0].set_id,
                            market=leg.market.key)
                return
        filled: list = []
        for leg in legs:
            try:
                await self.exec.place_order(leg)
            except Exception as exc:  # noqa: BLE001
                log.error("dutch_book.leg_error", market=leg.market.key, error=str(exc))
            if leg.status is OrderStatus.FILLED and leg.filled_size >= leg.size:
                filled.append(leg)
            else:
                # Incomplete set: stop legging in and unwind whatever filled.
                log.warning("dutch_book.incomplete_unwinding", set_id=legs[0].set_id,
                            wanted=len(legs), filled=len(filled),
                            failed_market=leg.market.key, failed_status=leg.status.value)
                await self._unwind_legs(filled, books)
                return
        log.info("dutch_book.locked", set_id=legs[0].set_id, legs=len(filled))
        await self._notify_trade(filled, "BUY")

    async def _unwind_legs(self, legs: list, books: dict[str, OrderBook]) -> None:
        """Flatten filled dutch-book legs by selling each back to its best bid
        (marketable). Accepts the spread/fee cost to shed directional risk."""
        sold: list = []
        for leg in legs:
            book = books.get(leg.market.key)
            bid = book.best_bid.price if (book and book.best_bid) else leg.price
            unwind = Order(
                market=leg.market, side=Side.SELL, size=leg.filled_size or leg.size,
                type=OrderType.LIMIT, price=bid, time_in_force="FOK",
                reason=f"unwind incomplete {leg.set_id}",
            )
            try:
                await self.exec.place_order(unwind)
                log.info("dutch_book.unwound", market=leg.market.key,
                         status=unwind.status.value)
                sold.append(unwind)
            except Exception as exc:  # noqa: BLE001
                log.error("dutch_book.unwind_error", market=leg.market.key, error=str(exc))
        await self._notify_trade(sold, "SELL")

    async def _notify_trade(self, legs: list, action: str) -> None:
        """Text the venue's bot after a trade (BUY = locked set, SELL = unwind)."""
        if not legs:
            return
        venue = legs[0].market.venue
        self._daily_trades[venue]["buys" if action == "BUY" else "sells"] += 1
        if self.notifier is None or not self.notifier.configured_for(venue):
            return
        title = legs[0].market.title or legs[0].market.event_id
        cost = float(sum((leg.filled_size or leg.size) * Decimal(str(leg.price or 0))
                         for leg in legs))
        edge = _parse_edge(legs[0].reason)
        equity, pnl = await self._venue_equity_pnl(venue)
        try:
            await self.notifier.trade(venue, action, title, len(legs), cost, edge, equity, pnl)
        except Exception as exc:  # noqa: BLE001
            log.warning("notify.error", venue=venue.value, error=str(exc))

    async def _venue_equity_pnl(self, venue) -> tuple[float, float]:
        """Live equity (cash balance + positions at entry) and P&L vs the per-venue
        deposit baseline. Best-effort — never raises into the trade path."""
        adapter = self.router._venues.get(venue)
        bal_fn = getattr(adapter, "fetch_balance", None)
        equity = 0.0
        if adapter is not None and bal_fn is not None:
            try:
                bal = await bal_fn()
                positions = await adapter.fetch_positions()
                equity = float(bal) + sum(float(p.size) * float(p.avg_price) for p in positions)
            except Exception as exc:  # noqa: BLE001
                log.warning("pnl.error", venue=venue.value, error=str(exc))
        return equity, equity - self.settings.notifier.baseline_for(venue)

    async def _venue_snapshot(self, venue) -> dict:
        """Cash, mark-to-market equity, P&L, and per-position lines (title, size,
        avg, mark, uPnL) for a venue — the rich daily-report payload."""
        adapter = self.router._venues.get(venue)
        cash = 0.0
        if adapter is not None and getattr(adapter, "fetch_balance", None):
            try:
                cash = float(await adapter.fetch_balance())
            except Exception as exc:  # noqa: BLE001
                log.warning("snapshot.balance_error", venue=venue.value, error=str(exc))
        positions, lines, pos_value = [], [], 0.0
        try:
            positions = await adapter.fetch_positions() if adapter is not None else []
        except Exception as exc:  # noqa: BLE001
            log.warning("snapshot.positions_error", venue=venue.value, error=str(exc))
        for p in positions:
            size, avg = float(p.size), float(p.avg_price)
            mark = None
            if self.stream is not None:                 # live mid from the arb stream
                b = self.stream.book(p.market.key)
                if b is not None and b.mid is not None:
                    mark = b.mid
            if mark is None and adapter is not None:     # untracked -> fetch its book
                try:
                    b = await adapter.fetch_order_book(p.market)
                    if b.mid is not None:
                        mark = b.mid
                except Exception:  # noqa: BLE001
                    pass
            if mark is None:                             # no live two-sided quote -> mark at cost
                mark = avg
            upnl = (mark - avg) * size
            pos_value += mark * size
            lines.append(f"• {p.market.title[:48]} ({p.market.outcome}): "
                         f"{size:g} @ {avg:.3f}, mark {mark:.3f}, uPnL ${upnl:+.2f}")
        equity = cash + pos_value
        baseline = self.settings.notifier.baseline_for(venue)
        return {"cash": cash, "equity": equity, "pnl": equity - baseline,
                "baseline": baseline, "positions": lines}

    async def _refresh_books(self) -> dict[str, OrderBook]:
        # Streaming path: read live books from the WS cache (instant). REST-fill
        # only markets not yet warmed up, capped to avoid rate limits.
        if self.stream is not None and self.stream.active:
            books: dict[str, OrderBook] = {}
            missing = []
            for m in self.markets:
                b = self.stream.book(m.key)
                if b is not None and (b.best_bid or b.best_ask):
                    books[m.key] = b
                else:
                    missing.append(m)
            for m in missing[: self.settings.streaming.rest_fallback_cap]:
                try:
                    books[m.key] = await self.router.fetch_order_book(m)
                except Exception as exc:  # noqa: BLE001
                    log.debug("book.error", market=m.key, error=str(exc))
            return books

        # Pure REST path.
        books = {}
        for m in self.markets:
            try:
                books[m.key] = await self.router.fetch_order_book(m)
            except Exception as exc:  # noqa: BLE001
                log.debug("book.error", market=m.key, error=str(exc))
        return books

    def _drain_fills(self) -> None:
        """Move new simulated fills into the portfolio (paper mode) and persist
        them. Resolves markets via the registry so a fill for a market that left
        the curated universe (e.g. an exit closing it) is never dropped."""
        if self._paper is None:
            return  # live fill reconciliation is a separate concern (venue polling)
        new = self._paper.fills[self._fill_cursor :]
        self._fill_cursor = len(self._paper.fills)
        for fill in new:
            market = self._market_registry.get(fill.market_key)
            if market is None:
                log.warning("fill.unresolved_market", market=fill.market_key)
                continue
            self.portfolio.record_fill(market, fill)
            if self.store is not None:
                self.store.record_fill(market, fill)
