"""run_hooks_sandbox.sh (the win-commit hook gate) never claims a finding (#3634).

pre-commit ends a hook's finding and a hook that could not run (it exited
2/3/127, its executable is missing) alike at 1, so the gate cannot prove
"change the content": any failure is 2 (owner ruling 2026-11-13). Driven
through the real script and the real pre-commit, on a toy repository whose
only hook is a local `system` one.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = REPO_ROOT / "scripts" / "ops" / "run_hooks_sandbox.sh"

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="bash script")


def _toy(tmp_path: Path, entry: str) -> Path:
    """A repository with the gate at scripts/ops/ (it finds the root two
    levels up) and one local hook running *entry*."""
    root = tmp_path / "repo"
    (root / "scripts" / "ops").mkdir(parents=True)
    shutil.copy(_SCRIPT, root / "scripts" / "ops" / "run_hooks_sandbox.sh")
    (root / ".pre-commit-config.yaml").write_text(
        "repos:\n- repo: local\n  hooks:\n  - id: toy\n    name: toy\n"
        f"    entry: {entry}\n    language: system\n    pass_filenames: false\n",
        encoding="utf-8")
    (root / "f.txt").write_text("x\n", encoding="utf-8")
    for cmd in (["git", "init", "-q"], ["git", "add", "-A"]):
        subprocess.run(cmd, cwd=root, check=True, timeout=60)
    return root


def _gate(root: Path) -> subprocess.CompletedProcess:
    if subprocess.run([sys.executable, "-m", "pre_commit", "--version"],
                      capture_output=True, timeout=60).returncode != 0:
        pytest.skip("pre-commit is not installed")
    env = {**os.environ, "PRECOMMIT_LOG": str(root.parent / "hooks.log"),
           "PRE_COMMIT_HOME": str(root.parent / "pc-home")}
    bash = shutil.which("bash") or pytest.skip("bash is not on PATH")
    return subprocess.run([bash, str(root / "scripts" / "ops" / "run_hooks_sandbox.sh"),
                           "f.txt"], cwd=root, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=300, env=env)


@pytest.mark.parametrize("entry", ["sh -c 'exit 1'", "sh -c 'exit 2'", "sh -c 'exit 127'",
                                   "/nonexistent/checker"])
def test_any_hook_failure_is_2(tmp_path, entry):
    proc = _gate(_toy(tmp_path, entry))
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "HOOKS STATUS=FAIL FILES=1" in proc.stderr


def test_passing_hooks_are_0(tmp_path):
    proc = _gate(_toy(tmp_path, "sh -c 'exit 0'"))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "HOOKS STATUS=PASS FILES=1" in proc.stdout
