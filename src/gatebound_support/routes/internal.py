"""Internal service-to-service API, mounted under ``/internal``.

Not part of the public contract in docs/SPEC.md and never documented there or in the
README — the only caller is the voicebot process (a separate Deployment, same image; see
``voicebot/service_client.py``), which runs the Discord gateway connection and therefore
can't touch this service's SQLite directly (two processes, one file, no locking story).

Auth: ``Authorization: Bearer <MCP_SECRET>`` — the same bearer secret already used for
``/mcp``, checked the same way (``hmac.compare_digest``, 401 otherwise, rejected outright
if ``MCP_SECRET`` is unset). Reusing it avoids minting yet another cluster secret for two
processes that already share a Kubernetes Secret via ``envFrom``.
"""

from __future__ import annotations

import hmac
from typing import Literal

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from .. import store
from ..logging import get_logger
from ..settings import Settings, enabled

logger = get_logger("gatebound_support.internal")

Category = Literal["account", "bug", "payment", "report_player", "other"]
Priority = Literal["normal", "high"]
Source = Literal["discord_text", "discord_voice"]


class CreateTicketBody(BaseModel):
    source: Source
    discord_user_id: str
    discord_username: str
    category: Category
    summary: str
    priority: Priority = "normal"
    conversation_id: str | None = None


class PatchTicketBody(BaseModel):
    thread_id: str
    guild_id: str
    discord_user_id: str
    conversation_id: str | None = None


def _check_auth(settings: Settings, authorization: str | None) -> None:
    expected = f"Bearer {settings.MCP_SECRET}"
    provided = authorization or ""
    if not enabled(settings.MCP_SECRET) or not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="unauthorized")


def build_router(settings: Settings) -> APIRouter:
    router = APIRouter(prefix="/internal")

    @router.post("/tickets")
    async def create_ticket(
        body: CreateTicketBody, authorization: str | None = Header(default=None)
    ) -> dict[str, str]:
        _check_auth(settings, authorization)
        ticket_id = store.create_ticket(
            settings.DATA_DIR,
            token=store.new_draft_token(),
            category=body.category,
            summary=body.summary,
            priority=body.priority,
            account_name=body.discord_username,
            character_name=None,
            conversation_id=body.conversation_id,
            status="open",
            source=body.source,
        )
        # Record the Discord user right away (thread/guild follow via PATCH once the bot has
        # created the thread) so a concurrent "do I already have an open ticket?" lookup sees
        # this one instead of racing a duplicate.
        store.update_ticket_discord(
            settings.DATA_DIR,
            ticket_id,
            guild_id="",
            thread_id="",
            user_id=body.discord_user_id,
            status="open",
        )
        logger.info(
            "internal ticket created",
            extra={"fields": {"ticket_id": ticket_id, "source": body.source, "category": body.category}},
        )
        return {"ticket_id": ticket_id}

    @router.patch("/tickets/{ticket_id}")
    async def patch_ticket(
        ticket_id: str, body: PatchTicketBody, authorization: str | None = Header(default=None)
    ) -> dict[str, bool]:
        _check_auth(settings, authorization)
        ticket = store.get_ticket(settings.DATA_DIR, ticket_id)
        if ticket is None:
            raise HTTPException(status_code=404, detail="not_found")
        store.update_ticket_discord(
            settings.DATA_DIR,
            ticket_id,
            guild_id=body.guild_id,
            thread_id=body.thread_id,
            user_id=body.discord_user_id,
            status="open",
            conversation_id=body.conversation_id,
        )
        return {"ok": True}

    @router.get("/tickets/open")
    async def get_open_ticket(
        discord_user_id: str, authorization: str | None = Header(default=None)
    ) -> dict[str, str]:
        _check_auth(settings, authorization)
        ticket = store.get_open_ticket_for_user(settings.DATA_DIR, discord_user_id)
        if ticket is None or not ticket.get("discord_thread_id"):
            raise HTTPException(status_code=404, detail="not_found")
        return {"ticket_id": ticket["ticket_id"], "thread_id": ticket["discord_thread_id"]}

    return router
