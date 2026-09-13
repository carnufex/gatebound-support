# Design decisions

ADR-style. Each entry: context, decision, rejected alternatives, consequences.

## 1. No database access from the support stack

**Context.** The obvious build is a read-only MariaDB user for the tool backend.
MariaDB can grant per table and per column, but a grant is a blocklist that must be
maintained every time the game adds a table, and the support stack would carry
knowledge of the game schema.

**Decision.** The support service never opens a database connection. The website,
which already owns the database and the queries, exposes `/api/support/*` with a
shared secret and returns explicit DTOs. What can leak is an allowlist by
construction. Existing rules (hidden staff characters, deleted characters) come for
free because the same query code is reused.

**Rejected.** Read-only DB user with table grants; MariaDB views plus a view-only
user (kept as a defence-in-depth option, not needed for step one).

**Consequences.** One more hop per tool call (in-cluster, milliseconds). The support
repo can be public: it only knows the API contract in `SPEC.md`.

## 2. MCP instead of webhook tools

**Context.** oncall-voice-copilot used twelve webhook tools, each a JSON config in the
ElevenLabs workspace. ElevenLabs agents can also consume a remote MCP server over
Streamable HTTP with a bearer secret and per-tool approval policy.

**Decision.** One MCP server (`/mcp`) exposes all tools. Tool schemas live in Python
type hints and docstrings, in one place, and the same server can be attached to
Claude Code or Codex when working on Gatebound.

**Rejected.** Webhook tools (twelve configs to keep in sync with the backend).

**Consequences.** The workspace setting `can_use_mcp_servers` had to be enabled.
Identity and conversation id travel as request headers configured on the MCP
server entry (dynamic variables), and the server reads them per call.

## 3. Ticket link instead of agent-created tickets

**Context.** The first draft had the agent create Discord tickets directly, which
raised the question of how to attach the right Discord user (account linking, claim
codes).

**Decision.** The agent only creates a *draft* and returns a link. The player opens
the link, signs in with Discord (OAuth2, `identify` scope), and the ticket is created
as a private thread in the #support channel with them added as a member, so each player sees only their own ticket. The click is the identity. The same
page, without a draft, is the fallback form that works when ElevenLabs is down.

**Rejected.** Pre-linked Discord accounts on the website; claim codes typed in
Discord; a third-party ticket bot (none can create a ticket on behalf of a user via
API).

**Consequences.** No account linking needed up front. Frustration handling and the
"ElevenLabs is down" fallback are the same code path.

## 4. Knowledge base is a build artefact from Git

**Context.** Uploading documents in the ElevenLabs UI is easy; keeping them in step
with game changes is not.

**Decision.** Markdown under `docs/support-kb/` in the Gatebound repo is the source.
`gatebound-support kb-sync` hashes each file, patches changed documents in place
(the document id and the agent attachment survive), creates and deletes as needed,
and writes `kb-manifest.json` next to the sources. `git log` on the manifest shows
what the agent knew when. `ops/deploy.ps1 -SyncKb` runs it after a game deploy;
`kb-sync --check` is the drift test.

**Rejected.** URL documents with auto-sync (no hash, uncontrolled timing, HTML
noise); editing in the UI.

**Consequences.** Anything derived from data (items, monsters, spells, status) is
served live over MCP and is never in the knowledge base, which keeps the synced set
small.

## 5. Cheapest models while iterating

**Decision.** LLM `gemini-2.5-flash-lite`, TTS `eleven_flash_v2` (English agents must
use flash or turbo v2) with `eleven_flash_v2_5` for the Swedish preset, text-first
widget so most conversations never touch TTS or ASR. Guardrail evaluator on
`gemini-3.1-flash-lite`.

**Consequences.** Lower quality answers are acceptable during the lab phase; the
evaluation criteria and tests are the yardstick for when to move up a tier.

## 6. Separate public repository

**Decision.** `carnufex/gatebound-support` is public; Gatebound stays private. The
boundary is `SPEC.md`: the support stack depends on the web API contract and nothing
else. Secrets live in Bitwarden Secrets Manager, delivered by ExternalSecrets.

**Consequences.** Portfolio value for the ElevenLabs work; forced discipline about
what the support stack may know.

## 7. Private agent, signed URLs minted by the support service

**Context.** With `enable_auth` off anyone who reads the agent id out of the page can run
conversations against the workspace; the only cost ceiling was `call_limits`. Turning auth
on broke the embed: its config fetch returned 401 ("Neither authorization header,
xi-api-key, nor conversation_signature received"), allowlist or not.

**Decision.** Auth on. `GET /widget/signed-url` in gatebound-support calls
`/v1/convai/conversation/get-signed-url` with the workspace key and returns the `wss://`
URL; the page passes it as `<elevenlabs-convai signed-url>`. The embed (verified in
`@elevenlabs/convai-widget-core`, `contexts/widget-config.tsx`) extracts `agent_id` and
`conversation_signature` from that URL, appends the signature to its config fetch and
starts the conversation over websocket with it, so the stock widget keeps working in text
mode. The endpoint is CORS-locked to the website origin, rate-limited per client (Cloudflare
`CF-Connecting-IP`) and globally, and never logs the key or the URL.

**Rejected.** `@elevenlabs/react` `Conversation` in text mode (more code, loses the hosted
widget UI, language selector and feedback for no security gain); `shareable_token`
(one long-lived secret in the page is the situation we are leaving); leaving auth off and
relying on `call_limits` alone.

**Consequences.** ElevenLabs credits can only be spent through gatebound-support, so the
per-minute budget is ours to set. Signed URLs expire, hence the 10 minute refresh and the
refresh after each call start. Local web development against production support does not
get a signed URL (production origin only); run the support service locally for that.

