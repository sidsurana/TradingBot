"""Entrypoint: wire config -> venues -> strategies -> engine and run.

Usage:
    tradingbot                  # paper mode (default), arbitrage strategy
    TB_LIVE=true tradingbot     # live (requires configured venue creds)

Safe by default: with no credentials and TB_LIVE unset, it connects to public
market data and simulates fills.
"""

from __future__ import annotations

import asyncio
import logging
import signal

import structlog

from tradingbot.ai import BotController, TradingAgent
from tradingbot.ai.autopilot import Autopilot
from tradingbot.api import ApiServer
from tradingbot.config import Settings, load_settings
from tradingbot.engine import Engine, ExchangeRouter
from tradingbot.exchanges import KalshiExchange, PolymarketExchange
from tradingbot.interface import TelegramBridge
from tradingbot.models import Venue
from tradingbot.strategies import build


def _setup_logging(level: str) -> None:
    logging.basicConfig(format="%(message)s", level=getattr(logging, level.upper(), logging.INFO))
    # httpx logs every request at INFO; too chatty when polling many books.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
    )


def build_engine(settings: Settings) -> Engine:
    venues = {}
    data_feed = None
    if settings.datafeed.enabled:
        from tradingbot.exchanges.marketdata import MarketDataExchange

        data_feed = MarketDataExchange(settings.datafeed)
        venues[Venue.DATA] = data_feed
    if not (settings.datafeed.enabled and settings.datafeed.only):
        # Only wire Kalshi when it's actually configured — otherwise the engine
        # would pointlessly hit Kalshi's public API each discovery cycle. The
        # Polymarket-only certainty-carry profile leaves this off.
        if settings.kalshi.configured:
            venues[Venue.KALSHI] = KalshiExchange(settings.kalshi)
        # Polymarket US (api.polymarket.us, Ed25519 key auth) is a separate
        # platform from .com; when its creds are set it IS the Polymarket venue.
        if settings.polymarket_us.configured:
            from tradingbot.exchanges.polymarket_us import PolymarketUSExchange

            venues[Venue.POLYMARKET] = PolymarketUSExchange(settings.polymarket_us)
        else:
            venues[Venue.POLYMARKET] = PolymarketExchange(settings.polymarket)
    router = ExchangeRouter(venues)
    strategies = [_build_strategy(name, settings) for name in settings.enabled_strategies]
    # Streaming is a prediction-market feature; in data-only mode there is
    # nothing to stream and the engine runs on the REST/cache tick.
    stream = None if (settings.datafeed.enabled and settings.datafeed.only) \
        else _build_stream(settings)
    engine = Engine(settings, router, strategies, stream=stream)
    if data_feed is not None:
        engine.candle_source = data_feed.candles_snapshot
    return engine


def _build_strategy(name: str, settings: Settings):
    if name == "market_maker":
        mm = settings.market_maker
        return build(name, min_spread=mm.min_spread, quote_size=mm.quote_size,
                     max_inventory=mm.max_inventory, max_markets=mm.max_markets,
                     exclude_sports=mm.exclude_sports,
                     min_hours_to_resolution=mm.min_hours_to_resolution)
    if name == "signal":
        sg = settings.signal
        return build(name, kelly_fraction=sg.kelly_fraction, bankroll=sg.bankroll,
                     min_edge=sg.min_edge, min_confidence=sg.min_confidence,
                     max_signal_age_s=sg.max_signal_age_s, max_position=sg.max_position)
    if name == "mean_reversion":
        return build(name, cfg=settings.mean_reversion, sizing=settings.sizing)
    if name == "breakout":
        return build(name, cfg=settings.breakout, sizing=settings.sizing)
    if name == "trend":
        return build(name, cfg=settings.trend, sizing=settings.sizing)
    if name == "certainty_carry":
        return build(name, cfg=settings.carry)
    if name == "arbitrage":
        a = settings.arbitrage
        return build(name, min_edge=a.min_edge, max_size=a.max_size)
    return build(name)


def _build_stream(settings: Settings):
    """Construct the WebSocket StreamManager when enabled. Polymarket's market
    channel is public; Kalshi's needs signed auth, so it's added only when
    Kalshi credentials are configured."""
    if not settings.streaming.enabled:
        return None
    from tradingbot.exchanges.streaming import KalshiStream, PolymarketStream, StreamManager

    # Polymarket US has its own WebSocket protocol (not yet wired); when it's the
    # active venue, skip the .com market stream and let REST fill its books.
    clients = [] if settings.polymarket_us.configured else [PolymarketStream(settings.polymarket)]
    if settings.kalshi.configured:
        from tradingbot.exchanges.kalshi_auth import load_signer

        # A bad key path must degrade to "no Kalshi stream", not kill the process:
        # launchd KeepAlive turns a startup raise here into an infinite crash loop.
        try:
            signer = load_signer(settings.kalshi.api_key_id, settings.kalshi.private_key_path)
            clients.append(KalshiStream(settings.kalshi, signer))
        except OSError as exc:
            structlog.get_logger("tradingbot").error(
                "kalshi.signer_unavailable_skipping_stream",
                path=settings.kalshi.private_key_path, error=str(exc))
    return StreamManager(clients)


async def _main() -> None:
    settings = load_settings()
    _setup_logging(settings.log_level)
    log = structlog.get_logger("tradingbot")
    log.info("startup", live=settings.live, strategies=settings.enabled_strategies,
             agent=settings.anthropic.configured, telegram=settings.telegram.configured)

    engine = build_engine(settings)
    await engine.router.connect()
    await engine.discover()

    # The brain: controller facade + Claude agent + Telegram front-end + autopilot.
    controller = BotController(engine)
    agent = TradingAgent(controller, settings.anthropic)
    bridge = TelegramBridge(settings.telegram, agent)
    autopilot = Autopilot(settings.autopilot, controller, agent, bridge)
    api = ApiServer(settings.api, controller)

    loop = asyncio.get_running_loop()

    def _shutdown() -> None:
        engine.stop()
        bridge.stop()
        autopilot.stop()
        api.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _shutdown)

    try:
        # Engine, Telegram bridge, autopilot, and the control API run concurrently
        # on one event loop, so everything reads consistent live state. Each is a
        # no-op if unconfigured (Telegram needs a token; autopilot/API need enabling).
        await asyncio.gather(engine.run(), bridge.run(), autopilot.run(), api.run())
    finally:
        await engine.router.close()


def run() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    run()
