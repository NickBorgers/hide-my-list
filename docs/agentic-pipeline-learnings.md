# Agentic Pipeline Learnings

Forked from [home-automation](https://github.com/NickBorgers/home-automation), absorbed ~100 PRs of hard lessons. **Prescriptive**: each rule = current pipeline contract or guardrail future changes must preserve. Includes why + failure mode. PRs cited.

CI/review-pipeline guidance only. Not runtime spec. Canonical behavioral spec sources: `docs/ai-prompts/`, `docs/architecture.md`, `docs/task-lifecycle.md`.

Two meta-lessons:

- **Silent failures = expensive.** Most fixes address things that ran but did wrong thing: dropped env vars, missing mounts, agents posting no comments, cron drifting silently. Add loud assertions + validation gates.
- **Pipeline = distributed system with humans in loop.** Loops, label state, re-trigger semantics must be explicit + capped. Default: don't re-run unless something explicitly asks.

---

## 1. Review Pipeline Architecture

### 1.1 Parallel review jobs are read-only; only dedicated single-writer stages push
**Why:** Parallel reviewers with write access pushed fixes that retriggered pipeline and posted near-duplicate comments. Fan-out review jobs stay read-only; branch mutations limited to dedicated single-writer stages — currently `review-fixer.yml` only; judge/finalize stays read-only.
**Before:** Every reviewer round produced 3× redundant comments.
**Evidence:** #70, #71, #274

### 1.2 Review progress state must live in durable GitHub state, not comments or workflow inputs
**Why:** Comment-pagination counters broke past page 1; `workflow_dispatch` inputs reset to 0 on manual reruns. Execution state lives on SHA-scoped commit statuses (`review/pipeline`, `review/*`, `review/cycle`) — keyed to immutable commits. Comments + workflow inputs = observability only, not authority.
**Before:** Cycle counters silently reset, allowing infinite loops.
**Evidence:** #182, #234, #303, #320

### 1.3 Hard-cap review cycles in shell, with redundant job-level skips
**Why:** Shell script in `trigger-follow-up` independently counts cycle labels and refuses dispatch at cap; job-level `if:` conditions catch any escape. Defense in depth.
**Before:** Single PR looped 4+ times as agents contradicted each other's prior fixes.
**Evidence:** #303, #315, #301

### 1.4 Merge-decision verdict shape: binary GO / NO-GO with category metadata
**Why:** Two states — GO / NO-GO. Cost-control is structural: fixer runs after reviewers + before judge; judge has `permissions: contents: read` and cannot push; fixer publishes new commit first then writes `review/pipeline` status immediately (GitHub rejects status for unpublished SHA). Re-review loops on finalized GO SHAs prevented by GO-only `All Required Agent Reviews = success` short-circuit at entry. NO-GO = human escalation: labels PR `needs-human-review`, does **not** close PR or auto-create replacement issue (avoids lessons-learned-issue → new PR → NO-GO infinite loop).

Finalize contract (`review-finalize.yml` via `.github/scripts/review/render-finalize-comment.sh`): one per-cycle "Agent Review Summary" comment posted on every run. Comment shape branches on `category` metadata field — `aggregate.mjs` emits `go`, `reviewer_blockers`, or `pipeline_error`; `review-pipeline.yml` sets `cycle_capped` (cap-exhausted job) or `inherited` (merge-from-main no-content-delta job, see §1.14):
- `go` — 🟢 GO header, five-row per-reviewer table with status emoji + hyperlink, no Next-step block.
- `reviewer_blockers` — 🔴 NO-GO header, five-row table, Unaddressed blockers section with hyperlinked IDs, reviewer-blocker next-step guidance.
- `pipeline_error` — ⚠️ NO-GO header, five-row table (offending row shows `⚠️ wrong sha (...)`), pipeline-bug next-step guidance.
- `cycle_capped` — 🛑 NO-GO header, cycle history fetched from commit statuses, no per-reviewer table.
- `inherited` — 🟢/🔴 GO/NO-GO (inherited) header, synthesized reason text only, no per-reviewer table or next-step block.

Per-row state is post-fixer: each row cross-references the reviewer's `blocking_issues[]` IDs against `verdict.unaddressed_blocker_ids[]` (namespaced `<role>/<id>`) so a `request_changes` decision whose blockers were all addressed by the fixer renders as 🟢 fixed `N/N fixed`, not 🔴 changes. Partial fixes render as 🔴 changes `M/N open` with M = still-open count.

`category` is messaging metadata only — not a third verdict state. Verdict remains strictly binary.
**Historical context:** A previous three-state pipeline (GO-CLEAN / GO-WITH-RESERVATIONS / NO-GO) tried to collapse re-review loops at the verdict layer; the current pipeline solves the same cost problem at the orchestration layer instead, which is why two states suffice.
**Evidence:** #315, #320, #322, #274, #336, #341, #342, #343

### 1.5 Inline review comments are blocking inputs — reviewers fold them in
**Why:** Judge (`.github/scripts/review/aggregate.mjs`) = deterministic Node script, no Codex, no git credentials, no PR API access — consumes ONLY structured reviewer JSON artifacts conforming to `schema/reviewer-v1.json`. To preserve blocking property: each reviewer ingests inline PR comments via `gh api repos/.../pulls/{n}/comments` inside its own prompt and folds blocking change requests into `blocking_issues[]` with `source: "inline_comment"`. Schema's `source` enum encodes this contract.
**Before:** A predecessor pipeline auto-approved PRs despite outstanding inline change requests because its merge-decision agent read all comments and applied judgment instead of using a resolution-state contract; lesson preserved here so future judges keep the reviewer-side fold-in invariant.
**Evidence:** #143, #341, #342

### 1.6 Manual re-trigger is a `/review` comment, not close/reopen
**Why:** Close/reopen fired both `pull_request` trigger and `workflow_run` trigger; concurrency group cancelled one at random.
**Before:** Manual re-reviews silently no-op'd.
**Evidence:** #244, #234

### 1.7 Track immutable `reviewed_sha`, separate from branch ref
**Why:** Reviewers + fixers must check out immutable `reviewed_sha`, not mutable branch ref — branch may be deleted or advanced while pipeline works. Any branch-writing stage must re-read `origin/<head_ref>` immediately before push and refuse if tip no longer equals frozen `reviewed_sha`; otherwise silently overwrites newer author commits with changes prepared against stale code.
**Before:** Post-merge follow-ups failed with "could not fetch ref".
**Evidence:** #308, #351, #353, #354

**Manual regression playbook:** Open PR, let `review-entry.yml` freeze `reviewed_sha`, push another commit before `review-fixer.yml` reaches push step. Fixer should log that `origin/<head_ref>` moved, rewrite `fixer-result.json` so `new_sha == input_sha == reviewed_sha`, add `skipped[]` reason, exit without pushing. Judge/finalize should evaluate unchanged reviewed tree instead of overwriting newer commit.

### 1.8 Dedupe workflow-failure issues by fingerprint
**Why:** Helper script fingerprints failures by (workflow, branch, commit) and reuses existing open issue.
**Before:** Every retry spawned a new failure issue, drowning issue tracker.
**Evidence:** #269

### 1.9 Guardrail: spec-critical `.md` files must stay on full review path
**Why:** Files like `setup/cron/reminder-check.md`, `TOOLS.md`, `SOUL.md` are *executable* — they define agent behavior. `docs_only=true` classifier = implementation shortcut, not semantic proof that matching Markdown is inert. Workflow correctly carves out known spec-critical Markdown like `design/adhd-priorities.md`, but future prompt-bearing docs under broad content trees like `design/` or `.github/` may be added without updating carve-outs. Treat as allowlist problem, not assume any Markdown-heavy directory is safe to bypass.
**Before:** Behavioral cron prompt slipped through with zero security review.
**Evidence:** #156, #142

### 1.10 Clear stale `*-started` labels before each new cycle
**Why:** Snapshot + clear `*-started` labels at cycle boundaries; only restore if new cycle label commits successfully.
**Before:** On cycle 2, humans couldn't tell which review stage was actually running.
**Evidence:** #320

### 1.11 Review-skip approvals must be SHA-bound, not PR-bound
**Why:** PR-level marker like `agent-reviews-passed` can outlive the diff it described. Safe auto-skip rule is always SHA-bound: orchestrator must see reviewed SHA already claimed on `review/pipeline`, with corresponding `review/*` and `review/cycle` statuses describing that exact commit chain, and any branch mutation still flows only through `review-fixer.yml`. PR labels communicate history to humans but must not gate execution for later commits.
**Before:** New head commits inherited green aggregate review check without any stage evaluating updated diff.
**Evidence:** #339, #338, #337

### 1.12 Security/infra review explicitly owns reviewer-routing regressions
**Why:** Review-pipeline dispatch + classifier changes can silently narrow who reviews future PRs while workflow still "works." When PR touches classifier, dispatch, or gating logic, Security & Infrastructure reviewer must compare proposed routing against current pipeline behavior and flag unintended loss of specialist coverage. Regressions that drop coverage for prompt/spec files, including `.github/scripts/review/prompts/*.md`, are blocking unless explicitly documented. v2: review-orchestration files must force security review even when PR is otherwise workflow-only or config-only.
**Before:** Reviewer prompt markdown under `.github/scripts/review/prompts/*.md` classified as config-only, which would have skipped security, psych, and prompt review — no reviewer called it out.
**Evidence:** #343, #349

### 1.13 Stop on GO, keep retrying on NO-GO: asymmetric cycle control
**Why:** v2 fixer must push before claiming `review/pipeline` — GitHub's commit-status API rejects unpublished SHAs. This leaves a synchronize-event race: `review-entry-v2-<head_ref>` concurrency group serializes the event behind the still-running cycle, and by the time queued entry runs, the post-fix SHA may already have moved from `review/pipeline = pending` to a terminal status. Automatic PR events for a fixer-produced SHA must not consume a new cycle; the SHA was already produced, claimed, and finalized by the preceding cycle. Without this skip, the fixer's own push can burn the retry budget one author-push early. On drift-sensitive PRs (anything touching spec-critical Markdown), redundant cycles can legitimately find fresh cross-doc drift introduced by the prior fixer — see PR NickBorgersProbably/hide-my-list#397, which burned 3 full cycles with monotonically diminishing blocker counts (6 → 3 → 1).

Fix is split by verdict:

- **GO cycle is terminal.** `review-entry.yml` refuses dispatch when reviewed SHA already carries `All Required Agent Reviews = success` commit status (written by `review-finalize.yml:167-186` on GO only). GO means pipeline is done with this SHA; fresh cycle would only rediscover drift from prior fixer. `/review` comments bypass check (`issue_comment` events are event-gated out) so humans can force re-review manually.
- **Fixer-owned SHA re-entry is skipped.** `review-entry.yml` refuses automatic `pull_request` dispatch when reviewed SHA has both a `review/pipeline` status whose description contains `fixer post-push claim` and a finalized `review/verdict` status. This catches both cases: pending-only dedup handles the event while the fixer claim is active, and the status-history guard handles the event after the judge writes `review/verdict`. Requiring `review/verdict` keeps a pipeline failure before the judge verdict retryable; failures after the verdict are treated as already-reviewed for cycle-accounting purposes, while `/review` comments remain a force override for the exact SHA.
- **NO-GO descendant cycles retry.** `All Required Agent Reviews` status on NO-GO is `failure` — doesn't match GO-only short-circuit, so a later author/fixer commit on top of the NO-GO SHA can continue into a fresh cycle. Deliberate: NO-GO means "fixer could not address all blockers this round." Retry is bounded by `MAX` in `review-pipeline.yml` (currently `2`, i.e. up to one NO-GO retry). `cap-exhausted` on NO-GO correctly finalizes as NO-GO with `needs-human-review`; cycle 2 GO cleanly replaces cycle 1 NO-GO label.

Why **not** just lower `MAX` to `1`: `cap-exhausted` hard-codes `verdict='NO-GO'` (the `cap-exhausted` job in `review-pipeline.yml`, currently lines 239–250). With `MAX=1`, every cycle-1 GO would be followed by synchronize event firing cycle 2, which caps immediately and stamps NO-GO on current HEAD — clobbering cycle 1 GO. Safely lowering `MAX` to 1 would require rewriting `cap-exhausted` to inherit parent SHA's verdict, which duplicates what GO short-circuit already does more cheaply at entry layer. `MAX=1` would also remove NO-GO retry capability.
**Before:** PR #397 posted three full Merge Decisions and ~15 reviewer comments for single docs-only PR already GO at cycle 1. PR #648 let the fixer's own post-fix SHA consume a retry slot, then capped the author's next real fix without review.
**Evidence:** #397, #649

### 1.14 A cycle represents new PR content, not branch-HEAD churn
**Why:** Cycle counter walks first-parents in `review-state` `read-cycle` (`.github/actions/review-state/action.yml`) and increments on each new HEAD SHA. But a `git merge main` into a PR branch creates a new HEAD that adds zero new PR-side content — it only absorbs main. Counting that as a fresh cycle pushes a PR with `cycle=2 GO` over `MAX=2` and into `cap-exhausted`, which invalidates the prior verdict. See §1.14 invariant: a cycle is consumed only when there is new PR content to review.

Detection lives in `review-pipeline.yml` `gather` job: HEAD has 2+ parents AND `HEAD^2` is reachable from `origin/main` ⇒ inherit. The `inherit` job re-stamps the prior verdict on the new HEAD via `review-finalize.yml` with `skip_verdict_artifact: true` and a `synthesized_reason_text` indicating the inherit. Reviewers/fixer/judge are skipped via additional `inherited != 'true'` guards alongside the existing `capped != 'true'` guards.

This is **not** the same problem as §1.13's GO short-circuit. §1.13 prevents redundant pipeline runs after a GO; §1.14 handles the case where main has moved and the PR resyncs but does not add content — `review-entry.yml`'s GO short-circuit can't fire because the new HEAD has no `All Required Agent Reviews = success` of its own yet. Both rules together: a PR that has converged stays converged across both no-op pushes (§1.13) and clean main resyncs (§1.14).
**Evidence:** PR #477

### 1.15 Terminal NO-GO paths must finalize labels and the merge-gate status without depending on judge artifacts
**Why:** `review-finalize.yml`'s `Download verdict artifact` step (`name: verdict-${sha}`) only finds an artifact when the judge has run. Cap-exhausted (§1.14) and inherit-on-merge-from-main bypass the judge entirely, so the artifact does not exist on those paths; an unconditional download fails the job before the label/status work runs. Result before fix: PR ends with `agent-reviews-passed` label still set (cycle 2 GO) AND no `All Required Agent Reviews` status on HEAD — branch protection blocks but the PR's labels look green, exactly the inconsistent state surfaced by PR #477.

Contract: any pipeline-exit path that calls `review-finalize.yml` without a `verdict-${sha}` artifact must pass `skip_verdict_artifact: true` so finalize can synthesize REASONS inline. The two paths render differently: the `inherited` path passes `synthesized_reason_text` with an inherit message, which `render-finalize-comment.sh` renders as the comment body; the `cycle_capped` path does not pass `synthesized_reason_text` — `render-finalize-comment.sh` renders cycle history and a next-steps block instead. Both cap-exhausted and inherit jobs in `review-pipeline.yml` follow this contract.

Single-writer rule from §1.1 still holds: label transitions and the `All Required Agent Reviews` status are still written exclusively from `review-finalize.yml`. `skip_verdict_artifact` is a parameterization, not a duplicate writer.
**Evidence:** PR #477

### 1.16 Human commits on top of a converged GO reset the cycle counter
**Why:** `MAX` in `review-pipeline.yml` bounds the number of cycles the **fixer** gets on a PR whose cycle 1 did not converge (§1.13). It is sized for fixer-retry stagnation, not for contributor follow-up. But `read-cycle` walks first-parents and increments on each new HEAD regardless of authorship, so a contributor pushing a small fix on top of a `cycle=2 GO` arrives at `next=3 > MAX=2` and gets a `cycle_capped` terminal NO-GO without any reviewer ever running on the new content. The `review-entry.yml` GO short-circuit (§1.13) does not save them: it only matches the **exact** GO SHA, not a descendant.

Detection lives in `review-pipeline.yml` `gather` job: read the git author email of HEAD via `gh api`; if it is not the fixer (`ci@hide-my-list.local`, set in `review-fixer.yml`) AND `read-cycle`'s `cycle_sha` carries `review/verdict = GO`, set `reset=true`. The `Compute next cycle` step then stamps `cycle=1` instead of `prior+1`, giving the new commit a fresh budget of cycle 1 + one fixer retry. Reviewers, fixer, and judge all run normally — only the counter resets. The `cap-exhausted` NO-GO scenario is preserved because its `cycle_sha` carries `review/verdict = NO-GO`, so the reset never fires after a cap-exhausted state.

This is **not** the same problem as §1.14's merge-from-main inherit. §1.14 covers commits with **zero PR-side content delta** and skips the pipeline entirely. §1.16 covers commits with **real new content** that should be reviewed; the only thing it skips is the cycle bill against the fixer-retry budget. Together: §1.13 short-circuits the exact GO SHA, §1.14 inherits clean main resyncs, §1.16 lets contributors edit a converged PR without bricking it.
**Before:** PR #492 — local push on top of cycle 2 GO immediately tripped `cycle_capped`, requiring an admin-merge or force-push to recover.
**Evidence:** PR #492

### 1.17 Author stays alive across review cycles via session resume
**Why:** Pre-redesign, `resolve-issue` ran a one-shot Codex that opened the PR and exited. The v2 fixer was a fresh Claude session that only saw reviewer JSON — it could patch what was literally flagged but had no memory of the structural choices the author made. Reviewer feedback on those choices reached the wrong agent: a stand-in fixer with no context for *why* the original author wrote it the way it did.

Fix is to persist the author's session state to a job-local directory during the author run, pack and upload it as the `author-session-<agent>-<run-id>` workflow artifact, write an `Author-Session: <agent>/<run-id>` trailer into the PR body, and have `review-fixer.yml` parse the trailer + download the artifact from the original run + unpack it back into the fixer container — then `codex exec resume --last` (or `claude --continue`) drops the original author back into the same conversation, this time with the reviewer artifacts in scope.

Required properties:

- **Symmetric two-agent support.** `resolve-issue` must accept Codex *and* Claude Code as first-class authors (selected per run via `/autoresolve <agent>` comment, `agent:codex` / `agent:claude` issue label, or repo default). The fixer must dispatch to a matching resume action (`review-codex-resume` / `review-claude-resume`) per the trailer.
- **Backstop reviewers always run.** Specialty reviewers (psych/security/docs/prompt) bring distinct domain expertise the generic resume model doesn't replicate. Reviewer + judge stages run regardless of dispatch mode, so a resumed-author PR receives the same scrutiny as a human-authored PR. Cost is bounded; regression cost from skipping isn't.
- **Per-run artifact keys avoid `--last` poisoning.** Artifact name is `author-session-<agent>-<run-id>`, scoped to the original `resolve-issue` run. A prior `/autoresolve` attempt's artifact lives under a different run-id, so the fixer's download-artifact lookup against the trailer's run-id can't pick it up.
- **Defensive fallback.** Trailer absent, malformed, artifact missing/expired, or unpack fails validation → fixer falls back to fresh Claude (today's behavior). Human-authored PRs and PRs from before the redesign keep working unchanged.
- **Artifact transit, not host bind-mount.** Homelab self-hosted runners are ephemeral — author and fixer run on different runner instances, so cross-run state cannot live on the host filesystem. The session ships through the GitHub Actions artifact store (~7-day retention). Slightly slower than a host bind-mount, but the only design that's runner-portable.
- **Cleanup is automatic.** Artifacts auto-expire at the configured retention; no host-side prune script needed. The review path never deletes during a cycle (resume must always find what's there until retention drops it).
- **Codex bind-mount is scoped to `~/.codex/sessions`, not `~/.codex`.** Codex CLI 0.125+ installs its standalone runtime under `~/.codex/packages/standalone/`, with the wrapper at `~/.local/bin/codex` symlinking into that tree. Bind-mounting an empty session dir on top of `~/.codex` breaks the symlink target → `command -v codex` fails inside the container. Mount only the `sessions/` subdir, which is what actually needs to persist between author and fixer runs. Claude Code is not affected (binary at `~/.local/bin/claude`, separate from `~/.claude`), so its bind-mount target stays the full `~/.claude` dir.
- **Fixer also resolves merge conflicts.** Almost every PR is AI-authored — no human in the loop to rebase/merge when main moves. Each fixer cycle attempts `git merge --no-commit --no-ff origin/main` on the host before invoking the agent. Three outcomes: `none` (already up to date) → no-op; `clean` (merge applied, no conflicts) → host commits the merge immediately as a single-parent merge commit, which the next cycle's "Inherit prior verdict (merge-from-main)" path short-circuits; `conflicts` → host leaves markers in the working tree, exposes `MERGE_STATE=conflicts` + `MERGE_CONFLICT_FILES` to the agent container, agent resolves markers as part of its run, host seals the merge along with reviewer-blocker fixes in a single commit. If markers remain after the agent exits, host runs `git merge --abort`, labels the PR `needs-human-review`, and posts a comment listing unresolved files. Merge happens in the fixer stage (not before reviewers) to avoid SHA churn that would break dedup keying on `reviewed_sha`.

`scripts/ci-session-store.sh` is the single source of path conventions and trailer parsing — workflows call its subcommands rather than recomputing paths inline. `scripts/test-ci-session-store.sh` covers the helper logic and is run by `review-fixer-resume-smoke.yml` on PRs that touch the resume code paths. Full cross-container resume round-trip is empirically validated by the first multi-cycle review on a `resolve-issue` PR — that requires real LiteLLM calls, too expensive for every PR's smoke.

### 1.18 Security reviewer is a two-lens orchestrator
**Why:** A single security prompt has to be both broad enough to catch generic web/agent vulns (injection, deserialization, weak crypto, JWT, IDOR, supply chain) and specific enough to enforce this repo's own invariants (constrained `httpx` tool surface, no-shell-out from `app/`, private-data placeholder rule, workflow `permissions:` hygiene, reviewer-routing regressions). Cramming both into one prompt produced shallow checks on either side and gave the LLM no built-in budget for confidence filtering, exclusion handling, or finding caps — large diffs trended toward naggy output.

**How:** The `security` role is dispatched as two sub-lenses:
- `security-breadth` (`.github/scripts/review/prompts/security-breadth.md`) — vendored verbatim (subject to MIT attribution) from [anthropics/claude-code-security-review](https://github.com/anthropics/claude-code-security-review), `claudecode/prompts.py` → `get_security_audit_prompt`. Pin in `.github/ci/vendored-prompts.env`; refreshed weekly by `update-ai-clis.yml` via `.github/scripts/vendor-security-prompt.py`. Carries Anthropic's confidence ladder (`<0.7` → don't report) and exclusion list (DoS, rate-limit, resource leaks, open-redirect, regex injection, memory safety in non-C/C++, SSRF in HTML, findings on `.md` files). The vendor script strips upstream's output-schema section before substituting into the BEGIN/END VENDORED PROMPT markers so the refresh can't reintroduce a conflicting `{findings:[]}` contract.
- `security-narrow` (`.github/scripts/review/prompts/security-narrow.md`) — repo-specific invariants only. Explicit instruction not to duplicate breadth's catalog so the merger sees minimal overlap.

Both lenses run as ordinary reviewers via `review-reviewer.yml` with no special arguments — they upload as `reviewer-security-{breadth,narrow}-<sha>`, publish their own PR Review comments, and write their own `review/reviewer-security-{breadth,narrow}` commit statuses.

**The judge collapses the lenses.** `judge.mjs` runs from `main`'s checkout with read-only permissions; it imports `.github/scripts/review/security-merge.mjs` (also from `main`) and, before calling `aggregate()`, folds any `security-breadth` and `security-narrow` artifacts into a single synthesized `role=security` artifact, dropping the originals from the reviewers list. This closes the script-trust attack class: there is no PR-controlled deterministic code running on the runner in the verdict path — the merger source is pinned by virtue of living in `main`. The judge job also writes the synthesized artifact out to `${runner.temp}/security-result.json` and re-uploads it as `reviewer-security-<sha>` so `render-finalize-comment.sh` (which iterates `design security docs prompt psych test`) finds a single security row to render.

`security-merge.mjs` is pure logic:
1. Drops findings matching Anthropic's exclusion list (port of `claudecode/findings_filter.py`) as a backstop.
2. Demotes blocking issues with paired `fix_suggestion[].confidence < 0.7` to `non_blocking_notes[]`.
3. Dedupes by `(normalized_file, floor(line/5))`. Category is intentionally **not** in the key because breadth (catalog categories) and narrow (repo-categories) use disjoint vocabularies; cross-category collisions in the same 5-line bucket collapse with narrow winning.
4. Caps at 5 blocking + 5 non-blocking per cycle, with severity-then-confidence sort. Overflow surfaces in `summary_metadata.truncated_*_count`.
5. Always emits `role: "security"` so the judge's `aggregate()` contract is unchanged.

`reviewer-v1.json` adds an optional `summary_metadata` block (counts of dropped / demoted / truncated findings, plus `merged_from: ["security-breadth", "security-narrow"]`) for diagnostics. The block is opt-in and ignored by other reviewer roles. Roles `security-breadth` and `security-narrow` are added to the schema enum; the existing bootstrap shim in `review-reviewer.yml` accepts them on the PR that introduces them.

**The fixer sees lens names the judge doesn't.** The fixer reads the raw `reviewer-security-{breadth,narrow}-<sha>` artifacts, whose `role` fields are the lens names, so left to itself it namespaces security blockers as `security-breadth/<id>`. The judge, by contrast, only ever sees the merged `role=security` artifact. Two things keep that asymmetry from throwing away a whole review cycle: `fix-result-v1.json` accepts the lens namespaces (a rejected fix-result fails the fixer job *before* the commit/push step, so the fixes are done but never land), and `aggregate()` matches a lens-prefixed id under `security/` as well as verbatim. The fixer prompts still instruct `security/<id>` as the canonical form — the schema and judge are the backstop, not the contract.

**Bootstrap caveat for the PR that introduces this design:** on the very PR that adds `security-merge.mjs` and the lens collapse to `judge.mjs`, `main`'s judge.mjs is still the pre-collapse version. During that PR's own review the lens artifacts flow into `aggregate()` as two separate reviewers (`security-breadth` and `security-narrow`); the synthesized `reviewer-security-<sha>` artifact is not produced and the agent-table row for `security` shows `_did not run_`. Lens findings still surface in the unaddressed-blockers list under namespaced ids like `security-breadth/secb-001`. Subsequent pipeline runs read the merged-in version of `judge.mjs` and operate normally.

### 1.19 A queued cycle must re-check that its SHA is still PR head before spending reviewers
**Why:** `review-entry.yml`'s concurrency group (`review-entry-v2-<head_ref>`, `cancel-in-progress: false`) serializes `pull_request` runs rather than cancelling them, and GitHub keeps at most one run pending per group. A burst of rapid pushes therefore produces: cycle for P1 running, P2 pending, P3 arrives and cancels pending P2, P3 pending behind P1. P1 runs to completion on a SHA that is two commits stale, then P3 runs. That is two full reviewer fan-outs plus two fixer and judge stages for one PR's worth of content, and the first verdict describes code that no longer exists at HEAD — a NO-GO there applies `needs-human-review` to a PR whose actual head may be fine.

Fix: `resolve` re-reads the live head SHA from the pulls API and skips the cycle when `reviewed_sha` no longer matches. The check belongs in the job rather than at dispatch, because the concurrency wait elapses before any job starts — freshness is only a meaningful question once the run has un-queued. The newest push always resolves to the live head, so it is never the run dropped. The guard skips cycles whose SHA is already stale when resolve runs; a push that lands after the freshness check passes can still produce a cycle on the now-superseded SHA. Gated to `pull_request` events: `/review` resolves its SHA from the API at run time and is head by construction.

This is a third distinct member of the §1.13 / §1.14 family. §1.13 stops re-review of a SHA already cleared GO; §1.14 stops a clean main resync from consuming a cycle; §1.19 stops a cycle whose subject was superseded while it waited in the queue. All three answer "should this cycle run at all," at different points in the SHA's life.

Structural guard: `tests/unit/test_review_entry_stale_sha_guard.py`.
**Before:** PR #621 pushed three commits 35s apart. The cycle for the first SHA spent seven reviewers, a fixer, and a judge, finalized NO-GO, and labeled the PR `needs-human-review` — while the cycle for the real head was still queued behind it.
**Evidence:** #621

---

## 2. CI Runtime Infrastructure

### 2.1 Pre-create bind-mount source directories before workflows that mount them
**Why:** Docker silently fails when bind-mount source path doesn't exist. Any workflow mounting host config dirs into devcontainer should `mkdir -p` those paths first. Current Codex review pipeline does this for `~/.config/gh`, `~/.claude`, `~/.codex`; future workflows adding same mounts need same guard.
**Evidence:** #77, #78

### 2.2 Pass only the active tool's minimal auth env into CI containers
**Why:** Stable pattern = run review jobs through baked container config, pass only the runtime env the selected CLI actually reads. Current split: Codex reviewer jobs forward `OPENAI_API_KEY=fake-key` and `GH_TOKEN=${WORKFLOW_PAT}`; Claude fixer jobs forward `LLM_PROXY_API_KEY=fake-key`, `LLM_PROXY_BASE_URL=https://llm.featherback-mermaid.ts.net/anthropic/`, and `GH_TOKEN=${WORKFLOW_PAT}`. Neither path should depend on OAuth/keychain passthrough.
**Before:** Earlier iterations tried to forward unused or mismatched LLM auth env vars directly, failed in confusing ways when container runtime forwarding differed from expectations.
**Evidence:** #90, #100, #475

### 2.3 Use `WORKFLOW_PAT` as `GH_TOKEN` for `gh` inside devcontainers
**Why:** `github.token` doesn't authenticate `gh pr comment` from inside `devcontainers/ci@v0.3` container on self-hosted runners.
**Before:** Reviewers ran successfully and posted zero comments. Perfect silent failure.
**Evidence:** #91 (applied across nine occurrences)

### 2.4 External fork PRs do not run Codex review; any devcontainer build must use the `main` ref
**Why:** Untrusted PR code on homelab runners with credential access = privilege escalation. Current workflow blocks Codex review for non-collaborator PR authors; devcontainer build path checks out `main` rather than PR ref to avoid Dockerfile injection.
**Evidence:** #110

### 2.5 Treat BuildKit default attestations as an explicit compatibility choice
**Why:** BuildKit's default attestations can produce OCI manifest lists without valid platform metadata, breaking `docker push`. If current image-push path needs `BUILDX_NO_DEFAULT_ATTESTATIONS=1`, set it explicitly in workflow code rather than docs only.
**Evidence:** #133

### 2.6 Run steps go through purpose-built composites against the right base image
**Why:** `devcontainers/ci@v0.3` hit post-step cleanup crash on self-hosted runners. Two stable patterns:
- **Non-review workflows** — build/push with `devcontainers/ci@v0.3`, then run via `.github/actions/run-devcontainer/action.yml` (uses `@devcontainers/cli up/exec` directly).
- **Review pipeline v2** — direct-`docker run` against the dedicated CI image (`.github/ci/Dockerfile`) via four composites: `.github/actions/review-codex-run` (read-only reviewers), `.github/actions/review-claude-run` (fresh-Claude fixer fallback for human PRs and missing-trailer cases), `.github/actions/review-codex-resume` (resumed Codex author session, `codex exec resume --last`), and `.github/actions/review-claude-resume` (resumed Claude Code author session, `claude --continue`). The CI image is purpose-built for agent jobs and avoids the devcontainer's shared-socket fragility.
**Evidence:** #179, #475, #494

### 2.7 Bake the Codex CLI into the Dockerfile; route models via LiteLLM with explicit `CODEX_MODEL`
**Why:** Runtime CLI downloads stalled on slow networks; missing model defaults caused fallback to mismatched models. Downgrading three of six review stages to `gpt-5-mini` cut review cost ~45% with no measurable quality loss.
**Evidence:** #110, #130, #145, #292

### 2.8 Keep local-only SSH agent mounts out of CI
**Why:** CI runners have no SSH agent, so any `${localEnv:SSH_AUTH_SOCK}` bind mount becomes empty or invalid source. Current repo avoids this by not defining that mount in `.devcontainer/devcontainer.json`; if future devcontainer adds it back for local use, CI override generator must strip or override it explicitly.
**Before:** Seven iterative PRs across two days chasing variations of "invalid value for 'source'".
**Evidence:** #224, #226, #230, #248, #260, #270, #280

### 2.9 Validate workflows with `actionlint` + cross-file ref checks, and keep a devcontainer smoke test for container changes
**Why:** Yamllint passes broken `uses:` refs, missing script paths, and local-action checkout races. Current `pr-tests.yml` always runs canonical local/CI runner in `scripts/run-required-checks.sh`, which includes `actionlint` plus `validate-workflow-refs.sh` for workflow changes, plus devcontainer build smoke test when `.devcontainer/**` changes. If workflow changes start depending on new devcontainer behavior, extend smoke-test trigger instead of assuming yamllint is enough.
**Evidence:** #232, #207, #219

### 2.10 Scope env loading per script; fall back to `.env` for host `gh` auth
**Why:** Each runtime script imports only its own env vars via `scripts/load-env.sh`. Host-based maintenance scripts validate `gh` auth via `.env` fallback so cron jobs don't silently fail when user's interactive session isn't logged in.
**Evidence:** #271, #284

### 2.11 An aggregator job is not a gate until the ruleset names it
**Why:** A merge gate is two halves that live in different places: an aggregator job in a workflow (visible in every diff) and a `required_status_checks` context in the `All Required Checks` ruleset on `main` (visible nowhere in the repo). Building the first and forgetting the second yields a check that runs, reports, turns red, and blocks nothing — while reading as a guarantee to anyone scanning the PR's check list. `All Required Tests` and `E2E Conversations Required` were each written as gates, named as gates, and documented as gates in `DEV-AGENTS.md`, while the ruleset required only `All Required Agent Reviews` and `Python Validation Required`. The sharper failure is not red-and-merged but early-and-merged: the two wired contexts settle in under a minute while E2E takes ~4.5m on the self-hosted runner, so a PR satisfied the whole ruleset before E2E had reported anything. `tests/unit/test_required_checks_wired.py` pins the intended set and fails when a gate-shaped job is added or renamed without a decision. It is offline by construction — unit tests have no network and no admin token — so it cannot read the live ruleset; confirming that half stays manual, with the `gh api` command recorded in the test. When adding a gate, wire both halves.
**Evidence:** #664

---

## 3. OpenClaw Runtime, Cron, and Recovery

### 3.1 Durable cron jobs replace bash daemons
**Why:** Signal-based bash daemons died silently with no monitoring. OpenClaw's durable cron has built-in retry, observability, and ownership.
**Before:** Reminders unreliable; pipeline monitoring inconsistent.
**Evidence:** #112, #199, #263

### 3.2 Split cron existence repair from drift correction
**Why:** Main-agent startup checks and daily heartbeat ensure canonical recurring crons exist. Weekly janitor compares live cron jobs against canonical specs in `setup/cron/` and patches drift via `CronUpdate`. Cron spec re-application after `pull-main` happens in janitor — isolated `pull-main` session cannot reliably call `CronList`/`CronUpdate`.
**Before:** Cron jobs drifted from registered config, causing exponential backoff and silent failures.
**Evidence:** #238, #266, #268, #277

### 3.3 Dirty-pull recovery preserves work via `.pull-dirty` + GitHub issue
**Why:** When `git pull` fails on uncommitted changes or merge conflict, write structured `.pull-dirty` signal and open GitHub issue capturing changes *before* resetting.
**Before:** Local agent modifications silently lost on conflicts.
**Evidence:** #146

### 3.4 The "OpenClaw can't edit prompts" rule is OpenClaw-only
**Why:** AGENTS.md restricts runtime agent from editing repo-managed prompt/spec files. Does **not** apply to Claude Code, Codex, or human contributors — those go through normal PR flow.
**Before:** Claude Code refused legitimate infrastructure edits, citing AGENTS.md as if universal.
**Evidence:** #247

### 3.5 Prefer passing stable IDs through Actions; keep body handoff narrowly scoped
**Why:** Multi-line bodies in Actions outputs cause shell-escaping failures and context loss. Default pattern: pass only stable identifiers like `COMMENT_ID`, `ISSUE_NUMBER`, `RUN_ID`, then `gh api` full body inside devcontainer. Narrow exception: reviewer/fixer jobs may receive base64-encoded PR title/body for immediate author-intent evaluation without extra API round-trip. Keep that exception tightly scoped.
**Evidence:** #41, #178

### 3.6 Reopen-gate PRs until `workflow_run` reviews complete
**Why:** Closing + reopening previously bypassed review gate — `all-reviews-passed` check evaluated before reviews actually re-ran.
**Evidence:** #92, #99

### 3.7 Cron specs must declare the full isolated-session contract explicitly
**Why:** Missing or inconsistent registration fields caused accumulating errors and ambiguous routing. Canonical cron specs spell out `name`, `durable`, `schedule`, `prompt`, `sessionTarget`, `model`, `payload.kind`, `timeout-seconds`, and stay silent by omitting direct-delivery `to`.
**Evidence:** #199, #231, #268

### 3.8 MEMORY.md is for lessons; tasks belong in Notion
**Why:** Mixing to-dos into persistent memory caused drift between "what I should do" and "what I learned." Hard separation: Notion for actions, MEMORY.md for context.
**Evidence:** Reinforced across #112 and AGENTS.md itself.

---

## How to use this doc

Touching pipeline:
1. Find section matching your change.
2. If change *reverses* a rule, burden of proof is on you — re-read cited PRs first.
3. New failure mode discovered? Add numbered rule here in same prescriptive format.
