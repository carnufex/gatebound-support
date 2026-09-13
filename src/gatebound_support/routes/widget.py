"""Signed-URL minting for the website widget (SPEC §6a).

The agent is private (``platform_settings.auth.enable_auth``), so the embed cannot start a
conversation with a bare ``agent-id``. The web page asks this endpoint for a signed URL and
hands it to ``<elevenlabs-convai signed-url=...>``; the widget uses the URL's
``conversation_signature`` both for its config fetch and for the conversation itself.

Abuse surface: whoever can call this can spend ElevenLabs credits, so
  * CORS is opened for the production website origin only,
  * a per-client and a global token bucket cap how many URLs leave per minute,
  * the ElevenLabs call has the client's 5 s timeout,
  * the API key never leaves ``ElevenLabsClient`` and neither the key nor the signed URL is
    ever logged (the URL carries the signature — it is a credential).
Cost is still bounded upstream by the agent's ``call_limits`` (daily 100, concurrency 3).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from ..elevenlabs import ElevenLabsClient
from ..logging import get_logger, log_event
from ..ratelimit import RateLimiter
from ..settings import Settings, enabled

ALLOWED_ORIGIN = "https://gatebound.rosenvall.se"

# 10 URLs per client per minute (burst 10), 120 per minute for the whole service.
PER_CLIENT_CAPACITY = 10
PER_CLIENT_REFILL_PER_SECOND = 10 / 60
GLOBAL_CAPACITY = 120
GLOBAL_REFILL_PER_SECOND = 120 / 60

logger = get_logger("gatebound_support.widget")


def client_key(request: Request) -> str:
    """The rate-limit key for a request.

    Traffic arrives via Cloudflare Tunnel → cluster gateway → uvicorn. uvicorn already
    rewrites ``request.client`` from ``X-Forwarded-For`` (``forwarded_allow_ips="*"``), but
    that takes the leftmost entry, which a caller can prepend to. ``CF-Connecting-IP`` is set
    by Cloudflare itself and cannot be spoofed through the tunnel, so it wins when present.
    """
    cf_ip = request.headers.get("cf-connecting-ip", "").strip()
    if cf_ip:
        return cf_ip
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def _cors_headers(request: Request) -> dict[str, str]:
    if request.headers.get("origin") == ALLOWED_ORIGIN:
        return {
            "Access-Control-Allow-Origin": ALLOWED_ORIGIN,
            "Access-Control-Allow-Methods": "GET, OPTIONS",
            "Access-Control-Max-Age": "600",
            "Vary": "Origin",
        }
    return {}


def build_router(
    settings: Settings,
    elevenlabs_client: ElevenLabsClient,
    *,
    per_client: RateLimiter | None = None,
    global_limit: RateLimiter | None = None,
) -> APIRouter:
    router = APIRouter()
    per_client = per_client or RateLimiter(
        capacity=PER_CLIENT_CAPACITY, refill_per_second=PER_CLIENT_REFILL_PER_SECOND
    )
    global_limit = global_limit or RateLimiter(
        capacity=GLOBAL_CAPACITY, refill_per_second=GLOBAL_REFILL_PER_SECOND
    )

    def _json(
        request: Request, status_code: int, body: dict[str, object], **extra_headers: str
    ) -> JSONResponse:
        headers = {"Cache-Control": "no-store", **_cors_headers(request), **extra_headers}
        return JSONResponse(body, status_code=status_code, headers=headers)

    @router.get("/widget/signed-url")
    async def widget_signed_url(request: Request) -> Response:
        origin = request.headers.get("origin")
        if origin is not None and origin != ALLOWED_ORIGIN:
            # Browsers would block the response anyway; be explicit and cheap about it.
            return _json(request, 403, {"status": "forbidden"})

        key = client_key(request)
        if not per_client.allow(key) or not global_limit.allow("global"):
            log_event(logger, "signed_url_rate_limited", level=logging.WARNING, client=key)
            return _json(
                request,
                429,
                {"status": "rate_limited"},
                **{"Retry-After": str(per_client.retry_after_seconds())},
            )

        if not elevenlabs_client.enabled or not enabled(settings.ELEVENLABS_AGENT_ID):
            return _json(request, 503, {"status": "unconfigured"})

        signed_url = await elevenlabs_client.get_signed_url(settings.ELEVENLABS_AGENT_ID)
        if signed_url is None:
            log_event(logger, "signed_url_unavailable", level=logging.WARNING, client=key)
            return _json(request, 503, {"status": "unavailable"})

        log_event(logger, "signed_url_issued", client=key)
        return _json(request, 200, {"signed_url": signed_url})

    @router.options("/widget/signed-url")
    async def widget_signed_url_preflight(request: Request) -> Response:
        return Response(status_code=204, headers=_cors_headers(request))

    return router
