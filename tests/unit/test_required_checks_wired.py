"""Structural lint: every merge-gate aggregator is declared as a required check.

A gate in this repo is two separate things that have to agree:

  1. Something that publishes a status context — an aggregator job whose
     display name *is* the context (`All Required Tests`), or an explicit
     statuses-API write (`All Required Agent Reviews`, posted by
     `review-finalize.yml`). This half lives in the repo and shows up in review.
  2. A `required_status_checks` entry in the `All Required Checks` repository
     ruleset on `main`. This half is what actually blocks a merge, and it lives
     in GitHub settings where no diff ever shows it.

Building (1) and forgetting (2) produces a check that runs, reports, turns red,
and stops nothing. That happened twice: `All Required Tests` and
`E2E Conversations Required` were both written as gates, named as gates, and
documented as gates, while the ruleset required only two contexts. A PR merged
with a red E2E run — and could have merged before E2E reported at all, since
the two wired contexts finish minutes earlier than the self-hosted E2E job.

Scope, stated plainly: this test is offline. Unit tests get no network and no
admin token, so it cannot read the live ruleset and does not claim to. It pins
the intended set and catches the drift that actually bit — a gate-shaped job
appearing or being renamed without anyone deciding whether it gates. Confirming
the live ruleset still matches `REQUIRED_CONTEXTS` is a manual step, and the
command is in the comment below.

Standalone run: pytest tests/unit/test_required_checks_wired.py -v
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).parent.parent.parent
_WORKFLOWS = _REPO_ROOT / ".github" / "workflows"

# The contexts required by the `All Required Checks` ruleset on `main`.
# Verify against the live ruleset with:
#   gh api repos/NickBorgersProbably/hide-my-list/rules/branches/main \
#     --jq '.[] | select(.type=="required_status_checks")
#           | .parameters.required_status_checks[].context'
REQUIRED_CONTEXTS: frozenset[str] = frozenset({
    "All Required Agent Reviews",
    "All Required Tests",
    "E2E Conversations Required",
    "Python Validation Required",
})

# A job-level `name:`, as opposed to a step-level one. Steps are list items and
# always carry a leading `- `, so the absence of that dash is what separates a
# job display name from a step description. A job's display name is the status
# context GitHub publishes, which is why a job called "... Required" that
# nothing requires is a trap rather than a naming quibble.
_JOB_NAME = re.compile(r"^\s*name:\s*(?P<name>\S.*?)\s*$")

# `context: '<name>'` in a statuses-API call.
_STATUS_CONTEXT = re.compile(r"^\s*context:\s*['\"](?P<name>[^'\"]+)['\"]\s*,?\s*$")


def _workflow_files() -> list[Path]:
    if not _WORKFLOWS.exists():
        return []
    return sorted(p for p in _WORKFLOWS.iterdir() if p.suffix in (".yml", ".yaml"))


def _strip_quotes(value: str) -> str:
    return value.strip().strip("\"'")


def _job_names() -> dict[str, str]:
    """Map every job display name to the workflow file that declares it."""
    found: dict[str, str] = {}
    for path in _workflow_files():
        for line in path.read_text(encoding="utf-8").splitlines():
            match = _JOB_NAME.match(line)
            if match:
                found.setdefault(_strip_quotes(match.group("name")), path.name)
    return found


def _published_contexts() -> dict[str, str]:
    """Every status context this repo can publish, by either mechanism."""
    found = _job_names()
    for path in _workflow_files():
        for line in path.read_text(encoding="utf-8").splitlines():
            match = _STATUS_CONTEXT.match(line)
            if match:
                found.setdefault(_strip_quotes(match.group("name")), path.name)
    return found


def test_every_gate_shaped_job_is_a_required_context() -> None:
    """A job named like a gate must be one, or be renamed so it stops claiming to be.

    This is the direction that failed silently. `All Required Tests` and
    `E2E Conversations Required` both read as guarantees to anyone scanning a
    PR's check list, and neither was enforcing anything.
    """
    gates = {
        name: workflow
        for name, workflow in _job_names().items()
        if re.search(r"\bRequired\b", name)
    }
    assert gates, "expected to find at least one gate-shaped job name"

    unwired = {name: wf for name, wf in gates.items() if name not in REQUIRED_CONTEXTS}
    assert not unwired, (
        "These workflow jobs are named as merge gates but are not in "
        "REQUIRED_CONTEXTS: "
        + ", ".join(f"{name!r} ({wf})" for name, wf in sorted(unwired.items()))
        + ". A gate is only a gate once its context is in the `All Required "
        "Checks` ruleset on main. Either add it there and list it here, or "
        "rename the job so it does not advertise a guarantee it lacks."
    )


@pytest.mark.parametrize("context", sorted(REQUIRED_CONTEXTS))
def test_every_required_context_is_produced(context: str) -> None:
    """A required context nothing reports leaves every PR pending forever.

    Renaming an aggregator job is the easy way to cause this: the ruleset keeps
    waiting on a context that no longer exists, and no run will ever satisfy it.
    """
    published = _published_contexts()
    assert context in published, (
        f"Required context {context!r} is published by no job name and no "
        "statuses-API call under .github/workflows/. A required check that "
        "nothing reports leaves every PR pending indefinitely. Restore the "
        "producer, or drop the context from the ruleset and from "
        "REQUIRED_CONTEXTS together."
    )
