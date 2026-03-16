"""Tests for coverage_gap_analysis.py — Per-file coverage ranking report."""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts", "tools", "dx"))
import coverage_gap_analysis as cga


# ============================================================
# Unit Tests — parse_coverage_output
# ============================================================

SAMPLE_COV_OUTPUT = """
Name                                    Stmts   Miss  Cover   Missing
---------------------------------------------------------------------
scripts/tools/ops/alert_correlate.py      150     8    95%   45-50, 120
scripts/tools/ops/drift_detect.py         120     2    99%   88-89
scripts/tools/dx/bump_docs.py             200    32    84%   10-20, 100-120
scripts/tools/lint/check_doc_links.py      80    56    30%   1-56
---------------------------------------------------------------------
TOTAL                                     550    98    82%
"""


class TestParseCoverageOutput:
    """Tests for coverage text output parsing."""

    def test_parse_basic_output(self):
        """解析標準 pytest-cov 輸出"""
        files = cga.parse_coverage_output(SAMPLE_COV_OUTPUT)
        assert len(files) == 4

    def test_parse_file_details(self):
        """確認解析出的每個檔案數據正確"""
        files = cga.parse_coverage_output(SAMPLE_COV_OUTPUT)
        by_name = {f.file_path: f for f in files}

        ac = by_name["scripts/tools/ops/alert_correlate.py"]
        assert ac.statements == 150
        assert ac.missed == 8
        assert ac.coverage_pct == 95.0
        assert "45-50" in ac.missing_lines

    def test_parse_empty_output(self):
        """空輸出返回空列表"""
        files = cga.parse_coverage_output("")
        assert len(files) == 0

    def test_parse_no_match(self):
        """無匹配行的輸出"""
        files = cga.parse_coverage_output("No data available\n")
        assert len(files) == 0

    def test_parse_100_percent(self):
        """100% 覆蓋率"""
        text = "scripts/tools/ops/foo.py  50  0  100%\n"
        files = cga.parse_coverage_output(text)
        assert len(files) == 1
        assert files[0].coverage_pct == 100.0
        assert files[0].missed == 0

    def test_parse_0_percent(self):
        """0% 覆蓋率"""
        text = "scripts/tools/ops/bar.py  30  30  0%   1-30\n"
        files = cga.parse_coverage_output(text)
        assert len(files) == 1
        assert files[0].coverage_pct == 0.0


# ============================================================
# Unit Tests — build_report
# ============================================================

class TestBuildReport:
    """Tests for report building."""

    def test_basic_report(self):
        """基本報告構建"""
        files = cga.parse_coverage_output(SAMPLE_COV_OUTPUT)
        report = cga.build_report(files, 70.0)
        assert report.target_pct == 70.0
        assert report.total_statements == 550
        assert report.total_missed == 98
        assert report.overall_pct == 82.2

    def test_below_target_count(self):
        """低於目標的文件數"""
        files = cga.parse_coverage_output(SAMPLE_COV_OUTPUT)
        report = cga.build_report(files, 70.0)
        assert report.below_target_count == 1  # check_doc_links at 30%
        assert report.at_target_count == 3

    def test_empty_report(self):
        """空報告"""
        report = cga.build_report([], 70.0)
        assert report.overall_pct == 0.0
        assert report.below_target_count == 0

    def test_all_below_target(self):
        """全部低於目標"""
        files = [
            cga.FileCoverage("a.py", 100, 80, 20.0),
            cga.FileCoverage("b.py", 100, 70, 30.0),
        ]
        report = cga.build_report(files, 70.0)
        assert report.below_target_count == 2
        assert report.at_target_count == 0


# ============================================================
# Unit Tests — format_text_report
# ============================================================

class TestFormatTextReport:
    """Tests for text report formatting."""

    def test_text_report_with_gaps(self):
        """有 gap 的文字報告"""
        files = cga.parse_coverage_output(SAMPLE_COV_OUTPUT)
        report = cga.build_report(files, 70.0)
        text = cga.format_text_report(report)
        assert "Coverage Gap Analysis" in text
        assert "target: 70.0%" in text
        assert "check_doc_links" in text  # Below target
        assert "prioritize" in text.lower()

    def test_text_report_all_passing(self):
        """全部通過的報告"""
        files = [cga.FileCoverage("a.py", 100, 5, 95.0)]
        report = cga.build_report(files, 70.0)
        text = cga.format_text_report(report)
        assert "0 below target" in text


# ============================================================
# Unit Tests — format_json_report
# ============================================================

class TestFormatJsonReport:
    """Tests for JSON report formatting."""

    def test_json_structure(self):
        """JSON 報告結構"""
        files = cga.parse_coverage_output(SAMPLE_COV_OUTPUT)
        report = cga.build_report(files, 70.0)
        text = cga.format_json_report(report)
        data = json.loads(text)
        assert data["target_pct"] == 70.0
        assert data["overall_pct"] == 82.2
        assert data["files_total"] == 4
        assert data["files_below_target"] == 1
        assert len(data["files"]) == 4

    def test_json_sorted_ascending(self):
        """JSON 報告按覆蓋率升序排列"""
        files = cga.parse_coverage_output(SAMPLE_COV_OUTPUT)
        report = cga.build_report(files, 70.0)
        text = cga.format_json_report(report)
        data = json.loads(text)
        coverages = [f["coverage_pct"] for f in data["files"]]
        assert coverages == sorted(coverages)

    def test_json_empty(self):
        """空報告 JSON"""
        report = cga.build_report([], 70.0)
        text = cga.format_json_report(report)
        data = json.loads(text)
        assert data["files_total"] == 0


# ============================================================
# Unit Tests — FileCoverage
# ============================================================

class TestFileCoverage:
    """Tests for FileCoverage dataclass."""

    def test_to_dict(self):
        """to_dict 序列化"""
        fc = cga.FileCoverage("a.py", 100, 20, 80.0, "10-15, 30")
        d = fc.to_dict()
        assert d["file"] == "a.py"
        assert d["statements"] == 100
        assert d["missed"] == 20
        assert d["coverage_pct"] == 80.0
        assert d["missing_lines"] == "10-15, 30"


# ============================================================
# CLI Tests
# ============================================================

class TestCLI:
    """Tests for CLI entry point."""

    def test_cli_with_coverage_text(self, tmp_path):
        """--coverage-text 從檔案讀取覆蓋率數據"""
        cov_file = tmp_path / "coverage.txt"
        cov_file.write_text(SAMPLE_COV_OUTPUT, encoding="utf-8")
        cga.main(["--coverage-text", str(cov_file)])

    def test_cli_json(self, tmp_path, capsys):
        """--json 輸出 JSON"""
        cov_file = tmp_path / "coverage.txt"
        cov_file.write_text(SAMPLE_COV_OUTPUT, encoding="utf-8")
        cga.main(["--coverage-text", str(cov_file), "--json"])
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert "target_pct" in data

    def test_cli_ci_exit_on_gap(self, tmp_path):
        """--ci 有 gap 時 exit 1"""
        cov_file = tmp_path / "coverage.txt"
        cov_file.write_text(SAMPLE_COV_OUTPUT, encoding="utf-8")
        with pytest.raises(SystemExit) as exc_info:
            cga.main(["--coverage-text", str(cov_file), "--ci", "--target", "70"])
        assert exc_info.value.code == 1

    def test_cli_ci_pass(self, tmp_path):
        """--ci 全通過時 exit 0"""
        text = "a.py  50  5  90%\nb.py  40  2  95%\n"
        cov_file = tmp_path / "coverage.txt"
        cov_file.write_text(text, encoding="utf-8")
        cga.main(["--coverage-text", str(cov_file), "--ci", "--target", "70"])

    def test_cli_custom_target(self, tmp_path, capsys):
        """--target 自定義目標"""
        cov_file = tmp_path / "coverage.txt"
        cov_file.write_text(SAMPLE_COV_OUTPUT, encoding="utf-8")
        cga.main(["--coverage-text", str(cov_file), "--json", "--target", "50"])
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["target_pct"] == 50.0
        assert data["files_below_target"] == 1  # check_doc_links at 30%

    def test_cli_empty_coverage(self, tmp_path):
        """空覆蓋率數據 exit 1"""
        cov_file = tmp_path / "coverage.txt"
        cov_file.write_text("No data\n", encoding="utf-8")
        with pytest.raises(SystemExit) as exc_info:
            cga.main(["--coverage-text", str(cov_file)])
        assert exc_info.value.code == 1
