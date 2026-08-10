# Devcontainer for hide-my-list (OpenClaw agent project)
# Provides Claude Code CLI, Codex CLI, GitHub CLI, and validation tools
# No Rust toolchain — this is a conversational AI project

FROM mcr.microsoft.com/devcontainers/base:ubuntu

# Install system packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    # Shell script validation
    shellcheck \
    # Python for YAML validation and scripting
    python3-pip python3-yaml \
    # Utilities
    bc curl jq tmux \
    # YAML validation
    yamllint \
    && rm -rf /var/lib/apt/lists/*

# Install actionlint for GitHub Actions workflow validation
ARG ACTIONLINT_VERSION=1.7.7
RUN set -eux; \
    archive="actionlint_${ACTIONLINT_VERSION}_linux_amd64.tar.gz"; \
    checksums="actionlint_${ACTIONLINT_VERSION}_checksums.txt"; \
    base_url="https://github.com/rhysd/actionlint/releases/download/v${ACTIONLINT_VERSION}"; \
    tmp_dir="$(mktemp -d)"; \
    curl -fsSL "${base_url}/${archive}" -o "${tmp_dir}/${archive}"; \
    curl -fsSL "${base_url}/${checksums}" -o "${tmp_dir}/${checksums}"; \
    (cd "${tmp_dir}" && grep " ${archive}$" "${checksums}" | sha256sum -c -); \
    tar -xzf "${tmp_dir}/${archive}" -C /usr/local/bin actionlint; \
    rm -rf "${tmp_dir}"

# Install uv, which provisions the project's Python toolchain in post-create.
# uv (not the base image's python3) owns the interpreter version: pyproject.toml
# requires >=3.12 and the base image's system Python is not pinned to that.
ARG UV_VERSION=0.11.14
RUN set -eux; \
    archive="uv-x86_64-unknown-linux-gnu.tar.gz"; \
    base_url="https://github.com/astral-sh/uv/releases/download/${UV_VERSION}"; \
    tmp_dir="$(mktemp -d)"; \
    curl -fsSL "${base_url}/${archive}" -o "${tmp_dir}/${archive}"; \
    curl -fsSL "${base_url}/${archive}.sha256" -o "${tmp_dir}/${archive}.sha256"; \
    (cd "${tmp_dir}" && sha256sum -c "${archive}.sha256"); \
    tar -xzf "${tmp_dir}/${archive}" -C "${tmp_dir}"; \
    install -m 0755 "${tmp_dir}/uv-x86_64-unknown-linux-gnu/uv" /usr/local/bin/uv; \
    install -m 0755 "${tmp_dir}/uv-x86_64-unknown-linux-gnu/uvx" /usr/local/bin/uvx; \
    rm -rf "${tmp_dir}"

# Install GitHub CLI
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | tee /etc/apt/sources.list.d/github-cli.list > /dev/null \
    && apt-get update \
    && apt-get install -y gh \
    && rm -rf /var/lib/apt/lists/*

# Pin AI CLI versions so Dockerfile, post-create script, and updater workflow stay in sync
ARG CLAUDE_CODE_VERSION=2.1.167
ARG CODEX_CLI_VERSION=0.137.0

# Install Node.js LTS (needed for Claude Code plugins and Mermaid validation)
ARG NODE_VERSION=20
RUN curl -fsSL https://deb.nodesource.com/setup_${NODE_VERSION}.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# Install Mermaid validation dependencies (used by scripts/validate-mermaid.sh)
RUN npm install -g mermaid@11 jsdom@25 dompurify@3

# Create directories for vscode user with correct ownership
RUN mkdir -p /home/vscode/.claude/tmp \
    && chown -R vscode:vscode /home/vscode/.claude

# Switch to vscode user for CLI installation
USER vscode
WORKDIR /home/vscode

# Ensure the native CLI installers land on PATH for subsequent RUN steps
ENV PATH=/home/vscode/.local/bin:$PATH

# Set TMPDIR to avoid EXDEV errors during plugin installation
ENV TMPDIR=/home/vscode/.claude/tmp

# Install Claude Code via the official installer (respects CLAUDE_CODE_VERSION)
RUN curl -fsSL --retry 5 --retry-delay 2 --retry-connrefused https://claude.ai/install.sh \
    | bash -s -- ${CLAUDE_CODE_VERSION}

# Pre-configure Claude Code for headless CI use
# Without hasCompletedOnboarding, Claude enters onboarding wizard in --print mode
RUN echo '{"hasCompletedOnboarding": true}' > /home/vscode/.claude.json

# Install Codex CLI (baked into image to avoid runtime download on every CI run)
RUN curl -fsSL --retry 5 --retry-delay 2 --retry-connrefused \
    "https://github.com/openai/codex/releases/download/rust-v${CODEX_CLI_VERSION}/install.sh" \
    | sh -s -- --release "${CODEX_CLI_VERSION}"

# Switch back to root so onCreateCommand can create users dynamically.
# The devcontainer's remoteUser setting ensures all commands run as the correct
# non-root user.
USER root
