"""Tests for PNG attachment support in app/tools/signal_client.py.

Covers:
- Valid PNG attachment: base64-encoded in base64_attachments field.
- Non-PNG extension raises ValueError.
- Nonexistent path raises ValueError.
- Path traversal ('..') raises ValueError.
- Relative path raises ValueError.
- Path outside reward_artifacts root raises ValueError.
- Multiple attachments encoded in order.

Private data discipline: attachment_path references private user data.
Test uses only tmp_path-based placeholder paths, never real content.
"""
from __future__ import annotations

import base64
import os
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_fake_png(path: Path) -> bytes:
    """Write a minimal PNG header to path and return the bytes."""
    # Minimal PNG: 8-byte signature + a handful of IDAT bytes (enough to be a file)
    fake_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
    path.write_bytes(fake_bytes)
    return fake_bytes


def _mock_httpx_response(json_data: dict[str, Any]) -> MagicMock:
    """Return a mock httpx response that returns json_data from .json()."""
    resp = MagicMock()
    resp.json.return_value = json_data
    resp.raise_for_status = MagicMock()
    return resp


# ---------------------------------------------------------------------------
# Valid PNG attachment — base64_attachments field present in request body
# ---------------------------------------------------------------------------

class TestValidPNGAttachment:
    """Valid PNG file must be base64-encoded and sent in base64_attachments."""

    @pytest.mark.asyncio
    async def test_single_png_attachment_encoded_in_request(self, tmp_path: Path) -> None:
        """A valid PNG under reward_artifacts root must appear in base64_attachments."""
        from app.tools import signal_client

        # Create a fake PNG inside a fake reward_artifacts dir
        artifacts_dir = tmp_path / "reward_artifacts"
        artifacts_dir.mkdir()
        png_path = artifacts_dir / "2026-01-01_120000_high.png"
        fake_bytes = _make_fake_png(png_path)
        expected_b64 = base64.b64encode(fake_bytes).decode("ascii")

        captured_payload: dict[str, Any] = {}

        async def fake_post(url: str, *, json: dict[str, Any]) -> MagicMock:
            captured_payload.update(json)
            return _mock_httpx_response({"timestamp": 123456})

        mock_client = AsyncMock()
        mock_client.post = fake_post

        with (
            patch.dict(os.environ, {
                "SIGNAL_ACCOUNT": "<test-account>",
                "REWARD_ARTIFACTS_DIR": str(artifacts_dir),
            }),
            patch("httpx.AsyncClient") as mock_httpx_cls,
        ):
            # Make AsyncClient work as an async context manager
            mock_httpx_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_httpx_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            await signal_client.send_message(
                recipient="<test-recipient>",
                message="Test message",
                attachment_paths=[str(png_path)],
                base_url="http://signal-cli-test:8080",
                account="<test-account>",
            )

        assert "base64_attachments" in captured_payload
        assert captured_payload["base64_attachments"] == [expected_b64]

    @pytest.mark.asyncio
    async def test_no_attachment_no_base64_field(self, tmp_path: Path) -> None:
        """When no attachment_paths provided, base64_attachments must not appear in body."""
        from app.tools import signal_client

        captured_payload: dict[str, Any] = {}

        async def fake_post(url: str, *, json: dict[str, Any]) -> MagicMock:
            captured_payload.update(json)
            return _mock_httpx_response({"timestamp": 123456})

        mock_client = AsyncMock()
        mock_client.post = fake_post

        with (
            patch.dict(os.environ, {"SIGNAL_ACCOUNT": "<test-account>"}),
            patch("httpx.AsyncClient") as mock_httpx_cls,
        ):
            mock_httpx_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_httpx_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            await signal_client.send_message(
                recipient="<test-recipient>",
                message="Test message",
                base_url="http://signal-cli-test:8080",
                account="<test-account>",
            )

        assert "base64_attachments" not in captured_payload

    @pytest.mark.asyncio
    async def test_multiple_attachments_encoded_in_order(self, tmp_path: Path) -> None:
        """Multiple PNG attachments must all appear in base64_attachments in order."""
        from app.tools import signal_client

        artifacts_dir = tmp_path / "reward_artifacts"
        artifacts_dir.mkdir()

        png1 = artifacts_dir / "first.png"
        png2 = artifacts_dir / "second.png"
        bytes1 = _make_fake_png(png1)
        bytes2 = _make_fake_png(png2)
        png2.write_bytes(bytes2 + b"\xff")  # make bytes2 distinct
        bytes2 = png2.read_bytes()

        expected_b64_1 = base64.b64encode(bytes1).decode("ascii")
        expected_b64_2 = base64.b64encode(bytes2).decode("ascii")

        captured_payload: dict[str, Any] = {}

        async def fake_post(url: str, *, json: dict[str, Any]) -> MagicMock:
            captured_payload.update(json)
            return _mock_httpx_response({"timestamp": 123456})

        mock_client = AsyncMock()
        mock_client.post = fake_post

        with (
            patch.dict(os.environ, {
                "SIGNAL_ACCOUNT": "<test-account>",
                "REWARD_ARTIFACTS_DIR": str(artifacts_dir),
            }),
            patch("httpx.AsyncClient") as mock_httpx_cls,
        ):
            mock_httpx_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_httpx_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            await signal_client.send_message(
                recipient="<test-recipient>",
                message="Test message",
                attachment_paths=[str(png1), str(png2)],
                base_url="http://signal-cli-test:8080",
                account="<test-account>",
            )

        assert captured_payload["base64_attachments"] == [expected_b64_1, expected_b64_2]


# ---------------------------------------------------------------------------
# Validation: non-PNG extension → ValueError
# ---------------------------------------------------------------------------

class TestNonPNGRejected:
    """Non-.png extensions must raise ValueError before any HTTP call."""

    def test_jpg_raises_value_error(self, tmp_path: Path) -> None:
        """JPEG file must be rejected with ValueError."""
        from app.tools.signal_client import _validate_attachment_path

        artifacts_dir = tmp_path / "reward_artifacts"
        artifacts_dir.mkdir()
        jpg_path = artifacts_dir / "image.jpg"
        jpg_path.write_bytes(b"fake-jpg")

        with patch.dict(os.environ, {"REWARD_ARTIFACTS_DIR": str(artifacts_dir)}):
            with pytest.raises(ValueError, match=r"\.png"):
                _validate_attachment_path(str(jpg_path))

    def test_gif_raises_value_error(self, tmp_path: Path) -> None:
        """GIF file must be rejected with ValueError."""
        from app.tools.signal_client import _validate_attachment_path

        artifacts_dir = tmp_path / "reward_artifacts"
        artifacts_dir.mkdir()
        gif_path = artifacts_dir / "image.gif"
        gif_path.write_bytes(b"GIF89a")

        with patch.dict(os.environ, {"REWARD_ARTIFACTS_DIR": str(artifacts_dir)}):
            with pytest.raises(ValueError, match=r"\.png"):
                _validate_attachment_path(str(gif_path))

    def test_uppercase_png_is_accepted(self, tmp_path: Path) -> None:
        """Case-insensitive: .PNG extension must be accepted."""
        from app.tools.signal_client import _validate_attachment_path

        artifacts_dir = tmp_path / "reward_artifacts"
        artifacts_dir.mkdir()
        upper_png = artifacts_dir / "IMAGE.PNG"
        upper_png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8)

        with patch.dict(os.environ, {"REWARD_ARTIFACTS_DIR": str(artifacts_dir)}):
            resolved = _validate_attachment_path(str(upper_png))
        assert resolved == upper_png.resolve()


# ---------------------------------------------------------------------------
# Validation: nonexistent path → ValueError
# ---------------------------------------------------------------------------

class TestNonexistentPathRejected:
    """Nonexistent paths must raise ValueError."""

    def test_nonexistent_png_raises(self, tmp_path: Path) -> None:
        """Path to a PNG that doesn't exist on disk must raise ValueError."""
        from app.tools.signal_client import _validate_attachment_path

        artifacts_dir = tmp_path / "reward_artifacts"
        artifacts_dir.mkdir()
        ghost_path = artifacts_dir / "ghost.png"
        # ghost_path does NOT exist

        with patch.dict(os.environ, {"REWARD_ARTIFACTS_DIR": str(artifacts_dir)}):
            with pytest.raises(ValueError, match="does not exist"):
                _validate_attachment_path(str(ghost_path))


# ---------------------------------------------------------------------------
# Validation: path traversal → ValueError
# ---------------------------------------------------------------------------

class TestPathTraversalRejected:
    """Paths containing '..' must raise ValueError before resolution."""

    def test_dotdot_in_path_raises(self) -> None:
        """'../etc/passwd' traversal must raise ValueError."""
        from app.tools.signal_client import _validate_attachment_path

        with pytest.raises(ValueError, match=r"\.\."):
            _validate_attachment_path("/tmp/reward_artifacts/../etc/passwd")

    def test_dotdot_component_raises(self, tmp_path: Path) -> None:
        """Path with '..' component must raise ValueError even if it would resolve inside root."""
        from app.tools.signal_client import _validate_attachment_path

        artifacts_dir = tmp_path / "reward_artifacts"
        artifacts_dir.mkdir()

        with patch.dict(os.environ, {"REWARD_ARTIFACTS_DIR": str(artifacts_dir)}):
            traversal = str(artifacts_dir) + "/../reward_artifacts/image.png"
            with pytest.raises(ValueError, match=r"\.\."):
                _validate_attachment_path(traversal)


# ---------------------------------------------------------------------------
# Validation: relative path → ValueError
# ---------------------------------------------------------------------------

class TestRelativePathRejected:
    """Relative paths must raise ValueError."""

    def test_relative_path_raises(self) -> None:
        """Relative path must raise ValueError."""
        from app.tools.signal_client import _validate_attachment_path

        with pytest.raises(ValueError, match="absolute"):
            _validate_attachment_path("reward_artifacts/image.png")

    def test_bare_filename_raises(self) -> None:
        """Bare filename without directory must raise ValueError."""
        from app.tools.signal_client import _validate_attachment_path

        with pytest.raises(ValueError, match="absolute"):
            _validate_attachment_path("image.png")


# ---------------------------------------------------------------------------
# Validation: path outside reward_artifacts root → ValueError
# ---------------------------------------------------------------------------

class TestOutsideRootRejected:
    """Paths outside the reward_artifacts root must raise ValueError."""

    def test_tmp_dir_outside_artifacts_raises(self, tmp_path: Path) -> None:
        """Absolute PNG path outside reward_artifacts root must raise ValueError."""
        from app.tools.signal_client import _validate_attachment_path

        artifacts_dir = tmp_path / "reward_artifacts"
        artifacts_dir.mkdir()

        # Write a real PNG outside the artifacts dir
        outside_png = tmp_path / "outside.png"
        outside_png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8)

        with patch.dict(os.environ, {"REWARD_ARTIFACTS_DIR": str(artifacts_dir)}):
            with pytest.raises(ValueError, match="outside the reward_artifacts root"):
                _validate_attachment_path(str(outside_png))

    def test_system_path_outside_artifacts_raises(self, tmp_path: Path) -> None:
        """Well-known system path must be rejected as outside reward_artifacts root."""
        from app.tools.signal_client import _validate_attachment_path

        artifacts_dir = tmp_path / "reward_artifacts"
        artifacts_dir.mkdir()

        # /tmp/etc-passwd-spoofed.png doesn't exist but path validation happens first
        with patch.dict(os.environ, {"REWARD_ARTIFACTS_DIR": str(artifacts_dir)}):
            with pytest.raises(ValueError):
                # This fails either on "does not exist" or "outside root",
                # both of which are the correct security outcome
                _validate_attachment_path("/tmp/etc-passwd-spoofed.png")
