You = Claude Code CI smoke test for the review fixer migration.

Goal: prove the CI container can use Claude Code against the LiteLLM
Anthropic endpoint, read the same reviewer-artifact directory the real
fixer uses, write a file into the bind-mounted workspace, and emit a
structured result JSON to `$OUTPUT_PATH`.

## Requirements

1. Read every `*-result.json` under `${REVIEWER_ARTIFACTS_DIR}`.
2. Parse the first blocker id and message you find.
3. Write exactly one line to `${SMOKE_TARGET_PATH}` in this format:
   `claude-smoke:<id>:<message>`
4. Write JSON to `$OUTPUT_PATH` with this shape:

```json
{
  "auth_ok": true,
  "artifact_files": ["relative/path.json"],
  "first_blocker_id": "docs/doc-001",
  "first_blocker_message": "example",
  "workspace_write_path": ".review-output/claude-smoke-target.txt",
  "workspace_write_ok": true
}
```

## Constraints

- No `git add`, `git commit`, `git push`, or any other git-write command.
- Keep changes inside `.review-output/`.
- Stop once the target file and `$OUTPUT_PATH` exist.
