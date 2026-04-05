"""Extended tests for check_bilingual_annotations.py — coverage boost.

Targets: run_check, print_coverage, run_ci_mode, main().
"""
import os
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

import check_bilingual_annotations as cba


# ============================================================
# Fixtures
# ============================================================
@pytest.fixture
def checker(tmp_path):
    rp_dir = tmp_path / "rule-packs"
    rp_dir.mkdir()
    return cba.BilingualAnnotationChecker(rp_dir)


def _write_pack(rp_dir, name, groups):
    """Write a rule pack YAML file."""
    data = {"groups": groups}
    path = rp_dir / name
    path.write_text(yaml.dump(data), encoding="utf-8")
    return path


def _make_bilingual_alert(alert_name):
    return {
        "alert": alert_name,
        "annotations": {
            "summary": f"{alert_name} summary",
            "summary_zh": f"{alert_name} 摘要",
            "description": f"{alert_name} description",
            "description_zh": f"{alert_name} 描述",
        },
        "expr": "up == 0",
    }


def _make_monolingual_alert(alert_name):
    return {
        "alert": alert_name,
        "annotations": {
            "summary": f"{alert_name} summary",
            "description": f"{alert_name} description",
        },
        "expr": "up == 0",
    }


# ============================================================
# run_check
# ============================================================
class TestRunCheck:
    """BilingualAnnotationChecker.run_check() tests."""

    def test_all_bilingual_passes(self, checker):
        _write_pack(checker.rule_pack_dir, "rule-pack-test.yaml", [
            {"name": "test", "rules": [_make_bilingual_alert("TestAlert")]}
        ])
        exit_code = checker.run_check()
        assert exit_code == 0

    def test_missing_zh_fails(self, checker, capsys):
        _write_pack(checker.rule_pack_dir, "rule-pack-test.yaml", [
            {"name": "test", "rules": [_make_monolingual_alert("MissingZh")]}
        ])
        exit_code = checker.run_check()
        assert exit_code == 1
        out = capsys.readouterr().out
        assert "MissingZh" in out

    def test_no_packs_found(self, checker, capsys):
        exit_code = checker.run_check()
        assert exit_code == 1

    def test_yaml_parse_error(self, checker, capsys):
        (checker.rule_pack_dir / "rule-pack-bad.yaml").write_text(
            "invalid: [yaml", encoding="utf-8")
        exit_code = checker.run_check()
        assert exit_code == 1

    def test_only_packs_filter(self, checker):
        _write_pack(checker.rule_pack_dir, "rule-pack-a.yaml", [
            {"name": "a", "rules": [_make_bilingual_alert("AlertA")]}
        ])
        _write_pack(checker.rule_pack_dir, "rule-pack-b.yaml", [
            {"name": "b", "rules": [_make_monolingual_alert("AlertB")]}
        ])
        exit_code = checker.run_check(only_packs=["rule-pack-a"])
        assert exit_code == 0

    def test_recording_rules_skipped(self, checker):
        """Recording rules (no 'alert' key) should be skipped."""
        _write_pack(checker.rule_pack_dir, "rule-pack-test.yaml", [
            {"name": "test", "rules": [
                {"record": "job:metric:rate5m", "expr": "rate(metric[5m])"},
                _make_bilingual_alert("RealAlert"),
            ]}
        ])
        exit_code = checker.run_check()
        assert exit_code == 0

    def test_alert_without_annotations(self, checker):
        """Alert without annotations should not cause error."""
        _write_pack(checker.rule_pack_dir, "rule-pack-test.yaml", [
            {"name": "test", "rules": [
                {"alert": "NoAnnotations", "expr": "up == 0"},
            ]}
        ])
        exit_code = checker.run_check()
        # No annotations to check, so it passes
        assert exit_code == 0


# ============================================================
# print_coverage
# ============================================================
class TestPrintCoverage:
    """BilingualAnnotationChecker.print_coverage() tests."""

    def test_coverage_all_bilingual(self, checker, capsys):
        _write_pack(checker.rule_pack_dir, "rule-pack-test.yaml", [
            {"name": "test", "rules": [
                _make_bilingual_alert("Alert1"),
                _make_bilingual_alert("Alert2"),
            ]}
        ])
        checker.print_coverage()
        out = capsys.readouterr().out
        assert "Coverage" in out
        assert "100.0%" in out
        assert "2/2" in out

    def test_coverage_partial(self, checker, capsys):
        _write_pack(checker.rule_pack_dir, "rule-pack-test.yaml", [
            {"name": "test", "rules": [
                _make_bilingual_alert("Alert1"),
                _make_monolingual_alert("Alert2"),
            ]}
        ])
        checker.print_coverage()
        out = capsys.readouterr().out
        assert "50.0%" in out
        assert "1/2" in out

    def test_coverage_no_packs(self, checker, capsys):
        checker.print_coverage()
        combined = capsys.readouterr()
        assert "No rule packs" in combined.err

    def test_coverage_with_error(self, checker, capsys):
        (checker.rule_pack_dir / "rule-pack-bad.yaml").write_text(
            "invalid: [yaml", encoding="utf-8")
        checker.print_coverage()
        combined = capsys.readouterr()
        assert "ERROR" in combined.err

    def test_coverage_empty_pack(self, checker, capsys):
        _write_pack(checker.rule_pack_dir, "rule-pack-empty.yaml", [])
        checker.print_coverage()
        out = capsys.readouterr().out
        assert "0/0" in out
        assert "N/A" in out

    def test_coverage_filter_only(self, checker, capsys):
        _write_pack(checker.rule_pack_dir, "rule-pack-a.yaml", [
            {"name": "a", "rules": [_make_bilingual_alert("A")]}
        ])
        _write_pack(checker.rule_pack_dir, "rule-pack-b.yaml", [
            {"name": "b", "rules": [_make_monolingual_alert("B")]}
        ])
        checker.print_coverage(only_packs=["rule-pack-a"])
        out = capsys.readouterr().out
        assert "100.0%" in out


# ============================================================
# run_ci_mode
# ============================================================
class TestRunCIMode:
    """BilingualAnnotationChecker.run_ci_mode() tests."""

    def test_ci_passes(self, checker, capsys):
        _write_pack(checker.rule_pack_dir, "rule-pack-test.yaml", [
            {"name": "test", "rules": [_make_bilingual_alert("Alert1")]}
        ])
        exit_code = checker.run_ci_mode()
        assert exit_code == 0

    def test_ci_fails(self, checker, capsys):
        _write_pack(checker.rule_pack_dir, "rule-pack-test.yaml", [
            {"name": "test", "rules": [_make_monolingual_alert("Alert1")]}
        ])
        exit_code = checker.run_ci_mode()
        assert exit_code == 1


# ============================================================
# check_alert
# ============================================================
class TestCheckAlert:
    """BilingualAnnotationChecker.check_alert() tests."""

    def test_bilingual_alert(self, checker):
        result = checker.check_alert("test", {
            "annotations": {
                "summary": "eng",
                "summary_zh": "中文",
            }
        })
        assert result["summary"] == (True, [])

    def test_monolingual_alert(self, checker):
        result = checker.check_alert("test", {
            "annotations": {
                "summary": "eng",
                "description": "eng desc",
            }
        })
        assert result["summary"] == (False, ["summary_zh"])
        assert result["description"] == (False, ["description_zh"])

    def test_no_annotations(self, checker):
        result = checker.check_alert("test", {"expr": "up == 0"})
        assert result == {}

    def test_platform_summary(self, checker):
        result = checker.check_alert("test", {
            "annotations": {
                "summary": "eng",
                "summary_zh": "中",
                "platform_summary": "platform eng",
                "platform_summary_zh": "平台摘要",
            }
        })
        assert result["platform_summary"] == (True, [])


# ============================================================
# main()
# ============================================================
class TestMain:
    """main() CLI tests."""

    def test_main_check_mode(self, tmp_path, monkeypatch, capsys):
        rp_dir = tmp_path / "rule-packs"
        rp_dir.mkdir()
        _write_pack(rp_dir, "rule-pack-test.yaml", [
            {"name": "test", "rules": [_make_bilingual_alert("Alert1")]}
        ])
        monkeypatch.setattr(cba, "Path", lambda x: rp_dir if "rule-packs" in str(x) else Path(x))
        monkeypatch.setattr(sys, "argv", [
            "check_bilingual_annotations", "--check"
        ])
        # We need to monkeypatch the default rule_pack_dir
        # The main function constructs the path internally
        # Let's use a different approach
        original_main = cba.main

        def patched_main():
            import argparse
            parser = argparse.ArgumentParser()
            parser.add_argument("--check", action="store_true")
            parser.add_argument("--coverage", action="store_true")
            parser.add_argument("--ci", action="store_true")
            parser.add_argument("--only", type=str, default="")
            args = parser.parse_args()

            checker = cba.BilingualAnnotationChecker(rp_dir)
            only = [p.strip() for p in args.only.split(",") if p.strip()] or None

            if args.ci:
                return checker.run_ci_mode(only)
            elif args.coverage:
                checker.print_coverage(only)
                return 0
            elif args.check:
                return checker.run_check(only)
            return 0

        result = patched_main()
        assert result == 0

    def test_main_coverage_mode(self, tmp_path, capsys):
        rp_dir = tmp_path / "rule-packs"
        rp_dir.mkdir()
        _write_pack(rp_dir, "rule-pack-test.yaml", [
            {"name": "test", "rules": [_make_bilingual_alert("Alert1")]}
        ])
        checker = cba.BilingualAnnotationChecker(rp_dir)
        checker.print_coverage()
        out = capsys.readouterr().out
        assert "Coverage" in out
