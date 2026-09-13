"""Ticket flow (SPEC §5): draft link -> Discord OAuth -> forum post, plus the no-agent
fallback form and a minimal status page."""

from __future__ import annotations

import hashlib
import hmac
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import store
from ..discord import DiscordClient
from ..logging import get_logger
from ..settings import Settings, enabled
from ..webclient import WebClient

logger = get_logger("gatebound_support.tickets")

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
_ALLOWED_CATEGORIES = ("account", "bug", "payment", "report_player", "other")


def _templates() -> Jinja2Templates:
    return Jinja2Templates(directory=str(_TEMPLATES_DIR))


def _sign_token(token: str, secret: str) -> str:
    sig = hmac.new(secret.encode("utf-8"), token.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{token}.{sig}"


def _verify_signed_token(state: str, secret: str) -> str | None:
    if "." not in state:
        return None
    token, _, sig = state.partition(".")
    expected = hmac.new(secret.encode("utf-8"), token.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return None
    return token


def _base_url(settings: Settings) -> str:
    return settings.PUBLIC_BASE_URL.rstrip("/") if enabled(settings.PUBLIC_BASE_URL) else ""


def _redirect_uri(settings: Settings) -> str:
    return f"{_base_url(settings)}/oauth/discord/callback"


def _opening_post(
    *,
    ticket_id: str,
    category: str,
    priority: str,
    summary: str,
    account_name: str | None,
    conversation_id: str | None,
    discord_user_id: str,
) -> str:
    lines = [
        f"**Ticket:** {ticket_id}",
        f"**Category:** {category}",
        f"**Priority:** {priority}",
        f"**Summary:** {summary}",
        f"**Account:** {account_name or 'unknown'}",
        f"**Conversation:** {conversation_id or 'none'}",
        f"<@{discord_user_id}>",
        "Transcript follows when the conversation ends.",
    ]
    return "\n".join(lines)


def build_router(settings: Settings, web_client: WebClient, discord_client: DiscordClient) -> APIRouter:
    router = APIRouter()
    templates = _templates()

    async def _create_ticket_from_draft(draft: dict, *, status: Literal["pending_manual", "open"] = "pending_manual") -> str:
        return store.create_ticket(
            settings.DATA_DIR,
            token=draft["token"],
            category=draft["category"],
            summary=draft["summary"],
            priority=draft["priority"],
            account_name=draft["account_name"],
            character_name=draft["character_name"],
            conversation_id=draft["conversation_id"],
            status=status,
        )

    @router.get("/t/{token}", response_class=HTMLResponse)
    async def get_draft(request: Request, token: str) -> HTMLResponse:
        draft = store.get_draft(settings.DATA_DIR, token)
        if draft is None:
            return templates.TemplateResponse(request, "ticket_expired.html", status_code=404)
        if draft.get("ticket_id"):
            return RedirectResponse(f"/ticket/{draft['ticket_id']}", status_code=302)
        return templates.TemplateResponse(
            request,
            "ticket_draft.html",
            {
                "category": draft["category"],
                "priority": draft["priority"],
                "summary": draft["summary"],
                "discord_enabled": discord_client.enabled,
                "continue_url": f"/t/{token}/discord",
            },
        )

    @router.get("/t/{token}/discord")
    async def start_discord(token: str) -> RedirectResponse:
        draft = store.get_draft(settings.DATA_DIR, token)
        if draft is None:
            return RedirectResponse(f"/t/{token}", status_code=302)

        if not discord_client.enabled:
            ticket_id = await _create_ticket_from_draft(draft, status="pending_manual")
            return RedirectResponse(f"/ticket/{ticket_id}", status_code=302)

        state = _sign_token(token, settings.SUPPORT_IDENTITY_SECRET)
        url = discord_client.build_authorize_url(state=state, redirect_uri=_redirect_uri(settings))
        return RedirectResponse(url, status_code=302)

    @router.get("/oauth/discord/callback", response_class=HTMLResponse)
    async def discord_callback(request: Request, code: str | None = None, state: str | None = None) -> HTMLResponse:
        if not code or not state:
            return templates.TemplateResponse(request, "ticket_expired.html", status_code=400)
        token = _verify_signed_token(state, settings.SUPPORT_IDENTITY_SECRET)
        if token is None:
            return templates.TemplateResponse(request, "ticket_expired.html", status_code=400)
        draft = store.get_draft(settings.DATA_DIR, token)
        if draft is None:
            return templates.TemplateResponse(request, "ticket_expired.html", status_code=404)

        token_data = await discord_client.exchange_code(code=code, redirect_uri=_redirect_uri(settings))
        if not token_data or "access_token" not in token_data:
            ticket_id = await _create_ticket_from_draft(draft, status="pending_manual")
            return RedirectResponse(f"/ticket/{ticket_id}", status_code=302)

        user = await discord_client.fetch_current_user(token_data["access_token"])
        if not user or "id" not in user:
            ticket_id = await _create_ticket_from_draft(draft, status="pending_manual")
            return RedirectResponse(f"/ticket/{ticket_id}", status_code=302)

        ticket_id = await _create_ticket_from_draft(draft, status="open")
        title = f"#{ticket_id} {draft['summary'][:80]}"
        content = _opening_post(
            ticket_id=ticket_id,
            category=draft["category"],
            priority=draft["priority"],
            summary=draft["summary"],
            account_name=draft["account_name"],
            conversation_id=draft["conversation_id"],
            discord_user_id=user["id"],
        )
        thread_id = await discord_client.create_forum_post(ticket_id=ticket_id, title=title, content=content)
        if not thread_id:
            return RedirectResponse(f"/ticket/{ticket_id}", status_code=302)

        await discord_client.add_thread_member(thread_id=thread_id, user_id=user["id"])
        store.update_ticket_discord(
            settings.DATA_DIR,
            ticket_id,
            guild_id=settings.DISCORD_GUILD_ID,
            thread_id=thread_id,
            user_id=user["id"],
            status="open",
        )
        return RedirectResponse(f"https://discord.com/channels/{settings.DISCORD_GUILD_ID}/{thread_id}", status_code=302)

    @router.get("/ticket/new", response_class=HTMLResponse)
    async def new_ticket_form(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request, "ticket_new.html", {})

    @router.post("/ticket/new", response_class=HTMLResponse)
    async def new_ticket_submit(
        request: Request,
        category: str = Form(...),
        summary: str = Form(...),
        character_name: str = Form(""),
    ) -> HTMLResponse:
        category = category.strip().lower()
        summary = summary.strip()
        if category not in _ALLOWED_CATEGORIES or not summary:
            return templates.TemplateResponse(
                request,
                "ticket_new.html",
                {
                    "error": "Please choose a category and describe what you need help with.",
                    "category": category,
                    "summary": summary,
                    "character_name": character_name,
                },
                status_code=400,
            )
        token = store.create_draft(
            settings.DATA_DIR,
            category=category,
            summary=summary,
            priority="normal",
            account_name=None,
            character_name=character_name.strip() or None,
            conversation_id=None,
        )
        return RedirectResponse(f"/t/{token}", status_code=302)

    @router.get("/ticket/{ticket_id}", response_class=HTMLResponse)
    async def ticket_status_page(request: Request, ticket_id: str) -> HTMLResponse:
        ticket = store.get_ticket(settings.DATA_DIR, ticket_id)
        if ticket is None:
            return templates.TemplateResponse(request, "ticket_expired.html", status_code=404)
        discord_url = None
        if ticket.get("discord_thread_id"):
            discord_url = f"https://discord.com/channels/{ticket['discord_guild_id']}/{ticket['discord_thread_id']}"
        return templates.TemplateResponse(
            request,
            "ticket_status.html",
            {"ticket_id": ticket_id, "status": ticket["status"], "discord_url": discord_url},
        )

    return router
