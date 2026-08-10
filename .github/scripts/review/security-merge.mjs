// Security reviewer merger for the v2 review pipeline.
//
// SCOPE: combine the two security-lens artifacts (`security-breadth`
// and `security-narrow`) into a single canonical `role=security`
// artifact that the judge consumes. The judge is unaware of the split;
// only this module knows about lenses.
//
// Pure function: two parsed JSON artifacts -> one merged artifact.
// The CLI driver at the bottom adds file I/O. No git, no API, no LLM.
//
// MERGE CONTRACT
//
//   1. Exclusion filter (ported from anthropics/claude-code-security-review
//      claudecode/findings_filter.py) drops generic low-signal findings
//      such as DoS, rate-limiting, open-redirect, regex-injection, and
//      memory-safety findings in non-C/C++ code. This is a backstop —
//      the breadth prompt also instructs the LLM to skip these
//      categories.
//   2. Confidence demotion. Blocking issues whose paired fix_suggestion
//      reports confidence below CONFIDENCE_FLOOR are demoted to
//      non_blocking_notes. Missing fix_suggestions are treated as
//      "confidence unknown" and not demoted.
//   3. Dedup. Findings keyed by (normalized_file, floor(line/5)).
//      Category excluded from key: breadth and narrow use different
//      vocabularies, so category equality would prevent cross-lens dedup.
//      On collision, narrow wins (repo-specific phrasing preferred) and
//      inherits breadth's category if narrow didn't set one.
//   4. Per-cycle cap. After dedup, sort blockers by severity
//      (critical > high > medium) then confidence desc, keep the top
//      BLOCKING_CAP. Sort non_blocking_notes (no severity to sort on)
//      by stable order from breadth-then-narrow, keep the top
//      NONBLOCKING_CAP. Overflow counts surface in summary_metadata.
//   5. Decision is derived from what survives: any blockers ->
//      request_changes; no findings at all -> approve; only
//      non_blocking notes -> comment.
//
// The judge consumes the merged artifact with role=security via the
// normal `reviewer-*-<sha>` artifact glob; the per-lens artifacts are
// uploaded under a `lens-*` prefix so the glob ignores them.

import { readFileSync, writeFileSync } from "node:fs";

const CONFIDENCE_FLOOR = 0.7;
const BLOCKING_CAP = 5;
const NONBLOCKING_CAP = 5;

const SEVERITY_RANK = { critical: 3, high: 2, medium: 1 };

// ───────────────────────── Exclusion filter ─────────────────────────
//
// Ported from anthropics/claude-code-security-review claudecode/
// findings_filter.py (MIT). Each rule returns true when the finding
// should be DROPPED.

const DOS_PATTERNS = [
  /denial of service/i,
  /\bdos\b/i,
  /resource exhaustion/i,
  /infinite loop/i,
];
const RATE_LIMIT_PATTERNS = [
  /missing rate limit/i,
  /rate limiting not implemented/i,
  /unlimited (requests|calls|api)/i,
];
const RESOURCE_LEAK_PATTERNS = [
  /(resource|memory|file) leak( potential)?/i,
  /unclosed resource/i,
  /(database|thread|socket|connection) leak/i,
];
const OPEN_REDIRECT_PATTERNS = [
  /open redirect/i,
  /unvalidated redirect/i,
  /redirect (attack|exploit)/i,
];
const REGEX_INJECTION_PATTERNS = [
  /regex injection/i,
  /regular expression (injection|denial of service|flooding)/i,
  /\bredos\b/i,
];
const MEMORY_SAFETY_PATTERNS = [
  /buffer overflow/i,
  /stack overflow/i,
  /use[- ]after[- ]free/i,
  /null pointer dereference/i,
  /integer overflow/i,
];
const SSRF_PATTERNS = [
  /\bssrf\b/i,
  /server[- ]side request forgery/i,
];

function isMarkdownFile(file) {
  return typeof file === "string" && file.toLowerCase().endsWith(".md");
}

function isCFamilyFile(file) {
  if (typeof file !== "string") return false;
  const lower = file.toLowerCase();
  return lower.endsWith(".c") || lower.endsWith(".cc") || lower.endsWith(".cpp") || lower.endsWith(".h");
}

function isHtmlFile(file) {
  return typeof file === "string" && file.toLowerCase().endsWith(".html");
}

function matchesAny(text, patterns) {
  if (typeof text !== "string") return false;
  return patterns.some((re) => re.test(text));
}

/**
 * Decide whether a finding should be dropped by the exclusion filter.
 * Returns the reason string for drop, or null to keep.
 */
export function dropReason(finding) {
  const file = finding.file ?? "";
  const msg = finding.message ?? "";

  if (isMarkdownFile(file) && finding.__source_role !== "security-narrow") return "finding in markdown documentation file";
  if (matchesAny(msg, DOS_PATTERNS)) return "generic DoS / resource-exhaustion (low signal)";
  if (matchesAny(msg, RATE_LIMIT_PATTERNS)) return "generic rate-limiting recommendation";
  if (matchesAny(msg, RESOURCE_LEAK_PATTERNS)) return "resource management (not a security vulnerability)";
  if (matchesAny(msg, OPEN_REDIRECT_PATTERNS)) return "open redirect (not high impact)";
  if (matchesAny(msg, REGEX_INJECTION_PATTERNS)) return "regex injection (not applicable here)";
  if (matchesAny(msg, MEMORY_SAFETY_PATTERNS) && !isCFamilyFile(file)) {
    return "memory-safety pattern in non-C/C++ file (not applicable)";
  }
  if (matchesAny(msg, SSRF_PATTERNS) && isHtmlFile(file)) {
    return "SSRF in HTML / client-side file (not applicable)";
  }
  return null;
}

// ───────────────────────── Dedup + sort helpers ─────────────────────────

function normalizePath(file) {
  if (typeof file !== "string") return "";
  return file.replace(/\\/g, "/").replace(/^\.\//, "");
}

function dedupKey(finding) {
  const file = normalizePath(finding.file ?? "");
  // Findings without a file location are treated as unique (keyed by id)
  // since two location-less meta-findings from different lenses are
  // unlikely to be the same underlying issue.
  if (!file) return `__nofile__|${finding.id ?? Math.random()}`;
  const lineBucket = Math.floor((finding.line ?? 0) / 5);
  // Category is intentionally NOT part of the key: breadth and narrow
  // use different category vocabularies (catalog vs. repo-specific), so
  // a same-file/same-line finding would never dedup if we required
  // category equality. The narrow-wins resolution carries through.
  return `${file}|${lineBucket}`;
}

function severityRank(s) {
  return SEVERITY_RANK[s] ?? 0;
}

// ───────────────────────── Core merge ─────────────────────────

/**
 * Merge breadth + narrow artifacts into the canonical security artifact.
 *
 * @param {object|null} breadth  parsed security-breadth-result.json (or null if missing)
 * @param {object|null} narrow   parsed security-narrow-result.json  (or null if missing)
 * @param {object} ctx           { reviewedSha, cycle }
 * @returns {object}             merged reviewer-v1.json artifact with role="security"
 */
export function merge(breadth, narrow, ctx) {
  const { reviewedSha, cycle } = ctx;
  const sources = [breadth, narrow].filter(Boolean);

  // Fail-closed: no inputs.
  if (sources.length === 0) {
    return {
      schema_version: "1",
      role: "security",
      reviewed_sha: reviewedSha,
      cycle,
      decision: "abstain",
      summary: "Security orchestrator received no lens artifacts.",
      blocking_issues: [],
      non_blocking_notes: [],
      fix_suggestions: [],
      followup_issues: [],
      summary_metadata: {
        merged_from: [],
        truncated_blocking_count: 0,
        truncated_nonblocking_count: 0,
        dropped_count: 0,
        demoted_count: 0,
      },
    };
  }

  const mergedFrom = sources.map((s) => s.role);

  // Index fix_suggestions by id across both sources so we can look up
  // confidence for demotion.
  const fixById = new Map();
  for (const src of sources) {
    for (const fix of src.fix_suggestions ?? []) {
      if (fix.id) fixById.set(fix.id, fix);
    }
  }

  // (1) Exclusion filter — applied to all candidate findings.
  let dropped = 0;
  const passFilter = (finding) => {
    const reason = dropReason(finding);
    if (reason) {
      dropped += 1;
      // stderr only — never leaks into the artifact.
      console.error(`security-merge: drop ${finding.id ?? "?"} (${reason})`);
      return false;
    }
    return true;
  };

  // Annotate each finding with its source role before merging so we can
  // resolve dedup conflicts in narrow's favor.
  const annotate = (finding, role) => ({ ...finding, __source_role: role });

  const allBlocking = [
    ...((breadth?.blocking_issues ?? []).map((f) => annotate(f, "security-breadth"))),
    ...((narrow?.blocking_issues ?? []).map((f) => annotate(f, "security-narrow"))),
  ].filter(passFilter);

  const allNonBlocking = [
    ...((breadth?.non_blocking_notes ?? []).map((n) => annotate(n, "security-breadth"))),
    ...((narrow?.non_blocking_notes ?? []).map((n) => annotate(n, "security-narrow"))),
  ].filter(passFilter);

  // (2) Confidence demotion — only applies to blocking_issues.
  let demoted = 0;
  const survivingBlocking = [];
  for (const b of allBlocking) {
    const fix = fixById.get(b.id);
    if (fix && typeof fix.confidence === "number" && fix.confidence < CONFIDENCE_FLOOR) {
      demoted += 1;
      console.error(`security-merge: demote ${b.id} (confidence ${fix.confidence} < ${CONFIDENCE_FLOOR})`);
      const note = {
        message: `[demoted ${b.severity}, ${b.id}] ${b.message}`,
      };
      if (b.file != null) note.file = b.file;
      if (b.line != null) note.line = b.line;
      allNonBlocking.push(annotate(note, b.__source_role));
    } else {
      survivingBlocking.push(b);
    }
  }

  // (3) Dedup. Narrow wins on collision.
  const dedupedBlocking = dedupCollapse(survivingBlocking);
  const dedupedNonBlocking = dedupCollapse(allNonBlocking);

  // (4) Cap. Sort blockers by severity then confidence desc.
  dedupedBlocking.sort((a, b) => {
    const sevA = severityRank(a.severity);
    const sevB = severityRank(b.severity);
    if (sevA !== sevB) return sevB - sevA;
    const confA = (fixById.get(a.id)?.confidence) ?? 0;
    const confB = (fixById.get(b.id)?.confidence) ?? 0;
    return confB - confA;
  });

  const truncatedBlocking = Math.max(0, dedupedBlocking.length - BLOCKING_CAP);
  const finalBlocking = dedupedBlocking.slice(0, BLOCKING_CAP);
  const truncatedNonBlocking = Math.max(0, dedupedNonBlocking.length - NONBLOCKING_CAP);
  const finalNonBlocking = dedupedNonBlocking.slice(0, NONBLOCKING_CAP);

  // Carry through only the fix_suggestions that correspond to surviving
  // blockers (so the fixer sees fixes for things it's actually being
  // asked to address).
  const survivingIds = new Set(finalBlocking.map((b) => b.id));
  const finalFixes = [];
  for (const id of survivingIds) {
    const fix = fixById.get(id);
    if (fix) finalFixes.push(stripAnnotation(fix));
  }

  // Followup issues: pass through both lenses' followups; not capped
  // because they're tightly scoped per the schema docstring.
  const finalFollowups = [
    ...((breadth?.followup_issues ?? [])),
    ...((narrow?.followup_issues ?? [])),
  ];

  // Decision.
  let decision = "approve";
  if (finalBlocking.length > 0) {
    decision = "request_changes";
  } else if (finalNonBlocking.length > 0) {
    decision = "comment";
  }

  // Summary (≤500 chars). Build defensively.
  const summary = buildSummary({
    mergedFrom,
    blocking: finalBlocking.length,
    nonBlocking: finalNonBlocking.length,
    dropped,
    demoted,
    truncatedBlocking,
    truncatedNonBlocking,
    decision,
  });

  return {
    schema_version: "1",
    role: "security",
    reviewed_sha: reviewedSha,
    cycle,
    decision,
    summary,
    blocking_issues: finalBlocking.map(stripAnnotation),
    non_blocking_notes: finalNonBlocking.map(stripAnnotationToNote),
    fix_suggestions: finalFixes,
    followup_issues: finalFollowups,
    summary_metadata: {
      merged_from: mergedFrom,
      truncated_blocking_count: truncatedBlocking,
      truncated_nonblocking_count: truncatedNonBlocking,
      dropped_count: dropped,
      demoted_count: demoted,
    },
  };
}

function dedupCollapse(findings) {
  const byKey = new Map();
  for (const f of findings) {
    const key = dedupKey(f);
    const existing = byKey.get(key);
    if (!existing) {
      byKey.set(key, f);
      continue;
    }
    // Narrow wins on collision; if narrow lacks a category, inherit from breadth.
    const narrowSide = f.__source_role === "security-narrow" ? f : existing.__source_role === "security-narrow" ? existing : null;
    const breadthSide = f.__source_role === "security-breadth" ? f : existing.__source_role === "security-breadth" ? existing : null;
    if (narrowSide) {
      const merged = { ...narrowSide };
      if (merged.category == null && breadthSide?.category != null) {
        merged.category = breadthSide.category;
      }
      byKey.set(key, merged);
    } else {
      // Same-source duplicate (shouldn't happen, but keep first).
      // No-op.
    }
  }
  return [...byKey.values()];
}

function stripAnnotation(finding) {
  const { __source_role, ...rest } = finding;
  return rest;
}

function stripAnnotationToNote(note) {
  const { __source_role, severity, id, category, source, ...rest } = note;
  // Schema for non_blocking_notes allows only {message, file?, line?}.
  const allowed = { message: rest.message };
  if (rest.file != null) allowed.file = rest.file;
  if (rest.line != null) allowed.line = rest.line;
  return allowed;
}

function buildSummary({ mergedFrom, blocking, nonBlocking, dropped, demoted, truncatedBlocking, truncatedNonBlocking, decision }) {
  const parts = [
    `Security review merged from ${mergedFrom.join(" + ")}.`,
    `${blocking} blocking + ${nonBlocking} non-blocking finding(s) after merge.`,
  ];
  if (dropped) parts.push(`Dropped ${dropped} by exclusion filter.`);
  if (demoted) parts.push(`Demoted ${demoted} to non-blocking (confidence < ${CONFIDENCE_FLOOR}).`);
  if (truncatedBlocking || truncatedNonBlocking) {
    parts.push(`Cap truncated ${truncatedBlocking} blocking + ${truncatedNonBlocking} non-blocking.`);
  }
  parts.push(`Decision: ${decision}.`);
  let s = parts.join(" ");
  if (s.length > 500) s = s.slice(0, 497) + "...";
  return s;
}

// ───────────────────────── CLI driver ─────────────────────────

function maybeRead(path) {
  if (!path) return null;
  try {
    return JSON.parse(readFileSync(path, "utf8"));
  } catch (err) {
    console.error(`security-merge: failed to read ${path}: ${err.message}`);
    return null;
  }
}

function main() {
  const breadthPath = process.env.BREADTH_PATH;
  const narrowPath = process.env.NARROW_PATH;
  const outputPath = process.env.OUTPUT_PATH;
  const reviewedSha = process.env.REVIEWED_SHA;
  const cycleStr = process.env.REVIEW_CYCLE;

  if (!outputPath || !reviewedSha || !cycleStr) {
    console.error("security-merge: OUTPUT_PATH, REVIEWED_SHA, REVIEW_CYCLE required");
    process.exit(2);
  }

  const breadth = maybeRead(breadthPath);
  const narrow = maybeRead(narrowPath);
  const cycle = Number.parseInt(cycleStr, 10);
  if (!Number.isFinite(cycle) || cycle < 1) {
    console.error(`security-merge: invalid REVIEW_CYCLE=${cycleStr}`);
    process.exit(2);
  }

  const merged = merge(breadth, narrow, { reviewedSha, cycle });
  writeFileSync(outputPath, JSON.stringify(merged, null, 2) + "\n");
  console.error(`security-merge: wrote ${outputPath} (decision=${merged.decision})`);
}

// Only execute the CLI when invoked directly, not when imported by tests.
if (import.meta.url === `file://${process.argv[1]}`) {
  main();
}
