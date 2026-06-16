"""Self-test for scripts/ops/_verify_download.sh — the CI tool-install
supply-chain guard.

Pinned contracts
----------------
1. A matching SHA-256 passes (exit 0) and leaves the artifact in place.
2. A mismatching SHA-256 fails (exit 1) AND deletes the untrusted artifact
   (so a later step can't accidentally consume a poisoned download).
3. An uppercase pinned digest still matches (workflow authors paste either case).
4. A missing file fails loudly rather than silently passing.
"""
from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_HELPER = _REPO / "scripts" / "ops" / "_verify_download.sh"

# The helper is a POSIX shell script (bash + coreutils). It is exercised for real
# on Linux CI. Skip on a Windows host: Python's subprocess resolves `bash` to the
# WSL stub (which mounts C: at /mnt/c and can't see a `C:/...` path), so it would
# false-fail rather than test the helper. Verified on Linux instead.
pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or not (shutil.which("bash") and shutil.which("sha256sum")),
    reason="POSIX helper self-test runs on Linux/CI (Windows Python uses WSL bash)",
)


def _run(file: Path, expected: str) -> subprocess.CompletedProcess:
    # Pass POSIX-style paths: Git Bash on a Windows host mangles backslashes in
    # `C:\...` (strips them as escapes); the forward-slash `C:/...` form is safe
    # on both Git Bash and Linux CI.
    return subprocess.run(
        ["bash", _HELPER.as_posix(), file.as_posix(), expected],
        capture_output=True, text=True, timeout=30,
    )


def _sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def test_helper_exists_and_is_shell():
    assert _HELPER.exists(), _HELPER
    assert _HELPER.read_text(encoding="utf-8").startswith("#!/usr/bin/env bash")


def test_matching_sha_passes_and_keeps_file(tmp_path):
    f = tmp_path / "artifact.bin"
    f.write_bytes(b"hello supply chain\n")
    r = _run(f, _sha256(f))
    assert r.returncode == 0, r.stderr
    assert f.exists()                       # verified artifact is preserved


def test_mismatch_fails_and_deletes_file(tmp_path):
    f = tmp_path / "artifact.bin"
    f.write_bytes(b"tampered payload\n")
    r = _run(f, "0" * 64)                   # wrong digest
    assert r.returncode == 1
    assert "mismatch" in r.stderr.lower()
    assert not f.exists()                   # poisoned artifact deleted


def test_uppercase_expected_digest_still_matches(tmp_path):
    f = tmp_path / "artifact.bin"
    f.write_bytes(b"case-insensitive\n")
    r = _run(f, _sha256(f).upper())
    assert r.returncode == 0, r.stderr


def test_missing_file_fails(tmp_path):
    r = _run(tmp_path / "does-not-exist.bin", "0" * 64)
    assert r.returncode == 1
    assert "not found" in r.stderr.lower()
