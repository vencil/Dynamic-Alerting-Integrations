"""Tests for migrate_rule.py output writers + CLI orchestrator.

The existing test_migrate_ast.py covers the AST helpers (extract_metrics_ast,
rewrite_expr_prefix, etc.). This file fills the audit-flagged gap (Top 5 #5):
the CLI orchestrator + the file-writing / report-rendering layer that takes
MigrationResult objects to disk.

Covers:
  - apply_auto_suppression: pairing logic, edge cases
  - write_triage_csv: CSV shape, BOM, escaping
  - write_prefix_mapping: with/without prefix, empty results
  - write_outputs: file generation across status buckets
  - print_dry_run / print_triage: stdout shape
  - main(): --dry-run / --triage / default / --no-prefix / --no-dictionary /
    --no-ast / missing input / empty groups / YAML error
"""
from __future__ import annotations

import csv
import io
import os
import sys
from pathlib import Path

import pytest
import yaml

_TOOLS_DIR = os.path.join(os.path.dirname(__file__), '..', '..', 'scripts', 'tools', 'ops')
sys.path.insert(0, _TOOLS_DIR)

import migrate_rule as mr  # noqa: E402
from _lib_exitcodes import EXIT_CALLER_ERROR  # noqa: E402


# ---------------------------------------------------------------------------
# Fixture builders for MigrationResult
# ---------------------------------------------------------------------------
def _make_result(
    alert_name: str,
    status: str = "perfect",
    severity: str = "warning",
    tenant_config: dict | None = None,
    op: str | None = ">",
    triage_action: str | None = None,
    dict_match: dict | None = None,
    alert_rules: list | None = None,
    recording_rules: list | None = None,
    original_expr: str = "",
    notes: list | None = None,
    llm_prompt: str | None = None,
) -> mr.MigrationResult:
    r = mr.MigrationResult(alert_name, status, severity)
    r.tenant_config = tenant_config if tenant_config is not None else {}
    r.op = op
    r.triage_action = triage_action
    r.dict_match = dict_match
    r.alert_rules = alert_rules if alert_rules is not None else []
    r.recording_rules = recording_rules if recording_rules is not None else []
    r.original_expr = original_expr
    if notes is not None:
        r.notes = notes
    # Unparseable results need a non-None llm_prompt for the report writer.
    if status == "unparseable":
        r.llm_prompt = llm_prompt if llm_prompt is not None else "stub-llm-prompt"
    elif llm_prompt is not None:
        r.llm_prompt = llm_prompt
    return r


# ---------------------------------------------------------------------------
# apply_auto_suppression
# ---------------------------------------------------------------------------
class TestApplyAutoSuppression:
    def test_returns_zero_when_no_results(self):
        assert mr.apply_auto_suppression([]) == 0

    def test_skips_unparseable(self):
        results = [_make_result("X", status="unparseable")]
        assert mr.apply_auto_suppression(results) == 0

    def test_skips_use_golden(self):
        results = [_make_result(
            "X", tenant_config={"k": "v"}, triage_action="use_golden",
        )]
        assert mr.apply_auto_suppression(results) == 0

    def test_skips_results_without_tenant_config(self):
        results = [_make_result("X", tenant_config={})]
        assert mr.apply_auto_suppression(results) == 0

    def test_pairs_warning_critical_same_base_key(self):
        # Pair: warning has key "mysql_connections", critical has
        # "mysql_connections_critical". They share base_key "mysql_connections".
        warn = _make_result(
            "WarnAlert",
            severity="warning",
            tenant_config={"mysql_connections": "70"},
            alert_rules=[{"alert": "Warn", "labels": {}}],
        )
        crit = _make_result(
            "CritAlert",
            severity="critical",
            tenant_config={"mysql_connections_critical": "90"},
            alert_rules=[{"alert": "Crit", "labels": {}}],
            # apply_auto_suppression requires len(crit.recording_rules) >= 2.
            recording_rules=[{}, {}],
        )
        n = mr.apply_auto_suppression([warn, crit])
        assert n == 1
        # metric_group label injected on both.
        assert warn.alert_rules[0]["labels"]["metric_group"] == "connections"
        assert crit.alert_rules[0]["labels"]["metric_group"] == "connections"
        # Warning got an explanatory note.
        assert any("Severity Dedup" in n for n in warn.notes)

    def test_unpaired_warning_returns_zero_pairs(self):
        warn = _make_result(
            "WarnOnly",
            severity="warning",
            tenant_config={"mysql": "70"},
            alert_rules=[{"alert": "X", "labels": {}}],
        )
        assert mr.apply_auto_suppression([warn]) == 0
        # No metric_group injected.
        assert "metric_group" not in warn.alert_rules[0]["labels"]

    def test_critical_with_too_few_recording_rules_skipped(self):
        # Critical needs >= 2 recording rules to be considered paired.
        warn = _make_result(
            "Warn", severity="warning",
            tenant_config={"k": "1"},
            alert_rules=[{"alert": "W", "labels": {}}],
        )
        crit = _make_result(
            "Crit", severity="critical",
            tenant_config={"k_critical": "9"},
            alert_rules=[{"alert": "C", "labels": {}}],
            recording_rules=[{}],  # only 1 — short of the threshold.
        )
        assert mr.apply_auto_suppression([warn, crit]) == 0

    def test_metric_group_uses_last_underscore_segment(self):
        warn = _make_result(
            "W", severity="warning",
            tenant_config={"container_cpu_usage": "0.8"},
            alert_rules=[{"alert": "W", "labels": {}}],
        )
        crit = _make_result(
            "C", severity="critical",
            tenant_config={"container_cpu_usage_critical": "0.95"},
            alert_rules=[{"alert": "C", "labels": {}}],
            recording_rules=[{}, {}],
        )
        mr.apply_auto_suppression([warn, crit])
        assert warn.alert_rules[0]["labels"]["metric_group"] == "usage"


# ---------------------------------------------------------------------------
# write_triage_csv
# ---------------------------------------------------------------------------
class TestWriteTriageCsv:
    def test_writes_csv_with_bom(self, tmp_path):
        results = [
            _make_result(
                "HighCPU",
                tenant_config={"cpu": "0.9"},
                triage_action="auto",
                original_expr="cpu > 0.9",
            ),
        ]
        csv_path = mr.write_triage_csv(results, str(tmp_path), {})
        content = Path(csv_path).read_text(encoding="utf-8")
        assert content.startswith("﻿")  # Excel BOM
        # Header + 1 row.
        rows = list(csv.reader(io.StringIO(content.lstrip("﻿"))))
        assert rows[0][0] == "Alert Name"
        assert rows[1][0] == "HighCPU"
        assert rows[1][1] == "auto"  # Triage Action

    def test_truncates_long_original_expr(self, tmp_path):
        long_expr = "x" * 500
        results = [_make_result("Long", original_expr=long_expr)]
        csv_path = mr.write_triage_csv(results, str(tmp_path), {})
        content = Path(csv_path).read_text(encoding="utf-8").lstrip("﻿")
        rows = list(csv.reader(io.StringIO(content)))
        # Original Expression column (last) is truncated to 200 chars.
        assert len(rows[1][-1]) == 200

    def test_unknown_action_when_none(self, tmp_path):
        results = [_make_result("NoAction", triage_action=None)]
        csv_path = mr.write_triage_csv(results, str(tmp_path), {})
        content = Path(csv_path).read_text(encoding="utf-8").lstrip("﻿")
        rows = list(csv.reader(io.StringIO(content)))
        assert rows[1][1] == "unknown"

    def test_includes_dictionary_match_columns(self, tmp_path):
        results = [_make_result(
            "Mapped",
            dict_match={
                "maps_to": "node_cpu_seconds_total",
                "golden_rule": "infrastructure.yaml#cpu",
                "rule_pack": "infrastructure",
                "note": "exact match",
            },
        )]
        csv_path = mr.write_triage_csv(results, str(tmp_path), {})
        content = Path(csv_path).read_text(encoding="utf-8").lstrip("﻿")
        rows = list(csv.reader(io.StringIO(content)))
        # Column order: ..., Golden Standard Match, Golden Rule, Rule Pack, Dictionary Note, ...
        # Find the column indexes from the header.
        header = rows[0]
        gold_idx = header.index("Golden Standard Match")
        assert rows[1][gold_idx] == "node_cpu_seconds_total"
        assert rows[1][gold_idx + 1] == "infrastructure.yaml#cpu"

    def test_empty_results_writes_header_only(self, tmp_path):
        csv_path = mr.write_triage_csv([], str(tmp_path), {})
        content = Path(csv_path).read_text(encoding="utf-8").lstrip("﻿")
        rows = list(csv.reader(io.StringIO(content)))
        assert len(rows) == 1
        assert rows[0][0] == "Alert Name"


# ---------------------------------------------------------------------------
# write_prefix_mapping
# ---------------------------------------------------------------------------
class TestWritePrefixMapping:
    def test_no_prefix_returns_none(self, tmp_path):
        results = [_make_result(
            "A",
            tenant_config={"custom_cpu": "0.9"},
        )]
        assert mr.write_prefix_mapping(results, str(tmp_path), "") is None

    def test_no_results_returns_none(self, tmp_path):
        assert mr.write_prefix_mapping([], str(tmp_path), "custom_") is None

    def test_skips_unparseable(self, tmp_path):
        results = [_make_result(
            "Bad", status="unparseable",
            tenant_config={"custom_cpu": "0.9"},
        )]
        # Only unparseable → mapping is empty → returns None.
        assert mr.write_prefix_mapping(results, str(tmp_path), "custom_") is None

    def test_writes_yaml_with_original_metric(self, tmp_path):
        results = [_make_result(
            "A",
            tenant_config={"custom_cpu_usage": "0.9"},
            dict_match={
                "maps_to": "node_cpu_usage",
                "golden_rule": "infra.yaml#cpu",
            },
        )]
        path = mr.write_prefix_mapping(results, str(tmp_path), "custom_")
        assert path is not None
        content = Path(path).read_text(encoding="utf-8")
        # Strip header comments to parse YAML body.
        yaml_body = "\n".join(
            line for line in content.splitlines() if not line.startswith("#")
        ).strip()
        data = yaml.safe_load(yaml_body)
        assert "custom_cpu_usage" in data
        assert data["custom_cpu_usage"]["original_metric"] == "cpu_usage"
        assert data["custom_cpu_usage"]["golden_match"] == "node_cpu_usage"


# ---------------------------------------------------------------------------
# write_outputs (smoke + buckets)
# ---------------------------------------------------------------------------
class TestWriteOutputs:
    def test_creates_output_dir_and_files(self, tmp_path):
        results = [_make_result(
            "Simple", status="perfect",
            tenant_config={"custom_cpu": "0.9"},
            alert_rules=[{"alert": "Simple", "expr": "x > 0.9"}],
            recording_rules=[{"record": "tenant:custom_cpu:max", "expr": "max(x)"}],
        )]
        dest = tmp_path / "out"
        mr.write_outputs(results, str(dest), prefix="custom_", dictionary={})
        # Directory was created.
        assert dest.is_dir()
        # Some output files exist (don't pin specific filenames — implementation
        # detail; just verify SOMETHING got written).
        assert any(dest.iterdir())

    def test_handles_empty_results(self, tmp_path):
        # Empty list shouldn't crash.
        dest = tmp_path / "out"
        result = mr.write_outputs([], str(dest), prefix="custom_", dictionary={})
        # Returns 4-tuple (perfect, complex, unparseable, golden).
        assert result == (0, 0, 0, 0)

    def test_returns_correct_counts_per_bucket(self, tmp_path):
        results = [
            _make_result("P1", status="perfect", tenant_config={"custom_a": "1"},
                         alert_rules=[{"alert": "P1", "expr": "a > 1"}],
                         recording_rules=[{"record": "r1", "expr": "max(a)"}]),
            _make_result("P2", status="perfect", tenant_config={"custom_b": "2"},
                         alert_rules=[{"alert": "P2", "expr": "b > 2"}],
                         recording_rules=[{"record": "r2", "expr": "max(b)"}]),
            _make_result("C1", status="complex", tenant_config={"custom_c": "3"},
                         alert_rules=[{"alert": "C1", "expr": "c > 3"}],
                         recording_rules=[{"record": "r3", "expr": "max(c)"}]),
            _make_result("U1", status="unparseable",
                         original_expr="garbage",),
        ]
        dest = tmp_path / "out"
        n_perfect, n_complex, n_unparseable, n_golden = mr.write_outputs(
            results, str(dest), prefix="custom_", dictionary={}
        )
        assert n_perfect == 2
        assert n_complex == 1
        assert n_unparseable == 1


# ---------------------------------------------------------------------------
# render_* pure serializers (extracted from write_outputs — ROI refactor W3)
# Golden-string tests: the render_* functions build the exact file content with
# no IO. Byte-identity vs the pre-refactor writer was verified out-of-band; here
# we pin the golden strings + dedup/convergence behavior + prove write_outputs
# is a thin writer (written file == render_*() output).
# ---------------------------------------------------------------------------
def _perfect_user_selected(name, **kw):
    """_make_result perfect helper that also pins agg_mode/agg_reason (which
    _make_result leaves as None) so the recording-rules comment line is stable."""
    r = _make_result(name, status="perfect", **kw)
    r.agg_mode = "max"
    r.agg_reason = "使用者手動選擇"
    return r


class TestRenderTenantConfig:
    def test_golden_simple_perfect(self):
        r = _make_result("CPU", status="perfect", severity="warning",
                         tenant_config={"custom_cpu": "0.9"})
        expected = (
            "# ============================================================\n"
            "# Tenant Config — 複製到 conf.d/<tenant>.yaml\n"
            "# ============================================================\n"
            "# 請將以下內容縮排並貼入您專屬的 tenant 設定中，例如：\n"
            "# tenants:\n"
            "#   my-tenant-name:\n"
            '#     custom_cpu: "0.9"\n'
            "\n"
            "# --- From: CPU (severity: warning) ---\n"
            'custom_cpu: "0.9"\n'
            "\n"
        )
        assert mr.render_tenant_config([r]) == expected

    def test_excludes_unparseable_and_use_golden(self):
        good = _make_result("Good", tenant_config={"custom_a": "1"})
        bad = _make_result("Bad", status="unparseable")
        gold = _make_result("Gold", tenant_config={"custom_g": "9"},
                            triage_action="use_golden")
        out = mr.render_tenant_config([good, bad, gold])
        assert "custom_a" in out
        assert "Bad" not in out       # unparseable excluded entirely
        assert "custom_g" not in out  # use_golden excluded from tenant config
        assert "Gold" not in out

    def test_notes_and_dim_hints_rendered(self):
        r = _make_result("CPU", tenant_config={"custom_cpu": "0.9"},
                         notes=["a note"])
        r.dim_hints = [{"labels": {"instance": "db01"}}]
        out = mr.render_tenant_config([r])
        assert "# 📖 a note\n" in out
        assert "# 維度標籤替代語法:\n" in out
        assert 'custom_cpu{instance="db01"}' in out


class TestRenderRecordingRules:
    def test_golden_simple_perfect(self):
        r = _perfect_user_selected(
            "CPU", recording_rules=[{"record": "tenant:cpu:max", "expr": "max(cpu)"}])
        expected = (
            "# ============================================================\n"
            "# Platform Recording Rules — 可直接合併至 Prometheus ConfigMap\n"
            "# ============================================================\n"
            "# 收斂率: 1 條規則 → 1 條 Recording Rules (壓縮 50.0%)\n"
            "# ============================================================\n\n"
            "groups:\n"
            "  - name: custom_migrated-recording-rules\n"
            "    rules:\n"
            "      # CPU | max — 使用者手動選擇\n"
            "      - record: tenant:cpu:max\n"
            "        expr: max(cpu)\n"
            "\n"
        )
        assert mr.render_recording_rules([r], "custom_") == expected

    def test_no_prefix_group_name(self):
        r = _perfect_user_selected(
            "CPU", recording_rules=[{"record": "r", "expr": "max(x)"}])
        out = mr.render_recording_rules([r], "")
        assert "  - name: migrated-recording-rules\n" in out

    def test_dedup_skips_duplicate_record_preserving_first(self):
        r1 = _perfect_user_selected(
            "A", recording_rules=[{"record": "dup", "expr": "max(a)"}])
        r2 = _perfect_user_selected(
            "B", recording_rules=[{"record": "dup", "expr": "max(b)"},
                                  {"record": "new", "expr": "max(c)"}])
        out = mr.render_recording_rules([r1, r2], "custom_")
        # dup collapses to one occurrence (first-seen expr wins).
        assert out.count("- record: dup\n") == 1
        assert "        expr: max(a)\n" in out      # first-seen kept
        assert "        expr: max(b)\n" not in out  # duplicate dropped
        assert out.count("- record: new\n") == 1

    def test_ai_guess_warning_box_for_complex(self):
        r = _make_result("Mem", status="complex",
                         recording_rules=[{"record": "r", "expr": "sum(m)"}])
        r.agg_mode = "sum"
        r.agg_reason = "AI 猜測"
        out = mr.render_recording_rules([r], "custom_")
        assert "🚨🚨🚨 [AI 智能猜測注意] 🚨🚨🚨" in out
        assert "聚合模式為 AI 自動猜測: sum" in out

    def test_complex_user_selected_uses_else_branch(self):
        r = _make_result("Mem", status="complex",
                         recording_rules=[{"record": "r", "expr": "sum(m)"}])
        r.agg_mode = "sum"
        r.agg_reason = "使用者手動選擇"
        out = mr.render_recording_rules([r], "custom_")
        assert "[AI 智能猜測注意]" not in out
        assert "      # Mem | sum — 使用者手動選擇\n" in out

    def test_convergence_rate_reflects_dedup(self):
        # 3 parseable inputs, 2 unique records -> total_input=3, total_output=2.
        # compression = round((1 - 2/max(6,1))*100, 1) = 66.7
        results = [
            _perfect_user_selected("A", recording_rules=[{"record": "x", "expr": "1"}]),
            _perfect_user_selected("B", recording_rules=[{"record": "x", "expr": "2"}]),
            _perfect_user_selected("C", recording_rules=[{"record": "y", "expr": "3"}]),
        ]
        out = mr.render_recording_rules(results, "custom_")
        assert "# 收斂率: 3 條規則 → 2 條 Recording Rules (壓縮 66.7%)\n" in out


class TestDedupRecordingRulesHelper:
    def test_order_preserved_and_golden_unparseable_excluded(self):
        a = _make_result("A", recording_rules=[{"record": "x", "expr": "1"}])
        dup = _make_result("Dup", recording_rules=[{"record": "x", "expr": "2"}])
        b = _make_result("B", recording_rules=[{"record": "y", "expr": "3"}])
        bad = _make_result("Bad", status="unparseable",
                           recording_rules=[{"record": "z", "expr": "9"}])
        gold = _make_result("Gold", triage_action="use_golden",
                            recording_rules=[{"record": "g", "expr": "9"}])
        dedup = mr._dedup_recording_rules([a, dup, b, bad, gold])
        assert [rr["record"] for _, rr in dedup] == ["x", "y"]


class TestRenderAlertRules:
    def test_golden_with_for_labels_annotations(self):
        r = _make_result("CPU", status="perfect", alert_rules=[{
            "alert": "CPU",
            "expr": "custom_cpu > 0.9",
            "for": "5m",
            "labels": {"severity": "warning"},
            "annotations": {"summary": "hi"},
        }])
        expected = (
            "# ============================================================\n"
            "# Platform Dynamic Alert Rules — 可直接合併至 Prometheus ConfigMap\n"
            "# ============================================================\n"
            "groups:\n"
            "  - name: custom_migrated-alert-rules\n"
            "    rules:\n"
            "      # --- CPU ---\n"
            "      - alert: CPU\n"
            "        expr: |\n"
            "          custom_cpu > 0.9\n"
            "        for: 5m\n"
            "        labels:\n"
            "          severity: warning\n"
            "        annotations:\n"
            '          summary: "hi"\n'
            "\n"
        )
        assert mr.render_alert_rules([r], "custom_") == expected

    def test_multiline_expr_literal_block(self):
        r = _make_result("Multi", alert_rules=[{"alert": "M", "expr": "a\nand b"}])
        out = mr.render_alert_rules([r], "custom_")
        assert "        expr: |\n          a\n          and b\n" in out

    def test_no_prefix_group_name(self):
        r = _make_result("CPU", alert_rules=[{"alert": "C", "expr": "x"}])
        out = mr.render_alert_rules([r], "")
        assert "  - name: migrated-alert-rules\n" in out

    def test_excludes_unparseable_and_use_golden(self):
        good = _make_result("Good", alert_rules=[{"alert": "G", "expr": "x"}])
        bad = _make_result("Bad", status="unparseable")
        gold = _make_result("Gold", alert_rules=[{"alert": "Z", "expr": "z"}],
                            triage_action="use_golden")
        out = mr.render_alert_rules([good, bad, gold], "custom_")
        assert "--- Good ---" in out
        assert "Bad" not in out
        assert "Gold" not in out


class TestRenderReport:
    def test_golden_single_perfect(self):
        r = _perfect_user_selected(
            "CPU", recording_rules=[{"record": "r1", "expr": "max(cpu)"}])
        # engine label is environment-dependent (promql-parser present or not).
        engine = "AST" if mr.HAS_AST else "regex"
        expected = (
            "=" * 60 + "\n"
            + f"遷移報告 (Migration Report) — v4 ({engine} engine)\n"
            + "=" * 60 + "\n\n"
            + "總規則數: 1\n"
            + "  ✅ 完美解析: 1\n"
            + "  ⚠️  複雜表達式 (已自動猜測): 0\n"
            + "  🚨 無法解析 (需 LLM 協助): 0\n"
            + "  📖 建議使用黃金標準: 0\n\n"
            + "📊 收斂率統計:\n"
            + "  輸入: 1 條傳統規則\n"
            + "  輸出: 1 條 Recording Rules + 1 條 Alert Rules\n"
            + "  壓縮率: 50.0%\n"
            + "\n"
            + "-" * 40 + "\n"
            + "✅ 完美解析的規則\n"
            + "-" * 40 + "\n"
            + "  • CPU: max (使用者手動選擇)\n"
            + "\n"
        )
        assert mr.render_report([r]) == expected

    def test_use_golden_section(self):
        r = _make_result("Gold", status="perfect", triage_action="use_golden",
                         dict_match={"golden_rule": "g.yaml#cpu",
                                     "maps_to": "node_cpu",
                                     "rule_pack": "infra",
                                     "note": "exact"})
        out = mr.render_report([r])
        assert "📖 建議使用黃金標準 — 請用 scaffold_tenant.py 設定閾值\n" in out
        assert "  • Gold\n" in out
        assert "    → 黃金標準: g.yaml#cpu\n" in out
        assert "    → Metric Key: node_cpu\n" in out
        assert "    → Rule Pack: infra\n" in out

    def test_unparseable_llm_prompt_section(self):
        r = _make_result("Bad", status="unparseable", llm_prompt="PROMPT")
        out = mr.render_report([r])
        assert "🚨 無法自動解析" in out
        assert "\n### Bad ###\nPROMPT\n" in out

    def test_convergence_output_count_reflects_dedup(self):
        a = _perfect_user_selected("A", recording_rules=[{"record": "x", "expr": "1"}])
        b = _perfect_user_selected("B", recording_rules=[{"record": "x", "expr": "2"}])
        out = mr.render_report([a, b])
        # duplicate record collapses -> 1 Recording Rule reported.
        assert "  輸出: 1 條 Recording Rules" in out


class TestWriteOutputsIsThinWriter:
    """Byte-identity protection: the old inline serialization path is gone, so
    instead we assert each written file byte-equals its render_*() output —
    proving write_outputs only persists the renderer strings (no extra
    transform)."""
    def test_written_files_equal_renderer_output(self, tmp_path):
        results = [
            _perfect_user_selected(
                "P", tenant_config={"custom_a": "1"},
                alert_rules=[{"alert": "P", "expr": "a > 1"}],
                recording_rules=[{"record": "r1", "expr": "max(a)"}]),
            _make_result("U", status="unparseable", llm_prompt="LLM"),
        ]
        dest = tmp_path / "out"
        mr.write_outputs(results, str(dest), prefix="custom_", dictionary={})
        assert (dest / "tenant-config.yaml").read_text(encoding="utf-8") == \
            mr.render_tenant_config(results)
        assert (dest / "platform-recording-rules.yaml").read_text(encoding="utf-8") == \
            mr.render_recording_rules(results, "custom_")
        assert (dest / "platform-alert-rules.yaml").read_text(encoding="utf-8") == \
            mr.render_alert_rules(results, "custom_")
        assert (dest / "migration-report.txt").read_text(encoding="utf-8") == \
            mr.render_report(results)


# ---------------------------------------------------------------------------
# print_dry_run / print_triage
# ---------------------------------------------------------------------------
class TestPrintDryRun:
    def test_prints_each_result(self, capsys):
        results = [
            _make_result("Alert1", status="perfect"),
            _make_result("Alert2", status="complex"),
        ]
        mr.print_dry_run(results)
        out = capsys.readouterr().out
        assert "Alert1" in out
        assert "Alert2" in out

    def test_handles_empty_results(self, capsys):
        # Smoke: empty list shouldn't crash.
        mr.print_dry_run([])
        capsys.readouterr()


class TestPrintTriage:
    def test_groups_by_action(self, capsys):
        results = [
            _make_result("Auto1", triage_action="auto"),
            _make_result("Auto2", triage_action="auto"),
            _make_result("Review1", triage_action="review"),
        ]
        mr.print_triage(results)
        out = capsys.readouterr().out
        # The summary should mention the categories.
        assert "auto" in out.lower() or "Auto" in out

    def test_handles_empty_results(self, capsys):
        mr.print_triage([])
        capsys.readouterr()


# ---------------------------------------------------------------------------
# main — CLI orchestrator
# ---------------------------------------------------------------------------
class TestMain:
    def _write_yaml(self, path: Path, content: str) -> None:
        path.write_text(content, encoding="utf-8")

    SIMPLE_YAML = (
        "groups:\n"
        "  - name: example\n"
        "    rules:\n"
        "      - alert: HighCPU\n"
        "        expr: cpu_usage > 0.9\n"
        "        for: 5m\n"
        "        labels:\n"
        "          severity: warning\n"
        "        annotations:\n"
        "          summary: 'High CPU'\n"
    )

    def test_missing_input_file_exits_caller_error(self, monkeypatch, tmp_path, capsys, cli_argv):
        ghost = tmp_path / "ghost.yaml"
        cli_argv("migrate_rule.py", str(ghost))
        with pytest.raises(SystemExit) as exc:
            mr.main()
        assert exc.value.code == EXIT_CALLER_ERROR
        err = capsys.readouterr().err
        assert "Error reading YAML" in err

    def test_invalid_yaml_exits_caller_error(self, monkeypatch, tmp_path, capsys, cli_argv):
        f = tmp_path / "bad.yaml"
        f.write_text("groups: [unterminated", encoding="utf-8")
        cli_argv("migrate_rule.py", str(f))
        with pytest.raises(SystemExit) as exc:
            mr.main()
        assert exc.value.code == EXIT_CALLER_ERROR
        err = capsys.readouterr().err
        assert "Error reading YAML" in err

    def test_no_groups_returns_cleanly(self, monkeypatch, tmp_path, capsys, cli_argv):
        f = tmp_path / "empty.yaml"
        f.write_text("groups: []\n", encoding="utf-8")
        cli_argv("migrate_rule.py", str(f))
        # No SystemExit — main() returns normally.
        mr.main()
        out = capsys.readouterr().out
        assert "No 'groups' found" in out

    def test_groups_without_rules_returns_cleanly(self, monkeypatch, tmp_path, capsys, cli_argv):
        f = tmp_path / "empty-rules.yaml"
        f.write_text(
            "groups:\n  - name: x\n    rules: []\n",
            encoding="utf-8",
        )
        cli_argv("migrate_rule.py", str(f))
        mr.main()
        out = capsys.readouterr().out
        assert "No alert rules found" in out

    def test_dry_run_does_not_create_output_dir(self, monkeypatch, tmp_path, cli_argv):
        f = tmp_path / "in.yaml"
        f.write_text(self.SIMPLE_YAML, encoding="utf-8")
        out_dir = tmp_path / "out"
        cli_argv("migrate_rule.py", str(f), "-o", str(out_dir), "--dry-run")
        mr.main()
        # --dry-run doesn't create files.
        assert not out_dir.exists()

    def test_triage_creates_csv(self, monkeypatch, tmp_path, capsys, cli_argv):
        f = tmp_path / "in.yaml"
        f.write_text(self.SIMPLE_YAML, encoding="utf-8")
        out_dir = tmp_path / "out"
        cli_argv("migrate_rule.py", str(f), "-o", str(out_dir), "--triage")
        mr.main()
        csv_path = out_dir / "triage-report.csv"
        assert csv_path.exists()
        out = capsys.readouterr().out
        assert "CSV" in out

    def test_default_run_writes_outputs(self, monkeypatch, tmp_path, capsys, cli_argv):
        f = tmp_path / "in.yaml"
        f.write_text(self.SIMPLE_YAML, encoding="utf-8")
        out_dir = tmp_path / "out"
        cli_argv("migrate_rule.py", str(f), "-o", str(out_dir))
        mr.main()
        # Output directory exists and contains files.
        assert out_dir.is_dir()
        assert any(out_dir.iterdir())

    def test_no_prefix_strips_custom_prefix(self, monkeypatch, tmp_path, cli_argv):
        f = tmp_path / "in.yaml"
        f.write_text(self.SIMPLE_YAML, encoding="utf-8")
        out_dir = tmp_path / "out"
        cli_argv("migrate_rule.py", str(f), "-o", str(out_dir), "--no-prefix")
        mr.main()
        # No prefix-mapping.yaml when --no-prefix.
        assert not (out_dir / "prefix-mapping.yaml").exists()

    def test_no_dictionary_disables_dict_loading(self, monkeypatch, tmp_path, cli_argv):
        # When --no-dictionary, load_metric_dictionary must NOT be called.
        f = tmp_path / "in.yaml"
        f.write_text(self.SIMPLE_YAML, encoding="utf-8")
        out_dir = tmp_path / "out"

        called = {"loaded": False}

        def fake_load(*args, **kwargs):
            called["loaded"] = True
            return {}

        monkeypatch.setattr(mr, "load_metric_dictionary", fake_load)
        cli_argv("migrate_rule.py", str(f), "-o", str(out_dir), "--no-dictionary", "--dry-run")
        mr.main()
        assert called["loaded"] is False

    def test_no_ast_warns_when_promql_parser_missing(
        self, monkeypatch, tmp_path, capsys, cli_argv,
    ):
        # Force HAS_AST=False so the warning branch fires under --no-ast=False.
        # (We deliberately do NOT pass --no-ast — the warn fires only when
        # the user wanted AST but parser is missing.)
        f = tmp_path / "in.yaml"
        f.write_text(self.SIMPLE_YAML, encoding="utf-8")
        out_dir = tmp_path / "out"
        monkeypatch.setattr(mr, "HAS_AST", False)
        cli_argv("migrate_rule.py", str(f), "-o", str(out_dir), "--dry-run")
        mr.main()
        err = capsys.readouterr().err
        assert "promql-parser" in err
