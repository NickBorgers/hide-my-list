# Bug 0613: signal-cli digest pinned with no refresh mechanism

**Issue:** #613

## Bug Story

`docker/compose.yaml` pinned `bbernhard/signal-cli-rest-api` by sha256 digest
on 2025-05-15 and nothing ever refreshed it. Over the following 14 months,
Signal changed its server-side envelope format. The pinned signal-cli threw
`NullPointerException: getServerGuid(...) must not be null` on every inbound
envelope and discarded it before it reached the app. Inbound Signal was dead
for seven weeks before the breakage was diagnosed.

The root cause was not the digest pin — pinning is correct for reproducibility.
The root cause was the absence of any automated refresh path. Pinning by digest
*with no refresh mechanism* is how a production dependency silently rots.

Two properties must hold together:

1. **The pin is an immutable digest**, so deploys are reproducible and the
   image that ships is the image that was tested.
2. **Something refreshes that digest on a schedule**, so the pin cannot drift
   silently away from the supported upstream version.

`update-ai-clis.yml` already refreshed the Claude Code and Codex CLI pins
weekly. The one dependency the product cannot function without had no
equivalent.

## Fix

- `.github/workflows/update-signal-cli.yml` — Cron (Mondays 10:00 UTC) +
  `workflow_dispatch`. Reads the digest currently pinned in `docker/compose.yaml`,
  resolves `:latest` from the registry via anonymous pull token (no image pull),
  validates the response against `^sha256:[0-9a-f]{64}$` before writing, then
  rewrites the image line and the `# Pinned: <date>` provenance comment and
  opens a PR via `peter-evans/create-pull-request@v7` when they differ.
- Scheduled an hour after `update-ai-clis.yml` (Mondays 10:00 UTC vs. 09:00
  UTC) so the two refresh PRs do not land together.

## Regression Tests

**Structural lint (unit):** tests live in `tests/unit/test_signal_cli_pin.py`:

- `test_signal_cli_pinned_by_digest` — image line uses `@sha256:<64 hex>`,
  never a mutable tag.
- `test_signal_cli_pin_is_not_a_mutable_tag` — guards against reversion to
  `:latest` or a named tag.
- `test_provenance_comment_matches_workflow_contract` — exactly one
  `# Pinned: YYYY-MM-DD against bbernhard/signal-cli-rest-api:latest` comment
  exists; the refresh workflow rewrites this line by regex and fails closed when
  the count is not 1.
- `test_refresh_workflow_exists` — `.github/workflows/update-signal-cli.yml`
  must be present; a digest with no upgrade path is the failure mode being
  prevented.
- `test_refresh_workflow_targets_the_pinned_image` — workflow references the
  same image the compose file pins.
- `test_refresh_workflow_is_scheduled` — workflow has a `schedule: cron:`
  stanza; a manual-only refresh is not a refresh.
- `test_refresh_workflow_validates_digest_before_writing` — workflow contains
  the `sha256:[0-9a-f]{64}` validation pattern; writing an unvalidated upstream
  value into a deploy manifest is the fail-open case.
