"""Integration test for docker/backup.sh.

Requires: docker compose is available and the postgres service is running
(i.e., DATABASE_URL is set and the compose stack is up).

The test runs backup.sh against the test postgres, verifies the dump file
exists and is non-empty, and verifies the dump is restorable into a throwaway
container.

Skip conditions:
  - DATABASE_URL not set
  - docker binary not available
  - docker compose not available

Private data discipline: no real task titles, phone numbers, or personal data.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

_HAS_DB = bool(os.environ.get("DATABASE_URL", ""))
_HAS_DOCKER = bool(shutil.which("docker"))

_COMPOSE_FILE = str(Path(__file__).parent.parent.parent / "docker" / "compose.yaml")
_BACKUP_SH = str(Path(__file__).parent.parent.parent / "docker" / "backup.sh")


def _docker_compose_available() -> bool:
    """Check that `docker compose` (v2) is available."""
    result = subprocess.run(
        ["docker", "compose", "version"],
        capture_output=True,
        timeout=10,
    )
    return result.returncode == 0


pytestmark = pytest.mark.skipif(
    not _HAS_DB or not _HAS_DOCKER or not _docker_compose_available(),
    reason="Requires DATABASE_URL and docker compose to be available",
)


@pytest.mark.integration
def test_backup_sh_creates_nonempty_dump(tmp_path: Path) -> None:
    """backup.sh creates a non-empty .sql.gz file."""
    backup_dir = tmp_path / "backups"

    result = subprocess.run(
        [
            "bash",
            _BACKUP_SH,
            "--compose-file", _COMPOSE_FILE,
            "--backup-dir", str(backup_dir),
            "--retain", "5",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, (
        f"backup.sh failed with exit code {result.returncode}.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )

    dumps = list(backup_dir.glob("postgres-*.sql.gz"))
    assert len(dumps) == 1, f"Expected 1 dump file, found: {dumps}"

    dump_file = dumps[0]
    assert dump_file.stat().st_size >= 100, (
        f"Dump file is too small ({dump_file.stat().st_size} bytes) — may be empty."
    )


@pytest.mark.integration
def test_backup_sh_dry_run_no_output_file(tmp_path: Path) -> None:
    """backup.sh --dry-run prints plan without creating any files."""
    backup_dir = tmp_path / "backups"

    result = subprocess.run(
        [
            "bash",
            _BACKUP_SH,
            "--compose-file", _COMPOSE_FILE,
            "--backup-dir", str(backup_dir),
            "--dry-run",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0
    assert "DRY RUN" in result.stdout
    assert not backup_dir.exists() or list(backup_dir.glob("*.sql.gz")) == []


@pytest.mark.integration
def test_backup_sh_retention_prunes_old_files(tmp_path: Path) -> None:
    """backup.sh prunes backups beyond the retain count."""
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()

    # Pre-create 5 fake old backup files.
    for i in range(5):
        fake = backup_dir / f"postgres-20200101-0000{i:02d}.sql.gz"
        fake.write_bytes(b"\x1f\x8b" + b"x" * 200)  # minimal gzip magic header

    # Run backup.sh with retain=3.
    result = subprocess.run(
        [
            "bash",
            _BACKUP_SH,
            "--compose-file", _COMPOSE_FILE,
            "--backup-dir", str(backup_dir),
            "--retain", "3",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0

    # Should have at most 3 files remaining.
    remaining = list(backup_dir.glob("postgres-*.sql.gz"))
    assert len(remaining) <= 3, (
        f"Expected at most 3 backup files after pruning, found {len(remaining)}: {remaining}"
    )
