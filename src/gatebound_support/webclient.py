"""httpx wrapper for the web support API (SPEC §3).

Sends ``X-Support-Token``. 5 second timeout. Never raises into callers (MCP tools/routes):
every method returns ``(data, error)`` where ``error`` is ``None`` on success, or
``"not_found"`` (web answered 404) / ``"unavailable"`` (connection error, timeout, or 5xx).
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx

from .logging import get_logger
from .settings import Settings

_TIMEOUT = 5.0

logger = get_logger("gatebound_support.webclient")

Result = tuple[dict[str, Any] | None, str | None]


class WebClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = httpx.AsyncClient(
            base_url=settings.WEB_API_BASE_URL if settings.web_api_enabled else "http://web-api-disabled.invalid",
            timeout=_TIMEOUT,
        )

    @property
    def enabled(self) -> bool:
        return self._settings.web_api_enabled

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, *, identity_header: str | None = None, params: dict[str, Any] | None = None) -> Result:
        if not self.enabled:
            return None, "unavailable"
        headers = {"X-Support-Token": self._settings.SUPPORT_API_TOKEN}
        if identity_header:
            headers["X-Support-Identity"] = identity_header
        try:
            response = await self._client.get(path, headers=headers, params=params)
        except httpx.RequestError:
            logger.warning("web api request failed", extra={"fields": {"path": path}})
            return None, "unavailable"
        if response.status_code == 404:
            return None, "not_found"
        if response.status_code == 401:
            return None, "invalid_identity"
        if response.status_code >= 500:
            logger.warning("web api 5xx", extra={"fields": {"path": path, "status": response.status_code}})
            return None, "unavailable"
        if response.status_code >= 400:
            return None, "unavailable"
        try:
            return response.json(), None
        except ValueError:
            return None, "unavailable"

    async def get_status(self) -> Result:
        return await self._get("/api/support/status")

    async def get_character(self, name: str) -> Result:
        return await self._get(f"/api/support/character/{quote(name)}")

    async def search_library(self, query: str, limit: int = 8) -> Result:
        return await self._get("/api/support/library/search", params={"q": query, "limit": limit})

    async def get_item(self, slug: str) -> Result:
        return await self._get(f"/api/support/library/item/{quote(slug)}")

    async def get_monster(self, slug: str) -> Result:
        return await self._get(f"/api/support/library/monster/{quote(slug)}")

    async def get_spell(self, slug: str) -> Result:
        return await self._get(f"/api/support/library/spell/{quote(slug)}")

    async def get_account(self, identity_header: str) -> Result:
        return await self._get("/api/support/account", identity_header=identity_header)
