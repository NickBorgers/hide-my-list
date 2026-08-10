# Bug 0621: review cycles run to completion on superseded SHAs

**Evidence:** #621

## Bug Story

`review-entry.yml` serializes `pull_request` runs through the
`review-entry-v2-<head_ref>` concurrency group with `cancel-in-progress: false`.
GitHub keeps at most one run pending per group, so a burst of pushes produces:

```
push P1 -> cycle running
push P2 -> pending
push P3 -> cancels pending P2, becomes pending behind P1
```

P1's cycle then runs to completion on a SHA that is two commits stale. Nothing
between the run being queued and the reviewers being dispatched re-asks whether
the SHA still matters.

Two costs, and the second is the one that surfaced first:

1. **Spend.** One PR's worth of content pays for two full reviewer fan-outs
   plus two fixer and judge stages.
2. **A verdict about code that no longer exists.** The stale cycle's judge
   decides against content that HEAD has already replaced. On NO-GO it applies
   `needs-human-review`, so the PR reads as "human takeover required" while the
   cycle reviewing the actual head is still queued behind it — the operator sees
   a blocking label and an active pipeline at the same time and has no way to
   tell which SHA the label describes.

On the observed PR, three commits landed 35s apart. The first cycle reviewed
the oldest SHA — whose `Python Validation` check had failed — and finalized
NO-GO. The two later commits fixed exactly that failure.

## Fix

- `.github/workflows/review-entry.yml` — the `resolve` job re-reads the live
  head SHA from the pulls API and sets `stale=true` when `reviewed_sha` no
  longer matches. The dedup claim, the prior-GO check, and the `pipeline` job
  are all gated on it.

The check lives in the job rather than at dispatch time because the concurrency
wait elapses before any job starts; freshness is only meaningful once the run
has un-queued. The newest push always resolves to the live head, so it is never
the run dropped. The guard skips cycles whose SHA is already stale when
resolve runs; a push that lands after the freshness check passes can
still produce a cycle on the now-superseded SHA. Gated to
`pull_request` events: `/review` resolves its SHA from the API at run
time and is head by construction.

See `docs/agentic-pipeline-learnings.md` §1.19 for how this relates to the
§1.13 GO short-circuit and the §1.14 merge-from-main inherit path.

## Regression Tests

**Structural lint (unit):** tests live in
`tests/unit/test_review_entry_stale_sha_guard.py`:

- `test_guard_step_present` — the guard step exists in `review-entry.yml`.
- `test_guard_compares_against_live_pr_head` — the guard resolves the head SHA
  via the pulls API; comparing the event payload SHA against itself is a no-op,
  and the payload SHA is stale by construction in the case being caught.
- `test_guard_runs_before_dedup_claim` — a claim written for a stale SHA leaves
  a dangling `review/pipeline` pending status nothing cleans up.
- `test_dedup_claim_is_gated_on_guard` — an ungated claim dispatches the
  pipeline for a superseded SHA.
- `test_pipeline_job_is_gated_on_guard` — redundant job-level skip, matching the
  belt-and-braces pattern the cycle cap uses (§1.3).
- `test_guard_is_scoped_to_pull_request_events` — the guard must not fire on
  `/review`, which is the human override.

Verified by reverting the workflow change and confirming all six fail.
