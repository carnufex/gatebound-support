from __future__ import annotations

import httpx
import pytest
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from gatebound_support.app import create_app
from gatebound_support.webclient import WebClient

from .conftest import make_settings, running_app


@pytest.fixture
def mcp_settings(base_env: dict[str, str]):
    env = dict(base_env)
    env["MCP_SECRET"] = "test-mcp-secret"
    return make_settings(env)


async def _fake_get_status(self):
    return {
        "online_players": 42,
        "boosted_creature": "Dragon Lord",
        "events_active": [],
        "events_upcoming": [],
        "server_time": "2026-09-13T00:00:00Z",
    }, None


async def test_mcp_requires_auth_401(mcp_settings) -> None:
    app = create_app(mcp_settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/mcp/",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            headers={"Accept": "application/json, text/event-stream", "Content-Type": "application/json"},
        )
    assert response.status_code == 401


async def test_mcp_wrong_secret_401(mcp_settings) -> None:
    app = create_app(mcp_settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/mcp/",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            headers={
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
                "Authorization": "Bearer wrong-secret",
            },
        )
    assert response.status_code == 401


async def test_mcp_initialize_and_list_tools(mcp_settings) -> None:
    app = create_app(mcp_settings)
    async with running_app(app):
        transport = httpx.ASGITransport(app=app)
        http_client = httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
            headers={"Authorization": "Bearer test-mcp-secret"},
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
            tools = await session.list_tools()
            names = {tool.name for tool in tools.tools}
            assert "get_server_status" in names
            assert "create_ticket_link" in names
            assert "escalate_to_human" in names
            assert len(names) == 9


async def test_mcp_call_get_server_status_tool(mcp_settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(WebClient, "get_status", _fake_get_status)
    app = create_app(mcp_settings)
    async with running_app(app):
        transport = httpx.ASGITransport(app=app)
        http_client = httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
            headers={"Authorization": "Bearer test-mcp-secret"},
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
            result = await session.call_tool("get_server_status", {})
            assert not result.isError
            text = result.content[0].text
            assert "42" in text
            assert "Dragon Lord" in text

