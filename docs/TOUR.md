# Code tour

A reading order for learning this codebase by following real requests through it.
Each stop names the file and function; open them side by side with this page. The
exercises at the end change one thing each and show where to observe the effect.

Prerequisites: `uv sync`, then `uv run pytest -q` should be green. Keep `docs/SPEC.md`
open: it is the contract every stop implements.

## 0. The map

```
website widget ──► ElevenLabs agent ──► /mcp (this service) ──► web /api/support/* ──► MariaDB
                        │                    │
                        │                    └─► ticket draft ─► /t/<token> ─► Discord OAuth ─► private thread
                        └─► post-call webhook ─► /webhooks/elevenlabs ─► SQLite ─► transcript into the thread

Discord bot (separate process) ──► /internal/tickets (this service)
        ├─ 🎫 / button / /ticket   → private thread
        └─ 📞 / /support           → private voice channel ─► ElevenLabs audio session
```

Two processes share one image and one SQLite file:

| Process | Entry point | Talks to |
|---|---|---|
| `gatebound-support serve` | `src/gatebound_support/app.py:create_app` | ElevenLabs (MCP in, webhook in), web API, Discord REST |
| `gatebound-support voicebot` | `src/gatebound_support/voicebot/runner.py:run` | Discord gateway, ElevenLabs audio, this service's `/internal` |

Only the first process opens SQLite. That is why the bot goes through `/internal`.

## 1. Settings and the "unset" convention

Start in `src/gatebound_support/settings.py`. Every integration secret defaults to the
string `unset`, and `enabled()` turns that into "feature off". Grep for `enabled(` to see
every place a feature degrades instead of failing. Tests rely on this: `tests/conftest.py`
builds a `Settings` with everything unset.

## 2. Follow a chat message: "how many players are online?"

1. The widget on the website starts an ElevenLabs conversation. Nothing in this repo runs
   yet, except that the widget fetched a signed URL first:
   `src/gatebound_support/routes/widget.py:widget_signed_url`. Read `docs/DECISIONS.md` §7
   for why.
2. The agent decides to call `get_server_status`. ElevenLabs POSTs to `/mcp`.
   `src/gatebound_support/mcp_server.py:McpAuthContextMiddleware.__call__` checks the bearer
   secret and stashes two headers (`X-Support-Identity`, `X-Conversation-Id`) in
   contextvars, because FastMCP tools cannot see the HTTP request.
3. `mcp_server.py:get_server_status` runs. Note the `_instrumented` decorator above it:
   a tool never raises (the model would only see "error"), it returns
   `{"status": "unavailable", "spoken_summary": ...}` instead, and logs name/duration.
4. `src/gatebound_support/webclient.py:WebClient.get_status` calls the website with
   `X-Support-Token`. The website (private repo, `web/app/api/support/status/route.ts`) is the
   only code that touches MariaDB.
5. The tool returns a dict with `spoken_summary`; the model reads it and answers.

Run it yourself against production, exactly as ElevenLabs does:

```powershell
$env:MCP_SECRET = "<from the cluster secret gatebound-support-secrets>"
uv run python elevenlabs/scripts/probe_mcp.py
```

## 3. Follow a ticket: "I want to talk to a human"

1. The prompt (`elevenlabs/scripts/build_agent_config.py`, rule 6) tells the model to call
   `escalate_to_human`. `mcp_server.py:escalate_to_human` → `_create_ticket_draft` →
   `src/gatebound_support/store.py:create_draft`: a random token, the summary, the account
   name if the identity token verified (`identity.py:verify_identity_token`).
2. The tool returns a URL `/t/<token>`. The model must show it (rule 9).
3. `src/gatebound_support/routes/tickets.py:get_draft` renders the page; the button goes to
   `start_discord`, which redirects to Discord OAuth with the token HMAC-signed into `state`
   (`_sign_token`), so the callback cannot be forged.
4. `tickets.py:discord_callback` exchanges the code, learns the Discord user id, creates the
   ticket row (`store.create_ticket`) and the private thread
   (`src/gatebound_support/discord.py:create_ticket_thread`, note the comment on why a
   private thread and not a forum post), adds the user, redirects them to the thread.
5. When the conversation ends, ElevenLabs posts the transcript:
   `src/gatebound_support/routes/webhooks.py:elevenlabs_webhook` verifies the HMAC
   (`elevenlabs.py:verify_webhook_signature`), stores it, and if a ticket has that
   `conversation_id`, posts summary + transcript into the thread.

Fallback with no agent at all: `/ticket/new` (`tickets.py:new_ticket_form`) creates the
same draft and joins the flow at step 3.

## 4. The agent as code

`elevenlabs/` is an ElevenLabs CLI project. Nothing is edited in their UI.

- `scripts/build_agent_config.py` writes `agent_configs/Gatebound-Support.json`. Read the
  `PROMPT` first, then `build()`. Vocabulary used there:
  - **dynamic variables** (`player_name`, `logged_in`, `identity_token`): values the widget
    injects per conversation; `{{player_name}}` in the prompt is substituted, and
    `identity_token` becomes an HTTP header on MCP calls (configured on the MCP server entry).
  - **data collection** (`data_collection_field`): fields an extraction LLM fills after
    each conversation.
  - **evaluation criteria** (`evaluation_criterion`): yes/no judgements about each
    conversation, shown in the dashboard.
  - **guardrail** (`platform_settings.guardrails.custom`): a second model that blocks a
    reply claiming something no tool result confirms.
  - **knowledge base** + **RAG**: the six markdown docs from the Gatebound repo, synced by
    `src/gatebound_support/kb_sync.py` (start at `compute_plan`, then `main`).
- `test_configs/*.json`: six behaviour tests; `scripts/run_agent_tests.py` runs them.
- `scripts/sync_agent_refs.py`: why it exists is the trap to remember: `agents push` force
  overrides, so live KB ids and the MCP server id must be mirrored into the JSON first.

## 5. The Discord bot

`src/gatebound_support/voicebot/bot.py` is one `discord.Client`. Read in this order:

1. `VoiceBot.setup_hook`: registers slash commands, persistent button views, the pinned
   message with its reactions, and sweeps leftover `support-*` voice channels.
2. Text tickets: `handle_support`'s sibling `create_text_ticket` → `service_client.py`
   (`POST /internal/tickets`) → `_create_ticket_thread`. Same thread shape as the website
   path; the id comes from the service so both paths share one numbering.
3. A voice call, start to end: `handle_support` → `_open_voice_ticket` →
   `_create_private_voice_channel` (`channels.py:build_voice_channel_overwrites` is why
   nobody else can join) → wait in `on_voice_state_update` → `_activate_call` →
   `elevenlabs_bridge.py:CallSession` → `_end_call`.
4. Audio: `sink.py` (Discord → 16 kHz mono, only the caller's stream) and
   `audio_source.py` (agent → 48 kHz stereo frames, 20 ms each). `resampler.py` in between.
5. `dave.py`: Discord requires end-to-end encrypted voice. Read the module docstring; it
   is the single most surprising thing in this repo and cost an evening.
6. Closing: `_perform_close`, reached from `/close`, the button, and ✅ (`reactions.py`).
   `tickets.py:can_close_ticket` is the permission rule.

## 6. Where the state lives

| State | Where | Why there |
|---|---|---|
| Drafts, tickets, transcripts | SQLite on the PVC (`store.py`) | one writer, tiny volume |
| Which thread belongs to which conversation | `tickets.conversation_id` | the webhook joins on it |
| Active voice call | in memory (`voicebot/state.py:CallRegistry`) | dies with the process, and the startup sweep cleans up |
| What the agent knows | ElevenLabs KB, mirrored in `kb-manifest.json` in the Gatebound repo | git history answers "what did it know when" |

## 7. Exercises

Each one is small, and the tests tell you when you are done.

1. **Add a tool.** Give the agent `get_online_players` returning the top five by level.
   Website side: a new route under `web/app/api/support/`; service side: one method in
   `webclient.py`, one function in `mcp_server.py`, a line in the prompt's tool list, a
   test in `tests/test_mcp.py`. Then `probe_mcp.py` must list it.
2. **Change a rule and prove it.** Make rule 2 name the account page URL. Regenerate the
   config, `elevenlabs agents push`, run `run_agent_tests.py --verbose` and watch the
   "never claims to have changed an account" transcript change.
3. **Add a data collection field** `language` ("en" or "sv"). Push, run one chat in
   Swedish on the website, find the field in the ElevenLabs conversation view.
4. **Break the webhook signature** on purpose (edit one byte in
   `tests/test_webhooks.py`'s signed payload) and see which assertion catches it.
5. **Give a voice call a longer leash.** Change `VOICE_MAX_MINUTES` in the homelab
   deployment, and find the line in `bot.py` that reads it.
6. **Drift check.** Edit one KB markdown file in the Gatebound repo without syncing, run
   `kb-sync --check`, then `ops/deploy.ps1 -SyncKb` and check again.

## 8. Running things locally

```powershell
uv sync
copy .env.example .env             # everything unset = every integration off
uv run gatebound-support serve     # http://localhost:8080/status
uv run pytest -q
uv run gatebound-support voicebot --check   # validates settings and imports, no connection
```

With only `MCP_SECRET` set in `.env`, `probe_mcp.py http://localhost:8080` exercises the
MCP layer against the local process; the web API calls then return `unavailable`, which is
the degrade path the tools are built for.
