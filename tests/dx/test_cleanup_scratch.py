"""Tests for scripts/session-guards/cleanup_scratch.py.

Covers (audit playbook-audit-2026-04 §T2):
  - Dry-run is default; --apply needed to delete
  - Recent files (<1h old) are NEVER deleted
  - Recognised scratch patterns are matched
  - Session-init markers >24h are stale, <24h kept
  - Non-matching files are not touched
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "session-guards" / "cleanup_scratch.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("cleanup_scratch", _SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_old_file(tmp_path: Path, name: str, age_hours: float = 2.0) -> Path:
    """Create a file with mtime in the past."""
    p = tmp_path / name
    p.write_text("scratch")
    past = time.time() - age_hours * 3600
    os.utime(p, (past, past))
    return p


@pytest.fixture
def isolated_dirs(monkeypatch, tmp_path):
    """Point the scan to an isolated tmp dir."""
    mod = _load_module()
    fake_dir = tmp_path / "fake_temp"
    fake_dir.mkdir()
    monkeypatch.setattr(mod, "_scan_dirs", lambda: [fake_dir])
    return mod, fake_dir


class TestPatternMatching:
    def test_matches_known_scratch(self):
        mod = _load_module()
        for name in [
            "vibe-bat-out.txt", "vibe-git-err.txt",
            "pr1-msg.txt", "pr12-backlog-body.md", "pr5-fix-msg.txt",
            "commit-out.txt", "commit-output.txt",
            "_jsx_out.txt", "_out.txt",
            "pre-commit-final.yaml",
            "audit_open.py", "bulk_annotate_tests.py",
            "fix_encoding.py", "probe.go",
            "_backup.css", "_backup.jsx", "_msg.txt",
            "test_violations.txt",
        ]:
            assert mod._matches_scratch(name), f"should match: {name}"

    def test_does_not_match_legit_files(self):
        mod = _load_module()
        for name in [
            "important-notes.md", "config.yaml", "main.py",
            "README.md", "secret.json",
            # vibe-session-init handled separately, not matched as scratch
            "vibe-session-init.abc123",
        ]:
            assert not mod._matches_scratch(name), f"should NOT match: {name}"


class TestStaleSessionMarker:
    def test_old_marker_is_stale(self):
        mod = _load_module()
        now = time.time()
        old = now - 25 * 3600  # 25 hours ago
        assert mod._is_stale_session_marker("vibe-session-init.abc", old, now)

    def test_recent_marker_kept(self):
        mod = _load_module()
        now = time.time()
        recent = now - 6 * 3600  # 6 hours ago
        assert not mod._is_stale_session_marker(
            "vibe-session-init.abc", recent, now
        )

    def test_non_marker_not_classified(self):
        mod = _load_module()
        now = time.time()
        old = now - 25 * 3600
        assert not mod._is_stale_session_marker("foo.txt", old, now)


class TestSweepBehavior:
    def test_recent_file_never_deleted(self, isolated_dirs, capsys):
        mod, d = isolated_dirs
        # 30 minutes old — under 60min floor
        f = _make_old_file(d, "vibe-bat-out.txt", age_hours=0.5)
        # Run apply mode directly via main()
        rc = mod.main.__wrapped__() if hasattr(mod.main, "__wrapped__") else None
        # Direct subprocess to also exercise CLI parser
        proc = subprocess.run(
            [sys.executable, str(_SCRIPT), "--apply"],
            env={**os.environ, "TEMP": str(d), "TMP": str(d)},
            capture_output=True, text=True, timeout=10,
        )
        # Recent file is never returned by _candidates_in -> not deleted
        assert f.exists()
        assert "no scratch artifacts found" in proc.stdout or "0 candidate" in proc.stdout

    def test_old_scratch_listed_in_dry_run(self, isolated_dirs, monkeypatch):
        mod, d = isolated_dirs
        _make_old_file(d, "pr3-msg.txt", age_hours=3)
        _make_old_file(d, "audit_thing.py", age_hours=3)
        _make_old_file(d, "important.md", age_hours=3)
        # Patch sys.argv for argparse, run main directly
        monkeypatch.setattr(sys, "argv", ["cleanup_scratch.py"])
        rc = mod.main()
        assert rc == 0
        # Files should still exist (dry-run)
        assert (d / "pr3-msg.txt").exists()
        assert (d / "audit_thing.py").exists()
        assert (d / "important.md").exists()

    def test_apply_deletes_only_matching(self, isolated_dirs, monkeypatch):
        mod, d = isolated_dirs
        _make_old_file(d, "pr3-msg.txt", age_hours=3)
        _make_old_file(d, "audit_thing.py", age_hours=3)
        _make_old_file(d, "important.md", age_hours=3)
        monkeypatch.setattr(sys, "argv", ["cleanup_scratch.py", "--apply"])
        rc = mod.main()
        assert rc == 0
        assert not (d / "pr3-msg.txt").exists()
        assert not (d / "audit_thing.py").exists()
        # Non-matching file untouched
        assert (d / "important.md").exists()

    def test_stale_session_marker_swept(self, isolated_dirs, monkeypatch):
        mod, d = isolated_dirs
        _make_old_file(d, "vibe-session-init.OLD", age_hours=25)
        _make_old_file(d, "vibe-session-init.NEW", age_hours=6)
        monkeypatch.setattr(sys, "argv", ["cleanup_scratch.py", "--apply"])
        mod.main()
        assert not (d / "vibe-session-init.OLD").exists()
        assert (d / "vibe-session-init.NEW").exists()


class TestCLIDefaults:
    def test_dry_run_is_default(self, tmp_path, monkeypatch):
        # Create a scratch file in a controlled dir
        d = tmp_path / "fakespace"
        d.mkdir()
        _make_old_file(d, "pr1-msg.txt", age_hours=3)
        # Mock _scan_dirs at the module level for the CLI invocation
        mod = _load_module()
        monkeypatch.setattr(mod, "_scan_dirs", lambda: [d])
        monkeypatch.setattr(sys, "argv", ["cleanup_scratch.py"])
        mod.main()
        assert (d / "pr1-msg.txt").exists()  # still there, dry-run default
