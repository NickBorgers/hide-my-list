# Test Rig: hide-my-list Behavioral + LLM-Swap Test Architecture

Authoritative reference for the test rig that enforces behavioral contracts,
catches dead-code wiring, and validates model-swap readiness across the
Python + LangGraph stack.

The rig review enforcer lives at `.github/scripts/review/prompts/test.md` and
fires on every PR that touches `app/**`, `migrations/**`, `setup/model-tiers.json`,
`app/prompts/**`, `docs/ai-prompts/**`, `tests/**`, the test reviewer prompt,
`.github/scripts/review/schema/*.json`, `docs/python-rewrite/test-rig.md`, or
`docker/compose.yaml`.

---

## Layer Architecture

| Layer | Directory | LLM | Postgres | Compose | Responsibility |
|---|---|---|---|---|---|
| Unit | `tests/unit/` | mocked (`MagicMock`) | none | no | Pure logic, prompt structure, regex/type assertions, structural lints |
| Integration | `tests/integration/` | mocked (with strict call-arg assertions) | real (container) | no | State machines, DB schema, async plumbing, wiring contracts |
| E2E | `tests/e2e/` | real, single-model via LiteLLM proxy | real (container) | no | Multi-turn conversations through the real compiled graph; cross-turn state handoff |
| Eval | `tests/evals/` | real, multi-model via LiteLLM proxy | none | no | Behavioral contracts across model swaps; judge-LLM scoring |
| Smoke | `tests/smoke/` | none | none | yes (boots stack) | Deployment-gap catch |
| Regressions | `tests/regressions/` | varies | varies | varies | One permanent test per production bug |

E2E and Eval both call the real model but answer different questions. Eval scores
**what one node says** against a fixed world, judged by a second model. E2E scores
**what the system does** across several turns — which Notion page was written,
whether a reminder row was resolved, what survived in the checkpoint — and never
judges wording. That is why E2E can gate a merge without flaking while Eval runs
nightly.

### Cost and frequency

| Layer | Wall time | LLM cost | Frequency |
|---|---|---|---|
| Unit | <30 s | $0 | every commit — `pytest-unit` job |
| Integration | <2 min | $0 | every commit — `pytest-db` job (Postgres service container) |
| Regressions | <1 min | $0 | every commit — `pytest-db` job |
| Structural lints | <10 s | $0 | every commit — `pytest-unit` job |
| E2E conversations | 6-12 min | $0 (self-hosted model) | every PR touching `app/`, `migrations/`, `tests/e2e/`, `tests/support/`, `setup/model-tiers.json`, `pyproject.toml`, or `.github/workflows/e2e.yml` — `.github/workflows/e2e.yml` on the `homelab` runner |
| Compose smoke | <3 min | $0 | gated by `ENABLE_COMPOSE_SMOKE=true` — runs on demand only |
| Evals (baseline) | 10-20 min | ~$2-5 | `.github/workflows/nightly-evals.yml` — cron 09:00 UTC + `workflow_dispatch` |
| Model-swap report | 15-30 min | ~$5-10 | `.github/workflows/model-swap.yml` — `workflow_dispatch` only |

CI never sets `ENABLE_LIVE_LLM_EVALS=true` for PRs. The nightly eval workflow
and model-swap workflow run on the self-hosted `homelab` runner (which can reach
the tailnet-only proxy) and provide the required proxy env vars directly — a
non-empty placeholder API key and the OpenAI-compatible `/v1` endpoint — rather
than repo secrets. The compose smoke is manually gated by `ENABLE_COMPOSE_SMOKE` —
there's no scheduled trigger (it boots the full stack and is too slow for PR CI).

**Why evals are not a PR check.** Fork PRs are blocked repo-wide, so running
PR-branch code on the `homelab` runner is acceptable — the same posture the E2E
workflow, `review-fixer.yml`, `review-reviewer.yml`, and `codex.yml` all take.
Evals remain nightly for two other reasons: cost (~$2–5 per run) and duration
(10–20 min) make them too slow for per-PR CI, and behavioral regressions surface
before merging if you run the suite locally on any tailnet-connected machine
before pushing. E2E conversations run per-PR specifically because cross-turn
state handoff regressions merge clean without leaving any trace in the mocked
layers — nightly would report them the morning after they land in production.
That is why the two workflows have different schedules despite sharing the same
runner and proxy.

### Running evals before you push

The suite is not runner-only. Any machine on the tailnet can reach the proxy, so
run it yourself on a change that touches prompts, graph nodes, model tiers, or
anything else whose output the fixtures score:

```bash
ENABLE_LIVE_LLM_EVALS=true \
EVAL_MODELS=gemma4-small \
EVAL_BASELINE_MODELS=gemma4-small \
EVAL_BUDGET_USD=5 \
LLM_PROXY_API_KEY=fake-key \
LLM_PROXY_BASE_URL=https://llm.featherback-mermaid.ts.net/v1 \
pytest tests/evals/ -q
```

`EVAL_MODELS` should match the deployed tier values in `setup/model-tiers.json`;
the nightly workflow derives them from that file rather than hardcoding them.
The proxy does not verify bearer tokens, so the API key is any non-empty
placeholder.

Do this before opening the PR, not after the nightly tells you. It is the only
point in the workflow where a prompt regression is caught before it merges —
the mocked layers cannot see one, because what they assert about is the prompt
string, not what the model does with it.

---

## The Twelve Bug Classes

Each bug class leaves a permanent test. Fix -> regression test ->
`tests/regressions/bug_<NNNN>_<slug>/`. The catalog grows; we don't relearn.

| # | Bug | Where tested | Key assertion |
|---|---|---|---|
| 1 | psycopg3 UUID coercion | `tests/regressions/bug_0570_reminder_uuid_coercion/test_uuid_round_trip.py` | Insert via `reminders.enqueue`, dispatch, assert no `AttributeError`, `state='delivered'`, correct recipient kwarg |
| 2 | LLM capability denial | `tests/unit/test_no_capability_denial.py` (structural) + `tests/evals/fixtures/chat/missed_reminder.yaml` exercised via `tests/evals/test_evals.py` | `regex_forbid` denial phrasing; judge score >= 0.7 for capability acknowledgment |
| 3 | Image orphaned from delivery | `tests/integration/test_reward_image_delivery.py` (follow-up; tracked in `tests/regressions/bug_0563_reward_image_orphan/README.md`) | `mock.call_args.kwargs["attachment_path"]` is non-None AND file exists; not just `mock.called` |
| 4 | Auth gate | `tests/unit/test_signal_listener_auth.py` | Unauthorized peer rejected; no state-table writes |
| 5 | Deployment gaps | `tests/smoke/test_compose_round_trip.py` | Full compose stack boots; reminder_outbox table created when migrations run; env vars threaded |
| 6 | Dead-code wiring | `tests/unit/test_reachability.py` (AST scan) | Every public top-level function in scanned dirs has >= 1 call site outside its definition |
| 7 | Migration filename collisions | `tests/unit/test_migration_filenames.py` | Unique prefixes, monotonic sequence, format `\d{4}_[a-z][a-z0-9_]*.sql` |
| 8 | mypy suppression sprawl | `tests/unit/test_mypy_suppression_budget.py` | Count of `ignore_errors = true` overrides matches frozen baseline; can only shrink |
| 9 | Production dependency pin staleness | `tests/unit/test_signal_cli_pin.py` | Image pinned by immutable digest AND a scheduled refresh workflow exists and targets the same image |
| 10 | Silent degradation behind intentional exception-swallowing, masked by a permissive mock | `tests/unit/test_rewards.py` (`TestImageGenerationCallContract`) | Assert outbound kwargs shape; validate each kwarg against `inspect.signature(real_dependency)` — mock return value is same whether call is valid or not |
| 11 | Cross-turn state handoff | `tests/e2e/scenarios/` | Turn N writes state (LangGraph checkpoint or `recent_outbound`); turn N+M reads it. No single-node test can see the seam — the assertion is the per-turn invariant set in `tests/support/invariants.py`, checked after every turn of every scenario |
| 12 | Notion database query truncation | `tests/regressions/bug_0668_notion_query_pagination/test_notion_query_pagination.py` | All five query verbs route through `_query_database()`; multi-page merge returns all results; `start_cursor` propagated on follow-up requests; cap stops runaway loop and logs `notion.query.pagination_capped` |

---

## Structural Lints (unit speed, always runs)

Six lints in `tests/unit/` that run without LLM or Postgres. Five catch five
of the eleven bug classes directly; one ensures the pre-commit Python gate stays
wired.

### `test_migration_filenames.py`

Globs `migrations/*.sql`, parses the `^\d+_` prefix, asserts:
- All prefixes are unique (no duplicate `0005_*.sql`).
- Prefixes are monotonic starting at 1 with no gaps.
- Filenames match `^\d{4}_[a-z][a-z0-9_]*\.sql$`.

### `test_reachability.py`

AST-scans `app/tools/*.py`, `app/graph/nodes/*.py`, `app/scheduler/*.py`,
`app/ingress/*.py` for top-level public functions (no leading `_`). For each
function, counts total occurrences (word-boundary regex) across all `app/**/*.py`
files. A function with exactly 1 occurrence (the definition line) has no callers
— that is dead-code wiring.

Two exemption sets:
- `_ENTRY_POINTS`: APScheduler callbacks and `main.py` entry points where the
  only reference is the framework registration in the same file.
- `_KNOWN_DEAD`: pre-existing dead code that predates this test. Removing a
  name from here (after fixing the source) is encouraged. Adding a new name
  requires PR justification.

### `test_mypy_suppression_budget.py`

Parses `[[tool.mypy.overrides]]` blocks with `ignore_errors = true` from
`pyproject.toml`. Asserts count matches `BASELINE_COUNT` and module names match
`BASELINE_MODULES`. Currently baseline is 0 (no suppressions). Adding a
suppression requires updating both constants with a PR comment explaining the
rationale and a plan for removal.

### `test_model_tier_swap_surface.py`

Two assertions:
1. `setup/model-tiers.json` parses as JSON with exactly `{expensive, medium, cheap, reminder}` keys and non-empty string values.
2. No Python file in `app/` (except `app/models.py`) contains a hardcoded model
   identifier matching `claude-(opus|sonnet|haiku)|gpt-|gemma`.

Pre-existing violations (before this test was introduced) are listed in
`_KNOWN_VIOLATIONS` as `(relative_path, line_number)` pairs. New violations
always fail. Cleaning up a known violation means removing it from both the
source and the set.

### `test_precommit_python_gate.py`

The pre-commit hook (`scripts/run-required-checks.sh pre-commit`) enforces a
Python gate whenever `.py` files are staged: it runs `ruff check` on the staged
paths and then `pytest tests/unit/ -x -q` against the full unit suite. Both
must pass before the commit proceeds. No auto-fix. Setting `HML_SKIP_HOOK_PYTEST=1`
skips the unit suite for dependency-less CI commit contexts (the review-fixer
host commit step uses this); `python-validation.yml`'s `pytest-unit` required
check covers the same suite there, on a read-only runner with the full dependency
set. This test asserts both the default local path (ruff + pytest) and the opt-in
skip path are wired correctly, so neither can silently regress.

### `test_signal_cli_pin.py`

Guards the two-property invariant for `bbernhard/signal-cli-rest-api` (bug
class 9 — production dependency pin staleness):

1. `docker/compose.yaml` pins the image by immutable sha256 digest (not a
   mutable tag).
2. `.github/workflows/update-signal-cli.yml` exists, references the same
   image, has a `schedule: cron:` stanza, and validates the registry digest
   before writing it.

The refresh workflow rewrites `docker/compose.yaml` by exact string match on
the image line and by regex on the `# Pinned: <date>` provenance comment.
Both formats are a contract between the two files — reformatting either here
silently breaks the refresh.

Adding a new production image dependency to `docker/compose.yaml` requires
adding a similar two-property lint for that image.

---

## Regression Directory Convention

```
tests/regressions/
  bug_<NNNN>_<slug>/
    README.md        # Bug story: what broke, why, issue/PR link
    __init__.py
    test_*.py        # At least one test, OR README notes "test lives in ..."
```

`NNNN` is the canonical GitHub issue or PR number (whichever tracks the bug).
The `README.md` must contain a `#NNN` reference. The structural lint
`tests/unit/test_regression_catalog.py` enforces this shape on every PR.

Standalone run: `pytest tests/regressions/bug_0570_reminder_uuid_coercion -v`

Seeded entries (from PR-1):
- `bug_0570_reminder_uuid_coercion/` — full integration test (skips without `DATABASE_URL`)
- `bug_0567_capability_denial/` — README + pointer; test in eval layer (PR-2)

---

## Eval Layer: Fixture Format

Each fixture in `tests/evals/fixtures/<node>/<scenario>.yaml` describes one
model-under-test scenario:

```yaml
id: chat-missed-reminder-001
node: chat
peer: "<test-peer-1>"
inbound: "you didn't remind me - why not?"
prior_state:
  last_reminder_at: "2026-05-26T14:00:00Z"
  active_task_title: "<placeholder>"
tier: medium
notion_tasks:
  - id: "<placeholder-page-id-1>"
    title: "Water the office plants"
    work_type: "Physical"
    time_estimate: 10
contracts:
  - kind: regex_forbid
    pattern: "(?i)(can(not|'t)|not able to)\\s+send\\s+reminders"
  - kind: regex_require
    pattern: "(?i)(missed|did(n'?t| not) (arrive|fire))"
  - kind: judge
    rubric: "Response acknowledges the system DOES send reminders and offers a concrete forward step."
    threshold: 0.7
  - kind: shame_safe
    threshold: 0.8
```

`notion_tasks` declares the candidate pool the node reads via
`notion.query_pending()`. The runner translates these flat mappings into
Notion's nested property shape and serves them from a stubbed client;
writes are accepted and discarded. Fixtures for nodes that read tasks
(`selection`, `rejection`) must declare a pool — an eval that reaches a
live Notion database scores whatever happens to be in it that day, and one
that reaches an empty pool scores a degenerate "nothing to suggest" reply.

Nodes catch their own exceptions and return a hand-written fallback
message. Those fallbacks are shame-safe by construction, so a fixture that
scores one passes its tone contracts without the model having been called.
The runner detects the node's terminal `<node>_node.error` event and fails
the fixture as an error rather than scoring the fallback. When adding a
node, keep that event name so the guard covers it.

Contract kinds:
- `regex_forbid` / `regex_require` — deterministic; no LLM
- `json_schema` — pydantic validation of structured outputs (intake node)
- `judge` — qualitative rubric scored by a stronger judge LLM (defaults to `claude-sonnet-5`; override via `EVAL_JUDGE_MODEL`)
- `shame_safe` — judge with fixed ADHD-safety rubric from `design/adhd-priorities.md`

Scoring surfaces: nodes write the literal `{task}` token in draft bodies and
`send_node` substitutes the exact stored title before delivery. The runner
mirrors that split — `regex_*` and `json_schema` contracts score the RAW
draft body (so fixtures can assert the token invariant), while `judge` and
`shame_safe` contracts score the DELIVERED body with the token substituted
from the draft's `notion_page_title` (what the user actually reads). Write
rubrics against the delivered text; write token-invariant assertions as
`regex_require: "\\{task\\}"`.

`prior_state.active_task` uses the runtime `ActiveTask` shape (`page_id`,
`title`, ...). The runner injects a fresh `selected_at` when the fixture
omits it — a static timestamp would silently age past `complete_node`'s
24h active-task TTL. Set `selected_at` explicitly only to test the stale
path. Fixture task titles are fictional-but-realistic ("Fold the laundry"),
never real user data — a `<placeholder-task>` literal reads as template
noise to both the model under test and the judge.

Privacy invariant: all fixtures use placeholder ids/peers (`<test-peer>`,
`<placeholder-page-id>`, random UUIDs) and fictional task content. No real
user data in fixtures, commits, or judge LLM payloads.

Eval layer is PR-2. Per-node fixture coverage target: >= 5 fixtures per node
across 9 nodes (intake, selection, chat, rejection, cannot_finish, need_help,
check_in, complete, classify_intent).

---

## Conversation Layer: Harness and Invariants

`tests/e2e/` drives scripted multi-turn conversations. `tests/support/harness.py`
holds the `Conversation` driver, `tests/support/invariants.py` the per-turn
checks, and `tests/support/` the shared Notion/Signal doubles. Full contributor
guide in `tests/e2e/README.md`.

**Entry is through `SignalListener`, not `graph.ainvoke`.** Three things under
test sit upstream of the graph: `thread_id` derivation from the peer E.164,
`_extract_peer_and_text` (production's only inbound parser), and the auth gate
plus the receipt/typing background tasks that run concurrently with `ainvoke`.
Entering at the graph would make the test assert its own assumption about
checkpoint partitioning — a change to that derivation would leave every chain
green while every real conversation silently lost its history.

**Assert side effects, not text.** Under a real model the wording of a reply is
not reproducible; which Notion page was written, whether `recent_outbound.awaiting_reply`
was cleared, what the checkpoint holds, and how many messages went out all are.
The `Expect` object is deliberately side-effect-heavy, and carries no `judge` or
`shame_safe` kind — judged text quality belongs to the eval layer.

**Misroutes are a distinct failure category.** An intent mismatch raises
`IntentMisrouteError`, never a generic assertion, and is never retried: retrying hides
the model drift this layer exists to detect.

**Reminders are delivered by the real worker.** `Conversation.deliver_reminder()`
calls `reminders.enqueue` then `dispatch_due_reminders`. The `INSERT INTO
recent_outbound` inside `reminder_worker` is that table's only writer and the row
a later COMPLETE turn resolves against; a fixture `INSERT` in a test would keep
passing with that production INSERT deleted, which is exactly the pre-#641 state
of the world.

**The clock is never faked.** `complete_node` reads `datetime.now(UTC)` while
Postgres reads `now()`; faking one invents a skew that exists in no deployment.
Staleness is produced by writing backdated values — `age_active_task` through
`graph.aupdate_state`, `expire_recent_outbound` through SQL.

The seven invariants below run after every turn of every scenario, so a
regression trips as soon as any scenario walks past it:

| # | Invariant | What it catches |
|---|---|---|
| I1 | No `<node>_node.error`, `classify_intent.error`, `signal_listener.graph_error`, or `*_failed` event | A node taking its exception fallback. The fallback is shame-safe and reads fine, which is what makes this invisible without the check |
| I2 | A draft carrying `notion_page_title` delivers that title; no `{task}`/`[task]` reaches the user | A suggestion the user cannot act on because it names no task |
| I3 | No `update_status` / `update_property` / `complete_reminder` targets a page the peer was never offered | The generalized form of #641's wrong-page completion |
| I4 | A COMPLETE turn resolves every reminder that was awaiting a reply; other intents do not clear context they did not answer | An unresolved reminder that the next "done" completes a second time |
| I5 | `(recipient, idempotency_key)` unique across the conversation | The duplicate celebration; checked conversation-wide because the second send may be several turns later |
| I6 | No banned shame phrase in delivered text | Regression in shame-safety at the delivery surface, post-substitution |
| I7 | Each LLM caller used its documented tier | A node downgraded to `cheap` gets `think=False` and `max_tokens=1024`, truncating structured JSON mid-object |

---

## Integration Mock Discipline

Integration tests that mock outbound side effects (Signal send, Notion write,
image gen) must assert the SHAPE of the call, not just the fact of the call.

**Wrong (bug class 3 pattern):**
```python
signal_mock.assert_called()
```

**Right:**
```python
signal_mock.assert_awaited_once()
assert signal_mock.await_args.kwargs["recipient"] == "<test-peer>"
assert signal_mock.await_args.kwargs["attachment_path"] is not None
```

This distinction matters because `assert_called()` passed even when
`signal_client.send_message` was called without an `attachment_path`,
meaning every reward image was silently discarded.

---

## LLM Swap: How It Works

`app/models.py` reads model tiers from `setup/model-tiers.json` at a path
hardcoded relative to the repo root. No `MODEL_TIERS_PATH` env override exists
in the current runtime. The eval runner swaps model tiers by writing a modified
`setup/model-tiers.json` into the test working tree before invoking the graph
under test. In the eval harness, LangChain routes through a LiteLLM proxy at
`LLM_PROXY_BASE_URL`; LiteLLM dispatches by model alias. The smoke harness
boots the full stack and makes no LLM calls (no real API keys are required —
compose smoke uses placeholder env values).
The production app runtime uses the same proxy configuration.

Model IDs are validated against a known-prefix allowlist (`claude-`, `gemma`,
`gpt-`) at startup. Swapping a tier to any supported alias is a one-line
change in `setup/model-tiers.json`; no Python adapter changes are required.
All LLM routing stays through `app/models.py:llm(tier)`.

Unit tests for provider-boundary behavior must assert the exact `ChatOpenAI`
constructor payload (model id, temperature, base_url, api_key, timeout,
max_retries, and tier-specific `extra_body`) to catch routing regressions that
would still pass a class-assertion-only check. `timeout` and `max_retries` must
be asserted for all tiers — an unbounded call holds the only inference slot and
stalls every queued conversation. `max_tokens` is tier-conditional: the `cheap`
tier sends it (capped output) and must be asserted; reasoning tiers
(expensive/medium/reminder) omit it and tests must assert its absence. Tests
must assert that the `cheap` tier includes `extra_body={'think': False}` and
that all other tiers do not set `think` unless a deliberate future change adds
it.

Three cost gates for eval runs:
- `ENABLE_LIVE_LLM_EVALS=true` — required for any real LLM call; absent = `pytest.skip`
- `EVAL_MODELS` — explicit comma-separated model alias allowlist; empty = no evals
- `EVAL_BUDGET_USD` — soft cap; runner halts with `pytest.fail("budget exceeded")`

Before running the full eval rig for a model swap, use the **perf harness**
(`tests/perf/`, gated by `ENABLE_LLM_PERF=true`) for a cheap latency + token
comparison. The perf harness measures only speed and token counts — not
behavioral correctness. See `docs/python-rewrite/llm-observability.md`.

---

## Test Discipline Rules (Developer-Facing)

These are the eleven contract clauses the test reviewer enforces (see
`.github/scripts/review/prompts/test.md` for the authoritative enforcement spec):

1. **New public function in `app/tools/`, `app/graph/nodes/`, `app/scheduler/`, `app/ingress/`** must have:
   - An integration test asserting reachability from an end-to-end flow.
   - For DB-typed fields (UUID, timestamp, JSON): an integration test with real Postgres round-trip.
   - For outbound side effects: assertion on `mock.call_args.kwargs` shape, not just `mock.called`.

2. **New or modified prompt template in `app/prompts/`** must have:
   - Updated structural test if it adds a new section anchor.
   - New eval fixture if behavior contract changed (new capability statement, new banned phrase).

3. **New migration in `migrations/`** must:
   - Use the next monotonic integer prefix (structural lint enforces this).
   - For schema changes touching code-read columns: include an integration test.

4. **New env var or compose service** must have:
   - Assertion in `tests/smoke/test_compose_round_trip.py` that it's threaded through.
   - Documentation in `docker/compose.yaml` comments.
   - **Exception — CI-only / perf-harness env vars**: `ENABLE_LLM_PERF`, `PERF_MODELS`,
     `PERF_RUNS_N`, and `PERF_RUNS_DIR` are perf-harness-only and are never threaded
     through `docker/compose.yaml`. They are documented in
     `docs/python-rewrite/llm-observability.md` and do not require
     `test_compose_round_trip.py` coverage.

5. **PR fixing a production bug** must add:
   - `tests/regressions/bug_<NNNN>_<slug>/` directory with README citing issue/PR.
   - At least one `test_*.py`, or README note "test lives in ...".

6. **Dropped tests** need explicit PR-body justification. Silent deletion of a failing test is always a blocker.

7. **Changes to `scripts/run-required-checks.sh` that add or modify a pre-commit dispatch branch** must have:
   - A structural test in `tests/unit/` asserting the new branch is wired: the function exists, is called from the dispatcher, and invokes the expected tools.
   - Example: `test_precommit_python_gate.py` covers `run_pre_commit_python_checks`.

8. **PRs that add a new production image dependency to `docker/compose.yaml`** must add:
   - A structural lint in `tests/unit/` asserting the two-property invariant: (1) the image is pinned by immutable sha256 digest, not a mutable tag; and (2) a scheduled refresh workflow exists that targets the same image and validates the digest before writing. (Catches bug class 9 — production dependency pin staleness; see `tests/unit/test_signal_cli_pin.py` as the canonical template.)
   - PRs that remove or weaken an existing production-dependency pin lint (e.g., deleting `test_signal_cli_pin.py` or removing the digest-validation assertion) are blockers under clause 6 unless the dependency itself is also removed.

9. **New or modified eval fixtures in `tests/evals/fixtures/<node>/`** must conform to the eval-rig architecture:
   - Fixtures for nodes that read tasks (`selection`, `rejection`, or any node whose body calls `query_pending`) must declare a `notion_tasks` pool. A fixture without `notion_tasks` for such a node scores whatever happens to be in the live Notion database, making results non-comparable across runs and models.
   - The fixture runner serves task pools from a stubbed Notion client (`_install_notion_stub`). Any PR that changes the `_as_notion_page` translator or the Notion stub must update `tests/unit/test_eval_rig.py` to assert the new translation round-trips through the real node-side extractors.
   - New eval-covered graph nodes must emit a terminal `<node>_node.error` event on exception. A node that swallows exceptions and returns a hand-written fallback will score that fallback as model output.
   - `regex_*` and `json_schema` contracts score the RAW draft body; `judge` and `shame_safe` contracts score the DELIVERED body (token substituted from `notion_page_title`). Write rubrics against the delivered text; assert token invariants as `regex_require: "\\{task\\}"`.
   - `prior_state.active_task` must use the runtime `ActiveTask` shape (`page_id`, `title`). Omit `selected_at` to let the runner inject a fresh timestamp; set it explicitly only to test the stale-task path.

10. **Side-effecting calls wrapped in intentional exception-swallowing handlers** must have:
    - A test that asserts the outbound call's kwargs shape directly — not just the fallback return value, which looks identical whether the call was valid or not.
    - A test that validates each kwarg name against `inspect.signature(real_dependency)` so a parameter rejected or removed by a future SDK version fails loudly rather than silently falling back. (Catches bug class 10 — silent degradation behind intentional exception-swallowing; see `tests/unit/test_rewards.py` `TestImageGenerationCallContract` as the canonical template.)

11. **PRs that add or modify E2E conversation scenarios, cross-turn invariants, or the conversation-layer harness** must:
    - Enter scenarios through `SignalListener`, not `graph.ainvoke`, so `thread_id` derivation, the auth gate, and concurrent background tasks (read receipts, typing indicators) are under test.
    - Assert side-effect shapes: which Notion page was written, whether `recent_outbound.awaiting_reply` cleared, what survived in the checkpoint, how many messages went out. Wording checks use `regex_require`/`regex_forbid`, not equality on model text.
    - Cover any new cross-turn handoff — for example, a `recent_outbound` row written by `reminder_worker` several turns before the COMPLETE turn that resolves against it — with a full multi-turn scenario rather than a single-node call with a hand-built `State`.
    - Never retry on `IntentMisrouteError`. Retrying hides the classifier drift this layer exists to detect. (Catches bug class 11 — cross-turn state handoff regressions.)
    - Rely on the seven per-turn invariants in `tests/support/invariants.py`, which run automatically after every `conversation.say()` call.

If this PR adds a new bug class or extends the layer architecture described in
this document, update this document AND update
`.github/scripts/review/prompts/test.md` to enforce the new contract.
