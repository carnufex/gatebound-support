"""Discord REST integration: OAuth2 identify, forum ticket posts, transcript follow-ups,
and the staff heads-up webhook. Every method is a no-op (returns None / False) when Discord
is not configured — SPEC §1: "unset" means disabled, and the affected feature degrades with
an explicit message rather than failing.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlencode

import httpx

from .logging import get_logger
from .settings import Settings, enabled

API_BASE = "https://discord.com/api/v10"
AUTHORIZE_URL = "https://discord.com/oauth2/authorize"
_TIMEOUT = 5.0
MESSAGE_CHUNK_LIMIT = 1900
ATTACHMENT_THRESHOLD = 6000

logger = get_logger("gatebound_support.discord")


def chunk_text(text: str, limit: int = MESSAGE_CHUNK_LIMIT) -> list[str]:
    """Split text into chunks <= limit chars, preferring line breaks."""
    if len(text) <= limit:
        return [text] if text else []
    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        cut = remaining.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    return chunks


class DiscordClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = httpx.AsyncClient(timeout=_TIMEOUT)

    @property
    def enabled(self) -> bool:
        return self._settings.discord_enabled

    @property
    def staff_webhook_enabled(self) -> bool:
        return enabled(self._settings.DISCORD_STAFF_WEBHOOK_URL)

    async def aclose(self) -> None:
        await self._client.aclose()

    def _bot_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bot {self._settings.DISCORD_BOT_TOKEN}"}

    # ---- OAuth2 ----

    def build_authorize_url(self, *, state: str, redirect_uri: str) -> str:
        params = {
            "client_id": self._settings.DISCORD_CLIENT_ID,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": "identify",
            "state": state,
        }
        return f"{AUTHORIZE_URL}?{urlencode(params)}"

    async def exchange_code(self, *, code: str, redirect_uri: str) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": self._settings.DISCORD_CLIENT_ID,
            "client_secret": self._settings.DISCORD_CLIENT_SECRET,
        }
        try:
            response = await self._client.post(
                f"{API_BASE}/oauth2/token",
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        except httpx.RequestError:
            logger.warning("discord token exchange failed")
            return None
        if response.status_code != 200:
            logger.warning("discord token exchange rejected", extra={"fields": {"status": response.status_code}})
            return None
        return response.json()

    async def fetch_current_user(self, access_token: str) -> dict[str, Any] | None:
        try:
            response = await self._client.get(
                f"{API_BASE}/users/@me",
                headers={"Authorization": f"Bearer {access_token}"},
            )
        except httpx.RequestError:
            return None
        if response.status_code != 200:
            return None
        return response.json()

    # ---- Forum ticket posts ----

    async def create_forum_post(self, *, ticket_id: str, title: str, content: str) -> str | None:
        """Creates the forum thread for a ticket. Returns the thread id, or None on failure."""
        if not self.enabled:
            return None
        body: dict[str, Any] = {"name": title[:100], "message": {"content": content}}
        if enabled(self._settings.DISCORD_TAG_OPEN_ID):
            body["applied_tags"] = [self._settings.DISCORD_TAG_OPEN_ID]
        try:
            response = await self._client.post(
                f"{API_BASE}/channels/{self._settings.DISCORD_FORUM_CHANNEL_ID}/threads",
                headers=self._bot_headers(),
                json=body,
            )
        except httpx.RequestError:
            logger.warning("discord forum post failed", extra={"fields": {"ticket_id": ticket_id}})
            return None
        if response.status_code not in (200, 201):
            logger.warning(
                "discord forum post rejected",
                extra={"fields": {"ticket_id": ticket_id, "status": response.status_code}},
            )
            return None
        return response.json().get("id")

    async def add_thread_member(self, *, thread_id: str, user_id: str) -> None:
        if not self.enabled:
            return
        try:
            await self._client.put(
                f"{API_BASE}/channels/{thread_id}/thread-members/{user_id}",
                headers=self._bot_headers(),
            )
        except httpx.RequestError:
            logger.warning("discord add thread member failed", extra={"fields": {"thread_id": thread_id}})

    async def post_message(self, *, thread_id: str, content: str) -> None:
        if not self.enabled or not content:
            return
        try:
            await self._client.post(
                f"{API_BASE}/channels/{thread_id}/messages",
                headers=self._bot_headers(),
                json={"content": content},
            )
        except httpx.RequestError:
            logger.warning("discord post message failed", extra={"fields": {"thread_id": thread_id}})

    async def post_message_with_attachment(self, *, thread_id: str, content: str, filename: str, file_text: str) -> None:
        if not self.enabled:
            return
        payload_json = json.dumps(
            {"content": content, "attachments": [{"id": 0, "filename": filename}]}
        )
        files = {"files[0]": (filename, file_text.encode("utf-8"), "text/plain")}
        try:
            await self._client.post(
                f"{API_BASE}/channels/{thread_id}/messages",
                headers=self._bot_headers(),
                data={"payload_json": payload_json},
                files=files,
            )
        except httpx.RequestError:
            logger.warning("discord post attachment failed", extra={"fields": {"thread_id": thread_id}})

    async def post_conversation_followup(self, *, thread_id: str, summary: str | None, transcript: list[dict[str, Any]]) -> None:
        """Posts the post-call summary + transcript to a ticket's thread (SPEC §5.6)."""
        transcript_lines = [f"**{turn.get('role', 'unknown')}:** {turn.get('message') or ''}" for turn in transcript]
        transcript_text = "\n".join(transcript_lines)
        summary_text = f"**Call summary:** {summary}" if summary else "**Call summary:** (none)"
        full = f"{summary_text}\n\n**Transcript:**\n{transcript_text}" if transcript_text else summary_text
        if len(full) > ATTACHMENT_THRESHOLD:
            await self.post_message_with_attachment(
                thread_id=thread_id,
                content=summary_text,
                filename="transcript.txt",
                file_text=transcript_text or "(empty transcript)",
            )
            return
        for chunk in chunk_text(full):
            await self.post_message(thread_id=thread_id, content=chunk)

    # ---- Staff heads-up ----

    async def post_staff_webhook(self, content: str) -> None:
        if not self.staff_webhook_enabled:
            return
        try:
            await self._client.post(self._settings.DISCORD_STAFF_WEBHOOK_URL, json={"content": content})
        except httpx.RequestError:
            logger.warning("discord staff webhook failed")
