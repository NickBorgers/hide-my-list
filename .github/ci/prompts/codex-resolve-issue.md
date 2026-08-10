You are autonomous agent. Resolve GitHub issue #${ISSUE_NUMBER} in ${REPO}.

ISSUE TITLE: ${ISSUE_TITLE}

YOUR TASK:
1. Fetch full issue: `gh api repos/${REPO}/issues/${ISSUE_NUMBER}`
2. If needed, fetch comments: `gh api repos/${REPO}/issues/${ISSUE_NUMBER}/comments`
3. Analyze issue, understand what to do
4. Explore codebase, find relevant files
5. Implement fix or feature
6. Run verification steps (see below)
7. Create branch, commit, open PR

## Project structure

This repo runs as a **Python + LangGraph app** (`app/`, `migrations/`, `tests/`, `docker/`, `pyproject.toml`).
Behavioral contracts live in spec docs (`docs/ai-prompts/`, `docs/architecture.md`, `docs/task-lifecycle.md`,
`docs/notion-schema.md`, `docs/user-interactions.md`, `docs/user-preferences.md`, `docs/reward-system.md`,
`design/adhd-priorities.md`). Editing a spec doc changes system behavior.

Read `DEV-AGENTS.md` for the full file list and safety rules before touching any file.

## Verification (run before committing)

**For shell script changes:**
- `shellcheck scripts/*.sh docker/*.sh`
- `yamllint .github/workflows/*.yml`
- `scripts/check-doc-links.sh` (doc link validation)

**For Python changes (`app/`, `migrations/`, `tests/`):**
- `ruff check app/ tests/` — linting (must pass)
- `mypy app/` — type checking (must pass)
- `pytest tests/unit/ -x` — unit tests (must pass without DATABASE_URL)
- `pytest tests/integration/ -x` — integration tests (requires DATABASE_URL; skip if not set)

**For all changes:**
- `shellcheck scripts/*.sh` for any shell script touched
- `yamllint .github/workflows/*.yml` for any workflow touched
- Never use `git push --no-verify`

## IMPORTANT RULES

- Treat spec doc changes (`docs/ai-prompts/`, `docs/architecture.md`, `docs/task-lifecycle.md`,
  `docs/notion-schema.md`, `docs/user-interactions.md`, `docs/user-preferences.md`,
  `docs/reward-system.md`, `design/adhd-priorities.md`) as behavioral changes — touch only if
  the issue explicitly targets that behavior.
- No general HTTP fetch tools in `app/` — `httpx.AsyncClient` only in the three authorized
  modules (`app/tools/notion.py`, `app/tools/signal_client.py`, `app/ingress/signal_listener.py`).
- No subprocess, os.system, eval, exec in `app/`.
- All psycopg queries must use parameterised `%s` placeholders — no string interpolation.
- Private data (task titles, phone numbers, reminder content) must never appear in log fields.
- Reference `docs/architecture.md` and `DEV-AGENTS.md` for system design.

BRANCH NAMING: Use `codex/issue-${ISSUE_NUMBER}`.

PR CREATION:
gh pr create --title '<brief description of what this PR accomplishes>' \
  --assignee NickBorgers \
  --body 'Resolves #${ISSUE_NUMBER}

## Summary
<describe what you changed>

## Test Plan
<how to verify the fix>

## Review Pipeline Fallback
If review pipeline checks do not appear within 2 minutes of opening this PR,
comment `/review` on the PR to manually trigger a fresh review cycle.

Generated with Codex

Author-Session: codex/${RUN_ID}' \
  --head codex/issue-${ISSUE_NUMBER}

The `Author-Session: codex/${RUN_ID}` trailer at the end of the body is
REQUIRED. The review pipeline reads it to resume this session in the
fixer stage so you receive reviewer feedback in the same conversation.
Do not omit, move, or change the line. `${RUN_ID}` is substituted from
the workflow run id at prompt time — keep it literal in this template.

Can't resolve? Comment explaining what clarification needed:
`gh issue comment ${ISSUE_NUMBER} --body 'YOUR_EXPLANATION'`
