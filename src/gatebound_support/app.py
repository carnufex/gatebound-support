"""FastAPI application factory: mounts the MCP server at /mcp and the HTTP routers for the
ticket flow, webhooks, and status endpoints (SPEC §5, §6)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from .discord import DiscordClient
from .elevenlabs import ElevenLabsClient
from .logging import configure_logging, get_logger
from .mcp_server import build_mcp_server, wrap_with_auth
from .routes import status as status_routes
from .routes import tickets as ticket_routes
from .routes import webhooks as webhook_routes
from .settings import Settings, get_settings
from .store import db_path
from .webclient import WebClient

logger = get_logger("gatebound_support.app")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.LOG_LEVEL)

    web_client = WebClient(settings)
    discord_client = DiscordClient(settings)
    elevenlabs_client = ElevenLabsClient(settings)

    mcp = build_mcp_server(settings, web_client, discord_client)
    mcp_asgi_app = wrap_with_auth(mcp, settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        Path(settings.DATA_DIR).mkdir(parents=True, exist_ok=True)
        # Touch the database once at startup so schema creation happens before traffic arrives.
        from . import store

        with store.get_connection(settings.DATA_DIR):
            pass
        logger.info("startup", extra={"fields": {"data_dir": settings.DATA_DIR, "db": db_path(settings.DATA_DIR)}})
        # FastMCP's streamable-HTTP session manager needs its task group entered for the
        # life of the app; Mount does not forward ASGI lifespan events to sub-apps, so we
        # enter it here instead (see mcp_server.wrap_with_auth for why).
        async with mcp.session_manager.run():
            try:
                yield
            finally:
                await web_client.aclose()
                await discord_client.aclose()
                await elevenlabs_client.aclose()

    app = FastAPI(title="gatebound-support", lifespan=lifespan)
    app.state.settings = settings
    app.state.web_client = web_client
    app.state.discord_client = discord_client
    app.state.elevenlabs_client = elevenlabs_client

    app.include_router(ticket_routes.build_router(settings, web_client, discord_client))
    app.include_router(webhook_routes.build_router(settings, discord_client, elevenlabs_client))
    app.include_router(status_routes.build_router(settings, web_client, elevenlabs_client, discord_client))

    app.mount("/mcp", mcp_asgi_app)

    return app
