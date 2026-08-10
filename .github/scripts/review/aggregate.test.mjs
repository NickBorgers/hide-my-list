// Unit tests for the v2 review pipeline judge logic.
//
// Run with: node --test .github/scripts/review/aggregate.test.mjs

import { test } from "node:test";
import assert from "node:assert/strict";
import { aggregate } from "./aggregate.mjs";

const SHA_A = "a".repeat(40);
const SHA_B = "b".repeat(40);

/** Helper: build a minimal reviewer artifact. */
function reviewer(role, decision, blockers = [], { sha = SHA_A, cycle = 1 } = {}) {
  return {
    schema_version: "1",
    role,
    reviewed_sha: sha,
    cycle,
    decision,
    summary: "",
    blocking_issues: blockers,
    non_blocking_notes: [],
    fix_suggestions: [],
    followup_issues: [],
  };
}

function blocker(id, severity = "high") {
  return { id, severity, message: `issue ${id}` };
}

/** Default no-op fix-result targeting SHA_A. */
const noFix = { input_sha: SHA_A, new_sha: SHA_A, addressed: [], skipped: [] };

// ───────────────────────── happy paths ─────────────────────────

test("all approve, no blockers -> GO", () => {
  const v = aggregate(
    [reviewer("design", "approve"), reviewer("security", "approve")],
    noFix
  );
  assert.equal(v.verdict, "GO");
  assert.deepEqual(v.unaddressed_blocker_ids, []);
});

test("approve + comment, no blockers -> GO", () => {
  const v = aggregate(
    [reviewer("design", "approve"), reviewer("security", "comment")],
    noFix
  );
  assert.equal(v.verdict, "GO");
});

test("blocker fully addressed by namespaced id -> GO", () => {
  const v = aggregate(
    [reviewer("security", "request_changes", [blocker("sec-1")])],
    { input_sha: SHA_A, new_sha: SHA_B, addressed: ["security/sec-1"], skipped: [] }
  );
  assert.equal(v.verdict, "GO");
});

test("request_changes overridden when fixer addressed every blocker -> GO", () => {
  const v = aggregate(
    [reviewer("security", "request_changes", [blocker("sec-1")])],
    { input_sha: SHA_A, new_sha: SHA_B, addressed: ["security/sec-1"], skipped: [] }
  );
  assert.equal(v.verdict, "GO");
});

// ───────────────────────── NO-GO paths ─────────────────────────

test("empty reviewer set is NO-GO", () => {
  const v = aggregate([], noFix);
  assert.equal(v.verdict, "NO-GO");
  assert.match(v.reasons.join("\n"), /No reviewer artifacts/);
});

test("all abstain -> NO-GO (fail closed, no vacuous GO)", () => {
  const v = aggregate(
    [reviewer("design", "abstain"), reviewer("security", "abstain")],
    noFix
  );
  assert.equal(v.verdict, "NO-GO");
  assert.match(v.reasons.join("\n"), /abstained/i);
});

test("unaddressed critical blocker -> NO-GO", () => {
  const v = aggregate(
    [reviewer("security", "request_changes", [blocker("sec-1", "critical")])],
    noFix
  );
  assert.equal(v.verdict, "NO-GO");
  assert.deepEqual(v.unaddressed_blocker_ids, ["security/sec-1"]);
});

test("unaddressed medium blocker -> NO-GO", () => {
  const v = aggregate(
    [reviewer("design", "request_changes", [blocker("d-1", "medium")])],
    noFix
  );
  assert.equal(v.verdict, "NO-GO");
});

test("partially addressed blockers -> NO-GO with remaining ids", () => {
  const v = aggregate(
    [
      reviewer("security", "request_changes", [
        blocker("sec-1"),
        blocker("sec-2"),
      ]),
      reviewer("design", "request_changes", [blocker("d-1")]),
    ],
    {
      input_sha: SHA_A,
      new_sha: SHA_A,
      addressed: ["security/sec-1"],
      skipped: [{ id: "security/sec-2", reason: "manual" }],
    }
  );
  assert.equal(v.verdict, "NO-GO");
  assert.deepEqual(
    v.unaddressed_blocker_ids.sort(),
    ["design/d-1", "security/sec-2"]
  );
});

// ───────────── fail-closed: mixed-epoch & invalid inputs ─────────────

test("mixed reviewed_sha across reviewers -> NO-GO (refuses mixed epochs)", () => {
  const v = aggregate(
    [
      reviewer("design", "approve", [], { sha: SHA_A }),
      reviewer("security", "approve", [], { sha: SHA_B }),
    ],
    noFix
  );
  assert.equal(v.verdict, "NO-GO");
  assert.match(v.reasons.join("\n"), /multiple reviewed_sha/);
});

test("mixed cycle across reviewers -> NO-GO", () => {
  const v = aggregate(
    [
      reviewer("design", "approve", [], { cycle: 1 }),
      reviewer("security", "approve", [], { cycle: 2 }),
    ],
    noFix
  );
  assert.equal(v.verdict, "NO-GO");
  assert.match(v.reasons.join("\n"), /multiple cycle/);
});

test("fixResult.input_sha mismatched against reviewers -> NO-GO", () => {
  const v = aggregate(
    [reviewer("design", "approve")],
    { input_sha: SHA_B, new_sha: SHA_B, addressed: [], skipped: [] }
  );
  assert.equal(v.verdict, "NO-GO");
  assert.match(v.reasons.join("\n"), /input_sha.*does not match/);
});

test("missing fixResult -> NO-GO", () => {
  const v = aggregate([reviewer("design", "approve")], null);
  assert.equal(v.verdict, "NO-GO");
  assert.match(v.reasons.join("\n"), /Missing fix-result/);
});

// ────────────── regression: blocker id collisions across reviewers ──────────────

test("two reviewers emit same bare id; addressing one MUST NOT clear the other", () => {
  // Both reviewers use bare id "issue-1". Fixer addresses only security's
  // namespaced "security/issue-1". Design's "design/issue-1" must remain
  // unaddressed → NO-GO. (Pre-fix: this returned GO.)
  const v = aggregate(
    [
      reviewer("design", "request_changes", [blocker("issue-1")]),
      reviewer("security", "request_changes", [blocker("issue-1")]),
    ],
    {
      input_sha: SHA_A,
      new_sha: SHA_A,
      addressed: ["security/issue-1"],
      skipped: [],
    }
  );
  assert.equal(v.verdict, "NO-GO");
  assert.deepEqual(v.unaddressed_blocker_ids, ["design/issue-1"]);
});

test("addressing both colliding ids by namespace -> GO", () => {
  const v = aggregate(
    [
      reviewer("design", "request_changes", [blocker("issue-1")]),
      reviewer("security", "request_changes", [blocker("issue-1")]),
    ],
    {
      input_sha: SHA_A,
      new_sha: SHA_B,
      addressed: ["design/issue-1", "security/issue-1"],
      skipped: [],
    }
  );
  assert.equal(v.verdict, "GO");
});

test("bare id in addressed[] (not namespaced) does NOT clear blockers", () => {
  // Defends the namespacing contract: if Phase 1 orchestration regresses
  // and emits bare ids, the judge fails closed instead of silently
  // clearing the wrong blockers.
  const v = aggregate(
    [reviewer("security", "request_changes", [blocker("sec-1")])],
    { input_sha: SHA_A, new_sha: SHA_A, addressed: ["sec-1"], skipped: [] }
  );
  assert.equal(v.verdict, "NO-GO");
  assert.deepEqual(v.unaddressed_blocker_ids, ["security/sec-1"]);
});

test("security lens namespace clears the merged security blocker", () => {
  // The fixer reads the per-lens artifacts (role=security-breadth /
  // security-narrow) but the judge only sees the merged role=security
  // artifact. Lens-prefixed ids normalize so the blocker still clears.
  for (const lens of ["security-breadth", "security-narrow"]) {
    const v = aggregate(
      [reviewer("security", "request_changes", [blocker("secb-001")])],
      { input_sha: SHA_A, new_sha: SHA_B, addressed: [`${lens}/secb-001`], skipped: [] }
    );
    assert.equal(v.verdict, "GO", `${lens} should clear the merged blocker`);
  }
});

test("security lens namespace still clears a lens-role reviewer (pre-merge shape)", () => {
  const v = aggregate(
    [reviewer("security-breadth", "request_changes", [blocker("secb-001")])],
    { input_sha: SHA_A, new_sha: SHA_B, addressed: ["security-breadth/secb-001"], skipped: [] }
  );
  assert.equal(v.verdict, "GO");
});

test("security lens namespace does NOT cross-clear another role's id", () => {
  // Normalization rewrites only the namespace, so a lens-prefixed entry
  // still cannot reach a blocker owned by a different reviewer.
  const v = aggregate(
    [
      reviewer("security", "request_changes", [blocker("x-1")]),
      reviewer("design", "request_changes", [blocker("x-1")]),
    ],
    { input_sha: SHA_A, new_sha: SHA_B, addressed: ["security-narrow/x-1"], skipped: [] }
  );
  assert.equal(v.verdict, "NO-GO");
  assert.deepEqual(v.unaddressed_blocker_ids, ["design/x-1"]);
});

// ──────────────────── category discriminator ────────────────────

test("GO carries category=go", () => {
  const v = aggregate([reviewer("design", "approve")], noFix);
  assert.equal(v.verdict, "GO");
  assert.equal(v.category, "go");
});

test("unaddressed blockers carry category=reviewer_blockers", () => {
  const v = aggregate(
    [reviewer("security", "request_changes", [blocker("sec-1")])],
    noFix
  );
  assert.equal(v.verdict, "NO-GO");
  assert.equal(v.category, "reviewer_blockers");
});

test("mixed reviewed_sha carries category=pipeline_error", () => {
  const v = aggregate(
    [
      reviewer("design", "approve", [], { sha: SHA_A }),
      reviewer("security", "approve", [], { sha: SHA_B }),
    ],
    noFix
  );
  assert.equal(v.category, "pipeline_error");
});

test("mixed cycle carries category=pipeline_error", () => {
  const v = aggregate(
    [
      reviewer("design", "approve", [], { cycle: 1 }),
      reviewer("security", "approve", [], { cycle: 2 }),
    ],
    noFix
  );
  assert.equal(v.category, "pipeline_error");
});

test("missing fix-result carries category=pipeline_error", () => {
  const v = aggregate([reviewer("design", "approve")], null);
  assert.equal(v.category, "pipeline_error");
});

test("fix input_sha mismatch carries category=pipeline_error", () => {
  const v = aggregate(
    [reviewer("design", "approve")],
    { input_sha: SHA_B, new_sha: SHA_B, addressed: [], skipped: [] }
  );
  assert.equal(v.category, "pipeline_error");
});

test("empty reviewers carries category=pipeline_error", () => {
  const v = aggregate([], noFix);
  assert.equal(v.category, "pipeline_error");
});

test("all-abstain carries category=pipeline_error", () => {
  const v = aggregate(
    [reviewer("design", "abstain"), reviewer("security", "abstain")],
    noFix
  );
  assert.equal(v.category, "pipeline_error");
});
