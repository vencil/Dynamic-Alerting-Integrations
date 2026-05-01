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
    # text=True alone uses the locale codec, which on Windows cp950 will
    # crash when the tool prints non-cp950 bytes (e.g. em-dash U+2014 in
    # error messages). Pass encoding="utf-8" explicitly — same pattern
    # codified in testing-playbook.md §v2.8.0 LL #2.
    return subprocess.run(  # subprocess-timeout: ignore
        [sys.executable, str(_SCRIPT), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=_REPO_ROOT,
    )


def test_cli_check_commit_msg_good(tmp_path: Path) -> None:
    msg = tmp_path / "m.txt"
    msg.write_text("feat(resilience): new tool\n\nbody\n")
    proc = _run_cli("--check-commit-msg", str(msg))
    assert proc.returncode == 0, f"stderr={proc.stderr}"


def test_cli_check_commit_msg_bad(tmp_path) -> None:
    # Reuse the real repo's .commitlintrc.yaml via cwd=_REPO_ROOT.
    # Write to a real tmp_path file instead of /dev/stdin — the tool
    # reads by path via Path.read_text(), and /dev/stdin only exists
    # on POSIX systems. PR #51 S#21 self-review caught this Windows
    # test failure; see dx-tooling-backlog.md.
    msg = tmp_path / "bad_msg.txt"
    msg.write_text("blam(unknown-scope): bogus\n", encoding="utf-8")
    proc = subprocess.run(  # subprocess-timeout: ignore
        [
            sys.executable,
            str(_SCRIPT),
            "--check-commit-msg",
            str(msg),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
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


# ---------------------------------------------------------------------------
# v2.8.0 Issue #53: body/footer line-length validation
# ---------------------------------------------------------------------------


def test_validate_body_catches_long_line() -> None:
    """Post-header line > 100 chars should produce [E] error entry."""
    mod = _load_module()
    long_line = "x" * 101
    lines = [
        "feat(dx): header ok",
        "",
        "Body paragraph intro.",
        long_line,
    ]
    findings = mod.validate_commit_msg_body(lines)
    errs = [f for f in findings if f.startswith("[E]")]
    assert any("too long" in f and "101 chars" in f for f in errs)


def test_validate_body_exactly_100_is_ok() -> None:
    """Boundary: 100 chars exact is allowed (max is inclusive)."""
    mod = _load_module()
    exactly_100 = "x" * 100
    lines = [
        "feat(dx): header ok",
        "",
        exactly_100,
    ]
    findings = mod.validate_commit_msg_body(lines)
    errs = [f for f in findings if f.startswith("[E]")]
    assert errs == [], f"expected no errors, got {errs}"


def test_validate_body_warn_missing_leading_blank() -> None:
    """Header immediately followed by body line (no blank) → warning."""
    mod = _load_module()
    lines = [
        "feat(dx): header",
        "Body line with no blank between",
    ]
    findings = mod.validate_commit_msg_body(lines)
    warns = [f for f in findings if f.startswith("[W]")]
    assert any("body-leading-blank" in f for f in warns)


def test_validate_body_blank_line_before_body_ok() -> None:
    """Header → blank → body is the conventional shape, no warning."""
    mod = _load_module()
    lines = [
        "feat(dx): header",
        "",
        "Body line normal length.",
    ]
    findings = mod.validate_commit_msg_body(lines)
    warns = [f for f in findings if f.startswith("[W]")]
    assert not any("body-leading-blank" in f for f in warns)


def test_validate_body_ignores_comment_lines() -> None:
    """Git comment lines (# prefix) must not trigger line-length."""
    mod = _load_module()
    lines = [
        "feat(dx): header",
        "",
        "# " + ("x" * 200),  # comment should be skipped entirely
        "Normal body.",
    ]
    findings = mod.validate_commit_msg_body(lines)
    errs = [f for f in findings if f.startswith("[E]")]
    assert errs == [], f"expected no errors for comment-only long line, got {errs}"


def test_validate_body_custom_max_length() -> None:
    """validate_commit_msg_body takes max_line_length override."""
    mod = _load_module()
    lines = [
        "feat(dx): header",
        "",
        "x" * 75,
    ]
    findings = mod.validate_commit_msg_body(lines, max_line_length=50)
    errs = [f for f in findings if f.startswith("[E]")]
    assert any("75 chars > 50" in f for f in errs)


def test_validate_body_empty_message_no_errors() -> None:
    """Empty / comment-only message returns no findings."""
    mod = _load_module()
    lines = ["", "# comment", ""]
    findings = mod.validate_commit_msg_body(lines)
    assert findings == []


def test_cli_check_commit_msg_rejects_long_body_line(tmp_path: Path) -> None:
    """End-to-end via --check-commit-msg: long body line → exit 1."""
    msg = tmp_path / "msg.txt"
    long_line = "- " + ("x" * 120)  # 122 chars
    msg.write_text(
        f"feat(dx): something\n\nBody intro normal.\n{long_line}\n",
        encoding="utf-8",
    )
    proc = _run_cli("--check-commit-msg", str(msg))
    assert proc.returncode == 1
    assert "too long" in proc.stderr
    assert "122 chars" in proc.stderr


def test_cli_check_commit_msg_accepts_short_body(tmp_path: Path) -> None:
    """Control: well-formed message passes."""
    msg = tmp_path / "msg.txt"
    msg.write_text(
        "feat(dx): new helper\n\n"
        "Body paragraph normal length (under 100 chars).\n"
        "Second line also short.\n",
        encoding="utf-8",
    )
    proc = _run_cli("--check-commit-msg", str(msg))
    assert proc.returncode == 0, f"stderr={proc.stderr}"


def test_cli_check_commit_msg_warnings_only_still_pass(tmp_path: Path) -> None:
    """Missing body-leading-blank is warning, not error. Exit 0."""
    msg = tmp_path / "msg.txt"
    msg.write_text(
        "feat(dx): header\nBody starting immediately without blank.\n",
        encoding="utf-8",
    )
    proc = _run_cli("--check-commit-msg", str(msg))
    # Should pass (0) but stderr contains warnings.
    assert proc.returncode == 0, f"stderr={proc.stderr}"
    assert "warnings" in proc.stderr or "body-leading-blank" in proc.stderr


# ---------------------------------------------------------------------------
# v2.8.0 Trap #61: BOM detection in commit-msg file
# ---------------------------------------------------------------------------


def test_detect_bom_utf8(tmp_path: Path) -> None:
    """UTF-8 BOM (EF BB BF) is flagged with actionable description."""
    mod = _load_module()
    msg = tmp_path / "msg.txt"
    msg.write_bytes(b"\xef\xbb\xbffeat(dx): header\n")
    desc = mod.detect_commit_msg_bom(msg)
    assert desc is not None
    assert "UTF-8 BOM" in desc
    assert "EF BB BF" in desc


def test_detect_bom_utf16_le(tmp_path: Path) -> None:
    """UTF-16 LE BOM (FF FE) — PS default Out-File encoding."""
    mod = _load_module()
    msg = tmp_path / "msg.txt"
    msg.write_bytes(b"\xff\xfefeat")
    desc = mod.detect_commit_msg_bom(msg)
    assert desc is not None
    assert "UTF-16 LE" in desc
    assert "FF FE" in desc


def test_detect_bom_utf16_be(tmp_path: Path) -> None:
    mod = _load_module()
    msg = tmp_path / "msg.txt"
    msg.write_bytes(b"\xfe\xfffeat")
    desc = mod.detect_commit_msg_bom(msg)
    assert desc is not None
    assert "UTF-16 BE" in desc


def test_detect_bom_clean_utf8_is_none(tmp_path: Path) -> None:
    """No BOM → None (the common case, no false positives)."""
    mod = _load_module()
    msg = tmp_path / "msg.txt"
    msg.write_text("feat(dx): header\n\nclean body\n", encoding="utf-8")
    assert mod.detect_commit_msg_bom(msg) is None


def test_detect_bom_cjk_without_bom_is_none(tmp_path: Path) -> None:
    """CJK content without BOM must not trip the detector."""
    mod = _load_module()
    msg = tmp_path / "msg.txt"
    msg.write_text("feat(dx): 中文 header\n\n中文 body\n", encoding="utf-8")
    assert mod.detect_commit_msg_bom(msg) is None


def test_detect_bom_empty_file_is_none(tmp_path: Path) -> None:
    """Empty file → None (no bytes to check)."""
    mod = _load_module()
    msg = tmp_path / "msg.txt"
    msg.write_bytes(b"")
    assert mod.detect_commit_msg_bom(msg) is None


def test_detect_bom_missing_file_is_none(tmp_path: Path) -> None:
    """Missing file → None (OSError caught; absence != BOM)."""
    mod = _load_module()
    assert mod.detect_commit_msg_bom(tmp_path / "does-not-exist") is None


def test_cli_rejects_utf8_bom_message(tmp_path: Path) -> None:
    """End-to-end: PS-style UTF-8 BOM commit message → exit 1 with recovery hint."""
    msg = tmp_path / "msg.txt"
    # Valid header content — but with a BOM that PS Out-File would prepend.
    msg.write_bytes(b"\xef\xbb\xbffeat(dx): valid header otherwise\n")
    proc = _run_cli("--check-commit-msg", str(msg))
    assert proc.returncode == 1
    assert "encoding error" in proc.stderr
    assert "UTF-8 BOM" in proc.stderr
    # Recovery hint must mention the bash/PS no-BOM incantation.
    assert "UTF8Encoding" in proc.stderr or "filter-branch" in proc.stderr


def test_cli_rejects_utf16_le_bom_message(tmp_path: Path) -> None:
    """UTF-16 LE BOM (most common PS default) → exit 1."""
    msg = tmp_path / "msg.txt"
    msg.write_bytes(b"\xff\xfefeat(dx): header")
    proc = _run_cli("--check-commit-msg", str(msg))
    assert proc.returncode == 1
    assert "UTF-16 LE" in proc.stderr


def test_cli_bom_check_precedes_body_validation(tmp_path: Path) -> None:
    """If a BOM is present, we report the BOM error — not a confusing header-format error.

    Before Trap #61 wiring, a BOM'd file would fail as 'type empty' or similar
    cascade because U+FEFF is the first char of what we parse as the header.
    The BOM-first check must emit the actionable encoding error instead.
    """
    msg = tmp_path / "msg.txt"
    # UTF-8 BOM + otherwise-valid header.
    msg.write_bytes(b"\xef\xbb\xbffeat(dx): valid header\n")
    proc = _run_cli("--check-commit-msg", str(msg))
    assert proc.returncode == 1
    assert "encoding error" in proc.stderr
    # The conventional-commits cascade errors must NOT be emitted — BOM is
    # handled first with a targeted diagnostic.
    assert "type '" not in proc.stderr
    assert "subject is empty" not in proc.stderr
