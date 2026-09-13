from __future__ import annotations

import httpx

from gatebound_support.app import create_app

from .conftest import make_settings


async def test_healthz_has_no_upstream_calls(base_env) -> None:
    settings = make_settings(base_env)
    app = create_app(settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"ok": True}


async def test_status_shape_everything_unset(base_env) -> None:
    settings = make_settings(base_env)
    app = create_app(settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/status")
    assert response.status_code == 200
    body = response.json()
    assert body == {
        "ok": True,
        "elevenlabs": "unconfigured",
        "discord": "disabled",
        "web_api": "unavailable",
        "agent_id": None,
    }


async def test_status_sets_cors_header_for_allowed_origin(base_env) -> None:
    settings = make_settings(base_env)
    app = create_app(settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/status", headers={"Origin": "https://gatebound.rosenvall.se"})
    assert response.headers.get("access-control-allow-origin") == "https://gatebound.rosenvall.se"


async def test_status_omits_cors_header_for_disallowed_origin(base_env) -> None:
    settings = make_settings(base_env)
    app = create_app(settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/status", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in response.headers
