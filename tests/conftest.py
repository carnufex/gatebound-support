from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from fastapi import FastAPI

from gatebound_support.settings import Settings


@asynccontextmanager
async def running_app(app: FastAPI) -> AsyncIterator[FastAPI]:
    """Drives the app's ASGI lifespan (startup/shutdown) around a block of requests —
    needed for anything that touches the FastMCP session manager, since httpx's
    ASGITransport does not trigger lifespan events on its own."""
    async with app.router.lifespan_context(app):
        yield app


@pytest.fixture
def base_env(tmp_path: Path) -> dict[str, str]:
    return {
        "DATA_DIR": str(tmp_path / "data"),
        "PUBLIC_BASE_URL": "http://testserver",
        "WEB_API_BASE_URL": "unset",
        "WEB_PUBLIC_URL": "http://localhost:3000",
        "LOG_LEVEL": "WARNING",
        "SUPPORT_API_TOKEN": "unset",
        "SUPPORT_IDENTITY_SECRET": "unset",
        "MCP_SECRET": "unset",
        "ELEVENLABS_API_KEY": "unset",
        "ELEVENLABS_WEBHOOK_SECRET": "unset",
        "ELEVENLABS_AGENT_ID": "unset",
        "DISCORD_CLIENT_ID": "unset",
        "DISCORD_CLIENT_SECRET": "unset",
        "DISCORD_BOT_TOKEN": "unset",
        "DISCORD_GUILD_ID": "unset",
        "DISCORD_FORUM_CHANNEL_ID": "unset",
        "DISCORD_TAG_OPEN_ID": "unset",
        "DISCORD_STAFF_WEBHOOK_URL": "unset",
    }


def make_settings(overrides: dict[str, str]) -> Settings:
    return Settings(**overrides)
