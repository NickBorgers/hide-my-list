# `.github/ci/` — CI agent container

Dedicated CI-only container image used by every automated agent
workflow in this repo (`codex`, `codex-diagnose-workflow-failure`,
`review-coverage-evaluator`, `review-reviewer`, `review-fixer`). This
image is distinct from `.devcontainer/Dockerfile`, which is for human
IDE workflows.

## Why the split

Running CI agents through the devcontainer was fragile across the
self-hosted runner's shared-docker-socket topology — bind-mount sources
had to live under the host-visible `/tmp/runner-work` tree, the inner
container needed to be writable by the runner's UID, and every path
assumption had to be re-verified. Splitting into a purpose-built CI
image eliminates the whole class of "devcontainer abstraction leaks
through the shared socket" bugs.

## Files

| File | Purpose |
|---|---|
| `Dockerfile` | Image recipe. `ubuntu:24.04` base, UID-1000 `ci` user, pinned Claude Code / Codex / actionlint / Node major. Installs shellcheck, yamllint, gh, Mermaid npm globals, envsubst. |
| `versions.env` | Single source of truth for CLI version pins. Consumed by `.github/workflows/ci-image.yml` as `--build-arg`s. |
| `caveman-rules.md` | Canonical CI-only caveman prompt contract. `review-codex-run`, `review-claude-run`, `review-codex-resume`, and `review-claude-resume` prepend it to review prompts and validate its pinned source version against `CAVEMAN_VERSION`. |
| `README.md` | This file. |

## Relationship to `.devcontainer/`

| Aspect | `.devcontainer/` | `.github/ci/` |
|---|---|---|
| Audience | Human developers | CI agent workflows |
| Lifecycle hooks | Yes (`initializeCommand`, `postCreateCommand`) | None |
| Host bind mounts | Yes (credentials passthrough, host config) | None |
| Credential passthrough | Copied from host files via `init-host-credentials.sh` | `-e` env vars at `docker run` time |
| User | `vscode` via `updateRemoteUserUID` | `ci` (UID 1000) baked in |
| Base image | `mcr.microsoft.com/devcontainers/base:ubuntu` | `ubuntu:24.04` |
| Cache image | `ghcr.io/nickborgersprobably/hide-my-list-devcontainer:latest` | `ghcr.io/nickborgersprobably/hide-my-list-ci:latest` |
| Build pipeline | (existing) | `.github/workflows/ci-image.yml` |

## How workflows consume the image

Review pipeline v2 uses four direct-`docker run` composite actions
against the CI image:

- `./.github/actions/review-codex-run` — read-only reviewers (Codex)
- `./.github/actions/review-claude-run` — fresh-Claude fixer fallback (human PRs, missing-trailer cases)
- `./.github/actions/review-codex-resume` — resumed Codex author session as fixer (`codex exec resume --last`)
- `./.github/actions/review-claude-resume` — resumed Claude Code author session as fixer (`claude --continue`)

The other agent workflows (`codex`, `codex-diagnose-workflow-failure`,
`review-coverage-evaluator`) invoke `docker run` inline.

All of them call `scripts/ensure-ci-image.sh` before launch. The helper
tries `docker pull` first and, if the configured tag is missing in GHCR
or otherwise unavailable, rebuilds the CI image locally from
`.github/ci/Dockerfile` using the pinned versions in `versions.env`.

They then follow the same topology rules learned from the
home-automation refactor:

- **Mount sources must be host-visible**. The runner is a container on
  dockergeneric with a shared `/var/run/docker.sock`, so bind-mount
  sources resolve against the *host's* filesystem, not the runner's.
  Only `/tmp/runner-work/*` (= `$GITHUB_WORKSPACE`, `$RUNNER_TEMP`) is
  bind-mounted from host → runner, so all staging lives there.
- **Files mounted into the container must be readable by UID 1000**.
  Files created by the runner's user (typically UID 1001) need
  `chmod 644` or looser so the `ci` user inside can read them.
- **Output captured via tee must write to a pre-chmod-777 staging
  directory** under `$RUNNER_TEMP`, then be copied back to the
  caller-requested path on the runner side. (Review pipeline v2
  sidesteps this because it writes its structured JSON output directly
  to `.review-output/` inside the bind-mounted workspace.)
- **Always `--network host`** so the nested container inherits the
  runner's Tailscale connection to LiteLLM at
  `https://llm.featherback-mermaid.ts.net/v1`.
- **Claude-over-LiteLLM uses the Anthropic-compatible path**.
  `review-claude-run` forwards `LLM_PROXY_BASE_URL=https://llm.featherback-mermaid.ts.net/anthropic/`
  and `LLM_PROXY_API_KEY=fake-key`; reviewer jobs keep using the
  OpenAI-compatible path via `.devcontainer/configure-codex.sh`.

## Issue Resolution Agent issue-resolution entry points

`.github/workflows/codex.yml` contains a `resolve-issue` job that opens
PRs for trusted issue-resolution requests. It has two entry points:

- Issue lifecycle events: `opened`, `reopened`, or `unlabeled` after
  `codex-started` or `claude-started`
- `/autoresolve [codex|claude]` issue comments on open non-PR issues
  from trusted original authors

Both Codex and Claude Code are first-class authors. The agent is
selected by:
1. `/autoresolve codex` or `/autoresolve claude` comment command
2. `agent:codex` or `agent:claude` issue label
3. Repo default (currently Codex)

The author's session is packed and uploaded as the
`author-session-<agent>-<run-id>` workflow artifact, and an
`Author-Session: <agent>/<run-id>` trailer is written into the PR body.
The v2 fixer parses the trailer, downloads the artifact from the
original `resolve-issue` run, and resumes the original author session
for context-aware fixes rather than using a fresh model.

The comment-command path is intentionally narrower than "any
collaborator can point Codex at any issue": the commenter still has to
pass the workflow's collaborator authorization gate, and the underlying
issue must already have been opened by an `OWNER`, `MEMBER`, or
`COLLABORATOR`. That preserves the same trust boundary as the
issue-lifecycle path before the self-hosted resolver reads issue
content or runs with write-capable credentials.

## Rebuilding the image locally

```bash
# From repo root:
source .github/ci/versions.env
docker build \
  -f .github/ci/Dockerfile \
  --build-arg CLAUDE_CODE_VERSION="$CLAUDE_CODE_VERSION" \
  --build-arg CODEX_CLI_VERSION="$CODEX_CLI_VERSION" \
  --build-arg ACTIONLINT_VERSION="$ACTIONLINT_VERSION" \
  --build-arg NODE_MAJOR="$NODE_MAJOR" \
  -t hide-my-list-ci-local .

# Verify the toolchain:
docker run --rm hide-my-list-ci-local -lc '
  set -e
  claude --version
  codex --version
  gh --version
  actionlint -version
  node --version
  shellcheck --version | head -2
  yamllint --version
  node -e "require(\"mermaid\")"
'

# Exercise against a real workspace:
docker run --rm \
  -v "$(pwd):/workspace" \
  -w /workspace \
  hide-my-list-ci-local \
  -lc 'shellcheck scripts/*.sh'
```

## Version bumps

Pinned versions live in `versions.env`. Bump the values there, then
rebuild the image (or let the next `ci-image.yml` run on a merge to
main pick them up via its `paths:` filter).
