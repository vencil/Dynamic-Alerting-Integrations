"""Tests for check_bilingual_annotations.py — rule pack bilingual validation."""
from __future__ import annotations

import os
import sys
import textwrap

import pytest

_TOOLS_DIR = os.path.join(os.path.dirname(__file__), '..', 'scripts', 'tools', 'lint')
sys.path.insert(0, _TOOLS_DIR)

import check_bilingual_annotations as cba  # noqa: E402


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
    """Helper to write a rule pack YAML file."""
    path = rp_dir / name
    path.write_text(yaml_content, encoding="utf-8")
    return path


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
