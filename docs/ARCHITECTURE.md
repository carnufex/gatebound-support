# Architecture

Player support for Gatebound: an ElevenLabs agent in a text-first widget on the
website, a Python service that gives it tools over MCP and turns conversations into
Discord tickets, and a knowledge base that is synced from Git. The contract between
the parts is [SPEC.md](SPEC.md); the reasoning is in [DECISIONS.md](DECISIONS.md).

## Components

| Component | Where | Role |
|---|---|---|
| Widget | `gatebound.rosenvall.se`, private repo (`web/components/SupportWidget*.tsx`) | Probes `/status`; renders the ElevenLabs widget with `identity_token`, `player_name`, `logged_in`; degrades to a plain "Support" link when the service or ElevenLabs is down |
| Web support API | private repo, `web/app/api/support/*` | Shared-secret, allowlisted DTOs over the game database. The only thing in the system that touches MariaDB |
| Agent | ElevenLabs workspace, config in `elevenlabs/` | Prompt, tools (via MCP), knowledge base, tests, evaluation criteria, guardrail, data collection |
| Support service | this repo, `gatebound-support` namespace | `/mcp` tools, ticket pages, Discord OAuth and forum posts, post-call webhook, `/status` |
| kb-sync | this repo, CLI | Git markdown → ElevenLabs knowledge base, manifest, drift check |

## Request paths

```
1. Chat                 player ─► widget ─► ElevenLabs ─► LLM (gemini flash-lite)
2. Tool call            ElevenLabs ─► POST /mcp (bearer) ─► service ─► GET web /api/support/* (X-Support-Token)
3. Identity             web /api/support/session (cookie) ─► identity_token ─► dynamic variable
                        ─► X-Support-Identity header on /mcp ─► forwarded to web /api/support/account
4. Ticket               tool returns /t/<token> ─► player clicks ─► Discord OAuth ─► forum post + thread member
                        ─► redirect to the Discord thread
5. Transcript           ElevenLabs post-call webhook (HMAC) ─► /webhooks/elevenlabs ─► SQLite ─► Discord thread
6. Fallback             widget sees /status != ok ─► link to /ticket/new (form) ─► same ticket path, no models
```

## Trust boundaries

- **Browser ↔ ElevenLabs**: agent auth allowlist (`gatebound.rosenvall.se`). The
  widget never sees a secret; the identity token is a 1 h JWT scoped to reading the
  player's own account.
- **ElevenLabs ↔ service**: `Authorization: Bearer MCP_SECRET` on `/mcp`; HMAC
  signature with a 30 min window on the webhook.
- **Service ↔ web**: `X-Support-Token` (constant-time compare) plus the forwarded
  identity token, which the web verifies itself (issuer, audience, expiry).
- **Service ↔ Discord**: OAuth2 `identify` for the player; bot token for creating
  the forum post and adding the thread member.
- **Model ↔ world**: the model never receives a credential, never sees another
  player's private data (the DTOs do not contain it), and cannot change anything
  (no write tool exists). Instructions inside tool results are data; the prompt,
  a test and an evaluation criterion cover it.

## Failure modes

| Failure | Behaviour |
|---|---|
| ElevenLabs down or agent misconfigured | `/status` reports `degraded`; widget shows the "Support" link to the fallback form |
| Support service down | Widget probe fails → same fallback link (the form is on the service, so the player gets the Discord invite from the site footer instead) |
| Web API down | Tools return `unavailable` with a spoken explanation; the agent offers a ticket |
| Discord not configured | Ticket stored as `pending_manual`, page shows the reference; staff read SQLite |
| Player frustrated | Prompt rule + `escalate_to_human` → ticket with priority high + staff heads-up; evaluation criterion checks the handover |
| Injected instructions in tool output or messages | Prompt rule, agent test, evaluation criterion; nothing can be changed by the agent anyway |
| Knowledge base drift | `kb-sync --check` exits 1; `ops/deploy.ps1 -SyncKb` is the normal update path |

## Observability

- Service: JSON logs per tool call (name, duration, status, conversation id), never
  secrets. SQLite holds drafts, tickets and transcripts.
- ElevenLabs: transcripts, tool calls with latency, cost per conversation, sentiment,
  evaluation results, data collection (`issue_category`, `resolved_by_agent`,
  `ticket_ref`, `handover_reason`).
- Discord: every ticket is a forum post with the summary and, after the call, the
  transcript.
