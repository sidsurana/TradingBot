"""Telegram bridge — chat with the agent from your phone.

Long-polls the Telegram Bot API (no extra dependency; uses httpx) and routes
each message to the Claude agent, then sends the reply back. Only chat IDs in
the allowlist may command the bot — everyone else is ignored, so the bot can't
be driven by a stranger who finds it.

Get a token from @BotFather; find your chat id by messaging the bot and reading
the logged "telegram.unauthorized" chat_id, or via @userinfobot.
"""

from __future__ import annotations

import asyncio

import httpx
import structlog

from tradingbot.ai.agent import TradingAgent
from tradingbot.config import TelegramCreds

log = structlog.get_logger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"


class TelegramBridge:
    def __init__(self, creds: TelegramCreds, agent: TradingAgent):
        self.creds = creds
        self.agent = agent
        self._offset = 0
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def broadcast(self, text: str) -> None:
        """Push an unsolicited message to every allowed chat (autopilot briefings)."""
        if not self.creds.configured:
            return
        async with httpx.AsyncClient(timeout=20.0) as client:
            for chat_id in self.creds.allowed_chat_ids:
                await self._send(client, chat_id, text)

    async def run(self) -> None:
        if not self.creds.configured:
            log.info("telegram.disabled",
                     reason="set TB_TELEGRAM_BOT_TOKEN and TB_TELEGRAM_ALLOWED_CHAT_IDS")
            return
        allowed = set(self.creds.allowed_chat_ids)
        log.info("telegram.started", allowed_chats=len(allowed))
        async with httpx.AsyncClient(timeout=40.0) as client:
            await self._send(client, next(iter(allowed)),
                             "🤖 TradingBot online. Ask me about PnL, risk, or positions.")
            while not self._stop.is_set():
                try:
                    updates = await self._get_updates(client)
                except Exception as exc:  # noqa: BLE001
                    log.warning("telegram.poll_error", error=str(exc))
                    await asyncio.sleep(3)
                    continue
                for upd in updates:
                    await self._handle(client, upd, allowed)

    async def _get_updates(self, client: httpx.AsyncClient) -> list[dict]:
        resp = await client.get(
            API.format(token=self.creds.bot_token, method="getUpdates"),
            params={"offset": self._offset, "timeout": 30},
        )
        resp.raise_for_status()
        return resp.json().get("result", [])

    async def _handle(self, client: httpx.AsyncClient, update: dict, allowed: set[int]) -> None:
        self._offset = max(self._offset, update["update_id"] + 1)
        msg = update.get("message") or update.get("edited_message")
        if not msg or "text" not in msg:
            return
        chat_id = msg["chat"]["id"]
        text = msg["text"].strip()
        if chat_id not in allowed:
            log.warning("telegram.unauthorized", chat_id=chat_id, text=text[:40])
            return
        if text in ("/start", "/help"):
            await self._send(client, chat_id,
                             "Ask me things like: 'what's my PnL?', 'show risk', "
                             "'run regime detection', 'deploy $200', 'pause'.")
            return

        await self._send_action(client, chat_id, "typing")
        reply = await self.agent.chat(str(chat_id), text)
        await self._send(client, chat_id, reply)

    async def _send(self, client: httpx.AsyncClient, chat_id: int, text: str) -> None:
        # Telegram caps messages at 4096 chars.
        for chunk in (text[i:i + 3800] for i in range(0, len(text), 3800)) or [text]:
            try:
                await client.post(
                    API.format(token=self.creds.bot_token, method="sendMessage"),
                    json={"chat_id": chat_id, "text": chunk},
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("telegram.send_error", error=str(exc))

    async def _send_action(self, client: httpx.AsyncClient, chat_id: int, action: str) -> None:
        try:
            await client.post(
                API.format(token=self.creds.bot_token, method="sendChatAction"),
                json={"chat_id": chat_id, "action": action},
            )
        except Exception:  # noqa: BLE001
            pass
