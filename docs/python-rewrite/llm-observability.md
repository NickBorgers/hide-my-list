# LLM Observability: Token + Latency Logging

Structural metadata (token counts, durations, model names) for every LLM call
ships to structlog and from there to Gravwell via the existing log pipeline.
The observability layer is always on in production — no flag required.

For model perf comparison before running the full eval rig, the perf harness
in `tests/perf/` provides controlled latency + token stats. See "Perf Harness"
below.

---

## Structlog Events

All events are emitted at `INFO` level (errors at `WARNING`) by
`app/observability/llm_callback.py` via `structlog.get_logger(__name__)`.

### `llm.call.start`

Emitted when an LLM invocation begins.

| Field           | Type          | Description                                         |
|---|---|---|
| `correlation_id`| `str`         | UUID hex, unique per call                           |
| `tier`          | `str`         | Model tier: `expensive`, `medium`, `cheap`, `reminder` |
| `model`         | `str`         | Resolved model ID, e.g. `claude-sonnet-4-6`        |
| `caller`        | `str \| None` | Node name, e.g. `intake`, `chat`, `classify`       |
| `messages_count`| `int`         | Number of messages in the prompt (not content)     |

### `llm.call.end`

Emitted on successful completion.

| Field           | Type          | Description                                         |
|---|---|---|
| `correlation_id`| `str`         | Matches the corresponding `llm.call.start`          |
| `tier`          | `str`         | Model tier                                          |
| `model`         | `str`         | Resolved model ID                                   |
| `caller`        | `str \| None` | Node name                                           |
| `duration_ms`   | `float`       | Wall-clock milliseconds from start to end           |
| `input_tokens`  | `int \| None` | Prompt tokens (None if provider did not return)     |
| `output_tokens` | `int \| None` | Completion tokens (None if provider did not return) |
| `total_tokens`  | `int \| None` | Total tokens; derived from parts when not provided  |

### `llm.call.error`

Emitted when the LLM call raises an exception. Partial calls appear in Gravwell
so failed calls are not invisible.

| Field           | Type          | Description                                         |
|---|---|---|
| `correlation_id`| `str`         | Matches the corresponding `llm.call.start`          |
| `tier`          | `str`         | Model tier                                          |
| `model`         | `str`         | Resolved model ID                                   |
| `caller`        | `str \| None` | Node name                                           |
| `duration_ms`   | `float`       | Wall-clock ms at time of error                      |
| `error_type`    | `str`         | `type(exc).__name__`, e.g. `ConnectionError`        |

### `image_gen.start` / `image_gen.end`

Emitted by `app/tools/rewards.py:generate_reward_image` for OpenAI image
generation calls (not via LangChain).

| Field         | Type    | Description                                           |
|---|---|---|
| `intensity`   | `str`   | Reward intensity level                                |
| `streak_count`| `int`   | (start only) Streak count at time of call             |
| `duration_ms` | `float` | (end only) Wall-clock ms for the image gen call       |

---

## Privacy Invariant

The callback **never** logs message content, prompt text, response text, or
task titles. Only structural metadata (counts, durations, model identifiers,
tier, caller, correlation_id) reaches logs. The `_redact_private_log_fields`
processor in `app/main.py` is a safety net; the callback does not rely on it.

---

## Wiring

`app/models.py:llm(tier, *, caller=None)` constructs one
`LLMObservabilityCallback` per call and attaches it via
`ChatOpenAI(...).with_config(callbacks=[handler])`. The callback captures
tier + model + caller at construction time so each log event is self-describing.

Every graph node passes `caller=` to `llm()`:

| Node / function     | caller value      |
|---|---|
| `classify_intent`   | `"classify"`      |
| `intake_node`       | `"intake"`        |
| `selection_node`    | `"selection"`     |
| `chat_node`         | `"chat"`          |
| `rejection_node`    | `"rejection"`     |
| `cannot_finish_node`| `"cannot_finish"` |
| `need_help_node`    | `"need_help"`     |
| `check_in_node`     | `"check_in"`      |

Callers without an explicit `caller=` kwarg log `caller=null` — backward
compatible.

---

## Perf Harness

The perf harness provides controlled latency + token stats for model comparison
without running the full behavioral eval rig.

### Quick start

```bash
ENABLE_LLM_PERF=true \
LLM_PROXY_BASE_URL=<openai-compatible-proxy-url> \
LLM_PROXY_API_KEY=<key> \
pytest tests/perf/test_llm_perf.py -v
```

Default: runs all four tiers (`medium`, `cheap`, `expensive`, `reminder`)
with 3 runs per prompt across 13 synthetic prompts (39 calls per tier).

### Env vars

| Var              | Default              | Description                                      |
|---|---|---|
| `ENABLE_LLM_PERF`| unset (skip)         | Set to `true` to enable; unset = all perf tests skip |
| `PERF_MODELS`    | all four tiers       | Comma-separated tier names to run                 |
| `PERF_RUNS_N`    | `3`                  | Runs per prompt per model                         |
| `PERF_RUNS_DIR`  | `tests/perf/runs/`   | Output directory for JSON + Markdown              |

### Comparing two models

To compare `gemma4-small` against the current medium tier:

1. Update `setup/model-tiers.json` to point `medium` at the target model ID.
   `app/models.py` accepts model IDs starting with `claude-`, `gemma`, or `gpt-`;
   no code change is required for aliases in the allowlist.
2. Run:
   ```bash
   ENABLE_LLM_PERF=true PERF_MODELS=medium PERF_RUNS_N=5 \
   pytest tests/perf/test_llm_perf.py -v -k medium
   ```
3. Revert `model-tiers.json` and run the same for the baseline.
4. Read the Markdown table in `tests/perf/runs/<timestamp>/report.md`.

### Output

Per-model JSON in `tests/perf/runs/<timestamp>/<tier>.json`:

```json
{
  "model": "medium",
  "n_runs": 3,
  "prompts_count": 13,
  "latency": { "min": 412.3, "median": 620.1, "p95": 1240.5, "max": 1580.2 },
  "tokens": { "mean_input_tokens": 82.4, "mean_output_tokens": 31.7, "mean_total_tokens": 114.1 },
  "per_prompt": [...]
}
```

Comparison table in `tests/perf/runs/<timestamp>/report.md`:

```
| Model   | min_ms | median_ms | p95_ms | max_ms | mean_input | mean_output | mean_total |
|---------|--------|-----------|--------|--------|------------|-------------|------------|
| medium  | 412.3  | 620.1     | 1240.5 | 1580.2 | 82.4       | 31.7        | 114.1      |
```

### Relationship to the eval rig

The perf harness measures latency and token counts only. It does not evaluate
behavioral correctness (shame-safety, JSON schema, capability denial). That is
the eval rig's role (`tests/evals/`, `ENABLE_LIVE_LLM_EVALS`). Run the perf
harness first to screen a new model for speed and cost; run the eval rig to
validate behavioral contracts. See `docs/python-rewrite/test-rig.md` for the
full layer architecture.
