from __future__ import annotations

import hashlib
import hmac
import json
import time

import httpx
import pytest

from gatebound_support import store
from gatebound_support.app import create_app
from gatebound_support.discord import DiscordClient

from .conftest import make_settings

SECRET = "internal-test-secret"


@pytest.fixture
def settings(base_env):
    env = dict(base_env)
    env["MCP_SECRET"] = SECRET
    return make_settings(env)


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {SECRET}"}


async def _client(app) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


async def test_create_ticket_requires_auth(settings) -> None:
    app = create_app(settings)
    async with await _client(app) as client:
        response = await client.post(
            "/internal/tickets",
            json={
                "source": "discord_text",
                "discord_user_id": "1",
                "discord_username": "Player#0001",
                "category": "bug",
                "summary": "Stuck",
            },
        )
    assert response.status_code == 401


async def test_create_ticket_wrong_secret_401(settings) -> None:
    app = create_app(settings)
    async with await _client(app) as client:
        response = await client.post(
            "/internal/tickets",
            headers={"Authorization": "Bearer wrong"},
            json={
                "source": "discord_text",
                "discord_user_id": "1",
                "discord_username": "Player#0001",
                "category": "bug",
                "summary": "Stuck",
            },
        )
    assert response.status_code == 401


async def test_internal_routes_disabled_when_mcp_secret_unset(base_env) -> None:
    settings = make_settings(base_env)  # MCP_SECRET == "unset"
    app = create_app(settings)
    async with await _client(app) as client:
        response = await client.post(
            "/internal/tickets",
            headers={"Authorization": "Bearer unset"},
            json={
                "source": "discord_text",
                "discord_user_id": "1",
                "discord_username": "Player#0001",
                "category": "bug",
                "summary": "Stuck",
            },
        )
    assert response.status_code == 401


async def test_get_open_ticket_404_when_none_exists(settings) -> None:
    app = create_app(settings)
    async with await _client(app) as client:
        response = await client.get(
            "/internal/tickets/open", headers=_auth(), params={"discord_user_id": "does-not-exist"}
        )
    assert response.status_code == 404


async def test_create_then_patch_then_open_lookup(settings) -> None:
    app = create_app(settings)
    async with await _client(app) as client:
        create_response = await client.post(
            "/internal/tickets",
            headers=_auth(),
            json={
                "source": "discord_voice",
                "discord_user_id": "42",
                "discord_username": "Aidenn#0001",
                "category": "other",
                "summary": "Voice call with Aidenn",
                "conversation_id": None,
            },
        )
        assert create_response.status_code == 200
        ticket_id = create_response.json()["ticket_id"]
        assert ticket_id.startswith("GB-")

        # No thread yet -> not "open" for lookup purposes (no discord_thread_id).
        open_response = await client.get(
            "/internal/tickets/open", headers=_auth(), params={"discord_user_id": "42"}
        )
        assert open_response.status_code == 404

        patch_response = await client.patch(
            f"/internal/tickets/{ticket_id}",
            headers=_auth(),
            json={
                "thread_id": "thread-123",
                "guild_id": "guild-1",
                "discord_user_id": "42",
                "conversation_id": "conv-voice-1",
            },
        )
        assert patch_response.status_code == 200
        assert patch_response.json() == {"ok": True}

        open_response = await client.get(
            "/internal/tickets/open", headers=_auth(), params={"discord_user_id": "42"}
        )
        assert open_response.status_code == 200
        assert open_response.json() == {"ticket_id": ticket_id, "thread_id": "thread-123"}

    ticket = store.get_ticket(settings.DATA_DIR, ticket_id)
    assert ticket["source"] == "discord_voice"
    assert ticket["conversation_id"] == "conv-voice-1"
    assert ticket["discord_guild_id"] == "guild-1"


async def test_patch_unknown_ticket_404(settings) -> None:
    app = create_app(settings)
    async with await _client(app) as client:
        response = await client.patch(
            "/internal/tickets/GB-00000",
            headers=_auth(),
            json={"thread_id": "t", "guild_id": "g", "discord_user_id": "1"},
        )
    assert response.status_code == 404


async def test_voice_conversation_webhook_finds_thread_created_via_internal_api(
    base_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end shape of SPEC's voice flow: the bot creates a ticket + thread via
    /internal, PATCHes the conversation_id once the ElevenLabs call starts, and the
    post-call webhook (routes/webhooks.py) must then be able to find that thread."""
    calls: list[dict] = []

    async def fake_followup(self, *, thread_id, summary, transcript):
        calls.append({"thread_id": thread_id, "summary": summary, "transcript": transcript})

    monkeypatch.setattr(DiscordClient, "post_conversation_followup", fake_followup)

    env = dict(base_env)
    env["MCP_SECRET"] = SECRET
    env["ELEVENLABS_WEBHOOK_SECRET"] = "webhook-secret"
    settings_with_webhook = make_settings(env)
    app = create_app(settings_with_webhook)

    async with await _client(app) as client:
        create_response = await client.post(
            "/internal/tickets",
            headers=_auth(),
            json={
                "source": "discord_voice",
                "discord_user_id": "7",
                "discord_username": "Voicer#0007",
                "category": "other",
                "summary": "Voice call with Voicer",
            },
        )
        ticket_id = create_response.json()["ticket_id"]
        await client.patch(
            f"/internal/tickets/{ticket_id}",
            headers=_auth(),
            json={
                "thread_id": "voice-thread-1",
                "guild_id": "guild-9",
                "discord_user_id": "7",
                "conversation_id": "conv-voice-webhook",
            },
        )

        body = json.dumps(
            {
                "type": "post_call_transcription",
                "data": {
                    "conversation_id": "conv-voice-webhook",
                    "analysis": {"transcript_summary": "Player asked about a lost item."},
                    "transcript": [{"role": "user", "message": "I lost an item."}],
                },
            }
        ).encode("utf-8")
        t = str(int(time.time()))
        signed_payload = f"{t}.".encode() + body
        v0 = hmac.new(b"webhook-secret", signed_payload, hashlib.sha256).hexdigest()

        webhook_response = await client.post(
            "/webhooks/elevenlabs", content=body, headers={"ElevenLabs-Signature": f"t={t},v0={v0}"}
        )

    assert webhook_response.status_code == 200
    assert len(calls) == 1
    assert calls[0]["thread_id"] == "voice-thread-1"
    assert calls[0]["summary"] == "Player asked about a lost item."
