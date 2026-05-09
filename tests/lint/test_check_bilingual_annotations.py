"""Tests for check_bilingual_annotations.py — rule pack bilingual validation.

Merged from previous _extended split (PR test-refactor sweep): YAML-string
based pack tests (TestFindRulePacks/TestCheckRulePack/base TestCheckAlert+
TestRunCheck) sit alongside dict-based pack tests + print_coverage / ci_mode /
main coverage classes appended below.
"""
from __future__ import annotations

import textwrap

import pytest
import yaml

import check_bilingual_annotations as cba


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def checker(tmp_path):
    """Create a BilingualAnnotationChecker with a temporary rule pack dir."""
    rp_dir = tmp_path / "rule-packs"
    rp_dir.mkdir()
    return cba.BilingualAnnotationChecker(rp_dir)


def _write_rule_pack(rp_dir, name, yaml_content):
    """Helper to write a rule pack YAML file from a YAML string."""
    path = rp_dir / name
    path.write_text(yaml_content, encoding="utf-8")
    return path


def _write_pack(rp_dir, name, groups):
    """Helper to write a rule pack YAML file from a groups list (dict-built)."""
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


# ---------------------------------------------------------------------------
# find_rule_packs
# ---------------------------------------------------------------------------
class TestFindRulePacks:
    def test_finds_rule_packs(self, checker):
        _write_rule_pack(checker.rule_pack_dir, "rule-pack-mariadb.yaml", "groups: []")
        _write_rule_pack(checker.rule_pack_dir, "rule-pack-postgresql.yaml", "groups: []")
        _write_rule_pack(checker.rule_pack_dir, "other-file.yaml", "groups: []")
        packs = checker.find_rule_packs()
        names = [p.name for p in packs]
        assert "rule-pack-mariadb.yaml" in names
        assert "rule-pack-postgresql.yaml" in names
        assert "other-file.yaml" not in names

    def test_filter_only(self, checker):
        _write_rule_pack(checker.rule_pack_dir, "rule-pack-mariadb.yaml", "groups: []")
        _write_rule_pack(checker.rule_pack_dir, "rule-pack-postgresql.yaml", "groups: []")
        packs = checker.find_rule_packs(only_packs=["rule-pack-mariadb"])
        assert len(packs) == 1
        assert packs[0].stem == "rule-pack-mariadb"

    def test_empty_dir(self, checker):
        packs = checker.find_rule_packs()
        assert packs == []


# ---------------------------------------------------------------------------
# check_alert
# ---------------------------------------------------------------------------
class TestCheckAlert:
    def test_fully_bilingual(self, checker):
        alert = {
            "annotations": {
                "summary": "High CPU",
                "summary_zh": "CPU 過高",
                "description": "CPU usage exceeds threshold",
                "description_zh": "CPU 使用率超過閾值",
            }
        }
        result = checker.check_alert("TestAlert", alert)
        assert result["summary"] == (True, [])
        assert result["description"] == (True, [])

    def test_missing_zh(self, checker):
        alert = {
            "annotations": {
                "summary": "High CPU",
                "description": "CPU usage exceeds threshold",
            }
        }
        result = checker.check_alert("TestAlert", alert)
        assert result["summary"] == (False, ["summary_zh"])
        assert result["description"] == (False, ["description_zh"])

    def test_partial_bilingual(self, checker):
        alert = {
            "annotations": {
                "summary": "High CPU",
                "summary_zh": "CPU 過高",
                "description": "Detail",
                # missing description_zh
            }
        }
        result = checker.check_alert("TestAlert", alert)
        assert result["summary"] == (True, [])
        assert result["description"] == (False, ["description_zh"])

    def test_no_annotations(self, checker):
        alert = {"expr": "up == 0"}
        result = checker.check_alert("TestAlert", alert)
        assert result == {}

    def test_platform_summary(self, checker):
        alert = {
            "annotations": {
                "platform_summary": "NOC view",
                "platform_summary_zh": "NOC 視角",
            }
        }
        result = checker.check_alert("TestAlert", alert)
        assert result["platform_summary"] == (True, [])


# ---------------------------------------------------------------------------
# check_rule_pack
# ---------------------------------------------------------------------------
class TestCheckRulePack:
    def test_all_bilingual(self, checker):
        pack = _write_rule_pack(checker.rule_pack_dir, "rule-pack-test.yaml", textwrap.dedent("""\
            groups:
              - name: test
                rules:
                  - alert: TestAlert
                    expr: up == 0
                    annotations:
                      summary: "Down"
                      summary_zh: "已停機"
        """))
        result = checker.check_rule_pack(pack)
        assert result["total_alerts"] == 1
        assert result["bilingual_alerts"] == 1
        assert result["monolingual_alerts"] == 0

    def test_monolingual(self, checker):
        pack = _write_rule_pack(checker.rule_pack_dir, "rule-pack-test.yaml", textwrap.dedent("""\
            groups:
              - name: test
                rules:
                  - alert: TestAlert
                    expr: up == 0
                    annotations:
                      summary: "Down"
        """))
        result = checker.check_rule_pack(pack)
        assert result["monolingual_alerts"] == 1

    def test_recording_rules_skipped(self, checker):
        pack = _write_rule_pack(checker.rule_pack_dir, "rule-pack-test.yaml", textwrap.dedent("""\
            groups:
              - name: test
                rules:
                  - record: instance:cpu:ratio
                    expr: avg(rate(cpu[5m]))
        """))
        result = checker.check_rule_pack(pack)
        assert result["total_alerts"] == 0

    def test_invalid_yaml(self, checker):
        pack = _write_rule_pack(checker.rule_pack_dir, "rule-pack-bad.yaml",
                                "groups:\n  - rules: [{{invalid}")
        result = checker.check_rule_pack(pack)
        assert "error" in result

    def test_mixed_alerts(self, checker):
        pack = _write_rule_pack(checker.rule_pack_dir, "rule-pack-test.yaml", textwrap.dedent("""\
            groups:
              - name: test
                rules:
                  - alert: BilingualAlert
                    expr: up == 0
                    annotations:
                      summary: "Down"
                      summary_zh: "已停機"
                  - alert: MonoAlert
                    expr: up == 0
                    annotations:
                      summary: "Down"
                  - record: recording_rule
                    expr: sum(up)
        """))
        result = checker.check_rule_pack(pack)
        assert result["total_alerts"] == 2
        assert result["bilingual_alerts"] == 1
        assert result["monolingual_alerts"] == 1


# ---------------------------------------------------------------------------
# run_check
# ---------------------------------------------------------------------------
class TestRunCheck:
    def test_clean_returns_zero(self, checker):
        _write_rule_pack(checker.rule_pack_dir, "rule-pack-test.yaml", textwrap.dedent("""\
            groups:
              - name: test
                rules:
                  - alert: TestAlert
                    expr: up == 0
                    annotations:
                      summary: "Down"
                      summary_zh: "已停機"
        """))
        assert checker.run_check() == 0

    def test_missing_zh_returns_one(self, checker):
        _write_rule_pack(checker.rule_pack_dir, "rule-pack-test.yaml", textwrap.dedent("""\
            groups:
              - name: test
                rules:
                  - alert: TestAlert
                    expr: up == 0
                    annotations:
                      summary: "Down"
        """))
        assert checker.run_check() == 1

    def test_no_packs_returns_one(self, checker):
        assert checker.run_check() == 1

    # ── unique edge cases (merged from _extended) ───────────────────────

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


# ---------------------------------------------------------------------------
# print_coverage / run_ci_mode / main coverage
# (was test_check_bilingual_extended.py)
#
# Note: test_main_check_mode from the _extended file was DROPPED — it
# redefined a `patched_main` inside the test instead of exercising
# cba.main(), so it tested only the test author's reimplementation.
# Use test_main_coverage_mode below for a real main-path smoke.
# ---------------------------------------------------------------------------


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


class TestMain:
    """main() CLI smoke."""

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
