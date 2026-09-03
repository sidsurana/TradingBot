"""Per-venue trade notifications over Telegram — one bot per venue.

Each venue texts its OWN bot on every trade (a locked dutch-book set = a BUY, an
unwind = a SELL), headed with the agent label and that venue's live P&L. A venue
whose bot isn't configured is simply silent (no-op), so this never blocks trading.
"""

from __future__ import annotations

import httpx
import structlog

from tradingbot.config import NotifierSettings
from tradingbot.models import Venue

log = structlog.get_logger(__name__)

API = "https://api.telegram.org/bot{token}/sendMessage"

# venue -> (emoji, header label)
_LABELS = {
    Venue.KALSHI: ("🟦", "KALSHI AGENT"),
    Venue.POLYMARKET: ("🟪", "POLYMARKET US AGENT"),
}


class TradeNotifier:
    def __init__(self, settings: NotifierSettings):
        self._bots = {
            Venue.KALSHI: (settings.kalshi_token, settings.kalshi_chat_id),
            Venue.POLYMARKET: (settings.pm_token, settings.pm_chat_id),
        }

    def configured_for(self, venue: Venue) -> bool:
        token, chat = self._bots.get(venue, ("", 0))
        return bool(token and chat)

    @property
    def any_configured(self) -> bool:
        return any(self.configured_for(v) for v in self._bots)

    def _header(self, venue: Venue) -> str:
        emoji, label = _LABELS.get(venue, ("⬜", str(venue)))
        return f"{emoji} {label}"

    async def trade(self, venue: Venue, action: str, title: str, legs: int,
                    cost: float, edge: float | None, equity: float, pnl: float) -> None:
        """Send a trade message to the venue's bot. `action` is BUY or SELL."""
        if not self.configured_for(venue):
            return
        sign = "+" if pnl >= 0 else "−"
        edge_s = f", edge {edge * 100:.1f}%" if edge is not None else ""
        text = (
            f"{self._header(venue)} — {action}\n"
            f"{title[:90]}\n"
            f"{legs} leg{'s' if legs != 1 else ''}, cost ${cost:.2f}{edge_s}\n"
            f"—\n"
            f"Equity ${equity:.2f}  |  P&L {sign}${abs(pnl):.2f}"
        )
        await self._send(venue, text)

    async def summary(self, venue: Venue, equity: float, pnl: float,
                      buys: int, sells: int) -> None:
        """Once-a-day check-in with the venue's P&L and today's trade counts."""
        if not self.configured_for(venue):
            return
        sign = "+" if pnl >= 0 else "−"
        traded = buys + sells
        activity = f"{traded} trade{'s' if traded != 1 else ''} today ({buys} buy, {sells} sell)" \
            if traded else "no trades today"
        text = (
            f"{self._header(venue)} — DAILY SUMMARY\n"
            f"Equity ${equity:.2f}  |  P&L {sign}${abs(pnl):.2f}\n"
            f"{activity}"
        )
        await self._send(venue, text)

    async def announce(self, venue: Venue, text: str) -> None:
        """A plain labeled message (startup ping, test, etc.)."""
        if not self.configured_for(venue):
            return
        await self._send(venue, f"{self._header(venue)}\n{text}")

    async def _send(self, venue: Venue, text: str) -> None:
        token, chat = self._bots[venue]
        try:
            async with httpx.AsyncClient(timeout=15.0) as c:
                r = await c.post(API.format(token=token), json={"chat_id": chat, "text": text})
                if r.status_code >= 400:
                    log.warning("notifier.send_rejected", venue=venue.value, body=r.text[:200])
        except Exception as exc:  # noqa: BLE001 — never let a notification break trading
            log.warning("notifier.send_error", venue=venue.value, error=str(exc))
