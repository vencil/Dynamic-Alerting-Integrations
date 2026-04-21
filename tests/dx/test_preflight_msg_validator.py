"""Tests for the conventional-commits validator added to pr_preflight.py (PR #44 C2).

Covers:
  - CONVENTIONAL_HEADER_RE parses valid forms
  - validate_conventional_header: type/scope/subject/length checks
  - _read_commitlint_enum: parses .commitlintrc.yaml type-enum and scope-enum
  - CLI: --check-commit-msg on good/bad files
  - CLI: --check-pr-title on good/bad/too-long titles
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "tools" / "dx" / "pr_preflight.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("pr_preflight", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# validate_conventional_header
# ---------------------------------------------------------------------------


def test_validator_accepts_type_only() -> None:
    mod = _load_module()
    errs = mod.validate_conventional_header(
        "feat: add thing", type_enum=["feat"], scope_enum=["dx"]
    )
    assert errs == []


def test_validator_accepts_type_scope() -> None:
    mod = _load_module()
    errs = mod.validate_conventional_header(
        "fix(ci): flaky test", type_enum=["fix"], scope_enum=["ci"]
    )
    assert errs == []


def test_validator_accepts_breaking_change_bang() -> None:
    mod = _load_module()
    errs = mod.validate_conventional_header(
        "feat(dx)!: breaking API change", type_enum=["feat"], scope_enum=["dx"]
    )
    assert errs == []


def test_validator_rejects_unknown_type() -> None:
    mod = _load_module()
    errs = mod.validate_conventional_header(
        "chore: bump", type_enum=["feat", "fix"], scope_enum=None
    )
    assert any("type 'chore'" in e for e in errs)


def test_validator_rejects_unknown_scope() -> None:
    mod = _load_module()
    errs = mod.validate_conventional_header(
        "feat(bogus): thing", type_enum=["feat"], scope_enum=["dx", "ci"]
    )
    assert any("scope 'bogus'" in e for e in errs)


def test_validator_rejects_empty_subject() -> None:
    mod = _load_module()
    errs = mod.validate_conventional_header("feat: ")
    assert any("subject is empty" in e for e in errs)


def test_validator_rejects_bad_format() -> None:
    mod = _load_module()
    errs = mod.validate_conventional_header("no colon here")
    assert any("does not match conventional-commits" in e for e in errs)


def test_validator_rejects_too_long() -> None:
    mod = _load_module()
    long_subject = "x" * 200
    errs = mod.validate_conventional_header(
        f"feat: {long_subject}", max_length=70
    )
    assert any("header too long" in e for e in errs)


def test_validator_empty_string() -> None:
    mod = _load_module()
    errs = mod.validate_conventional_header("")
    assert any("empty" in e for e in errs)


# ---------------------------------------------------------------------------
# _read_commitlint_enum
# ---------------------------------------------------------------------------


def test_read_type_enum_from_real_config() -> None:
    mod = _load_module()
    enum = mod._read_commitlint_enum(_REPO_ROOT, "type-enum")
    # PR #44 C1 just added chore and revert
    assert enum is not None
    assert "feat" in enum
    assert "chore" in enum
    assert "revert" in enum


def test_read_scope_enum_from_real_config() -> None:
    mod = _load_module()
    enum = mod._read_commitlint_enum(_REPO_ROOT, "scope-enum")
    assert enum is not None
    assert "dx" in enum
    assert "config" in enum  # added by PR #44 C1
    assert "resilience" in enum  # added by PR #44 C1


def test_read_enum_returns_none_for_missing_key(tmp_path: Path) -> None:
    mod = _load_module()
    (tmp_path / ".commitlintrc.yaml").write_text("rules:\n  foo-bar:\n    - 2\n")
    enum = mod._read_commitlint_enum(tmp_path, "nonexistent-enum")
    assert enum is None


def test_read_enum_ignores_level_and_always(tmp_path: Path) -> None:
    """The parser must skip the first two list entries (level and always)."""
    mod = _load_module()
    (tmp_path / ".commitlintrc.yaml").write_text(
        "rules:\n"
        "  type-enum:\n"
        "    - 2\n"
        "    - always\n"
        "    - - feat\n"
        "      - fix\n"
    )
    enum = mod._read_commitlint_enum(tmp_path, "type-enum")
    assert enum == ["feat", "fix"]


# ---------------------------------------------------------------------------
# CLI end-to-end
# ---------------------------------------------------------------------------


def _run_cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_SCRIPT), *args],
        capture_output=True,
        text=True,
        cwd=_REPO_ROOT,
    )


def test_cli_check_commit_msg_good(tmp_path: Path) -> None:
    msg = tmp_path / "m.txt"
    msg.write_text("feat(resilience): new tool\n\nbody\n")
    proc = _run_cli("--check-commit-msg", str(msg))
    assert proc.returncode == 0, f"stderr={proc.stderr}"


def test_cli_check_commit_msg_bad() -> None:
    # Reuse the real repo's .commitlintrc.yaml via cwd=_REPO_ROOT
    proc = subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "--check-commit-msg",
            "/dev/stdin",
        ],
        input="blam(unknown-scope): bogus\n",
        capture_output=True,
        text=True,
        cwd=_REPO_ROOT,
    )
    assert proc.returncode == 1
    assert "type 'blam'" in proc.stderr


def test_cli_check_pr_title_good() -> None:
    proc = _run_cli("--check-pr-title", "feat(resilience): bundle tooling")
    assert proc.returncode == 0, f"stderr={proc.stderr}"


def test_cli_check_pr_title_too_long() -> None:
    long = "feat: " + ("x" * 200)
    proc = _run_cli("--check-pr-title", long)
    assert proc.returncode == 1
    assert "header too long" in proc.stderr


def test_cli_check_pr_title_custom_max() -> None:
    # 50-char limit
    proc = _run_cli(
        "--check-pr-title",
        "feat: " + ("x" * 60),
        "--pr-title-max-length",
        "50",
    )
    assert proc.returncode == 1
    assert "header too long" in proc.stderr


def test_cli_check_commit_msg_missing_file(tmp_path: Path) -> None:
    proc = _run_cli("--check-commit-msg", str(tmp_path / "nope"))
    assert proc.returncode == 1
    assert "not found" in proc.stderr
