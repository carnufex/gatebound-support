"""The MCP server exposed at /mcp (SPEC §4): FastMCP, streamable HTTP, stateless, JSON
responses. Auth is a bearer secret (not OAuth) checked by a plain ASGI middleware wrapped
around the FastMCP streamable-HTTP app, which also stashes the per-request
``X-Support-Identity`` / ``X-Conversation-Id`` headers into contextvars — FastMCP tools have
no direct access to the incoming HTTP request, so this is the only way to get them there.

Verified against the installed mcp==1.30.0 package (mcp/server/fastmcp/server.py):
``FastMCP(stateless_http=True, json_response=True)`` + ``.streamable_http_app()``.
"""

from __future__ import annotations

import functools
import hmac
import time
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from . import store
from .discord import DiscordClient
from .identity import verify_identity_token
from .logging import get_logger, log_tool_call
from .settings import Settings, enabled
from .webclient import WebClient

logger = get_logger("gatebound_support.mcp")

_identity_header: ContextVar[str | None] = ContextVar("identity_header", default=None)
_conversation_id: ContextVar[str | None] = ContextVar("conversation_id", default=None)

Category = Literal["account", "bug", "payment", "report_player", "other"]
Priority = Literal["normal", "high"]
EscalationReason = Literal["frustrated", "requested_human", "out_of_scope", "safety"]


def current_conversation_id() -> str | None:
    return _conversation_id.get()


def current_identity_header() -> str | None:
    return _identity_header.get()


class McpAuthContextMiddleware:
    """Pure ASGI middleware: bearer auth against MCP_SECRET, then stashes context headers."""

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self._app = app
        self._settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        request = Request(scope)
        # The inner app is mounted at "/" and serves exactly MCP_PATH, so anything else
        # that reaches this middleware is an unknown path, not an auth failure.
        if scope.get("path", "") == MCP_PATH + "/":
            # accept the trailing-slash spelling too, without a redirect
            scope["path"] = MCP_PATH
            scope["raw_path"] = MCP_PATH.encode()
        if scope.get("path", "") != MCP_PATH:
            response = JSONResponse({"error": "not_found"}, status_code=404)
            await response(scope, receive, send)
            return
        secret = self._settings.MCP_SECRET
        auth_header = request.headers.get("authorization", "")
        expected = f"Bearer {secret}"
        if not enabled(secret) or not hmac.compare_digest(auth_header, expected):
            response = JSONResponse({"error": "unauthorized"}, status_code=401)
            await response(scope, receive, send)
            return

        identity_token = _identity_header.set(request.headers.get("x-support-identity"))
        conversation_token = _conversation_id.set(request.headers.get("x-conversation-id"))
        try:
            await self._app(scope, receive, send)
        finally:
            _identity_header.reset(identity_token)
            _conversation_id.reset(conversation_token)


def _instrumented(name: str) -> Callable[[Callable[..., Awaitable[dict[str, Any]]]], Callable[..., Awaitable[dict[str, Any]]]]:
    """Wraps a tool body: never lets an exception escape (the runtime hides error bodies
    from the model — SPEC §4), and logs name/duration/status/conversation id (SPEC §8)."""

    def decorator(fn: Callable[..., Awaitable[dict[str, Any]]]) -> Callable[..., Awaitable[dict[str, Any]]]:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> dict[str, Any]:
            start = time.perf_counter()
            conversation_id = _conversation_id.get()
            try:
                result = await fn(*args, **kwargs)
            except Exception:
                logger.exception("tool call raised", extra={"fields": {"tool": name}})
                result = {
                    "status": "unavailable",
                    "spoken_summary": "Something went wrong on our side. Please try again in a moment.",
                }
            duration_ms = (time.perf_counter() - start) * 1000
            log_tool_call(name, duration_ms, str(result.get("status", "ok")), conversation_id)
            return result

        return wrapper

    return decorator


def build_mcp_server(settings: Settings, web_client: WebClient, discord_client: DiscordClient) -> FastMCP:
    mcp = FastMCP(
        name="gatebound-support",
        instructions="Support tools for the Gatebound game: server status, character/library lookups, "
        "the player's own account, and creating a support ticket.",
        stateless_http=True,
        json_response=True,
        # FastMCP auto-enables DNS-rebinding Host-header protection whenever its (unused
        # here — we never call mcp.run()) `host` setting defaults to "127.0.0.1", which
        # would reject every real request (Host: gatebound-support.rosenvall.se). Auth is
        # the bearer secret checked by McpAuthContextMiddleware below, not the Host header,
        # so this protection is switched off explicitly rather than accidentally.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    # ---- helpers shared by tools ----

    async def _resolve_and_fetch(kind: Literal["item", "monster", "spell"], name: str) -> tuple[dict[str, Any] | None, str | None]:
        # Bosses are monsters too: try the monster kind first, then boss. The web API
        # filters by kind and puts exact name matches first (so "dragon" is Dragon, not
        # "dragon ham"), which is why the kind is passed instead of post-filtering.
        matches: list[dict[str, Any]] = []
        for search_kind in (["monster", "boss"] if kind == "monster" else [kind]):
            results, err = await web_client.search_library(name, limit=8, kind=search_kind)
            if err:
                return None, err
            matches = [r for r in (results or {}).get("results", []) if r.get("kind") == search_kind]
            if matches:
                break
        if not matches:
            return None, "not_found"
        exact = [r for r in matches if r.get("name", "").lower() == name.strip().lower()]
        slug = (exact or matches)[0]["slug"]
        fetcher = {"item": web_client.get_item, "monster": web_client.get_monster, "spell": web_client.get_spell}[kind]
        return await fetcher(slug)

    def _account_name_from_identity() -> str | None:
        identity = verify_identity_token(_identity_header.get(), settings)
        return identity["name"] if identity else None

    async def _create_ticket_draft(*, category: Category, summary: str, priority: Priority) -> dict[str, Any]:
        account_name = _account_name_from_identity()
        conversation_id = _conversation_id.get()
        token = store.create_draft(
            settings.DATA_DIR,
            category=category,
            summary=summary,
            priority=priority,
            account_name=account_name,
            conversation_id=conversation_id,
        )
        base_url = settings.PUBLIC_BASE_URL.rstrip("/") if enabled(settings.PUBLIC_BASE_URL) else ""
        url = f"{base_url}/t/{token}"
        return {"token": token, "url": url}

    # ---- tools (SPEC §4) ----

    @mcp.tool()
    @_instrumented("get_server_status")
    async def get_server_status() -> dict[str, Any]:
        """Get Gatebound's current server status: online players, the boosted creature, and events."""
        data, err = await web_client.get_status()
        if err:
            return {"status": "unavailable", "spoken_summary": "I can't reach the game server status right now."}
        online = data.get("online_players", 0)
        boosted = data.get("boosted_creature")
        active = data.get("events_active") or []
        parts = [f"There are {online} players online right now."]
        if boosted:
            parts.append(f"The boosted creature is {boosted}.")
        if active:
            names = ", ".join(e["name"] for e in active)
            parts.append(f"Active events: {names}.")
        return {"status": "ok", **data, "spoken_summary": " ".join(parts)}

    @mcp.tool()
    @_instrumented("lookup_character")
    async def lookup_character(name: str) -> dict[str, Any]:
        """Look up a Gatebound character's public profile by name."""
        data, err = await web_client.get_character(name)
        if err == "not_found":
            return {"status": "not_found", "spoken_summary": f"I couldn't find a character named {name}."}
        if err:
            return {"status": "unavailable", "spoken_summary": "I can't reach the character lookup right now."}
        online = "online" if data.get("online") else "offline"
        guild = data.get("guild")
        guild_text = f" in the guild {guild['name']} as {guild['rank']}" if guild else ""
        summary = f"{data['name']} is a level {data['level']} {data['vocation']}, currently {online}{guild_text}."
        return {"status": "ok", **data, "spoken_summary": summary}

    @mcp.tool()
    @_instrumented("search_library")
    async def search_library(query: str) -> dict[str, Any]:
        """Search the Gatebound library (items, monsters, bosses, spells) by name."""
        data, err = await web_client.search_library(query, limit=8)
        if err:
            return {"status": "unavailable", "spoken_summary": "I can't reach the library search right now."}
        results = data.get("results", [])
        if not results:
            return {"status": "not_found", "spoken_summary": f"I couldn't find anything in the library for {query}."}
        listing = ", ".join(f"{r['name']} ({r['kind']})" for r in results[:5])
        summary = f"I found {len(results)} results for {query}: {listing}."
        return {"status": "ok", "results": results, "spoken_summary": summary}

    @mcp.tool()
    @_instrumented("get_item")
    async def get_item(name: str) -> dict[str, Any]:
        """Look up a Gatebound item by name (resolves the name to the library entry first)."""
        data, err = await _resolve_and_fetch("item", name)
        if err == "not_found":
            return {"status": "not_found", "spoken_summary": f"I couldn't find an item named {name}."}
        if err:
            return {"status": "unavailable", "spoken_summary": "I can't reach the library right now."}
        summary = f"{data['name']} is a {data['category']}."
        if data.get("dropped_by"):
            top = data["dropped_by"][0]
            summary += f" It can drop from {top['monster']}."
        return {"status": "ok", **data, "spoken_summary": summary}

    @mcp.tool()
    @_instrumented("get_monster")
    async def get_monster(name: str) -> dict[str, Any]:
        """Look up a Gatebound monster or boss by name (resolves the name to the library entry first)."""
        data, err = await _resolve_and_fetch("monster", name)
        if err == "not_found":
            return {"status": "not_found", "spoken_summary": f"I couldn't find a monster named {name}."}
        if err:
            return {"status": "unavailable", "spoken_summary": "I can't reach the library right now."}
        kind = "boss" if data.get("boss") else "monster"
        summary = f"{data['name']} is a {kind} with {data['hp']} hit points, worth {data['exp']} experience."
        return {"status": "ok", **data, "spoken_summary": summary}

    @mcp.tool()
    @_instrumented("get_spell")
    async def get_spell(name: str) -> dict[str, Any]:
        """Look up a Gatebound spell by name (resolves the name to the library entry first)."""
        data, err = await _resolve_and_fetch("spell", name)
        if err == "not_found":
            return {"status": "not_found", "spoken_summary": f"I couldn't find a spell named {name}."}
        if err:
            return {"status": "unavailable", "spoken_summary": "I can't reach the library right now."}
        summary = f"{data['name']} is learned at level {data.get('level')} and costs {data.get('mana')} mana."
        return {"status": "ok", **data, "spoken_summary": summary}

    @mcp.tool()
    @_instrumented("get_my_account")
    async def get_my_account() -> dict[str, Any]:
        """Get the logged-in player's own account overview. Requires the player to be logged in."""
        identity_header = _identity_header.get()
        if not identity_header:
            return {
                "status": "not_logged_in",
                "spoken_summary": "You don't seem to be logged in. Please log in on the Gatebound website "
                "to check your account.",
            }
        data, err = await web_client.get_account(identity_header)
        if err in ("invalid_identity", "not_found"):
            return {
                "status": "not_logged_in",
                "spoken_summary": "I can't verify your login right now. Please log in on the Gatebound website "
                "and try again.",
            }
        if err:
            return {"status": "unavailable", "spoken_summary": "I can't reach the account service right now."}
        chars = data.get("characters", [])
        char_names = ", ".join(c["name"] for c in chars) if chars else "no characters yet"
        summary = f"Your account {data['account_name']} has {data.get('premium_days_left', 0)} premium days left and {char_names}."
        return {"status": "ok", **data, "spoken_summary": summary}

    @mcp.tool()
    @_instrumented("create_ticket_link")
    async def create_ticket_link(category: Category, summary: str, priority: Priority = "normal") -> dict[str, Any]:
        """Create a support ticket draft and return a link the player continues in Discord.

        category: one of "account", "bug", "payment", "report_player", "other".
        summary: what the player needs, 1-3 sentences, written for staff.
        priority: "normal" or "high".
        """
        draft = await _create_ticket_draft(category=category, summary=summary, priority=priority)
        return {
            "status": "ok",
            "url": draft["url"],
            "ticket_ref": draft["token"],
            "spoken_summary": "I've prepared a ticket link. When the player opens it and signs in with "
            "Discord, the ticket is created and the team answers there. The link is in the chat; do not "
            "read it aloud.",
        }

    @mcp.tool()
    @_instrumented("escalate_to_human")
    async def escalate_to_human(summary: str, reason: EscalationReason) -> dict[str, Any]:
        """Escalate to a human staff member: same as create_ticket_link but high priority, with a
        staff heads-up sent immediately if the staff webhook is configured.

        reason: one of "frustrated", "requested_human", "out_of_scope", "safety".
        """
        draft = await _create_ticket_draft(category="other", summary=summary, priority="high")
        if discord_client.staff_webhook_enabled:
            await discord_client.post_staff_webhook(
                f"Escalation ({reason}): {summary}\n{draft['url']}"
            )
        return {
            "status": "ok",
            "url": draft["url"],
            "ticket_ref": draft["token"],
            "spoken_summary": "I've flagged this for the team and prepared a ticket link. When the player opens "
            "it and signs in with Discord, the ticket is created and the team answers there. The link is "
            "in the chat; do not read it aloud.",
        }

    return mcp


def wrap_with_auth(mcp: FastMCP, settings: Settings) -> ASGIApp:
    """Builds the ASGI app to mount at /mcp: FastMCP's streamable-HTTP app wrapped in the
    bearer-auth + context-header middleware.

    Note: FastMCP's own ``streamable_http_app()`` Starlette app carries a ``lifespan=``
    that runs ``session_manager.run()`` — but ``Mount`` does not forward ASGI lifespan
    events to mounted sub-apps, so mounting this directly would leave the session manager's
    task group uninitialized. The caller must instead enter ``mcp.session_manager.run()``
    itself, in the *outer* app's lifespan (see app.py) — ``session_manager`` is only
    available after this function calls ``streamable_http_app()``.
    """
    # Served at exactly MCP_PATH with the inner app mounted at "/": a Mount("/mcp") would
    # answer a request for "/mcp" with a redirect to "/mcp/", which ElevenLabs (and the
    # MCP client) do not follow, and behind the TLS-terminating gateway that redirect
    # even pointed at plain http.
    mcp.settings.streamable_http_path = MCP_PATH
    inner_app = mcp.streamable_http_app()
    return McpAuthContextMiddleware(inner_app, settings)


MCP_PATH = "/mcp"
