"""Multi-model fixture runner for the eval layer.

Reads YAML behavior fixtures from `tests/evals/fixtures/<node>/*.yaml`,
runs each fixture against every model in `EVAL_MODELS`, evaluates the
declared contracts (regex_forbid, regex_require, json_schema, judge,
shame_safe), and writes a per-run JSON + Markdown comparison report.

Design notes:
- The model swap mechanism rewrites `setup/model-tiers.json` per session.
  The app reads model tiers from `app.models.llm(tier)` which reads that
  file at import time. Pytest fixtures clear the lru_cache between runs.
- Real LLM calls go through the LiteLLM proxy at `LLM_PROXY_BASE_URL`.
  Same proxy production uses; what we validate matches what users see.
- The judge LLM defaults to claude-sonnet-5 (per the rig spec; override via
  EVAL_JUDGE_MODEL), never the model under test. Judge cache is in `tests/evals/.cache/`.
- Cost gates: `ENABLE_LIVE_LLM_EVALS=true` required; `EVAL_BUDGET_USD`
  is a soft cap that aborts the run when projected spend exceeds it.

Privacy: fixtures contain only placeholder data per the rig spec.
The runner emits judge prompts with the fixture's inbound + model's
response. Both are placeholders by convention.

Scoring surfaces: nodes write the literal `{task}` token in message bodies
and `send_node` substitutes the exact stored title before delivery
(`app/graph/nodes/_task_token.py`). The runner mirrors that split:
regex/json contracts score the RAW draft body (so fixtures can assert the
token invariant), while judge/shame_safe contracts score the DELIVERED body
with the token substituted (what the user actually reads — a judge scoring
the raw token would ding it as an unfilled placeholder).

Invocation:
    ENABLE_LIVE_LLM_EVALS=true \\
    EVAL_MODELS=claude-haiku-4-5,gemma4-small \\
    EVAL_BUDGET_USD=15 \\
    pytest tests/evals/ -v
"""
from __future__ import annotations

import dataclasses
import json
import logging
import os
import re
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from tests.support import notion_fake
from tests.support.notion_fake import FakeNotion

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).parent.parent.parent
_FIXTURES_DIR = Path(__file__).parent / "fixtures"
_RUNS_DIR = Path(__file__).parent / "runs"
_MODEL_TIERS_PATH = _REPO_ROOT / "setup" / "model-tiers.json"

# Rough per-1M-token cost estimates (USD). Updated by hand; the rig spec
# acknowledges these are approximations. Used only for the EVAL_BUDGET_USD
# soft cap. Conservative — overestimate rather than underestimate.
_COST_PER_MTOK_USD: dict[str, dict[str, float]] = {
    # model alias -> {input, output}
    "claude-opus-4-6": {"input": 15.0, "output": 75.0},
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0},
    "claude-sonnet-5": {"input": 3.0, "output": 15.0},
    "claude-haiku-4-5": {"input": 0.80, "output": 4.0},
    "gemma4-small": {"input": 0.10, "output": 0.40},  # self-hosted estimate
}
_DEFAULT_COST = {"input": 5.0, "output": 25.0}  # unknown alias fallback


# ---------------------------------------------------------------------------
# Fixture loading
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class Contract:
    """One assertion attached to a fixture."""

    kind: str
    spec: dict[str, Any]


@dataclasses.dataclass
class Fixture:
    """A single behavior contract scenario."""

    id: str
    node: str
    tier: str
    inbound: str
    peer: str
    prior_state: dict[str, Any]
    notion_tasks: list[dict[str, Any]]
    contracts: list[Contract]
    path: Path  # Source file (for error messages)


def discover_fixtures(fixtures_dir: Path = _FIXTURES_DIR) -> list[Fixture]:
    """Walk fixtures/<node>/*.yaml and return parsed Fixture objects.

    Skips files with leading underscore (reserved for shared snippets).
    """
    fixtures: list[Fixture] = []
    if not fixtures_dir.is_dir():
        return fixtures
    for node_dir in sorted(fixtures_dir.iterdir()):
        if not node_dir.is_dir():
            continue
        node = node_dir.name
        for yaml_path in sorted(node_dir.glob("*.yaml")):
            if yaml_path.name.startswith("_"):
                continue
            raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError(f"{yaml_path}: top-level YAML must be a mapping")
            contracts_raw = raw.get("contracts", [])
            contracts = [
                Contract(kind=c["kind"], spec={k: v for k, v in c.items() if k != "kind"})
                for c in contracts_raw
            ]
            fixtures.append(
                Fixture(
                    id=raw.get("id", yaml_path.stem),
                    node=raw.get("node", node),
                    tier=raw.get("tier", "medium"),
                    inbound=raw.get("inbound", ""),
                    peer=raw.get("peer", "<test-peer>"),
                    prior_state=raw.get("prior_state", {}),
                    notion_tasks=raw.get("notion_tasks", []),
                    contracts=contracts,
                    path=yaml_path,
                )
            )
    return fixtures


# ---------------------------------------------------------------------------
# Model swap
# ---------------------------------------------------------------------------


def _read_tiers() -> dict[str, str]:
    return json.loads(_MODEL_TIERS_PATH.read_text(encoding="utf-8"))


def _write_tiers(tiers: dict[str, str]) -> None:
    _MODEL_TIERS_PATH.write_text(
        json.dumps(tiers, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _clear_model_caches() -> None:
    """Clear caches in app.models so the rewritten JSON is picked up."""
    try:
        import app.models as models_mod  # noqa: PLC0415

        # _load_model_tiers is @lru_cache'd
        if hasattr(models_mod, "_load_model_tiers"):
            cache_clear = getattr(models_mod._load_model_tiers, "cache_clear", None)
            if callable(cache_clear):
                cache_clear()
    except Exception as exc:  # noqa: BLE001
        log.warning("eval.clear_model_caches.failed", extra={"error": str(exc)})


class ModelSwap:
    """Context manager that substitutes a model at the given tier.

    Rewrites `setup/model-tiers.json` on entry, restores on exit. Clears
    `app.models` caches both times so the change takes effect immediately.
    """

    def __init__(self, *, tier: str, model: str) -> None:
        self._tier = tier
        self._model = model
        self._original: dict[str, str] = {}

    def __enter__(self) -> ModelSwap:
        self._original = _read_tiers()
        new_tiers = dict(self._original)
        new_tiers[self._tier] = self._model
        _write_tiers(new_tiers)
        _clear_model_caches()
        return self

    def __exit__(self, *_exc: Any) -> None:
        _write_tiers(self._original)
        _clear_model_caches()


# ---------------------------------------------------------------------------
# Contract evaluation
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class ContractResult:
    """Outcome of one contract evaluation."""

    kind: str
    passed: bool
    detail: str  # Why it passed or failed (short)


def _eval_regex_forbid(spec: dict[str, Any], response: str) -> ContractResult:
    pattern = spec["pattern"]
    match = re.search(pattern, response)
    if match:
        return ContractResult(
            kind="regex_forbid",
            passed=False,
            detail=f"matched forbidden pattern {pattern!r}: {match.group(0)!r}",
        )
    return ContractResult(kind="regex_forbid", passed=True, detail="no match")


def _eval_regex_require(spec: dict[str, Any], response: str) -> ContractResult:
    pattern = spec["pattern"]
    if re.search(pattern, response):
        return ContractResult(kind="regex_require", passed=True, detail="matched")
    return ContractResult(
        kind="regex_require",
        passed=False,
        detail=f"required pattern {pattern!r} not found",
    )


def _eval_json_schema(spec: dict[str, Any], response: str) -> ContractResult:
    schema_ref = spec.get("schema_ref")
    # v1: just check it parses as JSON. Schema-specific validation is a
    # follow-up; the spec ref is captured in the result for later wiring.
    try:
        json.loads(response)
        return ContractResult(
            kind="json_schema",
            passed=True,
            detail=f"valid JSON (schema_ref={schema_ref!r}, full validation deferred)",
        )
    except json.JSONDecodeError as exc:
        return ContractResult(
            kind="json_schema",
            passed=False,
            detail=f"not valid JSON: {exc.msg} at pos {exc.pos}",
        )


def _eval_judge(spec: dict[str, Any], response: str) -> ContractResult:
    from tests.evals.judge import score  # noqa: PLC0415

    rubric = spec["rubric"]
    threshold = float(spec.get("threshold", 0.7))
    result = score(rubric=rubric, response=response)
    passed = result.score >= threshold
    return ContractResult(
        kind="judge",
        passed=passed,
        detail=f"score={result.score:.2f} threshold={threshold:.2f} ({result.reasoning})",
    )


_SHAME_SAFE_RUBRIC = (
    "The response avoids shame-triggering language for an ADHD user. "
    "It must NOT use 'you didn't', 'you should have', 'you forgot', "
    "'you failed', 'you missed', 'you were supposed to', or similar "
    "blame framings. Difficulties are framed as information, not "
    "shortcomings. Score 1.0 if fully shame-safe, 0.0 if any blame "
    "framing is present, intermediate for borderline cases."
)


def _eval_shame_safe(spec: dict[str, Any], response: str) -> ContractResult:
    from tests.evals.judge import score  # noqa: PLC0415

    threshold = float(spec.get("threshold", 0.8))
    result = score(rubric=_SHAME_SAFE_RUBRIC, response=response)
    passed = result.score >= threshold
    return ContractResult(
        kind="shame_safe",
        passed=passed,
        detail=f"score={result.score:.2f} threshold={threshold:.2f} ({result.reasoning})",
    )


_CONTRACT_EVALUATORS = {
    "regex_forbid": _eval_regex_forbid,
    "regex_require": _eval_regex_require,
    "json_schema": _eval_json_schema,
    "judge": _eval_judge,
    "shame_safe": _eval_shame_safe,
}


# Contract kinds scored against the delivered (token-substituted) body rather
# than the raw draft body. See "Scoring surfaces" in the module docstring.
_DELIVERED_SURFACE_KINDS = frozenset({"judge", "shame_safe"})


def evaluate_contracts(
    contracts: list[Contract],
    response: str,
    delivered_response: str | None = None,
) -> list[ContractResult]:
    delivered = delivered_response if delivered_response is not None else response
    results: list[ContractResult] = []
    for c in contracts:
        surface = delivered if c.kind in _DELIVERED_SURFACE_KINDS else response
        evaluator = _CONTRACT_EVALUATORS.get(c.kind)
        if evaluator is None:
            results.append(
                ContractResult(
                    kind=c.kind,
                    passed=False,
                    detail=f"unknown contract kind {c.kind!r}",
                )
            )
            continue
        try:
            results.append(evaluator(c.spec, surface))
        except Exception as exc:  # noqa: BLE001
            results.append(
                ContractResult(
                    kind=c.kind,
                    passed=False,
                    detail=f"evaluator raised {type(exc).__name__}: {exc}",
                )
            )
    return results


# ---------------------------------------------------------------------------
# Node invocation
# ---------------------------------------------------------------------------


# The flat-shorthand -> Notion-property translator lives in tests/support so the
# eval and conversation layers cannot drift into two different readings of
# docs/notion-schema.md. Re-exported under the module-private names this file
# has always used.
_SELECT_PROPS = notion_fake._SELECT_PROPS
_NUMBER_PROPS = notion_fake._NUMBER_PROPS
_CHECKBOX_PROPS = notion_fake._CHECKBOX_PROPS
_as_notion_page = notion_fake.as_notion_page


def _install_notion_stub(fixture: Fixture) -> Callable[[], None]:
    """Point the Notion client at the fixture's declared tasks.

    Evals must score the model, not the state of a live Notion database.
    Reads return the fixture's `notion_tasks`; writes are accepted and
    discarded. Returns an undo callable.

    `discard_writes=True` is what keeps eval semantics unchanged: a node that
    marks a task In Progress must not alter what a later read in the same
    fixture returns, because the fixture — not the run — defines the world.
    """
    fake = FakeNotion(fixture.notion_tasks, discard_writes=True, filter_reads=False)
    return fake.install()


def _invoke_node(node: str, fixture: Fixture) -> tuple[str, str | None]:
    """Run the named graph node against the fixture.

    Returns `(body, notion_page_title)` from the first `pending_outbound`
    draft the node populates — the raw draft body plus the title `send_node`
    would substitute for the `{task}` token at delivery time.

    Each node has its own signature in `app/graph/nodes/<node>.py`. We
    import lazily and call the canonical `<node>_node(state)` function.

    Nodes wrap their body in a try/except that returns a hand-written
    fallback message on any failure. Those fallbacks are shame-safe by
    construction, so they satisfy most contracts without the model having
    been consulted at all — a fixture scoring one is green while testing
    nothing. We capture the node's terminal `<node>_node.error` event and
    raise, so that case is reported as an error rather than a pass.
    """
    from structlog.testing import capture_logs  # noqa: PLC0415

    from app.graph.state import State  # noqa: PLC0415

    state: State = {  # type: ignore[typeddict-item]
        "peer": fixture.peer,
        "incoming": fixture.inbound,
        **fixture.prior_state,
    }
    # Static fixtures cannot carry a fresh timestamp, but nodes treat a
    # missing or stale `selected_at` as "no active task" (24h TTL in
    # complete_node). Inject run-time now so the fixture's active task is
    # actually active. Fixtures that want the stale path set it explicitly.
    active = state.get("active_task")
    if isinstance(active, dict) and "selected_at" not in active:
        state["active_task"] = {  # type: ignore[typeddict-item]
            **active,
            "selected_at": datetime.now(UTC).isoformat(),
        }
    # Fixtures declare prior messages as `{role, content}` YAML mappings, but
    # nodes read LangChain message objects via getattr — a plain dict silently
    # yields empty strings, dropping the fixture's conversational context.
    raw_messages: list[Any] = list(state.get("messages") or [])
    if raw_messages:
        from langchain_core.messages import AIMessage, HumanMessage  # noqa: PLC0415

        state["messages"] = [
            AIMessage(content=str(m.get("content", "")))
            if m.get("role") == "assistant"
            else HumanMessage(content=str(m.get("content", "")))
            for m in raw_messages
            if isinstance(m, dict)
        ]

    import asyncio  # noqa: PLC0415

    if node == "classify_intent":
        # Intent classification lives in app/graph/routing.py, not under
        # app/graph/nodes/, and returns {"intent": <label>} rather than a
        # pending_outbound draft. The label string is the scored response.
        from app.graph.routing import classify_intent  # noqa: PLC0415

        with capture_logs() as captured:
            classified = asyncio.run(classify_intent(state))
        for entry in captured:
            if entry.get("event") == "classify_intent.error":
                raise RuntimeError(
                    "classify_intent took its exception fallback path — the returned "
                    "label is the hand-written CHAT default, not model output, and "
                    "scoring it would be meaningless. Fix the underlying error before "
                    "trusting this fixture."
                )
        return str(classified.get("intent") or ""), None

    module_path = f"app.graph.nodes.{node}"
    import importlib  # noqa: PLC0415

    module = importlib.import_module(module_path)
    fn_name = f"{node}_node"
    fn = getattr(module, fn_name, None)
    if fn is None:
        raise RuntimeError(
            f"{module_path}.{fn_name} not found — fixture targets a node that has no handler"
        )

    undo_notion = _install_notion_stub(fixture)
    try:
        with capture_logs() as captured:
            if asyncio.iscoroutinefunction(fn):
                update = asyncio.run(fn(state))
            else:
                update = fn(state)
    finally:
        undo_notion()

    terminal_error = f"{node}_node.error"
    for entry in captured:
        if entry.get("event") == terminal_error:
            raise RuntimeError(
                f"{fn_name} took its exception fallback path — the returned body is a "
                f"hand-written fallback, not model output, and scoring it would be "
                f"meaningless. Fix the underlying error before trusting this fixture."
            )

    if not isinstance(update, dict):
        raise RuntimeError(f"{fn_name} returned non-dict: {type(update).__name__}")
    pending = update.get("pending_outbound") or []
    if not pending:
        return "", None
    first = pending[0]
    if not isinstance(first, dict):
        return str(first), None
    title = first.get("notion_page_title")
    return str(first.get("body", "")), str(title) if title else None


# ---------------------------------------------------------------------------
# Budget tracking
# ---------------------------------------------------------------------------


def _estimate_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    rates = _COST_PER_MTOK_USD.get(model, _DEFAULT_COST)
    return (input_tokens * rates["input"] + output_tokens * rates["output"]) / 1_000_000.0


def _rough_tokens(text: str) -> int:
    """Rough token estimate. Conservative — overestimates."""
    return max(1, len(text) // 3)


# ---------------------------------------------------------------------------
# Run orchestration
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class FixtureRunResult:
    """Outcome of one (fixture, model) invocation."""

    fixture_id: str
    node: str
    model: str
    response: str
    contracts: list[ContractResult]
    duration_seconds: float
    estimated_cost_usd: float
    error: str | None = None

    @property
    def passed(self) -> bool:
        return self.error is None and all(c.passed for c in self.contracts)


def _enabled() -> bool:
    return os.environ.get("ENABLE_LIVE_LLM_EVALS", "").lower() in ("true", "1", "yes")


def _models_to_test() -> list[str]:
    raw = os.environ.get("EVAL_MODELS", "").strip()
    if not raw:
        return []
    return [m.strip() for m in raw.split(",") if m.strip()]


def _budget_usd() -> float:
    raw = os.environ.get("EVAL_BUDGET_USD", "").strip()
    if not raw:
        return float("inf")
    try:
        return float(raw)
    except ValueError:
        return float("inf")


def _run_one(fixture: Fixture, model: str) -> FixtureRunResult:
    """Run a single (fixture, model) and evaluate contracts."""
    from app.graph.nodes._task_token import render_task_token  # noqa: PLC0415

    start = time.monotonic()
    try:
        with ModelSwap(tier=fixture.tier, model=model):
            response, page_title = _invoke_node(fixture.node, fixture)
        # What send_node would deliver: judge/shame_safe score this surface.
        delivered = render_task_token(response, title=page_title)
        contracts = evaluate_contracts(fixture.contracts, response, delivered)
        duration = time.monotonic() - start
        input_tokens = _rough_tokens(fixture.inbound + json.dumps(fixture.prior_state))
        output_tokens = _rough_tokens(response)
        return FixtureRunResult(
            fixture_id=fixture.id,
            node=fixture.node,
            model=model,
            response=delivered,
            contracts=contracts,
            duration_seconds=duration,
            estimated_cost_usd=_estimate_cost_usd(model, input_tokens, output_tokens),
        )
    except Exception as exc:  # noqa: BLE001
        duration = time.monotonic() - start
        return FixtureRunResult(
            fixture_id=fixture.id,
            node=fixture.node,
            model=model,
            response="",
            contracts=[],
            duration_seconds=duration,
            estimated_cost_usd=0.0,
            error=f"{type(exc).__name__}: {exc}",
        )


def run_session(
    fixtures: list[Fixture],
    models: list[str],
    *,
    budget_usd: float = float("inf"),
    candidate_tier: str | None = None,
    candidate_model: str | None = None,
) -> Iterator[FixtureRunResult]:
    """Yield a result for every (fixture, model) pair.

    Budget enforcement: when projected spend exceeds `budget_usd`, the
    remaining pairs are emitted as `FixtureRunResult` with
    `error='budget_exceeded'` (and empty contracts). They count as
    failures for any baseline check — a low budget cannot produce a
    false-success by silently skipping pairs.

    When `candidate_model` is set, tier-scoped filtering applies:
    the candidate only runs against fixtures for `candidate_tier`; each
    baseline model only runs against fixtures for tiers it serves in
    production. When `candidate_model` is None, all (model, fixture)
    pairs run (backward-compatible behaviour for plain baseline runs).
    """
    production_tiers = _read_tiers() if candidate_model is not None else {}
    spent = 0.0
    budget_hit = False
    for model in models:
        is_candidate = candidate_model is not None and model == candidate_model
        for fixture in fixtures:
            if candidate_model is not None:
                if is_candidate:
                    if candidate_tier and fixture.tier != candidate_tier:
                        continue
                else:
                    if production_tiers.get(fixture.tier) != model:
                        continue
            if budget_hit or spent > budget_usd:
                if not budget_hit:
                    log.warning(
                        "eval.budget_exceeded",
                        extra={"spent_usd": spent, "budget_usd": budget_usd},
                    )
                    budget_hit = True
                yield FixtureRunResult(
                    fixture_id=fixture.id,
                    node=fixture.node,
                    model=model,
                    response="",
                    contracts=[],
                    duration_seconds=0.0,
                    estimated_cost_usd=0.0,
                    error=f"budget_exceeded (cap=${budget_usd:.2f}, spent=${spent:.4f})",
                )
                continue
            result = _run_one(fixture, model)
            spent += result.estimated_cost_usd
            yield result


# ---------------------------------------------------------------------------
# Report writer
# ---------------------------------------------------------------------------


def write_report(
    results: list[FixtureRunResult],
    out_dir: Path | None = None,
) -> Path:
    """Write per-run JSON + Markdown comparison report. Returns the report dir."""
    if out_dir is None:
        ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        out_dir = _RUNS_DIR / ts
    out_dir.mkdir(parents=True, exist_ok=True)

    # Per-result JSON files
    for r in results:
        path = out_dir / r.model / f"{r.fixture_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "fixture_id": r.fixture_id,
                    "node": r.node,
                    "model": r.model,
                    "response": r.response,
                    "contracts": [dataclasses.asdict(c) for c in r.contracts],
                    "duration_seconds": r.duration_seconds,
                    "estimated_cost_usd": r.estimated_cost_usd,
                    "error": r.error,
                    "passed": r.passed,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    # Markdown comparison table grouped by fixture
    by_fixture: dict[str, dict[str, FixtureRunResult]] = {}
    models_seen: list[str] = []
    for r in results:
        by_fixture.setdefault(r.fixture_id, {})[r.model] = r
        if r.model not in models_seen:
            models_seen.append(r.model)

    lines = [
        "# Eval Report",
        "",
        f"Generated: {datetime.now(UTC).isoformat()}",
        f"Models: {', '.join(models_seen)}",
        f"Fixtures: {len(by_fixture)}",
        "",
        "## Fixture × Model",
        "",
        "| Fixture | Node | " + " | ".join(models_seen) + " |",
        "|" + "---|" * (2 + len(models_seen)),
    ]
    for fid in sorted(by_fixture):
        row_runs = by_fixture[fid]
        node = next(iter(row_runs.values())).node
        cells = []
        for m in models_seen:
            r = row_runs.get(m)
            if r is None:
                cells.append("—")
            elif r.error:
                cells.append("❌ ERR")
            else:
                passed = sum(c.passed for c in r.contracts)
                total = len(r.contracts)
                mark = "✅" if r.passed else "❌"
                cells.append(f"{mark} {passed}/{total}")
        lines.append(f"| {fid} | {node} | " + " | ".join(cells) + " |")

    # Per-model totals
    lines += [
        "",
        "## Per-model totals",
        "",
        "| Model | Pass | Fail | Error | Total | Est. cost (USD) |",
        "|---|---|---|---|---|---|",
    ]
    for m in models_seen:
        runs = [r for r in results if r.model == m]
        passed = sum(1 for r in runs if r.passed)
        errors = sum(1 for r in runs if r.error is not None)
        failed = len(runs) - passed - errors
        cost = sum(r.estimated_cost_usd for r in runs)
        lines.append(f"| {m} | {passed} | {failed} | {errors} | {len(runs)} | ${cost:.4f} |")

    (out_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out_dir


# ---------------------------------------------------------------------------
# CLI / pytest entry points
# ---------------------------------------------------------------------------


def main() -> int:
    """Standalone entry point. Returns exit code."""
    if not _enabled():
        log.info("eval.skipped", extra={"reason": "ENABLE_LIVE_LLM_EVALS not set"})
        return 0
    models = _models_to_test()
    if not models:
        log.info("eval.skipped", extra={"reason": "EVAL_MODELS empty"})
        return 0
    fixtures = discover_fixtures()
    if not fixtures:
        log.warning("eval.no_fixtures")
        return 0
    baseline_raw = os.environ.get("EVAL_BASELINE_MODELS", "").strip()
    baseline_set = {m.strip() for m in baseline_raw.split(",") if m.strip()}
    candidate_tier = os.environ.get("EVAL_CANDIDATE_TIER", "").strip() or None
    candidate_models = [m for m in models if m not in baseline_set] if baseline_set else []
    candidate_model = candidate_models[0] if len(candidate_models) == 1 else None
    results = list(
        run_session(
            fixtures,
            models,
            budget_usd=_budget_usd(),
            candidate_tier=candidate_tier,
            candidate_model=candidate_model,
        )
    )
    out_dir = write_report(results)
    log.info("eval.report_written", extra={"path": str(out_dir)})
    # Exit non-zero if any BASELINE model failed any contract.
    if baseline_set:
        for r in results:
            if r.model in baseline_set and not r.passed:
                return 1
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    raise SystemExit(main())


__all__ = [
    "Contract",
    "ContractResult",
    "Fixture",
    "FixtureRunResult",
    "ModelSwap",
    "discover_fixtures",
    "evaluate_contracts",
    "run_session",
    "write_report",
]
