"""Strategies. Importing a module registers its strategy in the registry."""

from tradingbot.strategies import arbitrage  # noqa: F401  (registers)
from tradingbot.strategies import market_maker  # noqa: F401  (registers)
from tradingbot.strategies import signal  # noqa: F401  (registers)
from tradingbot.strategies.base import Context, Strategy, available, build, register

__all__ = ["Context", "Strategy", "available", "build", "register"]
