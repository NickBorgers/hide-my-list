"""Structural lint: public function reachability via AST scan.

Scans app/tools/*.py, app/graph/nodes/*.py, app/scheduler/*.py,
app/ingress/*.py for top-level public functions (no leading '_')
and asserts each name is referenced at least once in any Python file
across the entire app/ tree (including the defining module's own
call sites, but excluding the definition itself).

A function with exactly 1 occurrence across all files (the definition)
has no callers anywhere — that is a dead-code wiring bug (bug class 6).
The most illustrative example: record_reward_feedback in rewards.py existed
with passing unit tests but was never invoked from signal_listener.py.

Conservative by design:
- Only top-level (module-level) functions are checked — nested functions
  and class methods are excluded.
- The _ENTRY_POINTS allowlist covers functions whose sole external caller
  is the entry-point mechanism (APScheduler, main.py), where a naive
  text count reaches 2 (def + assignment in SCHEDULED_JOBS) within the
  same file. Adding a function here means accepting it as a legitimate
  entry point; add a comment explaining why.
- The _KNOWN_DEAD allowlist documents pre-existing dead code that predates
  this test. Removing a name from _KNOWN_DEAD is the right refactor; adding
  new names to this list requires PR justification.
- False positives are worse than misses. If the heuristic flags a real
  reachable function, add it to _ENTRY_POINTS with a comment.

Bug class 6: dead-code wiring.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent.parent
_APP_ROOT = _REPO_ROOT / "app"

# Directories to scan for public function definitions.
_SCAN_DIRS = [
    _APP_ROOT / "tools",
    _APP_ROOT / "graph" / "nodes",
    _APP_ROOT / "scheduler",
    _APP_ROOT / "ingress",
]

# Functions registered as APScheduler callbacks within their own module,
# or used as module entry points, where the only reference is the
# registration line in the same file. These are NOT dead code — they're
# called by the scheduler or framework, not by other Python modules.
_ENTRY_POINTS: frozenset[str] = frozenset(
    [
        # app/scheduler/jobs.py — registered via SCHEDULED_JOBS[*].func
        "dispatch_due_reminders",  # func=dispatch_due_reminders in JobSpec
        "check_notion_health",  # func=check_notion_health in JobSpec
        "send_pending_ops_alerts",  # func=send_pending_ops_alerts in JobSpec
        "run_state_audit",  # func=run_state_audit in JobSpec
        "generate_weekly_recap",  # func=generate_weekly_recap in JobSpec
        "dispatch_check_ins",  # func=dispatch_check_ins in JobSpec
        # app/ingress/signal_listener.py — called by main.py via asyncio.run()
        "run",  # asyncio.run(run()) in main.py
    ]
)

# Pre-existing dead code that predates this test. These are real bugs —
# public functions with no callers — but they are NOT new regressions.
# The test will flag any NEW dead function as a failure. Cleaning up the
# functions below (removing them from both the source and this list) is
# encouraged; adding new entries requires PR justification.
_KNOWN_DEAD: frozenset[str] = frozenset(
    [
        # app/tools/reminders.py — public CRUD helpers defined but not called.
        # reminder_worker.py uses inline SQL instead; these helpers are dead.
        # Cleanup: either wire them into reminder_worker.py or remove them.
        "get_due",  # never called; worker uses inline SELECT
        "mark_delivered",  # never called; worker uses inline UPDATE
        "mark_failed",  # never called; worker uses inline UPDATE
        "mark_dead",  # never called; worker uses inline UPDATE
    ]
)


def _all_py_files() -> list[Path]:
    """All .py files under app/ (excluding __pycache__)."""
    return [f for f in _APP_ROOT.rglob("*.py") if "__pycache__" not in str(f)]


def _public_top_level_fns(filepath: Path) -> list[str]:
    """Return names of top-level public functions in a Python file."""
    tree = ast.parse(filepath.read_text())
    return [
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and not node.name.startswith("_")
    ]


def _count_occurrences(name: str, all_files: list[Path]) -> int:
    """Count word-boundary occurrences of `name` across all Python files."""
    pattern = re.compile(r"\b" + re.escape(name) + r"\b")
    total = 0
    for f in all_files:
        total += len(pattern.findall(f.read_text()))
    return total


def _scanned_files() -> list[Path]:
    return [
        f
        for scan_dir in _SCAN_DIRS
        if scan_dir.exists()
        for f in sorted(scan_dir.glob("*.py"))
        if f.name != "__init__.py" and "__pycache__" not in str(f)
    ]


def test_no_unreachable_public_functions() -> None:
    """Every public top-level function in scanned dirs must have at least one call site.

    A function with exactly 1 total occurrence across ALL Python files in app/
    (that occurrence being the `def` line itself) has no callers anywhere — it
    is dead code. Functions in _ENTRY_POINTS are exempted as legitimate
    framework entry points.
    """
    all_py = _all_py_files()
    scanned = _scanned_files()

    dead: list[str] = []

    for filepath in scanned:
        fns = _public_top_level_fns(filepath)
        for fn in fns:
            if fn in _ENTRY_POINTS:
                continue
            if fn in _KNOWN_DEAD:
                continue
            count = _count_occurrences(fn, all_py)
            # count == 1 means only the definition line — no callers.
            if count <= 1:
                dead.append(f"{filepath.relative_to(_REPO_ROOT)}:{fn} (occurrences={count})")

    assert not dead, (
        "The following public functions have no call sites anywhere in app/. "
        "Either add a caller, add an integration test asserting end-to-end "
        "reachability, or add to _ENTRY_POINTS if this is a legitimate entry "
        "point. Bug class 6 (dead-code wiring):\n"
        + "\n".join(f"  - {d}" for d in sorted(dead))
    )
