# gatebound-support

Python/FastAPI backend for Gatebound's ElevenLabs support agent: MCP tools for the agent,
the Discord ticket flow, the post-call webhook, and the knowledge-base sync CLI.

`docs/SPEC.md` is the contract this service is built against — read it for exact env names,
endpoint paths, tool shapes, and the ticket flow. `docs/ARCHITECTURE.md` has the component
map, trust boundaries and failure modes; `docs/DECISIONS.md` the reasoning (no database
access from this stack, MCP instead of webhook tools, ticket links instead of agent-created
tickets, knowledge base synced from Git, cheapest models while iterating). This file is just
the "how do I run it" quick start.

Live: agent "Gatebound Support" in the text-first widget on
[gatebound.rosenvall.se](https://gatebound.rosenvall.se); service at
`gatebound-support.rosenvall.se`, deployed from the private homelab GitOps repo.

## ElevenLabs agent as code (`elevenlabs/`)

Everything about the agent lives in the ElevenLabs CLI project layout and is pushed, never
edited in the UI:

```powershell
$env:ELEVENLABS_API_KEY = "..."
cd elevenlabs
python scripts/build_agent_config.py     # prompt + settings -> agent_configs/Gatebound-Support.json
elevenlabs agents push                   # create/update the agent
elevenlabs tests push                    # the six behaviour tests in test_configs/
python scripts/run_agent_tests.py        # run them, table + exit code (add --verbose for transcripts)
python scripts/sync_agent_refs.py        # mirror live knowledge_base + mcp_server_ids into the config
$env:MCP_SECRET = "..."; uv run python scripts/probe_mcp.py   # talk to the deployed /mcp like ElevenLabs does
```

The MCP server entry and its bearer secret are registered once per workspace (see
`docs/DECISIONS.md`); the agent references it by id in `mcp_server_ids`.

## Run locally

```powershell
uv sync
copy .env.example .env   # edit as needed; every integration defaults to "unset" (disabled)
uv run gatebound-support serve
```

The server listens on `PORT` (default 8080). With everything left `unset` it still starts and
answers `/healthz` and `/status`; the ticket flow works without Discord (tickets land as
`pending_manual`), and the MCP endpoint at `/mcp` rejects all calls until `MCP_SECRET` is set.

## Tests

```powershell
uv run pytest -q
uv run ruff check .
```

## kb-sync

Syncs a directory of markdown files into an ElevenLabs agent's knowledge base (SPEC §7):

```powershell
uv run gatebound-support kb-sync --dir path\to\docs --manifest path\to\kb-manifest.json --agent <agent_id>
uv run gatebound-support kb-sync --dir ... --manifest ... --agent ... --dry-run   # print the plan, write nothing
uv run gatebound-support kb-sync --dir ... --manifest ... --agent ... --check     # exit 1 on drift, for CI/CronJob
```

Requires `ELEVENLABS_API_KEY`.

## Discord voice

`gatebound-support voicebot` is a second, separate long-lived process (own container
command, own Kubernetes Deployment — same image, `command: ["gatebound-support",
"voicebot"]`, no HTTP service) that runs the Discord gateway connection and bridges voice:

- **`/support` or `/call`** (run in a voice channel): the bot "Gatebound Support" joins the
  player's current voice channel and bridges audio both ways to the ElevenLabs
  Conversational AI agent — the same agent the website widget talks to, over the SDK's
  `Conversation` + a custom `AudioInterface` (see `src/gatebound_support/voicebot/`). A
  ticket + private Discord thread is opened for the call up front (source
  `discord_voice`), and the post-call transcript webhook posts the summary and transcript
  to that thread when the call ends, exactly like the website ticket flow.
- **`/hangup`**: ends the current call early.
- The call also ends when the player leaves the voice channel, the agent says goodbye
  (its own `end_call` tool), or after `VOICE_MAX_MINUTES` (default 15).
- One call per Discord server at a time — a second `/support` while busy gets an ephemeral
  "the line is busy" reply.
- **`/ticket`**, and a persistent **"Open a ticket"** button pinned in the support channel,
  open a text ticket via a modal (category + description) without going through the
  ElevenLabs agent at all — same private-thread flow, source `discord_text`. One open
  ticket per Discord user at a time; opening a second one just links back to the first.

The bot talks to this same service's `/internal` API (bearer `MCP_SECRET`, not part of the
public contract in `docs/SPEC.md`) to create and update tickets, since it's a separate
process from the one holding SQLite.

**Discord bot permissions needed** (added when inviting/authorizing the bot, or updating its
existing OAuth2 scopes): `View Channel`, `Send Messages`, `Create Private Threads`, `Send
Messages in Threads`, `Manage Threads` (already required for the text ticket flow) plus
**`Connect`**, **`Speak`**, and **`Use Voice Activity`** for voice.

**Cost note:** ElevenLabs bills voice conversation minutes at a materially higher rate than
text conversations — a `/support` call is not "free" the way a widget chat message is.

Run it locally the same way as `serve`:

```powershell
uv run gatebound-support voicebot
uv run gatebound-support voicebot --check   # validates settings + opus + imports, no connection, exits 0/1
```

## Docker

```powershell
docker build --platform linux/amd64 -t gatebound-support:dev .
docker run --rm -p 8080:8080 --env-file .env gatebound-support:dev
```

Runs as uid 1000, writes SQLite to `DATA_DIR` (default `/data` in the container).

## Environment variables

See `docs/SPEC.md` sections 1 and 8 for the full list and what each one is for. Quick
reference (all default to `"unset"`, meaning that integration is disabled):

| Variable | Purpose |
|---|---|
| `PORT`, `DATA_DIR`, `LOG_LEVEL` | runtime |
| `PUBLIC_BASE_URL`, `WEB_API_BASE_URL`, `WEB_PUBLIC_URL` | this service's own URL, the web app's internal API, and its public URL |
| `SUPPORT_API_TOKEN` | shared secret for calls to the web support API |
| `SUPPORT_IDENTITY_SECRET` | HS256 key for the player identity JWT |
| `MCP_SECRET` | bearer secret ElevenLabs sends on `/mcp` |
| `ELEVENLABS_API_KEY`, `ELEVENLABS_WEBHOOK_SECRET`, `ELEVENLABS_AGENT_ID` | ElevenLabs API access, post-call webhook HMAC key, and agent id |
| `DISCORD_CLIENT_ID`, `DISCORD_CLIENT_SECRET`, `DISCORD_BOT_TOKEN`, `DISCORD_GUILD_ID`, `DISCORD_SUPPORT_CHANNEL_ID`, `DISCORD_STAFF_WEBHOOK_URL` | the ticket flow's Discord integration (`DISCORD_BOT_TOKEN`/`DISCORD_GUILD_ID`/`DISCORD_SUPPORT_CHANNEL_ID` are also what the voicebot process uses) |
| `VOICE_MAX_MINUTES` | voicebot: max length of a `/support` call in minutes (default 15) |
| `SUPPORT_INTERNAL_URL` | voicebot: in-cluster base URL of this service's own `/internal` API (default assumes the standard `gatebound-support` namespace/service names) |

Copy `.env.example` to `.env` and fill in what you need; everything else can stay `unset`.
