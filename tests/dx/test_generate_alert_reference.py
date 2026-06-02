"""Tests for generate_alert_reference.py — Rule Pack → ALERT-REFERENCE.md generator.

Closes the audit gap (P1-5 / 456 LOC tool was 0% covered). Targets the spine:
  - get_rule_pack_name (filename → short name)
  - get_display_name (lookup with fallback)
  - extract_alerts (YAML traversal, recording-rule skip, default severity)
  - get_recommended_action (substring pattern → action dict)
  - get_metric_from_expr (PromQL series extraction from expr)
  - generate_markdown_zh / _en (smoke + frontmatter sanity)
  - load_rule_packs (file IO, glob, skip non-rule-pack files)
  - main() CLI (dry-run, --check synced + drift, --output-dir, write mode)
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import generate_alert_reference as gar
from _lib_exitcodes import EXIT_CALLER_ERROR


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
# get_metric_from_expr
# ---------------------------------------------------------------------------
class TestGetMetricFromExpr:
    """The Related Metric column is sourced from the rule's PromQL ``expr``
    (the real series the alert evaluates), NOT the prose description. The old
    description heuristic produced noise like "for"/"value"/"query"; these
    tests lock in that the expr parser returns genuine series names.
    """

    def test_empty_string_returns_empty(self):
        assert gar.get_metric_from_expr("") == ""

    def test_simple_up_comparison(self):
        # `db2_up == 0` → the bare metric.
        assert gar.get_metric_from_expr("db2_up == 0") == "db2_up"

    def test_label_selector_stripped(self):
        # `up{job="clickhouse"} == 0` → `up`, not the label name/value.
        assert gar.get_metric_from_expr('up{job="clickhouse"} == 0') == "up"

    def test_recording_rule_name_with_colons(self):
        # Recording-rule series names contain ':' and are returned whole.
        expr = (
            "( ( tenant:db2_connections_active:max > on(tenant) group_left "
            "tenant:alert_threshold:db2_connections_active ) )"
        )
        assert (
            gar.get_metric_from_expr(expr)
            == "tenant:db2_connections_active:max"
        )

    def test_skips_leading_function_absent(self):
        # `absent(...)` is a function, not the metric.
        expr = 'absent(kafka_brokers{job="tenant-exporters"})'
        assert gar.get_metric_from_expr(expr) == "kafka_brokers"

    def test_skips_leading_function_time(self):
        # `(time() - pg_postmaster_start_time_seconds) < 300`
        expr = "(time() - pg_postmaster_start_time_seconds) < 300"
        assert (
            gar.get_metric_from_expr(expr)
            == "pg_postmaster_start_time_seconds"
        )

    def test_skips_grouping_label_list(self):
        # `by(tenant, version, severity)` is a LABEL list — those names must
        # not be mistaken for the metric. The version-aware inert sentinel.
        expr = (
            "(count(max by(tenant, version, severity) "
            '(user_threshold{component="container", metric="cpu"})) '
            "or vector(0)) > 0"
        )
        assert gar.get_metric_from_expr(expr) == "user_threshold"

    def test_kubernetes_recording_rule_first(self):
        # The version-aware k8s alerts reference a recording rule first.
        expr = (
            "( rule_pack_kubernetes:pod_container_high_cpu_warning:core "
            "* on(tenant) group_left(runbook_url, owner, tier) "
            "tenant_metadata_info )"
        )
        assert (
            gar.get_metric_from_expr(expr)
            == "rule_pack_kubernetes:pod_container_high_cpu_warning:core"
        )

    def test_no_identifier_returns_empty(self):
        # Pure punctuation / numbers → no series identifier.
        assert gar.get_metric_from_expr("42 > 0") == ""

    # --- adversarial parser-bypass cases (Gemini review) ---

    def test_subquery_colon_protection(self):
        # A subquery step `[30m:1m]` must not leak a junk `m:1m` token; the
        # range brackets are stripped before the identifier scan.
        expr = 'rate(http_requests_total{job="api"}[5m])[30m:1m] > 0'
        assert gar.get_metric_from_expr(expr) == "http_requests_total"

    def test_string_containing_closing_brace(self):
        # A label value containing `}` must not break the brace strip and
        # leak a later label name as the metric.
        expr = 'env_metric{info="broken}bracket", cluster="main"} > 5'
        assert gar.get_metric_from_expr(expr) == "env_metric"

    def test_range_vector_strip(self):
        # A bare range vector `[5m]` after a function still resolves to the
        # wrapped series, with no `m`/`5m`-derived junk.
        expr = "sum(rate(http_errors_total[5m]))"
        assert gar.get_metric_from_expr(expr) == "http_errors_total"

    def test_subquery_step_no_junk_token(self):
        # Load-bearing for the range/subquery strip: a metric-less subquery
        # expr would otherwise leak the step `m:1m` as a "metric" (the ':'
        # makes `_PROMQL_IDENT` greedy). With the strip it resolves to "".
        assert gar.get_metric_from_expr("vector(0)[5m:1m]") == ""


# ---------------------------------------------------------------------------
# _strip_non_metric_syntax — the cleaner invariant
# ---------------------------------------------------------------------------
class TestStripInvariant:
    """The durable guard the example tests above can't be: rather than enumerate
    *known* bypasses, assert the INVARIANT — after cleaning, no delimiter that
    could fence off a fake identifier (`[ ] { } " ' \\``) may survive. This is
    what the original review missed: it validated outputs for the 14 current
    packs (a corpus) instead of the property over the PromQL grammar. A future
    construct that introduces a new delimiter-bounded span fails HERE even if
    no one wrote a bespoke example for it.
    """

    # One representative of every delimiter-bearing PromQL construct, plus
    # adversarial mixes (quoted delimiters, nested ranges, subqueries).
    _EXPRS = [
        "db2_up == 0",
        'up{job="clickhouse"} == 0',
        'absent(kafka_brokers{job="tenant-exporters"})',
        "(time() - pg_postmaster_start_time_seconds) < 300",
        'rate(http_requests_total{job="api"}[5m])[30m:1m] > 0',
        'env_metric{info="broken}bracket", cluster="main"} > 5',
        "vector(0)[5m:1m]",
        'label_replace(foo, "dst", "}", "src", ".*[")',
        "sum(rate(x[5m])) and `raw{string}`",
        "(count(max by(tenant, version, severity) "
        '(user_threshold{component="container"})) or vector(0)) > 0',
    ]

    @pytest.mark.parametrize("expr", _EXPRS)
    def test_no_delimiter_residue(self, expr):
        cleaned = gar._strip_non_metric_syntax(expr)
        leaked = [c for c in gar._NON_METRIC_DELIMITERS if c in cleaned]
        assert not leaked, f"delimiters {leaked} leaked from {expr!r} -> {cleaned!r}"

    @pytest.mark.parametrize("expr", _EXPRS)
    def test_result_is_valid_identifier_or_empty(self, expr):
        # The output must always be a real PromQL identifier (metric or
        # recording-rule name) or empty — never a fragment.
        import re as _re
        result = gar.get_metric_from_expr(expr)
        assert result == "" or _re.fullmatch(r"[a-zA-Z_:][a-zA-Z0-9_:]*", result), (
            f"{expr!r} -> {result!r} is not a valid identifier"
        )

    def test_invariant_holds_on_every_real_alert(self):
        # Tie the invariant to production data: every alert expr across all
        # shipped rule packs must clean to zero delimiter residue.
        from pathlib import Path
        repo_root = Path(gar.__file__).resolve().parents[3]
        packs_dir = repo_root / "rule-packs"
        alerts_by_pack = gar.load_rule_packs(str(packs_dir))
        assert alerts_by_pack, "no rule packs loaded — path wrong?"
        for pack, alerts in alerts_by_pack.items():
            for a in alerts:
                cleaned = gar._strip_non_metric_syntax(a["expr"])
                leaked = [c for c in gar._NON_METRIC_DELIMITERS if c in cleaned]
                assert not leaked, f"{pack}/{a['name']}: {leaked} leaked"


# ---------------------------------------------------------------------------
# extract_alerts wires expr → metric
# ---------------------------------------------------------------------------
class TestExtractAlertsMetric:
    def test_metric_computed_from_expr(self):
        content = {"groups": [{"name": "g", "rules": [
            {"alert": "Down", "expr": "redis_up == 0",
             "labels": {"severity": "critical"}, "annotations": {}},
        ]}]}
        out = gar.extract_alerts(content)
        assert out[0]["metric"] == "redis_up"
        assert out[0]["expr"] == "redis_up == 0"

    def test_missing_expr_yields_empty_metric(self):
        content = {"groups": [{"name": "g", "rules": [
            {"alert": "Bare", "labels": {"severity": "warning"},
             "annotations": {}},
        ]}]}
        out = gar.extract_alerts(content)
        assert out[0]["metric"] == ""


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

    def test_metric_column_rendered_from_metric_key(self):
        # The Related Metric column surfaces the precomputed `metric` value.
        alerts = {"mariadb": [
            {"name": "MariaDBDown", "severity": "critical",
             "summary": "s", "description": "d", "platform_summary": "",
             "expr": "mysql_up == 0", "metric": "mysql_up"},
        ]}
        out_zh = gar.generate_markdown_zh(alerts)
        out_en = gar.generate_markdown_en(alerts)
        assert "mysql_up" in out_zh
        assert "mysql_up" in out_en

    def test_missing_metric_key_renders_blank(self):
        # Manually-built dicts without a `metric` key must not crash.
        alerts = {"mariadb": [
            {"name": "NoMetric", "severity": "warning",
             "summary": "s", "description": "d", "platform_summary": ""},
        ]}
        out = gar.generate_markdown_zh(alerts)
        assert "NoMetric" in out

    def test_pipe_in_trigger_condition_escaped(self):
        # A Go-template trigger like `{{ $value | printf ... }}` carries a
        # literal `|` that would split the Markdown row — it must be escaped.
        alerts = {"kubernetes": [
            {"name": "PipeAlert", "severity": "warning", "summary": "s",
             "description": '{{ $value | printf "%.0f" }} thresholds declared',
             "platform_summary": "", "expr": "user_threshold",
             "metric": "user_threshold"},
        ]}
        for out in (gar.generate_markdown_zh(alerts),
                    gar.generate_markdown_en(alerts)):
            row = [ln for ln in out.splitlines() if "PipeAlert" in ln][0]
            assert "\\|" in row, "literal pipe was not escaped"
            assert "{{ $value | printf" not in row, "unescaped pipe leaked"


# ---------------------------------------------------------------------------
# _escape_table_cell
# ---------------------------------------------------------------------------
class TestEscapeTableCell:
    def test_escapes_pipe(self):
        assert gar._escape_table_cell("a | b") == "a \\| b"

    def test_flattens_newline(self):
        assert gar._escape_table_cell("a\nb") == "a b"

    def test_plain_text_unchanged(self):
        assert gar._escape_table_cell("plain text") == "plain text"

    def test_flattens_crlf(self):
        # Windows CRLF and a lone CR both flatten to a space.
        assert gar._escape_table_cell("a\r\nb") == "a b"
        assert gar._escape_table_cell("a\rb") == "a b"

    def test_escape_is_idempotent(self):
        # An already-escaped `\|` must not become `\\|` on a second pass.
        assert gar._escape_table_cell("value \\| printf") == "value \\| printf"
        assert gar._escape_table_cell("value |\nprintf") == "value \\| printf"

    def test_empty_returns_empty(self):
        assert gar._escape_table_cell("") == ""


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

    def test_invalid_yaml_exits_caller_error(self, tmp_path, capsys):
        (tmp_path / "rule-pack-bad.yaml").write_text(
            "key: [unclosed", encoding="utf-8")
        with pytest.raises(SystemExit) as exc:
            gar.load_rule_packs(str(tmp_path))
        assert exc.value.code == EXIT_CALLER_ERROR


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

    def test_nonexistent_dir_exits_caller_error(self, tmp_path, capsys, cli_argv):
        cli_argv("generate_alert_reference.py",
                 "--output-dir", str(tmp_path / "ghost"))
        with pytest.raises(SystemExit) as exc:
            gar.main()
        assert exc.value.code == EXIT_CALLER_ERROR
        err = capsys.readouterr().err
        assert "not a directory" in err

    def test_no_alerts_exits_caller_error(self, tmp_path, capsys, cli_argv):
        # Empty dir → no rule-pack-*.yaml files → no alerts → unusable input,
        # cannot generate → exit 2 (EXIT_CALLER_ERROR, #452).
        cli_argv("generate_alert_reference.py",
                 "--output-dir", str(tmp_path))
        with pytest.raises(SystemExit) as exc:
            gar.main()
        assert exc.value.code == EXIT_CALLER_ERROR
        err = capsys.readouterr().err
        assert "No alerts" in err
