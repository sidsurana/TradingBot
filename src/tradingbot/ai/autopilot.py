"""Autopilot — the autonomous optimization loop.

On an interval, the bot reassesses itself: where it stands against the day/week
profit goals, what the current market regime looks like, and whether posture
should change. It pushes a short briefing to Telegram so you stay informed
without watching.

Safety: autopilot does NOT auto-deploy capital or raise limits — those stay
behind the explicit confirmation flow. The only automatic action is lock-gains
(handled in the engine: pause once the daily target is hit, resume next day).
Everything else is advisory: the agent recommends, you decide.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

import structlog

from tradingbot.ai.agent import TradingAgent
from tradingbot.ai.controller import BotController
from tradingbot.config import AutopilotSettings

if TYPE_CHECKING:
    from tradingbot.interface.telegram import TelegramBridge

log = structlog.get_logger(__name__)

BRIEFING_PROMPT = """\
Autopilot check-in. Do this and reply in under 120 words for a phone notification:
1. Call get_goal_progress and state where we stand vs the daily and weekly targets.
2. Run the regime_detection quant skill and give the one-line regime read.
3. Give ONE concrete recommendation (e.g. which strategy to favor, or a limit to
   review). Do NOT take sensitive actions — recommend only.
Lead with the PnL-vs-goal line."""


class Autopilot:
    def __init__(
        self,
        settings: AutopilotSettings,
        controller: BotController,
        agent: TradingAgent,
        bridge: "TelegramBridge",
        clock=time.time,
    ):
        self.settings = settings
        self.controller = controller
        self.agent = agent
        self.bridge = bridge
        self._clock = clock
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        if not self.settings.enabled:
            log.info("autopilot.disabled")
            return
        log.info("autopilot.started", interval_min=self.settings.interval_min)
        # Let the engine discover markets and take a few ticks first.
        if not await self._sleep(30):
            return
        while not self._stop.is_set():
            try:
                await self.cycle()
            except Exception as exc:  # noqa: BLE001
                log.error("autopilot.cycle_error", error=str(exc))
            if not await self._sleep(self.settings.interval_min * 60):
                return

    async def cycle(self) -> None:
        prog = self.controller.goal_progress()
        log.info("autopilot.cycle",
                 daily_pnl=prog["daily"]["pnl"], paused=self.controller.engine.paused)
        if not self.settings.briefing:
            return
        text = await self._briefing(prog)
        if text:
            await self.bridge.broadcast(text)

    async def _briefing(self, prog: dict) -> str:
        if self.agent.configured:
            return await self.agent.chat("autopilot", BRIEFING_PROMPT)
        # No Claude available: send a plain rule-based summary instead.
        d, w = prog["daily"], prog["weekly"]
        lines = [f"📊 Autopilot — equity ${prog['equity']}"]
        if d.get("enabled"):
            lines.append(f"Daily: ${d['pnl']} / ${d['target']} ({d['pct_of_target']}%)"
                         + (" ✅ met" if d.get("met") else ""))
        if w.get("enabled"):
            ot = "on track" if w.get("on_track") else "behind pace"
            lines.append(f"Weekly: ${w['pnl']} / ${w['target']} ({ot})")
        if self.controller.engine.paused:
            lines.append("⏸ trading paused")
        return "\n".join(lines)

    async def _sleep(self, seconds: float) -> bool:
        """Interruptible sleep. Returns False if asked to stop."""
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
            return False
        except asyncio.TimeoutError:
            return True
