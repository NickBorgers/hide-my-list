#!/usr/bin/env node
// Judge driver for the v2 review pipeline.
//
// Reads reviewer artifacts from REVIEWER_DIR and the fix-result from
// FIX_RESULT_PATH, calls aggregate(), writes verdict-<sha>.json to
// VERDICT_OUTPUT_PATH, and prints `verdict=<GO|NO-GO>` to stdout
// (also appends to GITHUB_OUTPUT if set).
//
// Security-lens orchestrator collapse (§1.18): if `security-breadth`
// and/or `security-narrow` reviewer artifacts are present, this driver
// folds them into a single synthesized `role=security` artifact using
// `security-merge.mjs` BEFORE calling aggregate(). The merger and the
// schema both live in this `main` checkout — there is no PR-side
// surface in the judge's collapse path, which closes the script-trust
// attack class the breadth lens flagged on PR #585.
//
// If MERGED_SECURITY_OUTPUT_PATH is set and lens artifacts were
// collapsed, the synthesized security artifact is also written there
// so the surrounding workflow can re-upload it as
// `reviewer-security-<sha>` for the finalize comment renderer.
//
// Pure file IO + an aggregate() call + an optional merge(). No git,
// no API, no Codex. Designed to run in a job with permissions:
// contents: read so the judge structurally cannot push.

import { readFileSync, readdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { aggregate } from "./aggregate.mjs";
import { merge as mergeSecurityLenses } from "./security-merge.mjs";

const REVIEWER_DIR = process.env.REVIEWER_DIR;
const FIX_RESULT_PATH = process.env.FIX_RESULT_PATH;
const VERDICT_OUTPUT_PATH = process.env.VERDICT_OUTPUT_PATH;
const MERGED_SECURITY_OUTPUT_PATH = process.env.MERGED_SECURITY_OUTPUT_PATH;

if (!REVIEWER_DIR || !FIX_RESULT_PATH || !VERDICT_OUTPUT_PATH) {
  console.error(
    "judge.mjs: REVIEWER_DIR, FIX_RESULT_PATH, and VERDICT_OUTPUT_PATH must be set"
  );
  process.exit(2);
}

// Reviewer artifacts are downloaded as `reviewer-<role>-<sha>/<role>-result.json`.
// Walk one level down to find them.
let reviewers = [];
for (const entry of readdirSync(REVIEWER_DIR, { withFileTypes: true })) {
  if (!entry.isDirectory()) continue;
  const dir = join(REVIEWER_DIR, entry.name);
  const candidate = readdirSync(dir).find((f) => f.endsWith("-result.json"));
  if (!candidate) continue;
  reviewers.push(JSON.parse(readFileSync(join(dir, candidate), "utf8")));
}

// Collapse security lens artifacts. The merger applies cap, dedup,
// exclusion filter, and confidence demotion deterministically.
const breadthArtifact = reviewers.find((r) => r.role === "security-breadth");
const narrowArtifact = reviewers.find((r) => r.role === "security-narrow");
let synthesizedSecurityPath = "";
if (breadthArtifact || narrowArtifact) {
  // Sanity check: both lenses must agree on reviewed_sha and cycle, or
  // the orchestration is broken (mixed epochs). aggregate() catches this
  // downstream, but failing here is more localized.
  const refArtifact = breadthArtifact ?? narrowArtifact;
  const merged = mergeSecurityLenses(breadthArtifact ?? null, narrowArtifact ?? null, {
    reviewedSha: refArtifact.reviewed_sha,
    cycle: refArtifact.cycle,
  });
  // Remove the lens artifacts; insert the synthesized one.
  reviewers = reviewers.filter((r) => r.role !== "security-breadth" && r.role !== "security-narrow");
  reviewers.push(merged);

  if (MERGED_SECURITY_OUTPUT_PATH) {
    writeFileSync(MERGED_SECURITY_OUTPUT_PATH, JSON.stringify(merged, null, 2) + "\n");
    synthesizedSecurityPath = MERGED_SECURITY_OUTPUT_PATH;
  }
}

const fixResult = JSON.parse(readFileSync(FIX_RESULT_PATH, "utf8"));

const verdict = aggregate(reviewers, fixResult);

console.log(JSON.stringify(verdict, null, 2));
writeFileSync(VERDICT_OUTPUT_PATH, JSON.stringify(verdict, null, 2) + "\n");

// Emit GitHub Actions step outputs if running inside Actions.
// `synthesized_security_artifact` gates the workflow's upload step
// for the merged security artifact — using a step output here avoids
// the `hashFiles(absolute_path)` foot-gun (hashFiles silently returns
// empty for non-workspace-relative paths, which caused PR #592's
// upload to be skipped and the agent table to render `did not run`).
const ghOut = process.env.GITHUB_OUTPUT;
if (ghOut) {
  writeFileSync(
    ghOut,
    `verdict=${verdict.verdict}\nsynthesized_security_artifact=${synthesizedSecurityPath}\n`,
    { flag: "a" }
  );
}
