"""ElevenLabs API client: agent probe, conversation fetch, webhook signature
verification, and the knowledge-base calls used by kb-sync.

Endpoints verified against https://elevenlabs.io/docs/api-reference (fetched 2026-09-13):
  GET   /v1/convai/agents/{agent_id}
  GET   /v1/convai/conversation/get-signed-url?agent_id=...   (widget: private agent)
  GET   /v1/convai/conversations/{conversation_id}
  GET   /v1/convai/knowledge-base/{documentation_id}
  POST  /v1/convai/knowledge-base/text
  PATCH /v1/convai/knowledge-base/{documentation_id}
  DELETE /v1/convai/knowledge-base/{documentation_id}?force=true
  POST  /v1/convai/knowledge-base/{documentation_id}/rag-index
  PATCH /v1/convai/agents/{agent_id}   (conversation_config.agent.prompt.knowledge_base)

Auth header: ``xi-api-key``.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any, Literal

import httpx

from .logging import get_logger
from .settings import Settings

API_BASE = "https://api.elevenlabs.io"
_TIMEOUT = 5.0
_AGENT_PROBE_CACHE_SECONDS = 60
WEBHOOK_TIMESTAMP_TOLERANCE_SECONDS = 30 * 60

logger = get_logger("gatebound_support.elevenlabs")

AgentProbeStatus = Literal["ok", "degraded", "unconfigured"]


def verify_webhook_signature(*, secret: str, header: str, raw_body: bytes | str, now: float | None = None) -> bool:
    """Verifies ``ElevenLabs-Signature: t=<unix>,v0=<hex hmac_sha256(secret, "<t>.<raw body>")>``.

    Ported 1:1 from oncall-voice-copilot/services/oncall-tools/src/elevenlabs.ts
    (verifyElevenLabsSignature) — same header format, same 30 minute tolerance.
    """
    if now is None:
        now = time.time()
    parts: dict[str, str] = {}
    for piece in header.split(","):
        piece = piece.strip()
        if not piece or "=" not in piece:
            continue
        key, _, value = piece.partition("=")
        parts[key] = value
    t = parts.get("t")
    v0 = parts.get("v0")
    if not t or not v0:
        return False
    try:
        ts = float(t)
    except ValueError:
        return False
    if abs(now - ts) > WEBHOOK_TIMESTAMP_TOLERANCE_SECONDS:
        return False
    if isinstance(raw_body, str):
        raw_body = raw_body.encode("utf-8")
    signed_payload = f"{t}.".encode() + raw_body
    expected = hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, v0)


class ElevenLabsClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = httpx.AsyncClient(base_url=API_BASE, timeout=_TIMEOUT)
        self._agent_probe_cache: dict[str, tuple[float, AgentProbeStatus]] = {}

    @property
    def enabled(self) -> bool:
        return self._settings.elevenlabs_enabled

    async def aclose(self) -> None:
        await self._client.aclose()

    def _headers(self) -> dict[str, str]:
        return {"xi-api-key": self._settings.ELEVENLABS_API_KEY}

    # ---- status probe ----

    async def probe_agent_status(self, agent_id: str | None = None) -> AgentProbeStatus:
        agent_id = agent_id or self._settings.ELEVENLABS_AGENT_ID
        if not self.enabled or not agent_id or agent_id == "unset":
            return "unconfigured"
        cached = self._agent_probe_cache.get(agent_id)
        now = time.monotonic()
        if cached and now - cached[0] < _AGENT_PROBE_CACHE_SECONDS:
            return cached[1]
        status: AgentProbeStatus
        try:
            response = await self._client.get(f"/v1/convai/agents/{agent_id}", headers=self._headers())
            status = "ok" if response.status_code == 200 else "degraded"
        except httpx.RequestError:
            status = "degraded"
        self._agent_probe_cache[agent_id] = (now, status)
        return status

    # ---- signed URL for the website widget (private agent) ----

    async def get_signed_url(self, agent_id: str) -> str | None:
        """Mints a short-lived ``wss://`` signed URL that lets a browser start one conversation
        with the (auth-enabled) agent. None on any failure; never raises. The URL carries the
        conversation signature and must not be logged."""
        if not self.enabled or not agent_id or agent_id == "unset":
            return None
        try:
            response = await self._client.get(
                "/v1/convai/conversation/get-signed-url",
                params={"agent_id": agent_id},
                headers=self._headers(),
            )
        except httpx.RequestError as exc:
            logger.warning("signed_url_request_error", extra={"fields": {"error": type(exc).__name__}})
            return None
        if response.status_code != 200:
            logger.warning("signed_url_bad_status", extra={"fields": {"status_code": response.status_code}})
            return None
        try:
            signed_url = response.json().get("signed_url")
        except ValueError:
            return None
        return signed_url if isinstance(signed_url, str) and signed_url else None

    # ---- conversation fetch ----

    async def get_conversation(self, conversation_id: str) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        try:
            response = await self._client.get(f"/v1/convai/conversations/{conversation_id}", headers=self._headers())
        except httpx.RequestError:
            return None
        if response.status_code != 200:
            return None
        return response.json()

    # ---- knowledge base (kb-sync) ----

    async def get_document(self, document_id: str) -> dict[str, Any] | None:
        """Returns the document, or None if it doesn't exist (or any request error)."""
        try:
            response = await self._client.get(f"/v1/convai/knowledge-base/{document_id}", headers=self._headers())
        except httpx.RequestError:
            return None
        if response.status_code != 200:
            return None
        return response.json()

    async def create_text_document(self, *, name: str, text: str) -> dict[str, Any]:
        response = await self._client.post(
            "/v1/convai/knowledge-base/text",
            headers=self._headers(),
            json={"text": text, "name": name},
        )
        response.raise_for_status()
        return response.json()

    async def update_document_content(self, *, document_id: str, content: str) -> dict[str, Any]:
        response = await self._client.patch(
            f"/v1/convai/knowledge-base/{document_id}",
            headers=self._headers(),
            json={"content": content},
        )
        response.raise_for_status()
        return response.json()

    async def delete_document(self, *, document_id: str, force: bool = True) -> None:
        response = await self._client.delete(
            f"/v1/convai/knowledge-base/{document_id}",
            headers=self._headers(),
            params={"force": "true" if force else "false"},
        )
        if response.status_code not in (200, 204, 404):
            response.raise_for_status()

    async def request_rag_index(self, *, document_id: str, model: str = "e5_mistral_7b_instruct") -> dict[str, Any]:
        response = await self._client.post(
            f"/v1/convai/knowledge-base/{document_id}/rag-index",
            headers=self._headers(),
            json={"model": model},
        )
        response.raise_for_status()
        return response.json()

    async def get_agent(self, agent_id: str) -> dict[str, Any]:
        response = await self._client.get(f"/v1/convai/agents/{agent_id}", headers=self._headers())
        response.raise_for_status()
        return response.json()

    async def update_agent_knowledge_base(self, *, agent_id: str, knowledge_base: list[dict[str, Any]]) -> None:
        response = await self._client.patch(
            f"/v1/convai/agents/{agent_id}",
            headers=self._headers(),
            json={"conversation_config": {"agent": {"prompt": {"knowledge_base": knowledge_base}}}},
        )
        response.raise_for_status()
