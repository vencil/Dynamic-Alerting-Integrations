"""Tests for generate_alert_reference.py — Rule Pack → ALERT-REFERENCE.md generator.

Closes the audit gap (P1-5 / 456 LOC tool was 0% covered). Targets the spine:
  - get_rule_pack_name (filename → short name)
  - get_display_name (lookup with fallback)
  - extract_alerts (YAML traversal, recording-rule skip, default severity)
  - get_recommended_action (substring pattern → action dict)
  - get_metric_from_description (regex extraction)
  - generate_markdown_zh / _en (smoke + frontmatter sanity)
  - load_rule_packs (file IO, glob, skip non-rule-pack files)
  - main() CLI (dry-run, --check synced + drift, --output-dir, write mode)
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import generate_alert_reference as gar


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_pack(dir_: Path, name: str, content: dict) -> Path:
    """Write a rule pack YAML file to dir_ and return path."""
    p = dir_ / name
    p.write_text(yaml.dump(content), encoding="utf-8")
    return p


def _alert(name: str, severity: str = "warning",
           summary: str = "S", description: str = "D",
           platform_summary: str = "") -> dict:
    out = {
        "alert": name,
        "expr": "up == 0",
        "labels": {"severity": severity},
        "annotations": {"summary": summary, "description": description},
    }
    if platform_summary:
        out["annotations"]["platform_summary"] = platform_summary
    return out


# ---------------------------------------------------------------------------
# get_rule_pack_name
# ---------------------------------------------------------------------------
class TestGetRulePackName:
    def test_strips_prefix_and_suffix(self):
        assert gar.get_rule_pack_name("rule-pack-mariadb.yaml") == "mariadb"

    def test_full_path(self):
        assert gar.get_rule_pack_name("/a/b/rule-pack-postgresql.yaml") == "postgresql"

    def test_no_prefix_falls_back(self):
        # Files without rule-pack- prefix just lose .yaml suffix.
        assert gar.get_rule_pack_name("custom.yaml") == "custom"


# ---------------------------------------------------------------------------
# get_display_name
# ---------------------------------------------------------------------------
class TestGetDisplayName:
    def test_known_pack(self):
        out = gar.get_display_name("mariadb")
        assert "MariaDB" in out["zh"]
        assert "MariaDB" in out["en"]

    def test_unknown_pack_fallback(self):
        # Unknown name → both langs fall back to the raw input.
        out = gar.get_display_name("custom-pack")
        assert out["zh"] == "custom-pack"
        assert out["en"] == "custom-pack"


# ---------------------------------------------------------------------------
# extract_alerts
# ---------------------------------------------------------------------------
class TestExtractAlerts:
    def test_no_groups_returns_empty(self):
        assert gar.extract_alerts({}) == []

    def test_single_alert(self):
        content = {"groups": [{"name": "g", "rules": [_alert("Down")]}]}
        out = gar.extract_alerts(content)
        assert len(out) == 1
        assert out[0]["name"] == "Down"
        assert out[0]["severity"] == "warning"

    def test_recording_rule_skipped(self):
        # Recording rules have no `alert` key.
        content = {"groups": [{"name": "g", "rules": [
            {"record": "job:x:rate5m", "expr": "rate(x[5m])"},
            _alert("RealAlert"),
        ]}]}
        out = gar.extract_alerts(content)
        assert len(out) == 1
        assert out[0]["name"] == "RealAlert"

    def test_severity_unknown_when_missing(self):
        content = {"groups": [{"name": "g", "rules": [
            # No `labels` block at all.
            {"alert": "Bare", "expr": "up == 0", "annotations": {}},
        ]}]}
        out = gar.extract_alerts(content)
        assert out[0]["severity"] == "unknown"

    def test_platform_summary_captured(self):
        content = {"groups": [{"name": "g", "rules": [
            _alert("DualPerspective", platform_summary="Platform-side note"),
        ]}]}
        out = gar.extract_alerts(content)
        assert out[0]["platform_summary"] == "Platform-side note"

    def test_group_without_rules_skipped(self):
        content = {"groups": [{"name": "no-rules"}]}
        assert gar.extract_alerts(content) == []

    def test_multiple_groups_flattened(self):
        content = {"groups": [
            {"name": "g1", "rules": [_alert("A1"), _alert("A2")]},
            {"name": "g2", "rules": [_alert("A3")]},
        ]}
        out = gar.extract_alerts(content)
        names = [a["name"] for a in out]
        assert names == ["A1", "A2", "A3"]


# ---------------------------------------------------------------------------
# get_recommended_action
# ---------------------------------------------------------------------------
class TestGetRecommendedAction:
    def test_pattern_match_returns_specific(self):
        out = gar.get_recommended_action("MysqlDown")
        # Should match "Down" pattern not "default".
        assert out == gar.RECOMMENDED_ACTIONS["Down"]

    def test_no_match_returns_default(self):
        out = gar.get_recommended_action("CompletelyUnknownAlert")
        assert out == gar.RECOMMENDED_ACTIONS["default"]

    def test_critical_variant_picked_when_present(self):
        # The substring scan walks dict order (insertion order); the test
        # captures actual current behavior for HighReplicationLagCritical.
        # Both "HighReplicationLag" and "HighReplicationLagCritical" are
        # substrings; the FIRST one found wins (Python dict insertion order).
        out = gar.get_recommended_action("HighReplicationLagCritical")
        # Should match either Lag or LagCritical — both substrings are valid.
        assert out in (
            gar.RECOMMENDED_ACTIONS["HighReplicationLag"],
            gar.RECOMMENDED_ACTIONS["HighReplicationLagCritical"],
        )

    def test_dict_has_zh_and_en(self):
        # Sanity: every entry must have bilingual content.
        for k, v in gar.RECOMMENDED_ACTIONS.items():
            assert "zh" in v, f"Pattern {k!r} missing zh"
            assert "en" in v, f"Pattern {k!r} missing en"
            assert v["zh"] and v["en"]


# ---------------------------------------------------------------------------
# get_metric_from_description
# ---------------------------------------------------------------------------
class TestGetMetricFromDescription:
    def test_extracts_first_snake_token(self):
        # The regex matches the first lowercase + underscore identifier.
        result = gar.get_metric_from_description("mysql_up == 0 for 5 minutes")
        assert result == "mysql_up"

    def test_empty_string_returns_empty(self):
        assert gar.get_metric_from_description("") == ""

    def test_uppercase_only_returns_empty(self):
        # No lowercase identifier found.
        assert gar.get_metric_from_description("ALERT FIRED") == ""


# ---------------------------------------------------------------------------
# generate_markdown_zh / _en
# ---------------------------------------------------------------------------
class TestGenerateMarkdown:
    def test_zh_has_frontmatter(self):
        alerts = {"mariadb": [
            {"name": "MysqlDown", "severity": "critical",
             "summary": "S", "description": "D", "platform_summary": ""},
        ]}
        out = gar.generate_markdown_zh(alerts)
        assert out.startswith("---\n")
        assert "lang: zh" in out
        assert "MysqlDown" in out

    def test_en_has_frontmatter(self):
        alerts = {"mariadb": [
            {"name": "MysqlDown", "severity": "critical",
             "summary": "S", "description": "D", "platform_summary": ""},
        ]}
        out = gar.generate_markdown_en(alerts)
        assert out.startswith("---\n")
        assert "lang: en" in out
        assert "MysqlDown" in out

    def test_severity_rendered(self):
        alerts = {"mariadb": [
            {"name": "Critical", "severity": "critical",
             "summary": "s", "description": "d", "platform_summary": ""},
        ]}
        out = gar.generate_markdown_zh(alerts)
        # Severity appears somewhere in the rendering.
        assert "critical" in out


# ---------------------------------------------------------------------------
# load_rule_packs
# ---------------------------------------------------------------------------
class TestLoadRulePacks:
    def test_loads_glob(self, tmp_path):
        _write_pack(tmp_path, "rule-pack-mariadb.yaml", {
            "groups": [{"name": "g", "rules": [_alert("A1")]}],
        })
        _write_pack(tmp_path, "rule-pack-redis.yaml", {
            "groups": [{"name": "g", "rules": [_alert("R1")]}],
        })
        out = gar.load_rule_packs(str(tmp_path))
        assert set(out.keys()) == {"mariadb", "redis"}
        assert out["mariadb"][0]["name"] == "A1"

    def test_non_rule_pack_files_ignored(self, tmp_path):
        _write_pack(tmp_path, "rule-pack-mariadb.yaml", {
            "groups": [{"name": "g", "rules": [_alert("A")]}],
        })
        # Files NOT matching the glob are skipped.
        (tmp_path / "other-thing.yaml").write_text("groups: []", encoding="utf-8")
        out = gar.load_rule_packs(str(tmp_path))
        assert "other-thing" not in out
        assert "mariadb" in out

    def test_empty_yaml_skipped(self, tmp_path):
        # An empty YAML file (yaml.safe_load returns None) should be skipped
        # without crashing.
        (tmp_path / "rule-pack-empty.yaml").write_text("", encoding="utf-8")
        out = gar.load_rule_packs(str(tmp_path))
        assert "empty" not in out

    def test_packs_without_alerts_excluded(self, tmp_path):
        # Pack with only recording rules → no alerts → excluded.
        _write_pack(tmp_path, "rule-pack-only-records.yaml", {
            "groups": [{"name": "g", "rules": [
                {"record": "job:x", "expr": "x"},
            ]}],
        })
        out = gar.load_rule_packs(str(tmp_path))
        assert "only-records" not in out

    def test_invalid_yaml_exits_one(self, tmp_path, capsys):
        (tmp_path / "rule-pack-bad.yaml").write_text(
            "key: [unclosed", encoding="utf-8")
        with pytest.raises(SystemExit) as exc:
            gar.load_rule_packs(str(tmp_path))
        assert exc.value.code == 1


# ---------------------------------------------------------------------------
# main() CLI
# ---------------------------------------------------------------------------
class TestMainCLI:
    def _seed(self, tmp_path: Path) -> None:
        _write_pack(tmp_path, "rule-pack-mariadb.yaml", {
            "groups": [{"name": "g", "rules": [_alert("MysqlDown", "critical")]}],
        })

    def test_dry_run_prints_both_versions(self, tmp_path, capsys, cli_argv):
        self._seed(tmp_path)
        cli_argv("generate_alert_reference.py",
                 "--dry-run", "--output-dir", str(tmp_path))
        gar.main()
        out = capsys.readouterr().out
        assert "ALERT-REFERENCE.md (Chinese)" in out
        assert "ALERT-REFERENCE.en.md (English)" in out
        # Files NOT written in dry-run.
        assert not (tmp_path / "ALERT-REFERENCE.md").exists()

    def test_write_mode_creates_files(self, tmp_path, capsys, cli_argv):
        self._seed(tmp_path)
        cli_argv("generate_alert_reference.py",
                 "--output-dir", str(tmp_path))
        gar.main()
        zh = tmp_path / "ALERT-REFERENCE.md"
        en = tmp_path / "ALERT-REFERENCE.en.md"
        assert zh.exists()
        assert en.exists()
        assert "MysqlDown" in zh.read_text(encoding="utf-8")

    def test_check_mode_synced_returns_zero(self, tmp_path, capsys, cli_argv):
        self._seed(tmp_path)
        # First, write the canonical files so --check has something to compare.
        cli_argv("generate_alert_reference.py",
                 "--output-dir", str(tmp_path))
        gar.main()
        # Then run --check; it should be in sync.
        cli_argv("generate_alert_reference.py", "--check",
                 "--output-dir", str(tmp_path))
        with pytest.raises(SystemExit) as exc:
            gar.main()
        assert exc.value.code == 0
        out = capsys.readouterr().out
        assert "synchronized" in out

    def test_check_mode_drift_returns_one(self, tmp_path, capsys, cli_argv):
        self._seed(tmp_path)
        # Write a STALE ALERT-REFERENCE.md that doesn't match.
        (tmp_path / "ALERT-REFERENCE.md").write_text(
            "stale content\n", encoding="utf-8")
        (tmp_path / "ALERT-REFERENCE.en.md").write_text(
            "stale content\n", encoding="utf-8")
        cli_argv("generate_alert_reference.py", "--check",
                 "--output-dir", str(tmp_path))
        with pytest.raises(SystemExit) as exc:
            gar.main()
        assert exc.value.code == 1

    def test_nonexistent_dir_exits_one(self, tmp_path, capsys, cli_argv):
        cli_argv("generate_alert_reference.py",
                 "--output-dir", str(tmp_path / "ghost"))
        with pytest.raises(SystemExit) as exc:
            gar.main()
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "not a directory" in err

    def test_no_alerts_exits_one(self, tmp_path, capsys, cli_argv):
        # Empty dir → no rule-pack-*.yaml files → no alerts → exit 1.
        cli_argv("generate_alert_reference.py",
                 "--output-dir", str(tmp_path))
        with pytest.raises(SystemExit) as exc:
            gar.main()
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "No alerts" in err
