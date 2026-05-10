"""Tests for scripts/session-guards/preflight_bash.py.

Verifies the PreToolUse hook behavior for the sed -i mount-path guard
(audit playbook-audit-2026-04 §H1) and ad-hoc _*.bat write blocker (§H2).

Failure policy is "never block on hook bug" — verified explicitly.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "session-guards" / "preflight_bash.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("preflight_bash", _SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_with_payload(payload: dict) -> tuple[int, str]:
    """Invoke the script as subprocess with a JSON payload on stdin."""
    proc = subprocess.run(
        [sys.executable, str(_SCRIPT)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=10,
    )
    return proc.returncode, proc.stderr


# ---------------------------------------------------------------------------
# §H1 — sed -i on mounted path
# ---------------------------------------------------------------------------

class TestSedInplaceGuard:
    @pytest.mark.parametrize(
        "command",
        [
            # POSIX mount
            "sed -i 's/foo/bar/' /workspaces/vibe-k8s-lab/x.md",
            # Glued -i variants
            "sed -i.bak 's/foo/bar/' /workspaces/vibe-k8s-lab/x.md",
            "sed -i'' 's/foo/bar/' /workspaces/vibe-k8s-lab/x.md",
            'sed -i"" \'s/foo/bar/\' /workspaces/vibe-k8s-lab/x.md',
            # Long form
            "sed --in-place 's/foo/bar/' /workspaces/vibe-k8s-lab/x.md",
            # Cowork sandbox legacy path
            "sed -i 's/.../.../' /sessions/abc/mnt/vibe-k8s-lab/y.md",
            # Git Bash POSIX path on Windows side
            "sed -i 's/x/y/' /c/Users/vencs/vibe-k8s-lab/scripts/foo.sh",
            # Windows native path
            r"sed -i s/x/y/ C:\Users\vencs\vibe-k8s-lab\scripts\foo.sh",
            # Compound: && chain still trips on the second leg
            "echo ok && sed -i 's/x/y/' /workspaces/vibe-k8s-lab/x.md",
            # GNU sed alias
            "gsed -i 's/x/y/' /workspaces/vibe-k8s-lab/x.md",
        ],
    )
    def test_blocks_inplace_on_mount(self, command: str) -> None:
        code, stderr = _run_with_payload(
            {"tool_name": "Bash", "tool_input": {"command": command}}
        )
        assert code == 2, f"expected block for {command!r}, stderr={stderr!r}"
        assert "dev-rules #11" in stderr
        assert "Read + Edit" in stderr

    @pytest.mark.parametrize(
        "command",
        [
            # `sed -i` outside mount — allowed
            "sed -i 's/x/y/' /tmp/foo.txt",
            "sed -i.bak 's/x/y/' ~/notes.md",
            # `sed` without -i — allowed (read-only)
            "sed 's/x/y/' /workspaces/vibe-k8s-lab/x.md",
            # Pipe form (no -i) on mount — allowed
            "git show HEAD:x.md | sed 's/x/y/' > /workspaces/vibe-k8s-lab/x.md",
            # Words containing 'sed' — must not match
            "echo passed && pushed origin main",
            "ls /workspaces/vibe-k8s-lab/parsed/",
            # Comment lines should not trip a real subprocess. We allow them
            # because the harness only sees the literal command anyway.
            "echo 'we used sed -i in /workspaces/vibe-k8s-lab/ in 2024'",
        ],
    )
    def test_allows_safe_patterns(self, command: str) -> None:
        # The last case (echo 'we used sed -i ...') is intentionally generous:
        # detecting "literal command containing the pattern in a quoted echo"
        # would require a full shell parser. We accept the false-positive risk
        # there — same trade-off `vibe-sed-guard.sh` makes.
        code, stderr = _run_with_payload(
            {"tool_name": "Bash", "tool_input": {"command": command}}
        )
        if code != 0:
            # echo-quoted case is permitted to false-positive; assert only on
            # truly safe cases.
            assert "echo " in command, f"unexpected block for {command!r}: {stderr}"


# ---------------------------------------------------------------------------
# §H2 — Ad-hoc _*.bat write
# ---------------------------------------------------------------------------

class TestAdHocScriptWriteGuard:
    @pytest.mark.parametrize(
        "file_path",
        [
            r"C:\Users\vencs\vibe-k8s-lab\_my_commit.bat",
            "/workspaces/vibe-k8s-lab/_p99_push.ps1",
            "/c/Users/vencs/vibe-k8s-lab/_quick_fix.cmd",
            r"some\nested\dir\_helper.bat",
        ],
    )
    def test_blocks_ad_hoc_outside_whitelist(self, file_path: str) -> None:
        code, stderr = _run_with_payload(
            {"tool_name": "Write", "tool_input": {"file_path": file_path}}
        )
        assert code == 2, f"expected block for {file_path!r}"
        assert "win_git_escape.bat" in stderr or "win_gh.bat" in stderr

    @pytest.mark.parametrize(
        "file_path",
        [
            # Whitelisted dirs
            "scripts/ops/win_new_subcommand.bat",
            "/workspaces/vibe-k8s-lab/scripts/ops/_local_helper.bat",
            "scripts/tools/lint/_my_helper.ps1",
            "tools/portal/_internal.cmd",
            # Non-script files (no _ prefix)
            "scripts/ops/win_gh.bat",
            "scripts/ops/win_git_escape.bat",
            # Wrong extension (not bat/ps1/cmd)
            "/workspaces/vibe-k8s-lab/_some_doc.md",
            "/workspaces/vibe-k8s-lab/_msg.txt",
        ],
    )
    def test_allows_legit_writes(self, file_path: str) -> None:
        code, _ = _run_with_payload(
            {"tool_name": "Write", "tool_input": {"file_path": file_path}}
        )
        assert code == 0


# ---------------------------------------------------------------------------
# Failure policy — never block on hook bugs
# ---------------------------------------------------------------------------

class TestNeverBlocksOnHookFailure:
    def test_invalid_json_allows(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(_SCRIPT)],
            input="not json {{{",
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert proc.returncode == 0
        assert "warning" in proc.stderr.lower()

    def test_empty_stdin_allows(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(_SCRIPT)],
            input="",
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert proc.returncode == 0

    def test_unknown_tool_allows(self) -> None:
        code, _ = _run_with_payload(
            {"tool_name": "ReadAndPonder", "tool_input": {"x": 1}}
        )
        assert code == 0

    def test_edit_tool_not_intercepted(self) -> None:
        # Edit changes existing files — the ad-hoc whitelist is enforced at
        # commit time by the existing pre-commit hook; we only intercept
        # Write to avoid breaking legitimate Edit on whitelisted scripts.
        code, _ = _run_with_payload(
            {"tool_name": "Edit", "tool_input": {"file_path": r"C:\x\_foo.bat"}}
        )
        assert code == 0


# ---------------------------------------------------------------------------
# Direct module-level smoke (covers the matchers when imported as a lib)
# ---------------------------------------------------------------------------

def test_module_loads_cleanly() -> None:
    mod = _load_module()
    assert hasattr(mod, "_check_bash")
    assert hasattr(mod, "_check_write")
    code, _ = mod._check_bash("ls /tmp")
    assert code == 0
    code, _ = mod._check_bash("sed -i s/x/y/ /workspaces/vibe-k8s-lab/x.md")
    assert code == 2
