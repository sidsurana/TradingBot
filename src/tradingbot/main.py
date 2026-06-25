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
    venues = {
        Venue.KALSHI: KalshiExchange(settings.kalshi),
        Venue.POLYMARKET: PolymarketExchange(settings.polymarket),
    }
    router = ExchangeRouter(venues)
    strategies = [_build_strategy(name, settings) for name in settings.enabled_strategies]
    return Engine(settings, router, strategies, stream=_build_stream(settings))


def _build_strategy(name: str, settings: Settings):
    if name == "market_maker":
        mm = settings.market_maker
        return build(name, min_spread=mm.min_spread, quote_size=mm.quote_size,
                     max_inventory=mm.max_inventory, max_markets=mm.max_markets)
    return build(name)


def _build_stream(settings: Settings):
    """Construct the WebSocket StreamManager when enabled. Polymarket's market
    channel is public; Kalshi's needs signed auth, so it's added only when
    Kalshi credentials are configured."""
    if not settings.streaming.enabled:
        return None
    from tradingbot.exchanges.streaming import KalshiStream, PolymarketStream, StreamManager

    clients = [PolymarketStream(settings.polymarket)]
    if settings.kalshi.configured:
        from tradingbot.exchanges.kalshi_auth import load_signer

        signer = load_signer(settings.kalshi.api_key_id, settings.kalshi.private_key_path)
        clients.append(KalshiStream(settings.kalshi, signer))
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
