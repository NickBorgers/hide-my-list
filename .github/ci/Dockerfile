# hide-my-list CI agent image.
#
# Used EXCLUSIVELY by CI agent workflows (codex,
# codex-diagnose-workflow-failure, review-coverage-evaluator,
# review-reviewer, review-fixer). NOT the same image as
# `.devcontainer/Dockerfile` — that one is for human IDE workflows.
#
# Design:
#   * No @devcontainers/cli, no initializeCommand / postCreateCommand.
#   * No bind mounts of host personal config.
#   * Credentials arrive via env vars (fake-key for LiteLLM on Tailscale).
#   * Runs as UID 1000 `ci` user, with PATH set in /etc/profile.d/ci-path.sh
#     so login shells don't lose /home/ci/.local/bin.
#
# Built and pushed by `.github/workflows/ci-image.yml` to
# `ghcr.io/nickborgersprobably/hide-my-list-ci:latest`.

FROM ubuntu:24.04

ARG CLAUDE_CODE_VERSION
ARG CODEX_CLI_VERSION
ARG ACTIONLINT_VERSION
ARG NODE_MAJOR=20

ENV DEBIAN_FRONTEND=noninteractive

# System packages: shell/yaml/python validators + core utilities.
# Intentionally missing vs devcontainer: zsh, fonts, playwright deps —
# none of those are used by this repo's CI agents.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl git bash gnupg jq sudo gettext-base \
        bc tmux \
        shellcheck yamllint python3-pip python3-yaml \
    && rm -rf /var/lib/apt/lists/*

# GitHub CLI from GitHub's own apt repo.
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

# actionlint — pinned release tarball with checksum verification.
RUN set -eux; \
    archive="actionlint_${ACTIONLINT_VERSION}_linux_amd64.tar.gz"; \
    checksums="actionlint_${ACTIONLINT_VERSION}_checksums.txt"; \
    base_url="https://github.com/rhysd/actionlint/releases/download/v${ACTIONLINT_VERSION}"; \
    tmp_dir="$(mktemp -d)"; \
    curl -fsSL "${base_url}/${archive}" -o "${tmp_dir}/${archive}"; \
    curl -fsSL "${base_url}/${checksums}" -o "${tmp_dir}/${checksums}"; \
    (cd "${tmp_dir}" && grep " ${archive}$" "${checksums}" | sha256sum -c -); \
    tar -xzf "${tmp_dir}/${archive}" -C /usr/local/bin actionlint; \
    chmod +x /usr/local/bin/actionlint; \
    rm -rf "${tmp_dir}"

# Node.js LTS — needed for Claude Code plugins, Mermaid validation,
# and any npm globals the review pipeline uses.
RUN curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Mermaid validation dependencies (scripts/validate-mermaid.sh).
RUN npm install -g mermaid@11 jsdom@25 dompurify@3

# Non-root user for the CI workload. ubuntu:24.04 ships with a
# pre-existing `ubuntu` user at UID 1000 — remove it first so we can
# claim that uid for our own `ci` user (matches home-automation's CI
# image user numbering).
RUN userdel -r ubuntu 2>/dev/null || true \
    && groupadd --gid 1000 ci \
    && useradd --uid 1000 --gid ci --shell /bin/bash --create-home ci \
    && echo 'ci ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/ci \
    && chmod 0440 /etc/sudoers.d/ci

# Login shells re-source /etc/profile and reset PATH to the minimal
# Debian default. Reinstate /home/ci/.local/bin so both login and
# non-login invocations see the claude/codex binaries.
RUN printf '%s\n' \
        'export PATH="/home/ci/.local/bin:$PATH"' \
        'export TMPDIR="/home/ci/.claude/tmp"' \
        > /etc/profile.d/ci-path.sh \
    && chmod 0644 /etc/profile.d/ci-path.sh

USER ci
WORKDIR /home/ci

ENV PATH=/home/ci/.local/bin:$PATH
ENV TMPDIR=/home/ci/.claude/tmp

RUN mkdir -p /home/ci/.claude/tmp \
             /home/ci/.claude/plugins/cache \
             /home/ci/.claude/plugins/marketplaces \
             /home/ci/.codex

# Pre-configure Claude Code for headless use (skips onboarding wizard
# in --print mode, matching the devcontainer's pattern).
RUN printf '{"hasCompletedOnboarding": true}\n' > /home/ci/.claude.json

# Install Claude Code (pinned).
RUN curl -fsSL --retry 5 --retry-delay 2 --retry-connrefused https://claude.ai/install.sh \
        | bash -s -- "${CLAUDE_CODE_VERSION}"

# Install Codex CLI (pinned).
RUN curl -fsSL --retry 5 --retry-delay 2 --retry-connrefused \
        "https://github.com/openai/codex/releases/download/rust-v${CODEX_CLI_VERSION}/install.sh" \
    | sh -s -- --release "${CODEX_CLI_VERSION}"

ENTRYPOINT ["/bin/bash"]
