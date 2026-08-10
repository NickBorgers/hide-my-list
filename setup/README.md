# hide-my-list — Setup & Operations

## Prerequisites

- **Docker + Docker Compose** (Compose v2)
- **Notion** database created with the schema from `docs/notion-schema.md`
- **Signal** account with signal-cli configured (infra-provided; see below)

## Quick Start

1. Clone the repo:
   ```bash
   git clone https://github.com/NickBorgersProbably/hide-my-list.git
   cd hide-my-list
   ```

2. Install the repo-managed git hooks for this worktree:
   ```bash
   bash .githooks/install-hooks.sh
   ```

   `pre-commit` handles fast staged-file checks, and `pre-push` reruns the
   deterministic CI-equivalent checks for changed scripts, docs, and workflow-related
   paths so those failures are caught locally before GitHub is the first place they fail.

3. Create the `.env` file:
   ```bash
   cp .env.template .env
   # Edit .env with your values
   ```

4. Start the stack:
   ```bash
   docker compose up -d
   docker compose logs -f app
   ```

## Signal CLI

signal-cli is **infrastructure-provided**. The infra agent runs signal-cli with an existing
registered Signal account and exposes it over the Docker network. This repo does not own
signal-cli volume management or registration carry-over. The `signal-cli` service in
`docker/compose.yaml` is a thin bridge; the registration data lives on the infra host.

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `NOTION_API_KEY` | Yes | Notion integration API key |
| `NOTION_DATABASE_ID` | Yes | ID of the tasks database |
| `SIGNAL_ACCOUNT` | Yes | E.164 phone number registered with signal-cli |
| `AUTHORIZED_PEERS` | Yes | Comma-separated E.164 numbers allowed to send inbound messages; empty or unset refuses startup |
| `LLM_PROXY_BASE_URL` | Yes | LiteLLM proxy OpenAI-compatible `/v1` endpoint (e.g. `https://proxy.host/v1`) |
| `LLM_PROXY_API_KEY` | Yes | Bearer token for LiteLLM proxy; use any non-empty placeholder if proxy needs no auth |
| `DATABASE_URL` | Compose-managed | Postgres DSN; hardcoded in `docker/compose.yaml` for the compose network. Override only for non-compose runs. |
| `SIGNAL_CLI_URL` | Compose-managed | WebSocket URL of the signal-cli bridge; hardcoded in `docker/compose.yaml`. Override only for non-compose runs. |
| `USER_TZ` | No | IANA timezone identifier (default `America/Chicago`) |
| `OPENAI_API_KEY` | No | For AI-generated reward images (`app/tools/rewards.py`) |
| `OPS_ALERT_SIGNAL_NUMBER` | No | Separate Signal recipient for ops alerts |
| `LANGSMITH_TRACING` | No | Set `true` to enable LangSmith tracing (requires `ALLOW_PRIVATE_TRACE_EXPORT=true`) |
| `ALLOW_PRIVATE_TRACE_EXPORT` | No | Required alongside `LANGSMITH_TRACING=true`; explicit consent for private data export |

## Scheduled Jobs

The app uses APScheduler v3 with PostgresJobStore for durable scheduled jobs:

| Job | Schedule | Purpose |
|-----|----------|---------|
| `reminder_dispatcher` | Every 30 s | Poll `reminder_outbox` for due reminders; delivers via signal-cli |
| `notion_health` | Every 15 min | Notion connectivity check; enqueues ops alert on failure |
| `ops_alerts_drain` | Every 5 min | Drain `ops_alerts_throttle` table to ops alert recipient |
| `check_in_dispatcher` | Every 10 min | Check for overdue check-in tasks; sends nudge via signal-cli |
| `state_audit` | Daily 03:00 USER_TZ | VACUUM + prune `recent_outbound` rows older than 90 days |
| `weekly_recap` | Sunday 18:00 USER_TZ | Generate and deliver weekly task completion recap |

## Model Tiers

Model assignments use a tier system defined in `setup/model-tiers.json`. Read by `app/models.py`
at startup to validate all model references. All tiers are routed through `ChatOpenAI` to the
LiteLLM proxy at `LLM_PROXY_BASE_URL`; LiteLLM dispatches by model alias.

| Tier | Role |
|------|------|
| `expensive` | Complex reasoning (e.g. GET_TASK scoring) |
| `medium` | Intent classification and most graph nodes |
| `cheap` | Lightweight tasks |
| `reminder` | Reminder delivery cron |

To remap tiers: edit `setup/model-tiers.json` values to point at your model IDs, then restart
the stack. `app/models.py` validates the mapping at startup and raises if any tier is missing
or uses a model ID prefix not in the accepted allowlist (`claude-`, `gemma`, `gpt-`).

## Contributor Hooks

Install hooks in every worktree before committing:

```bash
bash .githooks/install-hooks.sh
```

`core.hooksPath` is stored per worktree, so re-run after each `git worktree add`.

## Troubleshooting

**Reminders not firing:**
- Check `reminder_outbox` table for rows stuck in `delivering` state
- Verify `SIGNAL_CLI_URL` is reachable from the app container
- Check `docker compose logs app` for `reminder_worker` errors
- Confirm `NOTION_API_KEY` and `NOTION_DATABASE_ID` are correct

**Ops alerts not arriving:**
- Verify `OPS_ALERT_SIGNAL_NUMBER` is set in `.env`
- Check `ops_alerts_throttle` table for queued alerts not draining

**Agent not responding:**
- Check `docker compose logs app` for startup errors
- Verify `DATABASE_URL` is reachable and migrations applied
- Check `SIGNAL_CLI_URL` WebSocket connectivity

**Relative dates land on the wrong day:**
- Confirm `USER_TZ` is set to the correct IANA timezone identifier in `.env`

**Git pull conflicts:**
- Spec docs (`docs/`, `design/`) are the authoritative source; the repo version wins
