"""Health and status endpoints (SPEC §6). /healthz is plain liveness, no upstream calls.
/status probes ElevenLabs and the web API, and is the only route with CORS enabled."""

from __future__ import annotations

from fastapi import APIRouter, Request, Response

from ..discord import DiscordClient
from ..elevenlabs import ElevenLabsClient
from ..settings import Settings, enabled
from ..webclient import WebClient

_ALLOWED_ORIGINS = {"https://gatebound.rosenvall.se", "http://localhost:3000"}


def _cors_headers(request: Request) -> dict[str, str]:
    origin = request.headers.get("origin")
    if origin in _ALLOWED_ORIGINS:
        return {"Access-Control-Allow-Origin": origin, "Vary": "Origin"}
    return {}


def build_router(
    settings: Settings,
    web_client: WebClient,
    elevenlabs_client: ElevenLabsClient,
    discord_client: DiscordClient,
) -> APIRouter:
    router = APIRouter()

    @router.get("/healthz")
    async def healthz() -> dict[str, bool]:
        return {"ok": True}

    @router.get("/status")
    async def status(request: Request, response: Response) -> dict[str, object]:
        for key, value in _cors_headers(request).items():
            response.headers[key] = value

        if not elevenlabs_client.enabled or not enabled(settings.ELEVENLABS_AGENT_ID):
            elevenlabs_status = "unconfigured"
        else:
            probe = await elevenlabs_client.probe_agent_status(settings.ELEVENLABS_AGENT_ID)
            elevenlabs_status = probe

        _, web_err = await web_client.get_status()
        web_status = "ok" if web_err is None else "unavailable"

        return {
            "ok": True,
            "elevenlabs": elevenlabs_status,
            "discord": "ok" if discord_client.enabled else "disabled",
            "web_api": web_status,
            "agent_id": settings.ELEVENLABS_AGENT_ID if enabled(settings.ELEVENLABS_AGENT_ID) else None,
        }

    @router.options("/status")
    async def status_preflight(request: Request) -> Response:
        return Response(status_code=204, headers=_cors_headers(request))

    return router
