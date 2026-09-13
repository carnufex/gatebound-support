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

SECRET = "webhook-test-secret"


@pytest.fixture
def settings(base_env):
    env = dict(base_env)
    env["ELEVENLABS_WEBHOOK_SECRET"] = SECRET
    return make_settings(env)


def _sign(body: bytes, secret: str = SECRET, ts: float | None = None) -> str:
    t = str(int(ts if ts is not None else time.time()))
    signed_payload = f"{t}.".encode() + body
    v0 = hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    return f"t={t},v0={v0}"


def _payload(conversation_id: str = "conv-1") -> bytes:
    return json.dumps(
        {
            "type": "post_call_transcription",
            "data": {
                "conversation_id": conversation_id,
                "analysis": {"transcript_summary": "Player asked about a stuck quest."},
                "transcript": [
                    {"role": "agent", "message": "Hello!"},
                    {"role": "user", "message": "My quest is stuck."},
                ],
            },
        }
    ).encode("utf-8")


async def _post(app, body: bytes, headers: dict[str, str]) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.post("/webhooks/elevenlabs", content=body, headers=headers)


async def test_valid_signature_accepted_and_stored(settings) -> None:
    app = create_app(settings)
    body = _payload("conv-valid")
    response = await _post(app, body, {"ElevenLabs-Signature": _sign(body)})
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    conversation = store.get_conversation(settings.DATA_DIR, "conv-valid")
    assert conversation is not None
    assert conversation["summary"] == "Player asked about a stuck quest."
    assert len(conversation["transcript"]) == 2


async def test_invalid_signature_rejected(settings) -> None:
    app = create_app(settings)
    body = _payload()
    bad_header = _sign(body, secret="wrong-secret")
    response = await _post(app, body, {"ElevenLabs-Signature": bad_header})
    assert response.status_code == 401
    assert response.json() == {"error": "invalid_signature"}


async def test_missing_signature_header_rejected(settings) -> None:
    app = create_app(settings)
    body = _payload()
    response = await _post(app, body, {})
    assert response.status_code == 401


async def test_expired_timestamp_rejected(settings) -> None:
    app = create_app(settings)
    body = _payload()
    old_header = _sign(body, ts=time.time() - 3600)
    response = await _post(app, body, {"ElevenLabs-Signature": old_header})
    assert response.status_code == 401


async def test_disabled_secret_returns_ok_without_storing(base_env) -> None:
    settings = make_settings(base_env)  # ELEVENLABS_WEBHOOK_SECRET == "unset"
    app = create_app(settings)
    body = _payload("conv-disabled")
    response = await _post(app, body, {})
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert store.get_conversation(settings.DATA_DIR, "conv-disabled") is None


async def test_ignores_non_transcription_events(settings) -> None:
    app = create_app(settings)
    body = json.dumps({"type": "something_else", "data": {"conversation_id": "conv-x"}}).encode("utf-8")
    response = await _post(app, body, {"ElevenLabs-Signature": _sign(body)})
    assert response.status_code == 200
    assert store.get_conversation(settings.DATA_DIR, "conv-x") is None


async def test_posts_followup_to_discord_thread_when_ticket_exists(settings, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict] = []

    async def fake_followup(self, *, thread_id, summary, transcript):
        calls.append({"thread_id": thread_id, "summary": summary, "transcript": transcript})

    monkeypatch.setattr(DiscordClient, "post_conversation_followup", fake_followup)

    token = store.create_draft(
        settings.DATA_DIR, category="bug", summary="Stuck quest", priority="normal", conversation_id="conv-thread"
    )
    ticket_id = store.create_ticket(
        settings.DATA_DIR,
        token=token,
        category="bug",
        summary="Stuck quest",
        priority="normal",
        account_name=None,
        character_name=None,
        conversation_id="conv-thread",
        status="open",
    )
    store.update_ticket_discord(settings.DATA_DIR, ticket_id, guild_id="g1", thread_id="t1", user_id="u1")

    app = create_app(settings)
    body = _payload("conv-thread")
    response = await _post(app, body, {"ElevenLabs-Signature": _sign(body)})

    assert response.status_code == 200
    assert len(calls) == 1
    assert calls[0]["thread_id"] == "t1"
    assert calls[0]["summary"] == "Player asked about a stuck quest."
