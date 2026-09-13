"""ElevenLabs post-call webhook (SPEC §5.6). Signature scheme ported from
oncall-voice-copilot/services/oncall-tools/src/routes/webhooks.ts and elevenlabs.ts:
header ``ElevenLabs-Signature: t=<unix>,v0=<hex hmac_sha256(secret, "<t>.<raw body>")>``,
30 minute tolerance window."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .. import store
from ..discord import DiscordClient
from ..elevenlabs import ElevenLabsClient, verify_webhook_signature
from ..logging import get_logger
from ..settings import Settings, enabled

logger = get_logger("gatebound_support.webhooks")


async def _handle_post_call_transcription(settings: Settings, discord_client: DiscordClient, payload: dict[str, Any]) -> None:
    data = payload.get("data") or {}
    conversation_id = data.get("conversation_id")
    if not conversation_id:
        logger.warning("post-call webhook missing conversation_id")
        return

    transcript = [
        {"role": turn.get("role"), "message": turn.get("message")}
        for turn in (data.get("transcript") or [])
        if turn.get("message")
    ]
    summary = (data.get("analysis") or {}).get("transcript_summary")

    store.store_conversation(settings.DATA_DIR, conversation_id, summary=summary, transcript=transcript)

    ticket = store.find_ticket_by_conversation(settings.DATA_DIR, conversation_id)
    if ticket and ticket.get("discord_thread_id"):
        await discord_client.post_conversation_followup(
            thread_id=ticket["discord_thread_id"], summary=summary, transcript=transcript
        )


def build_router(settings: Settings, discord_client: DiscordClient, elevenlabs_client: ElevenLabsClient) -> APIRouter:
    router = APIRouter()

    @router.post("/webhooks/elevenlabs")
    async def elevenlabs_webhook(request: Request) -> JSONResponse:
        raw_body = await request.body()

        if not enabled(settings.ELEVENLABS_WEBHOOK_SECRET):
            logger.warning("post-call webhook received but ELEVENLABS_WEBHOOK_SECRET is unset; ignoring")
            return JSONResponse({"ok": True})

        header = request.headers.get("elevenlabs-signature")
        if not header or not verify_webhook_signature(secret=settings.ELEVENLABS_WEBHOOK_SECRET, header=header, raw_body=raw_body):
            return JSONResponse({"error": "invalid_signature"}, status_code=401)

        try:
            payload = json.loads(raw_body)
        except ValueError:
            return JSONResponse({"error": "bad_request"}, status_code=400)

        if payload.get("type") != "post_call_transcription":
            return JSONResponse({"ok": True})

        await _handle_post_call_transcription(settings, discord_client, payload)
        return JSONResponse({"ok": True})

    return router
