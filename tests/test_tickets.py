from __future__ import annotations

import json

import httpx
import pytest
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from gatebound_support import store
from gatebound_support.app import create_app

from .conftest import make_settings, running_app


@pytest.fixture
def settings(base_env):
    env = dict(base_env)
    env["MCP_SECRET"] = "ticket-test-secret"
    env["PUBLIC_BASE_URL"] = "http://testserver"
    return make_settings(env)


async def test_create_ticket_link_tool_creates_draft_and_page_renders(settings) -> None:
    app = create_app(settings)
    async with running_app(app):
        transport = httpx.ASGITransport(app=app)
        http_client = httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
            headers={"Authorization": "Bearer ticket-test-secret"},
        )
        async with (
            http_client,
            streamable_http_client("http://testserver/mcp/", http_client=http_client) as (
                read_stream,
                write_stream,
                _get_session_id,
            ),
            ClientSession(read_stream, write_stream) as session,
        ):
            await session.initialize()
            result = await session.call_tool(
                "create_ticket_link",
                {"category": "bug", "summary": "My sword vanished after a crash.", "priority": "normal"},
            )
            assert not result.isError
            payload = json.loads(result.content[0].text)
            assert payload["status"] == "ok"
            url = payload["url"]
            assert url.startswith("http://testserver/t/")
            path = url.removeprefix("http://testserver")

        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            page = await client.get(path)
        assert page.status_code == 200
        assert "My sword vanished after a crash." in page.text
        assert "bug" in page.text


async def test_unknown_token_shows_friendly_expired_page(settings) -> None:
    app = create_app(settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/t/does-not-exist")
    assert response.status_code == 404
    assert "expired" in response.text.lower() or "no longer valid" in response.text.lower()
    assert "/ticket/new" in response.text


async def test_fallback_form_get_renders(settings) -> None:
    app = create_app(settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/ticket/new")
    assert response.status_code == 200
    assert "category" in response.text.lower()
    assert "<form" in response.text.lower()


async def test_fallback_form_post_creates_draft(settings) -> None:
    app = create_app(settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver", follow_redirects=False) as client:
        response = await client.post(
            "/ticket/new",
            data={"category": "account", "summary": "I can't log in to my account.", "character_name": "Aidenn"},
        )
    assert response.status_code == 302
    location = response.headers["location"]
    assert location.startswith("/t/")
    token = location.removeprefix("/t/")
    draft = store.get_draft(settings.DATA_DIR, token)
    assert draft is not None
    assert draft["category"] == "account"
    assert draft["character_name"] == "Aidenn"


async def test_fallback_form_post_rejects_missing_summary(settings) -> None:
    app = create_app(settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post("/ticket/new", data={"category": "bug", "summary": "   "})
    assert response.status_code == 400
    assert "category" in response.text.lower()


async def test_discord_disabled_creates_pending_manual_ticket(settings) -> None:
    app = create_app(settings)
    token = store.create_draft(settings.DATA_DIR, category="other", summary="Need help", priority="normal")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver", follow_redirects=False) as client:
        response = await client.get(f"/t/{token}/discord")
    assert response.status_code == 302
    ticket_location = response.headers["location"]
    assert ticket_location.startswith("/ticket/GB-")

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        page = await client.get(ticket_location)
    assert page.status_code == 200
    assert "pending_manual" in page.text


async def test_ticket_status_page_unknown_ticket_404(settings) -> None:
    app = create_app(settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/ticket/GB-00000")
    assert response.status_code == 404
