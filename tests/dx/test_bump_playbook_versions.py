#!/usr/bin/env python3
"""test_bump_playbook_versions — playbook verified-at-version 寫入工具測試。

覆蓋：
  1. 正常 bump（UPDATED / OK / MISSING 三種狀態）
  2. --check 模式：exit code + 不寫檔
  3. --dry-run 模式：不寫檔 + 列印
  4. Idempotent：重跑無變化
  5. Front-matter 其他欄位保留不動
  6. CRLF / LF line ending 保留
"""

from __future__ import annotations

from pathlib import Path

import pytest

import bump_playbook_versions as bpv  # noqa: E402


# ── helpers ────────────────────────────────────────────────────────────

FRONTMATTER_TEMPLATE = """---
title: "Sample Playbook"
tags: [documentation]
audience: [all]
version: v2.7.0
verified-at-version: {verified}
lang: zh
---
# Sample

Body.
"""


def _make_playbook_tree(tmp_path: Path, verified_by_name: dict) -> Path:
    """Create a fake repo root with the 4 canonical playbooks under
    `docs/internal/` using the provided verified-at-version values.
    """
    (tmp_path / ".git").mkdir()
    internal = tmp_path / "docs" / "internal"
    internal.mkdir(parents=True)
    for rel in bpv.PLAYBOOK_PATHS:
        name = Path(rel).name
        verified = verified_by_name.get(name, "v2.7.0")
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(
            FRONTMATTER_TEMPLATE.format(verified=verified),
            encoding="utf-8",
            newline="",
        )
    return tmp_path


def _run_cli(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
    repo_root: Path,
    *args: str,
) -> tuple[int, str, str]:
    """Invoke main() in-process so coverage.py captures execution.

    Returns (exit_code, stdout, stderr). SystemExit from argparse is
    handled transparently.
    """
    monkeypatch.chdir(repo_root)
    monkeypatch.setattr(
        bpv.sys, "argv",
        ["bump_playbook_versions.py", *args],
    )
    try:
        exit_code = bpv.main()
    except SystemExit as exc:
        exit_code = int(exc.code) if exc.code is not None else 0
    captured = capsys.readouterr()
    return exit_code, captured.out, captured.err


# ── TestApplyBump ──────────────────────────────────────────────────────


class TestApplyBump:
    """Unit tests for bpv.apply_bump() — the per-file writer."""

    def test_updates_stale_version(self, tmp_path):
        f = tmp_path / "pb.md"
        f.write_text(
            FRONTMATTER_TEMPLATE.format(verified="v2.7.0"),
            encoding="utf-8",
            newline="",
        )
        status, detail = bpv.apply_bump(f, "v2.8.0", write=True)
        assert status == "UPDATED"
        assert "v2.7.0" in detail and "v2.8.0" in detail
        assert "verified-at-version: v2.8.0" in f.read_text(encoding="utf-8")

    def test_ok_when_already_at_target(self, tmp_path):
        f = tmp_path / "pb.md"
        f.write_text(
            FRONTMATTER_TEMPLATE.format(verified="v2.8.0"),
            encoding="utf-8",
            newline="",
        )
        status, _ = bpv.apply_bump(f, "v2.8.0", write=True)
        assert status == "OK"

    def test_check_mode_does_not_write(self, tmp_path):
        f = tmp_path / "pb.md"
        original = FRONTMATTER_TEMPLATE.format(verified="v2.7.0")
        f.write_text(original, encoding="utf-8", newline="")
        status, _ = bpv.apply_bump(f, "v2.8.0", write=False)
        assert status == "UPDATED"
        assert f.read_text(encoding="utf-8") == original

    def test_missing_field_reported(self, tmp_path):
        f = tmp_path / "pb.md"
        f.write_text(
            "---\ntitle: x\nversion: v2.7.0\n---\nbody\n",
            encoding="utf-8",
            newline="",
        )
        status, _ = bpv.apply_bump(f, "v2.8.0", write=True)
        assert status == "MISSING"

    def test_no_frontmatter_reported(self, tmp_path):
        f = tmp_path / "pb.md"
        f.write_text("# Heading\n\nbody\n", encoding="utf-8", newline="")
        status, _ = bpv.apply_bump(f, "v2.8.0", write=True)
        assert status == "MISSING"

    def test_preserves_other_frontmatter_keys(self, tmp_path):
        f = tmp_path / "pb.md"
        f.write_text(
            FRONTMATTER_TEMPLATE.format(verified="v2.7.0"),
            encoding="utf-8",
            newline="",
        )
        bpv.apply_bump(f, "v2.8.0", write=True)
        text = f.read_text(encoding="utf-8")
        assert "title: \"Sample Playbook\"" in text
        assert "version: v2.7.0" in text
        assert "lang: zh" in text

    def test_preserves_lf_line_endings(self, tmp_path):
        f = tmp_path / "pb.md"
        content = FRONTMATTER_TEMPLATE.format(verified="v2.7.0")
        f.write_text(content, encoding="utf-8", newline="")
        bpv.apply_bump(f, "v2.8.0", write=True)
        raw = f.read_bytes()
        assert b"\r\n" not in raw
        assert b"\n" in raw

    def test_preserves_crlf_line_endings(self, tmp_path):
        f = tmp_path / "pb.md"
        content = FRONTMATTER_TEMPLATE.format(verified="v2.7.0").replace(
            "\n", "\r\n"
        )
        f.write_bytes(content.encode("utf-8"))
        bpv.apply_bump(f, "v2.8.0", write=True)
        raw = f.read_bytes()
        # The updated verified-at-version line must end with CRLF as well.
        assert b"verified-at-version: v2.8.0\r\n" in raw

    def test_idempotent(self, tmp_path):
        f = tmp_path / "pb.md"
        f.write_text(
            FRONTMATTER_TEMPLATE.format(verified="v2.7.0"),
            encoding="utf-8",
            newline="",
        )
        bpv.apply_bump(f, "v2.8.0", write=True)
        status, _ = bpv.apply_bump(f, "v2.8.0", write=True)
        assert status == "OK"


# ── TestCLI ────────────────────────────────────────────────────────────


class TestCLI:
    """In-process main() exercises — covers CLI surface + argparse branches."""

    def test_bumps_all_four_playbooks(self, tmp_path, monkeypatch, capsys):
        repo = _make_playbook_tree(tmp_path, {})
        exit_code, stdout, _ = _run_cli(
            monkeypatch, capsys, repo, "--to", "v2.8.0"
        )
        assert exit_code == 0
        assert "Bumped 4 playbook(s)" in stdout
        for rel in bpv.PLAYBOOK_PATHS:
            text = (repo / rel).read_text(encoding="utf-8")
            assert "verified-at-version: v2.8.0" in text

    def test_check_passes_when_clean(self, tmp_path, monkeypatch, capsys):
        repo = _make_playbook_tree(
            tmp_path,
            {Path(rel).name: "v2.8.0" for rel in bpv.PLAYBOOK_PATHS},
        )
        exit_code, stdout, _ = _run_cli(
            monkeypatch, capsys, repo, "--to", "v2.8.0", "--check"
        )
        assert exit_code == 0
        assert "All 4 playbooks at v2.8.0" in stdout

    def test_check_fails_when_stale(self, tmp_path, monkeypatch, capsys):
        repo = _make_playbook_tree(
            tmp_path,
            {Path(rel).name: "v2.7.0" for rel in bpv.PLAYBOOK_PATHS},
        )
        exit_code, _, stderr = _run_cli(
            monkeypatch, capsys, repo, "--to", "v2.8.0", "--check"
        )
        assert exit_code == 1
        assert "4 playbook(s) need bump" in stderr

    def test_check_fails_when_file_missing_field(
        self, tmp_path, monkeypatch, capsys
    ):
        """--check should exit 1 not only on stale but also on missing
        verified-at-version field (maintainer visibility).
        """
        repo = _make_playbook_tree(tmp_path, {})
        # Strip the field from one playbook to simulate a rot state.
        target = repo / bpv.PLAYBOOK_PATHS[0]
        target.write_text(
            "---\ntitle: x\nversion: v2.7.0\n---\nbody\n",
            encoding="utf-8",
            newline="",
        )
        exit_code, _, stderr = _run_cli(
            monkeypatch, capsys, repo, "--to", "v2.7.0", "--check"
        )
        assert exit_code == 1
        assert "missing field" in stderr

    def test_dry_run_no_write(self, tmp_path, monkeypatch, capsys):
        repo = _make_playbook_tree(
            tmp_path,
            {Path(rel).name: "v2.7.0" for rel in bpv.PLAYBOOK_PATHS},
        )
        before = (repo / bpv.PLAYBOOK_PATHS[0]).read_text(encoding="utf-8")
        exit_code, stdout, _ = _run_cli(
            monkeypatch, capsys, repo, "--to", "v2.8.0", "--dry-run"
        )
        assert exit_code == 0
        assert "would update" in stdout
        after = (repo / bpv.PLAYBOOK_PATHS[0]).read_text(encoding="utf-8")
        assert before == after

    def test_rejects_bad_version_format(
        self, tmp_path, monkeypatch, capsys
    ):
        repo = _make_playbook_tree(tmp_path, {})
        exit_code, _, stderr = _run_cli(
            monkeypatch, capsys, repo, "--to", "2.8", "--check"
        )
        assert exit_code == 2
        assert "must match vX.Y.Z" in stderr

    def test_accepts_version_without_v_prefix(
        self, tmp_path, monkeypatch, capsys
    ):
        repo = _make_playbook_tree(
            tmp_path,
            {Path(rel).name: "v2.7.0" for rel in bpv.PLAYBOOK_PATHS},
        )
        exit_code, _, _ = _run_cli(
            monkeypatch, capsys, repo, "--to", "2.8.0"
        )
        assert exit_code == 0
        text = (repo / bpv.PLAYBOOK_PATHS[0]).read_text(encoding="utf-8")
        assert "verified-at-version: v2.8.0" in text

    def test_reports_missing_file(self, tmp_path, monkeypatch, capsys):
        """Unknown playbook path should be reported as MISSING (file not
        found) without crashing the tool."""
        (tmp_path / ".git").mkdir()
        (tmp_path / "docs" / "internal").mkdir(parents=True)
        # Deliberately skip creating playbook files.
        exit_code, stdout, stderr = _run_cli(
            monkeypatch, capsys, tmp_path, "--to", "v2.8.0"
        )
        assert exit_code == 0
        # All 4 paths missing → one MISSING row each in stdout summary.
        assert stdout.count("MISSING") == 4
        assert "4 playbook(s) lack" in stderr


# ── TestApplyBumpEdgeCases ─────────────────────────────────────────────


class TestApplyBumpEdgeCases:
    """Cover defensive branches in apply_bump()."""

    def test_handles_read_oserror(self, tmp_path, monkeypatch):
        """read_bytes() raising OSError should produce MISSING status,
        not propagate the exception."""
        target = tmp_path / "missing.md"
        # File does not exist → read_bytes raises FileNotFoundError.
        status, detail = bpv.apply_bump(target, "v2.8.0", write=True)
        assert status == "MISSING"
        assert "read error" in detail

    def test_handles_undecodable_bytes(self, tmp_path):
        """Non-UTF-8 bytes should produce MISSING, not UnicodeDecodeError."""
        f = tmp_path / "binary.md"
        f.write_bytes(b"\xff\xfe\x00binary garbage")
        status, detail = bpv.apply_bump(f, "v2.8.0", write=True)
        assert status == "MISSING"
        assert "decode error" in detail

    def test_unterminated_frontmatter_reported(self, tmp_path):
        """`---` at top but no closing `---` → MISSING."""
        f = tmp_path / "pb.md"
        f.write_text(
            "---\ntitle: x\nverified-at-version: v2.7.0\n\n# no end marker\n",
            encoding="utf-8",
            newline="",
        )
        status, _ = bpv.apply_bump(f, "v2.8.0", write=True)
        assert status == "MISSING"


# ── TestFindRepoRoot ───────────────────────────────────────────────────


class TestFindRepoRoot:
    def test_walks_up_to_git_dir(self, tmp_path, monkeypatch):
        (tmp_path / ".git").mkdir()
        nested = tmp_path / "a" / "b" / "c"
        nested.mkdir(parents=True)
        monkeypatch.chdir(nested)
        assert bpv.find_repo_root() == tmp_path

    def test_fallback_when_no_git_dir(self, tmp_path, monkeypatch):
        """Without a .git ancestor, fall back to the script-location
        heuristic (3 levels up from the module file). The important
        guarantee is that find_repo_root() never raises."""
        monkeypatch.chdir(tmp_path)
        result = bpv.find_repo_root()
        # Must be a real directory (the heuristic target).
        assert result.is_dir()


# ── TestNormalizeVersion ───────────────────────────────────────────────


class TestNormalizeVersion:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("v2.8.0", "v2.8.0"),
            ("2.8.0", "v2.8.0"),
            ("  v2.8.0  ", "v2.8.0"),
        ],
    )
    def test_normalizes(self, raw, expected):
        assert bpv._normalize_version(raw) == expected
