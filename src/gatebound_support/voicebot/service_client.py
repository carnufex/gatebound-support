"""Tiny httpx client for this same service's own ``/internal`` API (routes/internal.py).

The voicebot is a separate process/Deployment from the FastAPI app, so it can't call into
``store`` directly (two processes sharing one SQLite file with no coordination). It talks to
the app over HTTP instead, in-cluster, authenticated the same way ``/mcp`` is.
"""

from __future__ import annotations

from typing import Any, Literal

import httpx

from ..logging import get_logger
from ..settings import Settings

logger = get_logger("gatebound_support.voicebot.service_client")

_TIMEOUT = 5.0

Category = Literal["account", "bug", "payment", "report_player", "other"]
Priority = Literal["normal", "high"]
Source = Literal["discord_text", "discord_voice"]


class SupportServiceClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = httpx.AsyncClient(base_url=settings.SUPPORT_INTERNAL_URL, timeout=_TIMEOUT)

    async def aclose(self) -> None:
        await self._client.aclose()

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._settings.MCP_SECRET}"}

    async def create_ticket(
        self,
        *,
        source: Source,
        discord_user_id: str,
        discord_username: str,
        category: Category,
        summary: str,
        priority: Priority = "normal",
        conversation_id: str | None = None,
    ) -> str | None:
        """Returns the new ``ticket_id``, or ``None`` on any failure (network error or
        non-200) — callers degrade to an ephemeral "something went wrong" reply rather than
        raising, same convention as the rest of this service's outbound clients."""
        try:
            response = await self._client.post(
                "/internal/tickets",
                headers=self._headers(),
                json={
                    "source": source,
                    "discord_user_id": discord_user_id,
                    "discord_username": discord_username,
                    "category": category,
                    "summary": summary,
                    "priority": priority,
                    "conversation_id": conversation_id,
                },
            )
        except httpx.RequestError:
            logger.warning("internal create_ticket request failed")
            return None
        if response.status_code != 200:
            logger.warning(
                "internal create_ticket rejected", extra={"fields": {"status": response.status_code}}
            )
            return None
        return response.json().get("ticket_id")

    async def patch_ticket(
        self,
        ticket_id: str,
        *,
        thread_id: str,
        guild_id: str,
        discord_user_id: str,
        conversation_id: str | None = None,
    ) -> bool:
        try:
            response = await self._client.patch(
                f"/internal/tickets/{ticket_id}",
                headers=self._headers(),
                json={
                    "thread_id": thread_id,
                    "guild_id": guild_id,
                    "discord_user_id": discord_user_id,
                    "conversation_id": conversation_id,
                },
            )
        except httpx.RequestError:
            logger.warning("internal patch_ticket request failed")
            return False
        return response.status_code == 200

    async def get_open_ticket(self, discord_user_id: str) -> dict[str, Any] | None:
        try:
            response = await self._client.get(
                "/internal/tickets/open",
                headers=self._headers(),
                params={"discord_user_id": discord_user_id},
            )
        except httpx.RequestError:
            logger.warning("internal get_open_ticket request failed")
            return None
        if response.status_code != 200:
            return None
        return response.json()

    async def get_ticket_by_thread(self, thread_id: str) -> dict[str, Any] | None:
        """Resolves a Discord thread id to its ticket — used by ``/close``, the persistent
        close button, and close-by-reaction, none of which know the ticket id up front."""
        try:
            response = await self._client.get(
                f"/internal/tickets/by-thread/{thread_id}", headers=self._headers()
            )
        except httpx.RequestError:
            logger.warning("internal get_ticket_by_thread request failed")
            return None
        if response.status_code != 200:
            return None
        return response.json()

    async def close_ticket(self, ticket_id: str, *, closed_by: str) -> dict[str, Any] | None:
        """Returns the close response body, or ``None`` on any failure — network error, the
        ticket not existing (404), or it being already closed (409). Callers treat all three
        the same way: closing didn't happen, report that back rather than touch the thread."""
        try:
            response = await self._client.post(
                f"/internal/tickets/{ticket_id}/close",
                headers=self._headers(),
                json={"closed_by": closed_by},
            )
        except httpx.RequestError:
            logger.warning("internal close_ticket request failed")
            return None
        if response.status_code != 200:
            logger.warning(
                "internal close_ticket rejected", extra={"fields": {"status": response.status_code}}
            )
            return None
        return response.json()
