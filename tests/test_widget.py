from __future__ import annotations

import httpx
import respx

from gatebound_support.app import create_app
from gatebound_support.elevenlabs import ElevenLabsClient
from gatebound_support.ratelimit import RateLimiter

from .conftest import make_settings

ORIGIN = "https://gatebound.rosenvall.se"
SIGNED = "wss://api.elevenlabs.io/v1/convai/conversation?agent_id=agent_x&conversation_signature=sig123"


def _configured(base_env: dict[str, str]) -> dict[str, str]:
    env = dict(base_env)
    env["ELEVENLABS_API_KEY"] = "xi-secret-key"
    env["ELEVENLABS_AGENT_ID"] = "agent_x"
    return env


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


async def test_unconfigured_returns_503(base_env) -> None:
    app = create_app(make_settings(base_env))
    async with _client(app) as client:
        response = await client.get("/widget/signed-url", headers={"Origin": ORIGIN})
    assert response.status_code == 503
    assert response.json() == {"status": "unconfigured"}
    assert response.headers["access-control-allow-origin"] == ORIGIN


@respx.mock
async def test_mints_signed_url_with_api_key_and_cors(base_env) -> None:
    route = respx.get("https://api.elevenlabs.io/v1/convai/conversation/get-signed-url").mock(
        return_value=httpx.Response(200, json={"signed_url": SIGNED})
    )
    app = create_app(make_settings(_configured(base_env)))
    async with _client(app) as client:
        response = await client.get("/widget/signed-url", headers={"Origin": ORIGIN})
    assert response.status_code == 200
    assert response.json() == {"signed_url": SIGNED}
    assert response.headers["access-control-allow-origin"] == ORIGIN
    assert response.headers["cache-control"] == "no-store"
    assert route.called
    request = route.calls.last.request
    assert request.headers["xi-api-key"] == "xi-secret-key"
    assert request.url.params["agent_id"] == "agent_x"
    assert "xi-secret-key" not in response.text


@respx.mock
async def test_upstream_failure_is_503_unavailable(base_env) -> None:
    respx.get("https://api.elevenlabs.io/v1/convai/conversation/get-signed-url").mock(
        return_value=httpx.Response(401, json={"detail": "nope"})
    )
    app = create_app(make_settings(_configured(base_env)))
    async with _client(app) as client:
        response = await client.get("/widget/signed-url")
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}


@respx.mock
async def test_upstream_timeout_is_503_unavailable(base_env) -> None:
    respx.get("https://api.elevenlabs.io/v1/convai/conversation/get-signed-url").mock(
        side_effect=httpx.ReadTimeout("slow")
    )
    app = create_app(make_settings(_configured(base_env)))
    async with _client(app) as client:
        response = await client.get("/widget/signed-url")
    assert response.status_code == 503


async def test_foreign_origin_is_forbidden_without_cors(base_env) -> None:
    app = create_app(make_settings(_configured(base_env)))
    async with _client(app) as client:
        response = await client.get("/widget/signed-url", headers={"Origin": "https://evil.example"})
    assert response.status_code == 403
    assert "access-control-allow-origin" not in response.headers


async def test_localhost_origin_is_not_allowed(base_env) -> None:
    """Unlike /status, the signed-URL endpoint is production-origin only."""
    app = create_app(make_settings(_configured(base_env)))
    async with _client(app) as client:
        response = await client.get("/widget/signed-url", headers={"Origin": "http://localhost:3000"})
    assert response.status_code == 403


async def test_preflight(base_env) -> None:
    app = create_app(make_settings(base_env))
    async with _client(app) as client:
        response = await client.options("/widget/signed-url", headers={"Origin": ORIGIN})
    assert response.status_code == 204
    assert response.headers["access-control-allow-origin"] == ORIGIN
    assert "GET" in response.headers["access-control-allow-methods"]


@respx.mock
async def test_per_client_rate_limit(base_env) -> None:
    respx.get("https://api.elevenlabs.io/v1/convai/conversation/get-signed-url").mock(
        return_value=httpx.Response(200, json={"signed_url": SIGNED})
    )
    app = create_app(make_settings(_configured(base_env)))
    async with _client(app) as client:
        codes_a = [
            (await client.get("/widget/signed-url", headers={"CF-Connecting-IP": "203.0.113.1"})).status_code
            for _ in range(11)
        ]
        other = await client.get("/widget/signed-url", headers={"CF-Connecting-IP": "203.0.113.2"})
    assert codes_a[:10] == [200] * 10
    assert codes_a[10] == 429
    assert other.status_code == 200


def test_rate_limiter_refills_and_prunes() -> None:
    limiter = RateLimiter(capacity=2, refill_per_second=1.0, max_keys=2)
    assert limiter.allow("a", now=0.0)
    assert limiter.allow("a", now=0.0)
    assert not limiter.allow("a", now=0.0)
    assert limiter.allow("a", now=1.0)  # one token refilled
    assert not limiter.allow("a", now=1.0)
    # Third distinct key pushes past max_keys; "a" is still active, so prune keeps it and
    # evicts by age only when everyone is active.
    assert limiter.allow("b", now=1.0)
    assert limiter.allow("c", now=10.0)  # "a" and "b" have fully refilled by now -> pruned
    assert set(limiter._buckets) == {"c"}


@respx.mock
async def test_client_get_signed_url_never_raises(base_env) -> None:
    respx.get("https://api.elevenlabs.io/v1/convai/conversation/get-signed-url").mock(
        return_value=httpx.Response(200, content=b"not json")
    )
    client = ElevenLabsClient(make_settings(_configured(base_env)))
    assert await client.get_signed_url("agent_x") is None
    assert await client.get_signed_url("unset") is None
    await client.aclose()
