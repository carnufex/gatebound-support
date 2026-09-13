# gatebound-support

Python/FastAPI backend for Gatebound's ElevenLabs support agent: MCP tools for the agent,
the Discord ticket flow, the post-call webhook, and the knowledge-base sync CLI.

`docs/SPEC.md` is the contract this service is built against — read it for exact env names,
endpoint paths, tool shapes, and the ticket flow. This file is just the "how do I run it"
quick start.

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
| `DISCORD_CLIENT_ID`, `DISCORD_CLIENT_SECRET`, `DISCORD_BOT_TOKEN`, `DISCORD_GUILD_ID`, `DISCORD_FORUM_CHANNEL_ID`, `DISCORD_TAG_OPEN_ID`, `DISCORD_STAFF_WEBHOOK_URL` | the ticket flow's Discord integration |

Copy `.env.example` to `.env` and fill in what you need; everything else can stay `unset`.
