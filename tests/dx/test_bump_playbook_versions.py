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

import os
import subprocess
import sys
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


def _run_cli(repo_root: Path, *args: str) -> subprocess.CompletedProcess:
    script = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / "tools"
        / "dx"
        / "bump_playbook_versions.py"
    )
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, str(script), *args],
        cwd=repo_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )


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
    """End-to-end via subprocess — guards CLI surface stability."""

    def test_bumps_all_four_playbooks(self, tmp_path):
        repo = _make_playbook_tree(
            tmp_path, {name: "v2.7.0" for name in []}  # all default v2.7.0
        )
        result = _run_cli(repo, "--to", "v2.8.0")
        assert result.returncode == 0, result.stderr
        for rel in bpv.PLAYBOOK_PATHS:
            text = (repo / rel).read_text(encoding="utf-8")
            assert "verified-at-version: v2.8.0" in text

    def test_check_passes_when_clean(self, tmp_path):
        repo = _make_playbook_tree(
            tmp_path,
            {Path(rel).name: "v2.8.0" for rel in bpv.PLAYBOOK_PATHS},
        )
        result = _run_cli(repo, "--to", "v2.8.0", "--check")
        assert result.returncode == 0
        assert "All 4 playbooks at v2.8.0" in result.stdout

    def test_check_fails_when_stale(self, tmp_path):
        repo = _make_playbook_tree(
            tmp_path,
            {Path(rel).name: "v2.7.0" for rel in bpv.PLAYBOOK_PATHS},
        )
        result = _run_cli(repo, "--to", "v2.8.0", "--check")
        assert result.returncode == 1
        assert "4 playbook(s) need bump" in result.stderr

    def test_dry_run_no_write(self, tmp_path):
        repo = _make_playbook_tree(
            tmp_path,
            {Path(rel).name: "v2.7.0" for rel in bpv.PLAYBOOK_PATHS},
        )
        before = (repo / bpv.PLAYBOOK_PATHS[0]).read_text(encoding="utf-8")
        result = _run_cli(repo, "--to", "v2.8.0", "--dry-run")
        assert result.returncode == 0
        assert "would update" in result.stdout
        after = (repo / bpv.PLAYBOOK_PATHS[0]).read_text(encoding="utf-8")
        assert before == after

    def test_rejects_bad_version_format(self, tmp_path):
        repo = _make_playbook_tree(tmp_path, {})
        result = _run_cli(repo, "--to", "2.8", "--check")
        assert result.returncode == 2
        assert "must match vX.Y.Z" in result.stderr

    def test_accepts_version_without_v_prefix(self, tmp_path):
        repo = _make_playbook_tree(
            tmp_path,
            {Path(rel).name: "v2.7.0" for rel in bpv.PLAYBOOK_PATHS},
        )
        result = _run_cli(repo, "--to", "2.8.0")
        assert result.returncode == 0
        text = (repo / bpv.PLAYBOOK_PATHS[0]).read_text(encoding="utf-8")
        assert "verified-at-version: v2.8.0" in text


# ── TestFindRepoRoot ───────────────────────────────────────────────────


class TestFindRepoRoot:
    def test_walks_up_to_git_dir(self, tmp_path, monkeypatch):
        (tmp_path / ".git").mkdir()
        nested = tmp_path / "a" / "b" / "c"
        nested.mkdir(parents=True)
        monkeypatch.chdir(nested)
        assert bpv.find_repo_root() == tmp_path


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
