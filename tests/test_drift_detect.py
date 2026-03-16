#!/usr/bin/env python3
"""test_drift_detect.py — drift_detect.py pytest 測試。

驗證:
  1. compute_dir_manifest() — SHA-256 計算
  2. compare_manifests() — added/removed/modified 分類
  3. classify expected vs unexpected drift
  4. analyze_drift() — 全管線 pairwise 比較
  5. suggest_reconcile() — 修復建議
  6. 輸出格式 (text/JSON/markdown)
  7. CLI (argparse + main)
"""

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

import drift_detect as dd  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_yaml(dir_path: Path, filename: str, content: str):
    """Write a YAML file to a directory."""
    (dir_path / filename).write_text(content, encoding="utf-8")


@pytest.fixture
def two_dirs(tmp_path):
    """Create two config directories with overlapping content."""
    dir_a = tmp_path / "cluster-a"
    dir_b = tmp_path / "cluster-b"
    dir_a.mkdir()
    dir_b.mkdir()

    # Identical file
    _write_yaml(dir_a, "db-shared.yaml", "tenants: {shared: true}")
    _write_yaml(dir_b, "db-shared.yaml", "tenants: {shared: true}")

    # Modified file
    _write_yaml(dir_a, "db-prod.yaml", "tenants: {timeout: 30}")
    _write_yaml(dir_b, "db-prod.yaml", "tenants: {timeout: 60}")

    # Only in A
    _write_yaml(dir_a, "db-legacy.yaml", "tenants: {legacy: true}")

    # Only in B
    _write_yaml(dir_b, "db-new.yaml", "tenants: {new: true}")

    # Expected drift (cluster-specific)
    _write_yaml(dir_a, "_cluster_override.yaml", "cluster: a")
    _write_yaml(dir_b, "_cluster_override.yaml", "cluster: b")

    return dir_a, dir_b


@pytest.fixture
def three_dirs(tmp_path):
    """Three dirs for pairwise testing."""
    dirs = []
    for name in ("alpha", "beta", "gamma"):
        d = tmp_path / name
        d.mkdir()
        _write_yaml(d, "db-common.yaml", f"env: {name}")
        dirs.append(d)
    # alpha and beta share an extra file
    _write_yaml(dirs[0], "db-extra.yaml", "extra: true")
    _write_yaml(dirs[1], "db-extra.yaml", "extra: true")
    return dirs


# ---------------------------------------------------------------------------
# TestComputeManifest
# ---------------------------------------------------------------------------


class TestComputeManifest:
    """compute_dir_manifest() SHA-256 計算測試。"""

    def test_empty_dir(self, tmp_path):
        """空目錄回傳空 manifest。"""
        d = tmp_path / "empty"
        d.mkdir()
        m = dd.compute_dir_manifest(str(d), label="test")
        assert m.label == "test"
        assert m.files == {}

    def test_single_file(self, tmp_path):
        """單一 YAML 檔案正確計算 SHA-256。"""
        d = tmp_path / "single"
        d.mkdir()
        _write_yaml(d, "db-a.yaml", "tenants: {}")
        m = dd.compute_dir_manifest(str(d))
        assert "db-a.yaml" in m.files
        assert len(m.files["db-a.yaml"]) == 64  # sha256 hex length

    def test_multiple_files(self, tmp_path):
        """多檔案 manifest 包含所有 YAML。"""
        d = tmp_path / "multi"
        d.mkdir()
        for name in ("db-a.yaml", "db-b.yaml", "db-c.yaml"):
            _write_yaml(d, name, f"name: {name}")
        m = dd.compute_dir_manifest(str(d))
        assert len(m.files) == 3

    def test_hidden_files_skipped(self, tmp_path):
        """隱藏檔案 (.) 被忽略。"""
        d = tmp_path / "hidden"
        d.mkdir()
        _write_yaml(d, ".hidden.yaml", "secret: true")
        _write_yaml(d, "db-a.yaml", "visible: true")
        m = dd.compute_dir_manifest(str(d))
        assert ".hidden.yaml" not in m.files
        assert "db-a.yaml" in m.files

    def test_nonexistent_dir(self, tmp_path):
        """不存在的目錄回傳空 manifest。"""
        m = dd.compute_dir_manifest(str(tmp_path / "nope"))
        assert m.files == {}

    def test_same_content_same_hash(self, tmp_path):
        """相同內容產生相同 SHA-256。"""
        d1 = tmp_path / "d1"
        d2 = tmp_path / "d2"
        d1.mkdir()
        d2.mkdir()
        _write_yaml(d1, "db-a.yaml", "identical: true")
        _write_yaml(d2, "db-a.yaml", "identical: true")
        m1 = dd.compute_dir_manifest(str(d1))
        m2 = dd.compute_dir_manifest(str(d2))
        assert m1.files["db-a.yaml"] == m2.files["db-a.yaml"]

    def test_label_defaults_to_dirname(self, tmp_path):
        """未指定 label 時使用目錄名稱。"""
        d = tmp_path / "my-cluster"
        d.mkdir()
        m = dd.compute_dir_manifest(str(d))
        assert m.label == "my-cluster"


# ---------------------------------------------------------------------------
# TestCompareManifests
# ---------------------------------------------------------------------------


class TestCompareManifests:
    """compare_manifests() 差異分類測試。"""

    def test_identical(self, tmp_path):
        """完全相同的目錄無 drift。"""
        d = tmp_path / "same"
        d.mkdir()
        _write_yaml(d, "db-a.yaml", "same: true")
        m = dd.compute_dir_manifest(str(d))
        report = dd.compare_manifests(m, m)
        assert len(report.items) == 0

    def test_added_file(self, two_dirs):
        """target 多出的檔案標記為 added。"""
        dir_a, dir_b = two_dirs
        m_a = dd.compute_dir_manifest(str(dir_a), "A")
        m_b = dd.compute_dir_manifest(str(dir_b), "B")
        report = dd.compare_manifests(m_a, m_b)
        added = [i for i in report.items if i.drift_type == "added"]
        assert any(i.filename == "db-new.yaml" for i in added)

    def test_removed_file(self, two_dirs):
        """source 獨有的檔案標記為 removed。"""
        dir_a, dir_b = two_dirs
        m_a = dd.compute_dir_manifest(str(dir_a), "A")
        m_b = dd.compute_dir_manifest(str(dir_b), "B")
        report = dd.compare_manifests(m_a, m_b)
        removed = [i for i in report.items if i.drift_type == "removed"]
        assert any(i.filename == "db-legacy.yaml" for i in removed)

    def test_modified_file(self, two_dirs):
        """內容不同的檔案標記為 modified。"""
        dir_a, dir_b = two_dirs
        m_a = dd.compute_dir_manifest(str(dir_a), "A")
        m_b = dd.compute_dir_manifest(str(dir_b), "B")
        report = dd.compare_manifests(m_a, m_b)
        modified = [i for i in report.items if i.drift_type == "modified"]
        assert any(i.filename == "db-prod.yaml" for i in modified)

    def test_expected_drift(self, two_dirs):
        """_cluster_ 前綴的檔案標記為 expected。"""
        dir_a, dir_b = two_dirs
        m_a = dd.compute_dir_manifest(str(dir_a), "A")
        m_b = dd.compute_dir_manifest(str(dir_b), "B")
        report = dd.compare_manifests(m_a, m_b)
        cluster_items = [
            i for i in report.items
            if i.filename == "_cluster_override.yaml"
        ]
        assert len(cluster_items) == 1
        assert cluster_items[0].expected is True

    def test_unexpected_count(self, two_dirs):
        """unexpected_count 正確計算。"""
        dir_a, dir_b = two_dirs
        m_a = dd.compute_dir_manifest(str(dir_a), "A")
        m_b = dd.compute_dir_manifest(str(dir_b), "B")
        report = dd.compare_manifests(m_a, m_b)
        # db-prod (modified), db-legacy (removed), db-new (added) = 3 unexpected
        assert report.unexpected_count == 3


# ---------------------------------------------------------------------------
# TestClassifyDrift
# ---------------------------------------------------------------------------


class TestClassifyDrift:
    """expected vs unexpected 分類邏輯。"""

    def test_local_prefix_expected(self, tmp_path):
        """_local_ 前綴也標記為 expected。"""
        d1 = tmp_path / "d1"
        d2 = tmp_path / "d2"
        d1.mkdir()
        d2.mkdir()
        _write_yaml(d1, "_local_tuning.yaml", "local: d1")
        _write_yaml(d2, "_local_tuning.yaml", "local: d2")
        m1 = dd.compute_dir_manifest(str(d1), "D1")
        m2 = dd.compute_dir_manifest(str(d2), "D2")
        report = dd.compare_manifests(m1, m2)
        assert report.items[0].expected is True

    def test_custom_ignore_prefix(self, tmp_path):
        """自定義 ignore_prefixes。"""
        d1 = tmp_path / "d1"
        d2 = tmp_path / "d2"
        d1.mkdir()
        d2.mkdir()
        _write_yaml(d1, "env_override.yaml", "env: d1")
        _write_yaml(d2, "env_override.yaml", "env: d2")
        m1 = dd.compute_dir_manifest(str(d1), "D1")
        m2 = dd.compute_dir_manifest(str(d2), "D2")
        report = dd.compare_manifests(m1, m2,
                                       ignore_prefixes=("env_",))
        assert report.items[0].expected is True

    def test_no_ignore_prefix(self, tmp_path):
        """空 ignore_prefixes → 全部 unexpected。"""
        d1 = tmp_path / "d1"
        d2 = tmp_path / "d2"
        d1.mkdir()
        d2.mkdir()
        _write_yaml(d1, "_cluster_x.yaml", "x: 1")
        _write_yaml(d2, "_cluster_x.yaml", "x: 2")
        m1 = dd.compute_dir_manifest(str(d1), "D1")
        m2 = dd.compute_dir_manifest(str(d2), "D2")
        report = dd.compare_manifests(m1, m2, ignore_prefixes=())
        assert report.items[0].expected is False


# ---------------------------------------------------------------------------
# TestAnalyzeDrift
# ---------------------------------------------------------------------------


class TestAnalyzeDrift:
    """analyze_drift() 全管線 pairwise 比較。"""

    def test_two_dirs(self, two_dirs):
        """兩個目錄產生 1 個 DriftReport。"""
        dir_a, dir_b = two_dirs
        reports = dd.analyze_drift(
            [str(dir_a), str(dir_b)],
            labels=["A", "B"],
        )
        assert len(reports) == 1
        assert reports[0].source_label == "A"
        assert reports[0].target_label == "B"

    def test_three_dirs(self, three_dirs):
        """三個目錄產生 3 個 pairwise DriftReport。"""
        reports = dd.analyze_drift(
            [str(d) for d in three_dirs],
            labels=["alpha", "beta", "gamma"],
        )
        assert len(reports) == 3

    def test_auto_labels(self, two_dirs):
        """不指定 labels 時自動產生 dir-1, dir-2。"""
        dir_a, dir_b = two_dirs
        reports = dd.analyze_drift([str(dir_a), str(dir_b)])
        assert reports[0].source_label == "dir-1"
        assert reports[0].target_label == "dir-2"

    def test_identical_dirs(self, tmp_path):
        """完全相同的目錄 → drift_free。"""
        d1 = tmp_path / "d1"
        d2 = tmp_path / "d2"
        d1.mkdir()
        d2.mkdir()
        _write_yaml(d1, "db-a.yaml", "same: true")
        _write_yaml(d2, "db-a.yaml", "same: true")
        reports = dd.analyze_drift([str(d1), str(d2)])
        assert len(reports[0].items) == 0


# ---------------------------------------------------------------------------
# TestSuggestReconcile
# ---------------------------------------------------------------------------


class TestSuggestReconcile:
    """suggest_reconcile() 修復建議。"""

    def test_added(self):
        """added 建議 copy 或 remove。"""
        item = dd.DriftItem(
            filename="db-new.yaml",
            drift_type="added",
            source_label="A",
            target_label="B",
        )
        suggestion = dd.suggest_reconcile(item)
        assert "Copy" in suggestion
        assert "db-new.yaml" in suggestion

    def test_removed(self):
        """removed 建議 copy 或 remove。"""
        item = dd.DriftItem(
            filename="db-old.yaml",
            drift_type="removed",
            source_label="A",
            target_label="B",
        )
        suggestion = dd.suggest_reconcile(item)
        assert "Copy" in suggestion
        assert "db-old.yaml" in suggestion

    def test_modified(self):
        """modified 建議 review diff。"""
        item = dd.DriftItem(
            filename="db-prod.yaml",
            drift_type="modified",
            source_label="A",
            target_label="B",
        )
        suggestion = dd.suggest_reconcile(item)
        assert "diff" in suggestion.lower()
        assert "db-prod.yaml" in suggestion


# ---------------------------------------------------------------------------
# TestBuildSummary
# ---------------------------------------------------------------------------


class TestBuildSummary:
    """build_summary() 結構化摘要。"""

    def test_structure(self, two_dirs):
        """摘要包含必要欄位。"""
        dir_a, dir_b = two_dirs
        reports = dd.analyze_drift(
            [str(dir_a), str(dir_b)], labels=["A", "B"],
        )
        summary = dd.build_summary(reports)
        assert "timestamp" in summary
        assert "pair_count" in summary
        assert "total_drift" in summary
        assert "unexpected_drift" in summary
        assert "drift_free" in summary
        assert "pairs" in summary

    def test_drift_free_when_identical(self, tmp_path):
        """完全相同 → drift_free = True。"""
        d1 = tmp_path / "d1"
        d2 = tmp_path / "d2"
        d1.mkdir()
        d2.mkdir()
        _write_yaml(d1, "db-a.yaml", "same: true")
        _write_yaml(d2, "db-a.yaml", "same: true")
        reports = dd.analyze_drift([str(d1), str(d2)])
        summary = dd.build_summary(reports)
        assert summary["drift_free"] is True

    def test_not_drift_free(self, two_dirs):
        """有差異 → drift_free = False。"""
        dir_a, dir_b = two_dirs
        reports = dd.analyze_drift(
            [str(dir_a), str(dir_b)], labels=["A", "B"],
        )
        summary = dd.build_summary(reports)
        assert summary["drift_free"] is False


# ---------------------------------------------------------------------------
# TestOutputFormatting
# ---------------------------------------------------------------------------


class TestOutputFormatting:
    """輸出格式測試。"""

    def test_text_drift_free(self, tmp_path):
        """無 drift 的 text 報告。"""
        d1 = tmp_path / "d1"
        d2 = tmp_path / "d2"
        d1.mkdir()
        d2.mkdir()
        _write_yaml(d1, "db-a.yaml", "same: true")
        _write_yaml(d2, "db-a.yaml", "same: true")
        reports = dd.analyze_drift([str(d1), str(d2)])
        summary = dd.build_summary(reports)
        text = dd.format_text_report(summary)
        assert "No unexpected drift" in text

    def test_text_with_drift(self, two_dirs):
        """有 drift 的 text 報告包含檔名和類型。"""
        dir_a, dir_b = two_dirs
        reports = dd.analyze_drift(
            [str(dir_a), str(dir_b)], labels=["A", "B"],
        )
        summary = dd.build_summary(reports)
        text = dd.format_text_report(summary)
        assert "db-prod.yaml" in text
        assert "modified" in text

    def test_json_format(self, two_dirs):
        """JSON 報告可解析。"""
        dir_a, dir_b = two_dirs
        reports = dd.analyze_drift(
            [str(dir_a), str(dir_b)], labels=["A", "B"],
        )
        summary = dd.build_summary(reports)
        output = dd.format_json_report(summary)
        data = json.loads(output)
        assert data["pair_count"] == 1
        assert data["unexpected_drift"] > 0

    def test_markdown_format(self, two_dirs):
        """Markdown 報告包含表格。"""
        dir_a, dir_b = two_dirs
        reports = dd.analyze_drift(
            [str(dir_a), str(dir_b)], labels=["A", "B"],
        )
        summary = dd.build_summary(reports)
        md = dd.format_markdown_report(summary)
        assert "# Cross-Cluster" in md
        assert "| File |" in md
        assert "Reconciliation" in md

    def test_markdown_drift_free(self, tmp_path):
        """無 drift 的 Markdown 報告。"""
        d1 = tmp_path / "d1"
        d2 = tmp_path / "d2"
        d1.mkdir()
        d2.mkdir()
        _write_yaml(d1, "db-a.yaml", "same: true")
        _write_yaml(d2, "db-a.yaml", "same: true")
        reports = dd.analyze_drift([str(d1), str(d2)])
        summary = dd.build_summary(reports)
        md = dd.format_markdown_report(summary)
        assert "No unexpected drift" in md


# ---------------------------------------------------------------------------
# TestCLI
# ---------------------------------------------------------------------------


class TestCLI:
    """CLI argparse + main() 測試。"""

    def test_parser_defaults(self):
        """Parser 預設值正確。"""
        parser = dd.build_parser()
        args = parser.parse_args(["--dirs", "a,b"])
        assert args.dirs == "a,b"
        assert args.json is False
        assert args.ci is False

    def test_main_text_output(self, two_dirs, monkeypatch, capsys):
        """預設 text 輸出。"""
        dir_a, dir_b = two_dirs
        monkeypatch.setattr(sys, "argv", [
            "drift_detect",
            "--dirs", f"{dir_a},{dir_b}",
            "--labels", "A,B",
        ])
        dd.main()
        out = capsys.readouterr().out
        assert "Cross-Cluster" in out
        assert "Unexpected" in out

    def test_main_json_output(self, two_dirs, monkeypatch, capsys):
        """--json 輸出 JSON。"""
        dir_a, dir_b = two_dirs
        monkeypatch.setattr(sys, "argv", [
            "drift_detect",
            "--dirs", f"{dir_a},{dir_b}",
            "--json",
        ])
        dd.main()
        out = capsys.readouterr().out
        data = json.loads(out)
        assert "pair_count" in data

    def test_main_markdown_output(self, two_dirs, monkeypatch, capsys):
        """--markdown 輸出 Markdown。"""
        dir_a, dir_b = two_dirs
        monkeypatch.setattr(sys, "argv", [
            "drift_detect",
            "--dirs", f"{dir_a},{dir_b}",
            "--markdown",
        ])
        dd.main()
        out = capsys.readouterr().out
        assert "# Cross-Cluster" in out

    def test_main_ci_exits_on_drift(self, two_dirs, monkeypatch, capsys):
        """--ci 有 unexpected drift 時 exit 1。"""
        dir_a, dir_b = two_dirs
        monkeypatch.setattr(sys, "argv", [
            "drift_detect",
            "--dirs", f"{dir_a},{dir_b}",
            "--ci",
        ])
        with pytest.raises(SystemExit) as exc_info:
            dd.main()
        assert exc_info.value.code == 1

    def test_main_ci_success(self, tmp_path, monkeypatch, capsys):
        """--ci 無 drift 時 exit 0。"""
        d1 = tmp_path / "d1"
        d2 = tmp_path / "d2"
        d1.mkdir()
        d2.mkdir()
        _write_yaml(d1, "db-a.yaml", "same: true")
        _write_yaml(d2, "db-a.yaml", "same: true")
        monkeypatch.setattr(sys, "argv", [
            "drift_detect",
            "--dirs", f"{d1},{d2}",
            "--ci",
        ])
        # Should not raise
        dd.main()

    def test_main_insufficient_dirs(self, tmp_path, monkeypatch):
        """只給 1 個目錄 → exit 1。"""
        d = tmp_path / "only"
        d.mkdir()
        monkeypatch.setattr(sys, "argv", [
            "drift_detect", "--dirs", str(d),
        ])
        with pytest.raises(SystemExit) as exc_info:
            dd.main()
        assert exc_info.value.code == 1

    def test_main_missing_dir(self, tmp_path, monkeypatch):
        """不存在的目錄 → exit 1。"""
        d = tmp_path / "exists"
        d.mkdir()
        monkeypatch.setattr(sys, "argv", [
            "drift_detect",
            "--dirs", f"{d},{tmp_path / 'nope'}",
        ])
        with pytest.raises(SystemExit) as exc_info:
            dd.main()
        assert exc_info.value.code == 1

    def test_main_label_mismatch(self, two_dirs, monkeypatch):
        """--labels 數量不符 → exit 1。"""
        dir_a, dir_b = two_dirs
        monkeypatch.setattr(sys, "argv", [
            "drift_detect",
            "--dirs", f"{dir_a},{dir_b}",
            "--labels", "A,B,C",
        ])
        with pytest.raises(SystemExit) as exc_info:
            dd.main()
        assert exc_info.value.code == 1
