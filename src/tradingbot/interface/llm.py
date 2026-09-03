"""Minimal OpenAI chat call over httpx (no extra dependency).

Used to answer a user's Telegram message conversationally, grounded in a system
prompt built from the venue's live state. Never raises — returns None on any
failure so the caller can fall back to a fixed report.
"""

from __future__ import annotations

import httpx
import structlog

log = structlog.get_logger(__name__)

_URL = "https://api.openai.com/v1/chat/completions"


async def chat(key: str, model: str, system: str, user: str,
               max_tokens: int = 300) -> str | None:
    if not key:
        return None
    try:
        async with httpx.AsyncClient(timeout=30.0) as c:
            r = await c.post(
                _URL,
                headers={"Authorization": f"Bearer {key}"},
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "max_tokens": max_tokens,
                    "temperature": 0.3,
                },
            )
            if r.status_code >= 400:
                log.warning("llm.error", status=r.status_code, body=r.text[:200])
                return None
            return r.json()["choices"][0]["message"]["content"].strip()
    except Exception as exc:  # noqa: BLE001
        log.warning("llm.exception", error=str(exc))
        return None
