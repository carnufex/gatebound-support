# Gatebound Support — contract

This file is the contract between the three parts of the system. Every part is built
against it; nothing here is optional unless marked so.

```
Player ──► gatebound.rosenvall.se (Next.js, private repo)
             │  <SupportWidget>  ElevenLabs widget, text by default, voice optional
             │  GET /api/support/session   → identity token for logged-in players
             ▼
       ElevenLabs Agent "Gatebound Support"
             │  MCP (Streamable HTTP, bearer secret)
             ▼
       gatebound-support.rosenvall.se (this repo, Python/FastAPI)
             │  /mcp                 tools for the agent
             │  /t/<token>           ticket link page → Discord OAuth → private thread
             │  /ticket/new          fallback form (no agent involved)
             │  /webhooks/elevenlabs post-call transcript
             │  /status              health incl. ElevenLabs reachability
             ▼
       gatebound-web /api/support/*   (internal, shared secret, allowlisted DTOs)
             ▼
       MariaDB (only the web app ever touches it)
```

## 1. Trust boundaries and secrets

| Name | Held by | Purpose |
|---|---|---|
| `SUPPORT_API_TOKEN` | web + support | Shared secret. Every call from support → web carries `X-Support-Token: <token>`. Web compares constant-time and returns 401 otherwise. |
| `SUPPORT_IDENTITY_SECRET` | web + support | HS256 key for identity tokens (see §2). |
| `MCP_SECRET` | support + ElevenLabs | Bearer token ElevenLabs sends on `/mcp` (`Authorization: Bearer <MCP_SECRET>`). |
| `ELEVENLABS_API_KEY` | support (kb-sync, status probe, transcript fetch) | Workspace API key. |
| `ELEVENLABS_WEBHOOK_SECRET` | support | HMAC secret of the post-call webhook. |
| `ELEVENLABS_AGENT_ID` | support + web | The agent id. Web needs it for the widget (`SUPPORT_AGENT_ID` env, read server-side at request time — never `NEXT_PUBLIC_*`). |
| `DISCORD_CLIENT_ID`, `DISCORD_CLIENT_SECRET` | support | OAuth2 app (scope `identify`). |
| `DISCORD_BOT_TOKEN` | support | Bot in the guild with, on the support channel: View Channel, Send Messages, Create Private Threads, Send Messages in Threads, Manage Threads. |
| `DISCORD_GUILD_ID`, `DISCORD_SUPPORT_CHANNEL_ID` | support | The guild and the text channel (visible to everyone, read-only) whose private threads are the tickets. |
| `DISCORD_STAFF_WEBHOOK_URL` | support, optional | Incoming webhook for the staff heads-up on escalations. |

Convention (same as oncall-voice-copilot): a secret with the literal value `unset` or empty
means "integration disabled". The service must start and stay healthy with every
integration disabled; the affected features degrade with an explicit message.

## 2. Identity token

Minted by the web app for a logged-in player; passed to the agent as the dynamic variable
`identity_token`; forwarded by ElevenLabs to the MCP server as header
`X-Support-Identity`; forwarded by the MCP server to the web API as the same header.

JWT, HS256, key `SUPPORT_IDENTITY_SECRET`:

```json
{ "iss": "gatebound-web", "aud": "gatebound-support", "sub": "<accountId as string>",
  "name": "<account name>", "iat": <unix>, "exp": <unix, iat + 3600> }
```

- Web: `GET /api/support/session` (same origin, uses the `gb_session` cookie). Response
  `200 {"identity_token": "...", "name": "..."}` when logged in, `204` otherwise. Never cached.
- Web: `/api/support/account` verifies the header itself (iss, aud, exp) — the web never
  trusts an account id that arrives any other way.
- Support: verifies the same token (same key) only to attach `account_name` to ticket
  drafts. An invalid or missing token is not an error for the MCP server; the tools that
  need identity return `{"status": "not_logged_in", ...}`.
- Anonymous players: the widget passes `identity_token: ""` (the agent config declares the
  placeholder so the variable always exists).

## 3. Web support API (private repo, `web/app/api/support/*`)

All routes: `export const dynamic = "force-dynamic"`, require `X-Support-Token`, respond
JSON, never expose email, IP, coins, orders, secrets, `group_id`, or staff accounts
(`accounts.type` ≥ 2 → treat characters of staff accounts as not found). Names are matched
case-insensitively. Every route answers 404 as `{"status": "not_found"}` with HTTP 404.

| Route | Response (200) |
|---|---|
| `GET /api/support/status` | `{ "online_players": n, "boosted_creature": "name" \| null, "events_active": [{"name","ends_at"}], "events_upcoming": [{"name","starts_at"}], "server_time": iso8601 }` |
| `GET /api/support/character/{name}` | `{ "name", "level", "vocation": "Knight", "sex": "male"\|"female", "online": bool, "last_login": iso8601 \| null, "guild": {"name","rank"} \| null, "recent_deaths": [{"time": iso8601, "level", "killed_by"}] (max 3) }` |
| `GET /api/support/library/search?q=&limit=` | `{ "results": [{"kind": "item"\|"monster"\|"boss"\|"spell", "name", "slug", "url": "https://gatebound.rosenvall.se/library/..."}] }` (reuse `lib/wiki.ts searchWiki` and spells) |
| `GET /api/support/library/item/{slug}` | `{ "name", "category", "attributes": {..}, "dropped_by": [{"monster", "chance"}] (max 8), "url" }` — `chance` is a percent (0–100, two decimals) or null |
| `GET /api/support/library/monster/{slug}` | `{ "name", "hp", "exp", "boss": bool, "boss_class", "loot": [{"item","chance"}] (max 12), "url" }` — `chance` is a percent (0–100, two decimals) or null |
| `GET /api/support/library/spell/{slug}` | `{ "name", "words", "level", "mana", "vocations": [..], "price", "url" }` (fields as available in `lib/spells.ts`) |
| `GET /api/support/account` (needs `X-Support-Identity`) | `{ "account_name", "created": iso8601, "premium_days_left": n, "characters": [{"name","level","vocation","online": bool}] }`. 401 `{"status":"invalid_identity"}` if the token is missing/invalid. |

## 4. MCP server (this repo, `/mcp`, Streamable HTTP)

- Auth: `Authorization: Bearer <MCP_SECRET>`; anything else → 401. Also rejects if
  `MCP_SECRET` is `unset`.
- Per-request context headers sent by ElevenLabs (configured on the MCP server entry):
  `X-Support-Identity: {{identity_token}}`, `X-Conversation-Id: {{system__conversation_id}}`.
- Every tool returns a flat JSON object with a `status` field and a `spoken_summary`
  string written for a voice agent (numbers spelled naturally, no markdown). Expected
  failures are `status: "not_found" | "not_logged_in" | "unavailable"`, never exceptions,
  because the tool runtime hides error bodies from the model.
- Tool names and parameters:

| Tool | Params | Notes |
|---|---|---|
| `get_server_status` | – | web `/status`. `unavailable` when the web API is down. |
| `lookup_character` | `name: str` | public profile only |
| `search_library` | `query: str` | max 8 results, with URLs |
| `get_item` / `get_monster` / `get_spell` | `name: str` | resolve name → slug via search first, then fetch. |
| `get_my_account` | – | needs identity header; `not_logged_in` otherwise, with a spoken hint to log in on the website. |
| `create_ticket_link` | `category: "account" \| "bug" \| "payment" \| "report_player" \| "other"`, `summary: str` (what the player needs, 1–3 sentences, written for staff), `priority: "normal" \| "high"` | Creates a ticket draft, returns `{status:"ok", url, ticket_ref, spoken_summary}`. `url` = `PUBLIC_BASE_URL/t/<token>`. The agent shows the link (text) or tells the player the button/link is on the page (voice). |
| `escalate_to_human` | `summary: str`, `reason: "frustrated" \| "requested_human" \| "out_of_scope" \| "safety"` | Same as `create_ticket_link` with priority high + staff heads-up via `DISCORD_STAFF_WEBHOOK_URL` if configured. Returns the same shape. |

- `token` is 22+ chars url-safe random (128 bit). Drafts expire after 24 h.

## 5. Ticket flow (this repo)

1. `GET /t/<token>` — shows the draft summary, category, and one button "Continue in
   Discord". Expired/unknown token → friendly page linking to `/ticket/new`.
2. Button → `GET /t/<token>/discord` → redirect to Discord OAuth2 authorize
   (`scope=identify`, `state` = signed token, `prompt=none` not required).
3. `GET /oauth/discord/callback?code&state` → exchange code (client secret), `GET /users/@me`
   → creates a PRIVATE thread with the bot token (forum posts cannot be private, and a
   thread member still needs to see the parent channel, so the channel is public/read-only
   and visibility is controlled by thread membership):
   `POST /channels/{DISCORD_SUPPORT_CHANNEL_ID}/threads` body
   `{ "name": "#<ticket_id> <summary, max 80 chars>", "type": 12, "auto_archive_duration": 10080, "invitable": false }`,
   then `POST /channels/{thread_id}/messages` with the opening post,
   then `PUT /channels/{thread_id}/thread-members/{user_id}`.
   Opening post: category, priority, summary, account name (if known), conversation id,
   `<@user_id>`, and "Transcript follows when the conversation ends."
   Then redirect to `https://discord.com/channels/{guild}/{thread_id}`.
4. Discord disabled → ticket is stored with status `pending_manual`, page shows the ticket
   ref and "we will get back to you via the Discord server" text, no redirect.
5. `GET /ticket/new` + `POST /ticket/new` — fallback form (category, summary, optional
   character name). Creates a draft exactly like the tool does, then continues at step 1.
   This path has no dependency on ElevenLabs.
6. `POST /webhooks/elevenlabs` — ElevenLabs post-call webhook. Signature scheme identical to
   `oncall-voice-copilot/services/oncall-tools/src/routes/webhooks.ts` (header
   `ElevenLabs-Signature: t=<unix>,v0=<hex hmac_sha256(secret, "<t>.<raw body>")>`, 30 min
   window). Stores summary + transcript for `conversation_id`; if a ticket with that
   conversation has a Discord thread, posts a follow-up message with the summary and the
   transcript (chunked under 2000 chars, or as a `.txt` attachment if > 6000 chars).
7. `GET /ticket/<ticket_id>` — minimal status page (ref, status, Discord link if any).

Persistence: SQLite at `DATA_DIR/support.db` (stdlib `sqlite3`, WAL). Tables `drafts`,
`tickets`, `conversations`. Ticket ids are `GB-<5 char base32>`.

## 6. Status endpoint (widget degrade mode)

`GET /status` → `{ "ok": true, "elevenlabs": "ok" | "degraded" | "unconfigured", "discord": "ok" | "disabled", "web_api": "ok" | "unavailable", "agent_id": "..." }`.
ElevenLabs probe: `GET https://api.elevenlabs.io/v1/convai/agents/{agent_id}` with the API
key, 5 s timeout, cached 60 s. CORS: allow origin `https://gatebound.rosenvall.se` (and
`http://localhost:3000`) on `/status` only.

The widget (web) fetches `/status` once on mount; if `elevenlabs != "ok"` it renders a
"Create a ticket" button linking to `PUBLIC_BASE_URL/ticket/new` instead of the ElevenLabs
widget. `/healthz` is a plain liveness `{"ok": true}` with no upstream calls.

## 7. kb-sync (this repo, `gatebound-support kb-sync`)

```
gatebound-support kb-sync --dir <markdown dir> --manifest <path> --agent <agent_id> [--dry-run] [--check]
```

- Each `*.md` file: optional front matter `name:` (stable document name); default is the
  file name without extension. Content hash = sha256 of the body after front matter.
- Manifest (`kb-manifest.json`, committed next to the docs):
  `{ "agent_id": "...", "documents": { "<name>": { "id": "<kb doc id>", "sha256": "...", "file": "rules.md", "synced_at": iso8601 } } }`
- Algorithm: new name → create text document, attach to the agent; changed hash → PATCH
  content in place (same id); name missing on disk → detach from agent, delete document;
  finally PATCH the agent's `conversation_config.agent.prompt.knowledge_base` list
  (only entries with `type: "text"` managed by this tool are touched; other entries kept),
  then request RAG indexing for every created/updated document. Write the manifest.
- `--check`: no writes; exit 1 and print a table if any doc on disk differs from the
  manifest, or if any manifest id no longer exists in the workspace. Used by the nightly
  CronJob (Slack alert is the job's business, not this tool's).
- `--dry-run`: print the plan, write nothing.
- Endpoints (verify against https://elevenlabs.io/docs/api-reference before implementing):
  `POST /v1/convai/knowledge-base/text`, `PATCH /v1/convai/knowledge-base/{id}`,
  `DELETE /v1/convai/knowledge-base/{id}`, `POST /v1/convai/knowledge-base/{id}/rag-index`,
  `GET/PATCH /v1/convai/agents/{id}`.

## 8. Runtime

- Python 3.11+, `uv`, FastAPI + uvicorn, `mcp` SDK (FastMCP, streamable HTTP, stateless),
  httpx, pydantic-settings, Jinja2, PyJWT. Tests with pytest + httpx `ASGITransport`.
- Container listens on `PORT` (default 8080), runs as uid 1000, `DATA_DIR=/data`.
- Env: `PUBLIC_BASE_URL` (e.g. `https://gatebound-support.rosenvall.se`),
  `WEB_API_BASE_URL` (in-cluster `http://gatebound-web.gatebound.svc.cluster.local:3000`),
  `WEB_PUBLIC_URL` (`https://gatebound.rosenvall.se`), `LOG_LEVEL`, plus §1.
- Logging: structured JSON lines; every tool call logged with name, duration, status, and
  the conversation id. Never log tokens, identity JWTs, or Discord secrets.
