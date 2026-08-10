// Unit tests for the security reviewer merger.
//
// Run with: node --test .github/scripts/review/security-merge.test.mjs

import { test } from "node:test";
import assert from "node:assert/strict";
import { merge, dropReason } from "./security-merge.mjs";

const SHA = "a".repeat(40);
const CTX = { reviewedSha: SHA, cycle: 1 };

function lens(role, blockers = [], fixes = [], nonBlocking = []) {
  return {
    schema_version: "1",
    role,
    reviewed_sha: SHA,
    cycle: 1,
    decision: blockers.length ? "request_changes" : "approve",
    summary: "",
    blocking_issues: blockers,
    non_blocking_notes: nonBlocking,
    fix_suggestions: fixes,
    followup_issues: [],
  };
}

function block(id, { severity = "high", file = "app/x.py", line = 10, category = null, message = `issue ${id}` } = {}) {
  const b = { id, severity, message };
  if (file) b.file = file;
  if (line) b.line = line;
  if (category) b.category = category;
  return b;
}

function fix(id, confidence) {
  return { id, applicable: "manual", patch_hint: "do the thing", confidence };
}

// ───────────────────────── happy path ─────────────────────────

test("both lenses empty -> approve", () => {
  const m = merge(lens("security-breadth"), lens("security-narrow"), CTX);
  assert.equal(m.role, "security");
  assert.equal(m.decision, "approve");
  assert.equal(m.blocking_issues.length, 0);
});

test("only breadth blocker survives -> request_changes", () => {
  const m = merge(
    lens("security-breadth", [block("secb-1")], [fix("secb-1", 0.9)]),
    lens("security-narrow"),
    CTX
  );
  assert.equal(m.decision, "request_changes");
  assert.equal(m.blocking_issues.length, 1);
  assert.equal(m.blocking_issues[0].id, "secb-1");
});

test("only narrow blocker survives -> request_changes", () => {
  const m = merge(
    lens("security-breadth"),
    lens("security-narrow", [block("sec-1", { category: "tool_surface" })], [fix("sec-1", 0.95)]),
    CTX
  );
  assert.equal(m.decision, "request_changes");
  assert.equal(m.blocking_issues[0].id, "sec-1");
});

// ───────────────────────── dedup ─────────────────────────

test("dedup: same file/line/category -> narrow wins", () => {
  const m = merge(
    lens("security-breadth",
      [block("secb-1", { file: "app/tools/notion.py", line: 42, category: "input_validation", message: "generic SQLi" })],
      [fix("secb-1", 0.9)]),
    lens("security-narrow",
      [block("sec-1", { file: "app/tools/notion.py", line: 42, category: "input_validation", message: "parameterised query required" })],
      [fix("sec-1", 0.95)]),
    CTX
  );
  assert.equal(m.blocking_issues.length, 1);
  assert.equal(m.blocking_issues[0].id, "sec-1");
  assert.match(m.blocking_issues[0].message, /parameterised/);
});

test("dedup: narrow with no category inherits breadth's category", () => {
  const m = merge(
    lens("security-breadth",
      [block("secb-1", { file: "app/x.py", line: 5, category: "input_validation" })],
      [fix("secb-1", 0.9)]),
    lens("security-narrow",
      [block("sec-1", { file: "app/x.py", line: 5, category: null })],
      [fix("sec-1", 0.9)]),
    CTX
  );
  assert.equal(m.blocking_issues.length, 1);
  assert.equal(m.blocking_issues[0].id, "sec-1");
  // Inherits breadth's category onto the surviving (narrow) entry.
  assert.equal(m.blocking_issues[0].category, "input_validation");
});

test("dedup: near-line collision (within bucket of 5) collapses", () => {
  const m = merge(
    lens("security-breadth",
      [block("secb-1", { file: "app/x.py", line: 10, category: "auth" })],
      [fix("secb-1", 0.9)]),
    lens("security-narrow",
      [block("sec-1", { file: "app/x.py", line: 12, category: "auth" })],
      [fix("sec-1", 0.9)]),
    CTX
  );
  assert.equal(m.blocking_issues.length, 1);
});

// ───────────────────────── exclusion filter ─────────────────────────

test("exclusion: DoS finding dropped", () => {
  const m = merge(
    lens("security-breadth",
      [block("secb-1", { message: "Denial of service via unbounded loop" })],
      [fix("secb-1", 0.9)]),
    lens("security-narrow"),
    CTX
  );
  assert.equal(m.blocking_issues.length, 0);
  assert.equal(m.summary_metadata.dropped_count, 1);
});

test("exclusion: rate-limit finding dropped", () => {
  const m = merge(
    lens("security-breadth",
      [block("secb-1", { message: "Missing rate limit on endpoint" })],
      [fix("secb-1", 0.85)]),
    lens("security-narrow"),
    CTX
  );
  assert.equal(m.blocking_issues.length, 0);
});

test("exclusion: memory-safety in Python dropped", () => {
  const m = merge(
    lens("security-breadth",
      [block("secb-1", { file: "app/x.py", message: "Buffer overflow risk" })],
      [fix("secb-1", 0.9)]),
    lens("security-narrow"),
    CTX
  );
  assert.equal(m.blocking_issues.length, 0);
});

test("exclusion: memory-safety in C kept", () => {
  const m = merge(
    lens("security-breadth",
      [block("secb-1", { file: "src/parser.c", message: "Buffer overflow risk" })],
      [fix("secb-1", 0.9)]),
    lens("security-narrow"),
    CTX
  );
  assert.equal(m.blocking_issues.length, 1);
});

test("exclusion: markdown-file finding dropped", () => {
  const m = merge(
    lens("security-breadth",
      [block("secb-1", { file: "docs/README.md", message: "Outdated security note" })],
      [fix("secb-1", 0.9)]),
    lens("security-narrow"),
    CTX
  );
  assert.equal(m.blocking_issues.length, 0);
});

test("exclusion: narrow markdown-file finding survives, breadth dropped", () => {
  const m = merge(
    lens("security-breadth",
      [block("secb-1", { file: "docs/README.md", message: "Outdated security note" })],
      [fix("secb-1", 0.9)]),
    lens("security-narrow",
      [block("secn-1", { file: ".github/scripts/review/prompts/security-narrow.md", message: "Reviewer-routing regression" })],
      [fix("secn-1", 0.95)]),
    CTX
  );
  // Breadth markdown finding dropped; narrow markdown finding survives.
  assert.equal(m.blocking_issues.length, 1);
  assert.equal(m.blocking_issues[0].id, "secn-1");
});

test("dropReason: open-redirect dropped, but real vuln kept", () => {
  assert.equal(dropReason({ message: "Open redirect vulnerability" }), "open redirect (not high impact)");
  assert.equal(dropReason({ message: "SQL injection" }), null);
});

// ───────────────────────── confidence demotion ─────────────────────────

test("confidence < 0.7 demotes blocker to non-blocking note", () => {
  const m = merge(
    lens("security-breadth",
      [block("secb-1", { severity: "medium", message: "speculative thing" })],
      [fix("secb-1", 0.5)]),
    lens("security-narrow"),
    CTX
  );
  assert.equal(m.blocking_issues.length, 0);
  assert.equal(m.non_blocking_notes.length, 1);
  assert.match(m.non_blocking_notes[0].message, /\[demoted/);
  assert.equal(m.summary_metadata.demoted_count, 1);
});

test("confidence >= 0.7 keeps blocker", () => {
  const m = merge(
    lens("security-breadth",
      [block("secb-1")],
      [fix("secb-1", 0.7)]),
    lens("security-narrow"),
    CTX
  );
  assert.equal(m.blocking_issues.length, 1);
  assert.equal(m.summary_metadata.demoted_count, 0);
});

test("missing fix_suggestion keeps blocker (unknown confidence)", () => {
  const m = merge(
    lens("security-breadth",
      [block("secb-1")],
      [] /* no fix suggestions */),
    lens("security-narrow"),
    CTX
  );
  assert.equal(m.blocking_issues.length, 1);
});

// ───────────────────────── cap ─────────────────────────

test("blocking cap: keeps top 5 by severity then confidence", () => {
  const blockers = Array.from({ length: 10 }, (_, i) =>
    block(`secb-${i}`, { severity: i < 3 ? "critical" : "medium", file: `app/f${i}.py`, line: 10 + i * 6, category: `cat${i}` })
  );
  const fixes = blockers.map((b, i) => fix(b.id, 0.8 + i * 0.01));
  const m = merge(
    lens("security-breadth", blockers, fixes),
    lens("security-narrow"),
    CTX
  );
  assert.equal(m.blocking_issues.length, 5);
  // All three criticals must be present.
  const ids = m.blocking_issues.map((b) => b.id);
  assert.ok(ids.includes("secb-0"));
  assert.ok(ids.includes("secb-1"));
  assert.ok(ids.includes("secb-2"));
  assert.equal(m.summary_metadata.truncated_blocking_count, 5);
});

test("non-blocking cap: keeps top 5", () => {
  const notes = Array.from({ length: 8 }, (_, i) => ({ message: `note ${i}`, file: `app/f${i}.py`, line: 1 + i * 6 }));
  const m = merge(
    lens("security-breadth", [], [], notes),
    lens("security-narrow"),
    CTX
  );
  assert.equal(m.non_blocking_notes.length, 5);
  assert.equal(m.summary_metadata.truncated_nonblocking_count, 3);
});

// ───────────────────────── role + sha invariants ─────────────────────────

test("merged role is always 'security' regardless of inputs", () => {
  const m = merge(
    lens("security-breadth"),
    lens("security-narrow"),
    CTX
  );
  assert.equal(m.role, "security");
});

test("merged sha + cycle come from ctx, not inputs", () => {
  const m = merge(
    lens("security-breadth"),
    lens("security-narrow"),
    { reviewedSha: SHA, cycle: 3 }
  );
  assert.equal(m.reviewed_sha, SHA);
  assert.equal(m.cycle, 3);
});

// ───────────────────────── degenerate inputs ─────────────────────────

test("both inputs null -> abstain", () => {
  const m = merge(null, null, CTX);
  assert.equal(m.decision, "abstain");
  assert.equal(m.summary_metadata.merged_from.length, 0);
});

test("only breadth present, narrow null -> works", () => {
  const m = merge(
    lens("security-breadth", [block("secb-1")], [fix("secb-1", 0.9)]),
    null,
    CTX
  );
  assert.equal(m.decision, "request_changes");
  assert.equal(m.blocking_issues.length, 1);
  assert.deepEqual(m.summary_metadata.merged_from, ["security-breadth"]);
});

// ───────────────────────── non_blocking_notes shape ─────────────────────────

test("demoted notes retain only {message,file,line} fields (schema-compliant)", () => {
  const m = merge(
    lens("security-breadth",
      [block("secb-1", { file: "app/x.py", line: 20, category: "auth", severity: "high" })],
      [fix("secb-1", 0.5)]),
    lens("security-narrow"),
    CTX
  );
  assert.equal(m.non_blocking_notes.length, 1);
  const note = m.non_blocking_notes[0];
  assert.deepEqual(Object.keys(note).sort(), ["file", "line", "message"]);
  assert.equal(note.file, "app/x.py");
  assert.equal(note.line, 20);
});
