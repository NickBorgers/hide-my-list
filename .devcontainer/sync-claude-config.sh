#!/bin/bash
# Place the developer's host Claude Code configuration inside the container.
#
# devcontainer.json provides two bind mounts of the host ~/.claude:
#
#   CLAUDE_HOST_CONFIG_DIR   the whole directory, READ-ONLY. Everything that
#                            can cause execution lives here — settings.json,
#                            hooks/, plugins/, CLAUDE.md. Repo-controlled code
#                            runs in this container, so nothing in it may be
#                            able to write a hook the host agent later runs.
#   CLAUDE_HOST_PROJECTS_DIR ~/.claude/projects only, WRITABLE. This holds
#                            per-project state: memory, sessions, plans. Claude
#                            Code writes there while it works, so a read-only
#                            mount fails every one of those writes. These are
#                            data files, not executable configuration.
#
# Three placement strategies follow from that split:
#
#   LINK_ITEMS  — symlinked out of the read-only mount, so host edits show up
#                 in the container immediately and stay unwritable.
#   COPY_ITEMS  — carry runtime state (caches, installed plugin checkouts) that
#                 a read-only symlink would make unwritable. Copied instead;
#                 container-side changes do not flow back to the host.
#   projects    — symlinked into the writable mount, so memory written inside
#                 the container persists to the host.
#
# Project state is stored under a directory named after the working directory,
# with every non-alphanumeric character replaced by "-". devcontainer.json
# mounts the repo at its host path, so the host and the container derive the
# same name and share one project identity.
#
# Every step is a no-op when its source is missing, so a contributor without a
# host ~/.claude (or a CI runner with an empty mount) is unaffected.
#
# Inputs (all environment variables, so tests can point them at fixtures):
#   CLAUDE_HOST_CONFIG_DIR   read-only mount of the host ~/.claude
#   CLAUDE_HOST_PROJECTS_DIR writable mount of the host ~/.claude/projects
#   CONTAINER_REPO_ROOT      repo path inside the container
#   HOME                     container user's home
set -euo pipefail

LINK_ITEMS=(CLAUDE.md settings.json hooks agents skills output-styles)
COPY_ITEMS=(plugins)

CONTAINER_CLAUDE_DIR="$HOME/.claude"

# Claude Code's project-directory name: every character that is not a letter
# or a digit becomes "-". /home/dev/code/repo → -home-dev-code-repo
slugify_path() {
  printf '%s' "$1" | sed 's/[^a-zA-Z0-9]/-/g'
}

# Remove a path that is a real file or directory, so a symlink can take its
# place. A path that is already a symlink is left for `ln -sfn` to replace.
clear_non_symlink() {
  local path=$1
  if [ -e "$path" ] && [ ! -L "$path" ]; then
    rm -rf "$path"
  fi
}

sync_config_items() {
  local host_dir=$1

  mkdir -p "$CONTAINER_CLAUDE_DIR"

  local item src
  for item in "${LINK_ITEMS[@]}"; do
    src="$host_dir/$item"
    if [ -e "$src" ]; then
      # `ln -sfn` into an existing real directory creates the link *inside*
      # it and still exits 0, which would leave the image's own hooks/ or
      # agents/ in place while reporting success. Clear the path first.
      clear_non_symlink "$CONTAINER_CLAUDE_DIR/$item"
      ln -sfn "$src" "$CONTAINER_CLAUDE_DIR/$item"
      echo "Linked host Claude Code $item from $src"
    fi
  done

  for item in "${COPY_ITEMS[@]}"; do
    src="$host_dir/$item"
    if [ -e "$src" ]; then
      # Drop any stale symlink from an earlier revision of this script first,
      # otherwise cp would try to write through it into the read-only mount.
      [ -L "$CONTAINER_CLAUDE_DIR/$item" ] && rm -f "$CONTAINER_CLAUDE_DIR/$item"
      rm -rf "${CONTAINER_CLAUDE_DIR:?}/$item"
      cp -r "$src" "$CONTAINER_CLAUDE_DIR/$item"
      echo "Copied host Claude Code $item from $src"
    fi
  done
}

link_project_state() {
  local projects_dir=$1 container_repo_root=$2

  if [ ! -d "$projects_dir" ]; then
    return 0
  fi

  mkdir -p "$CONTAINER_CLAUDE_DIR/projects"

  local slug link
  slug="$(slugify_path "$container_repo_root")"
  link="$CONTAINER_CLAUDE_DIR/projects/$slug"

  mkdir -p "$projects_dir/$slug"

  # A real directory here holds state Claude Code wrote before the link
  # existed. Carry it into the host mount rather than dropping it, then clear
  # the path so the symlink can take it. `-n` keeps the host's copy of any
  # file that exists on both sides.
  if [ -d "$link" ] && [ ! -L "$link" ]; then
    cp -rn "$link/." "$projects_dir/$slug/" 2>/dev/null || true
    echo "Merged container-local project state into $projects_dir/$slug"
  fi
  clear_non_symlink "$link"

  ln -sfn "$projects_dir/$slug" "$link"
  echo "Linked project state $link to the writable host mount"
}

main() {
  local host_dir="${CLAUDE_HOST_CONFIG_DIR:-}"

  if [ -z "$host_dir" ] || [ ! -d "$host_dir" ]; then
    echo "Note: no host Claude Code config mounted; skipping."
    return 0
  fi
  if [ "$host_dir" = "$CONTAINER_CLAUDE_DIR" ]; then
    echo "Note: host Claude Code config is already the container config; skipping."
    return 0
  fi

  sync_config_items "$host_dir"
  link_project_state \
    "${CLAUDE_HOST_PROJECTS_DIR:-}" \
    "${CONTAINER_REPO_ROOT:-$PWD}"
}

main "$@"
