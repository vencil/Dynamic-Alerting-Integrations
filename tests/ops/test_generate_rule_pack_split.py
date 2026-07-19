"""Tests for generate_rule_pack_split.py — edge/central rule pack splitter.

Audit flagged 0% coverage. This is a release-artifact tool: it splits
Rule Packs into Federation Scenario B (edge cluster: Part 1 metric
normalisation; central cluster: Parts 2+3 threshold + alerting). A
regression at release time would silently emit malformed CRDs.

Tests cover all pure helpers + process_rule_packs orchestrator + main()
CLI exit codes. No real Kubernetes / cluster contact — every external
dep is monkeypatched.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

_TOOLS_DIR = os.path.join(os.path.dirname(__file__), '..', '..', 'scripts', 'tools', 'ops')
sys.path.insert(0, _TOOLS_DIR)

import generate_rule_pack_split as grps  # noqa: E402


# ---------------------------------------------------------------------------
# i18n helpers — light coverage
# ---------------------------------------------------------------------------
class TestI18n:
    def test_get_lang_returns_string(self):
        assert isinstance(grps.get_lang(), str)

    def test_t_picks_branch_by_lang(self, monkeypatch):
        monkeypatch.setattr(grps, "get_lang", lambda: "zh_TW")
        # When detect_cli_lang/i18n_text aren't injected, t() uses get_lang().
        monkeypatch.setattr(grps, "i18n_text", None)
        assert grps.t("中文", "english") == "中文"

        monkeypatch.setattr(grps, "get_lang", lambda: "en_US")
        assert grps.t("中文", "english") == "english"


# ---------------------------------------------------------------------------
# _safe_write — file write
# ---------------------------------------------------------------------------
class TestSafeWrite:
    def test_fallback_path_write_text(self, tmp_path, monkeypatch):
        # Force the fallback branch (write_text_secure unavailable).
        monkeypatch.setattr(grps, "write_text_secure", None)
        f = tmp_path / "out.yaml"
        grps._safe_write(str(f), "groups: []\n")
        assert f.read_text(encoding="utf-8") == "groups: []\n"

    def test_uses_write_text_secure_when_available(self, tmp_path, monkeypatch):
        called = {}

        def fake_secure(path, content):
            called["path"] = path
            called["content"] = content
            Path(path).write_text(content, encoding="utf-8")

        monkeypatch.setattr(grps, "write_text_secure", fake_secure)
        f = tmp_path / "out.yaml"
        grps._safe_write(str(f), "groups: []\n")
        assert called["path"] == str(f)
        assert called["content"] == "groups: []\n"


# ---------------------------------------------------------------------------
# extract_metrics_from_expr — pure regex
# ---------------------------------------------------------------------------
class TestExtractMetricsFromExpr:
    def test_empty_expr_returns_empty(self):
        assert grps.extract_metrics_from_expr("") == set()

    def test_non_string_returns_empty(self):
        assert grps.extract_metrics_from_expr(None) == set()
        assert grps.extract_metrics_from_expr(123) == set()

    def test_extracts_metric_with_label_selector(self):
        expr = 'http_requests_total{tenant="db-a"}'
        metrics = grps.extract_metrics_from_expr(expr)
        assert "http_requests_total" in metrics

    def test_extracts_metric_with_range_vector(self):
        expr = "rate(node_cpu_seconds_total[5m])"
        metrics = grps.extract_metrics_from_expr(expr)
        assert "node_cpu_seconds_total" in metrics
        # Built-in functions filtered out.
        assert "rate" not in metrics

    def test_filters_builtin_funcs(self):
        expr = "sum(rate(http_requests[5m])) by (tenant)"
        metrics = grps.extract_metrics_from_expr(expr)
        # http_requests is the metric; sum/rate/by are builtins.
        assert "http_requests" in metrics
        for builtin in ("sum", "rate", "by"):
            assert builtin not in metrics

    def test_filters_uppercase_labels(self):
        # Tokens starting with uppercase are treated as labels (skipped).
        expr = "metric_name{Region=\"us\"}"
        metrics = grps.extract_metrics_from_expr(expr)
        assert "metric_name" in metrics
        assert "Region" not in metrics

    def test_multiple_metrics_extracted(self):
        expr = "metric_a{x=1} + on(tenant) metric_b{y=2}"
        metrics = grps.extract_metrics_from_expr(expr)
        assert "metric_a" in metrics
        assert "metric_b" in metrics

    def test_set_operators_or_and_filtered(self):
        # Defect i: un-parenthesised `or`/`and` set operators must NOT be
        # extracted as metric names. Tokens are captured only when followed by
        # `{`, `[`, or whitespace, so lay the operands out with trailing spaces
        # (mirrors the multi-line `metric_a \n or \n metric_b` rule-pack shape).
        expr = "metric_a \n or \n metric_b \n and on(x) metric_c "
        metrics = grps.extract_metrics_from_expr(expr)
        assert metrics == {"metric_a", "metric_b", "metric_c"}
        assert "or" not in metrics
        assert "and" not in metrics

    def test_promql_functions_not_captured(self):
        # Functions are always written `fn(` (followed by `(`), so the regex
        # never captures them — they need no builtin listing.
        expr = "label_replace(vector(0), \"t\", \"$1\", \"ns\", \"(.*)\")"
        metrics = grps.extract_metrics_from_expr(expr)
        assert "label_replace" not in metrics
        assert "vector" not in metrics


# ---------------------------------------------------------------------------
# extract_recording_outputs — pure
# ---------------------------------------------------------------------------
class TestExtractRecordingOutputs:
    def test_empty_rules_returns_empty(self):
        assert grps.extract_recording_outputs([]) == set()

    def test_picks_record_field_only(self):
        rules = [
            {"record": "metric_a", "expr": "x"},
            {"alert": "X", "expr": "y"},  # alerting rule, skipped
            {"record": "metric_b", "expr": "z"},
        ]
        assert grps.extract_recording_outputs(rules) == {"metric_a", "metric_b"}


# ---------------------------------------------------------------------------
# validate_central_references_edge — pure
# ---------------------------------------------------------------------------
class TestValidateCentralReferencesEdge:
    def test_valid_when_edge_covers_central(self):
        is_valid, missing = grps.validate_central_references_edge(
            edge_outputs={"a", "b", "c"},
            central_inputs={"a", "b"},
            filename="rule-pack-x.yaml",
        )
        assert is_valid is True
        assert missing == []

    def test_invalid_when_central_references_missing_metric(self):
        is_valid, missing = grps.validate_central_references_edge(
            edge_outputs={"a"},
            central_inputs={"a", "b", "c"},
            filename="rule-pack-x.yaml",
        )
        assert is_valid is False
        assert missing == ["b", "c"]  # sorted

    def test_empty_inputs_means_valid(self):
        is_valid, missing = grps.validate_central_references_edge(
            edge_outputs=set(), central_inputs=set(), filename="x.yaml",
        )
        assert is_valid is True

    def test_central_recording_output_covers_own_reference(self):
        # Defect ii-a: a metric produced by a CENTRAL recording rule and
        # consumed by that same bundle's alert is available — not "missing in
        # edge". Here the tenant_version:* threshold is a central output.
        is_valid, missing = grps.validate_central_references_edge(
            edge_outputs={"tenant:pod_weakest_cpu_percent:max"},
            central_inputs={
                "tenant:pod_weakest_cpu_percent:max",
                "tenant_version:alert_threshold:container_cpu",
            },
            filename="rule-pack-kubernetes.yaml",
            central_outputs={"tenant_version:alert_threshold:container_cpu"},
        )
        assert is_valid is True
        assert missing == []

    def test_federated_external_inputs_excused(self):
        # Defect ii-b (federation match[]={tenant!=""}): tenant-labeled series are
        # federated so a central rule may read them — liveness up/*_up, platform-
        # injected config, AND per-tenant db-exporter raw (mysql_global_* /
        # kafka_brokers / mongodb_* / rabbitmq_* — every *Down/*NoPrimary alert
        # relies on these).
        is_valid, missing = grps.validate_central_references_edge(
            edge_outputs=set(),
            central_inputs={
                "up", "mysql_up", "oracledb_up",
                "tenant_metadata_info", "tenant_expected_exporter",
                "user_threshold", "user_state_filter", "da_config_event",
                "kafka_brokers", "mysql_global_status_uptime",
                "mongodb_mongod_replset_member_state", "rabbitmq_identity_info",
            },
            filename="rule-pack-x.yaml",
        )
        assert is_valid is True
        assert missing == []

    def test_raw_kube_on_central_flagged(self):
        # ONLY cluster-level kube-state-metrics/kubelet raw is namespace-labeled
        # (not tenant-labeled) → not federated → a central reference to it is a
        # genuine topology bug. Per-tenant exporter raw stays excused (above).
        is_valid, missing = grps.validate_central_references_edge(
            edge_outputs=set(),
            central_inputs={"kube_pod_info", "kubelet_volume_stats_available_bytes"},
            filename="rule-pack-x.yaml",
        )
        assert is_valid is False
        assert missing == ["kube_pod_info", "kubelet_volume_stats_available_bytes"]

    def test_bare_dangling_reference_still_caught(self):
        # The allowlist stays NARROW: a bare reference that matches no external
        # pattern (typo / lost recording group) is still a genuine dangling ref.
        is_valid, missing = grps.validate_central_references_edge(
            edge_outputs={"only_metric"},
            central_inputs={"only_metric", "missing_metric"},
            filename="rule-pack-x.yaml",
        )
        assert is_valid is False
        assert missing == ["missing_metric"]

    def test_recording_namespace_metric_never_treated_as_external(self):
        # Colon (recording-namespace) metrics must always be pipeline-produced,
        # so a dropped recording group is still caught (defect iii guard) — a
        # `tenant:` metric produced by nobody is flagged even though it shares a
        # prefix family with injected metrics.
        is_valid, missing = grps.validate_central_references_edge(
            edge_outputs=set(),
            central_inputs={"tenant:container_waiting_reason:count"},
            filename="rule-pack-kubernetes.yaml",
        )
        assert is_valid is False
        assert missing == ["tenant:container_waiting_reason:count"]


class TestIsExternalPipelineInput:
    def test_liveness_metrics_external(self):
        assert grps._is_external_pipeline_input("up") is True
        assert grps._is_external_pipeline_input("mysql_up") is True
        assert grps._is_external_pipeline_input("oracledb_up") is True

    def test_platform_config_metrics_external(self):
        for m in ("tenant_metadata_info", "tenant_expected_exporter",
                  "da_config_event", "user_threshold", "user_state_filter",
                  "user_severity_dedup", "user_silent_mode"):
            assert grps._is_external_pipeline_input(m) is True, m

    def test_per_tenant_exporter_raw_external(self):
        # Per-tenant exporters are tenant-labeled → federated → excused.
        for m in ("mysql_global_status_uptime", "mongodb_up", "kafka_brokers",
                  "rabbitmq_identity_info", "pg_stat_database_deadlocks",
                  "redis_up", "db2_up", "clickhouse_replication_queue"):
            assert grps._is_external_pipeline_input(m) is True, m

    def test_cluster_ksm_raw_not_external(self):
        # kube-state-metrics / kubelet raw is namespace-labeled → NOT federated
        # → NOT external (the only raw families the validator flags on central).
        for m in ("kube_pod_info", "kube_pod_labels", "kube_pod_status_phase",
                  "kubelet_volume_stats_available_bytes",
                  "kubelet_volume_stats_capacity_bytes"):
            assert grps._is_external_pipeline_input(m) is False, m

    def test_recording_namespace_not_external(self):
        for m in ("tenant:container_cpu_percent:by_container",
                  "tenant_version:alert_threshold:container_cpu",
                  "rule_pack_kubernetes:node_not_ready:core"):
            assert grps._is_external_pipeline_input(m) is False, m

    def test_bare_dangling_not_external(self):
        assert grps._is_external_pipeline_input("missing_metric") is False
        assert grps._is_external_pipeline_input("only_metric") is False


# ---------------------------------------------------------------------------
# split_rule_pack — pure
# ---------------------------------------------------------------------------
class TestSplitRulePack:
    def test_normalization_goes_to_edge(self):
        groups = [{"name": "mysql-normalization", "rules": []}]
        edge, central = grps.split_rule_pack(groups)
        assert len(edge) == 1
        assert central == []

    def test_threshold_normalization_goes_to_central(self):
        groups = [{"name": "mysql-threshold-normalization", "rules": []}]
        edge, central = grps.split_rule_pack(groups)
        # NOTE: -threshold-normalization ends with both -normalization
        # AND -threshold-normalization. The check order in the source
        # tests -threshold-normalization first → goes to central.
        assert edge == []
        assert len(central) == 1

    def test_alerts_goes_to_central(self):
        groups = [{"name": "mysql-alerts", "rules": []}]
        edge, central = grps.split_rule_pack(groups)
        assert edge == []
        assert len(central) == 1

    def test_unknown_suffix_routed_per_rule_not_dropped(self):
        # Defect iii: non-suffix groups are NO LONGER silently dropped. They are
        # routed PER RULE by data-locality — alerting rule → central, recording
        # rule → edge.
        alert_group = {"name": "weird-alerts-x",
                       "rules": [{"alert": "A", "expr": "x > 0"}]}
        record_group = {"name": "weird-state-matching",
                        "rules": [{"record": "tenant:foo:count", "expr": "x"}]}
        edge, central = grps.split_rule_pack([alert_group, record_group])
        assert alert_group in central
        assert record_group in edge

    def test_empty_non_suffix_group_preserved_not_dropped(self):
        # CodeRabbit #1171: an EMPTY non-suffix group has no rules to route, but
        # suffix groups preserve empty groups whole — so the per-rule branch must
        # preserve it too (on edge), keeping the "nothing dropped" invariant
        # symmetric. Guards a future empty non-suffix group from silently
        # vanishing.
        empty = {"name": "weird-empty", "rules": []}
        missing_rules = {"name": "weird-no-rules-key"}
        edge, central = grps.split_rule_pack([empty, missing_rules])
        edge_names = {g["name"] for g in edge}
        central_names = {g["name"] for g in central}
        assert "weird-empty" in edge_names
        assert "weird-no-rules-key" in edge_names
        # every input group appears exactly once, nowhere duplicated
        assert "weird-empty" not in central_names
        assert "weird-no-rules-key" not in central_names

    def test_mixed_non_suffix_group_split_across_planes(self):
        # A MIXED non-suffix group (raw-reading recordings + an alert over their
        # :core output, e.g. kubernetes-node-health) is split: recordings → edge,
        # alert → central (both carry the same group name). This is a CORRECTNESS
        # fix — sending the whole group to central would make the recordings
        # (reading namespace-labeled raw kube) produce nothing there → the alert
        # never fires.
        mixed = {
            "name": "node-health",
            "rules": [
                {"record": "tenant:node_owner:info", "expr": "kube_pod_info"},
                {"record": "rule_pack_x:node_not_ready:core",
                 "expr": "tenant:node_owner:info and kube_node_status_condition"},
                {"alert": "NodeNotReady",
                 "expr": "rule_pack_x:node_not_ready:core * tenant_metadata_info"},
            ],
        }
        edge, central = grps.split_rule_pack([mixed])
        assert len(edge) == 1 and len(central) == 1
        edge_records = [r.get("record") for r in edge[0]["rules"]]
        central_alerts = [r.get("alert") for r in central[0]["rules"]]
        assert edge_records == ["tenant:node_owner:info",
                                "rule_pack_x:node_not_ready:core"]
        assert central_alerts == ["NodeNotReady"]

    def test_group_follows_suffix_predicate(self):
        # Drives the fail-loud routing WARN.
        assert grps._group_follows_suffix("x-alerts") is True
        assert grps._group_follows_suffix("x-normalization") is True
        assert grps._group_follows_suffix("x-threshold-normalization") is True
        assert grps._group_follows_suffix("kubernetes-state-matching") is False
        assert grps._group_follows_suffix("node-health") is False

    def test_mixed_pack_distributes_correctly(self):
        groups = [
            {"name": "mysql-normalization", "rules": [{"record": "m"}]},
            {"name": "mysql-threshold-normalization", "rules": [{"record": "t"}]},
            {"name": "mysql-alerts", "rules": [{"alert": "A"}]},
            {"name": "sidecar-recording",  # no suffix, recording-only → edge
             "rules": [{"record": "custom_info"}]},
        ]
        edge, central = grps.split_rule_pack(groups)
        # sidecar-recording lands on edge instead of being dropped.
        assert len(edge) == 2
        assert len(central) == 2


# ---------------------------------------------------------------------------
# to_prometheus_rule_crd — CRD shape
# ---------------------------------------------------------------------------
class TestToPrometheusRuleCrd:
    def test_basic_shape(self):
        groups = [{"name": "x", "rules": []}]
        crd = grps.to_prometheus_rule_crd(
            groups, "rule-pack-clickhouse.yaml", namespace="monitoring",
        )
        assert crd["apiVersion"] == "monitoring.coreos.com/v1"
        assert crd["kind"] == "PrometheusRule"
        assert crd["metadata"]["name"] == "rule-pack-clickhouse"
        assert crd["metadata"]["namespace"] == "monitoring"
        assert crd["metadata"]["labels"]["prometheus"] == "kube-prometheus"
        assert crd["spec"]["groups"] == groups

    def test_custom_namespace(self):
        crd = grps.to_prometheus_rule_crd(
            [], "rule-pack-x.yaml", namespace="custom-ns",
        )
        assert crd["metadata"]["namespace"] == "custom-ns"

    def test_metadata_name_strips_yaml_suffix_only(self):
        # Path.stem strips final extension.
        crd = grps.to_prometheus_rule_crd([], "edge-rule-pack-x.yaml")
        assert crd["metadata"]["name"] == "edge-rule-pack-x"


# ---------------------------------------------------------------------------
# load_rule_pack — YAML IO
# ---------------------------------------------------------------------------
class TestLoadRulePack:
    def test_loads_valid_yaml(self, tmp_path):
        f = tmp_path / "pack.yaml"
        f.write_text("groups:\n  - name: x\n    rules: []\n", encoding="utf-8")
        data = grps.load_rule_pack(str(f))
        assert data == {"groups": [{"name": "x", "rules": []}]}

    def test_empty_file_returns_empty_dict(self, tmp_path):
        f = tmp_path / "empty.yaml"
        f.write_text("", encoding="utf-8")
        assert grps.load_rule_pack(str(f)) == {}

    def test_missing_file_raises_runtime(self, tmp_path):
        ghost = tmp_path / "ghost.yaml"
        with pytest.raises(RuntimeError):
            grps.load_rule_pack(str(ghost))

    def test_invalid_yaml_raises_runtime(self, tmp_path):
        f = tmp_path / "bad.yaml"
        f.write_text("groups: [unterminated", encoding="utf-8")
        with pytest.raises(RuntimeError):
            grps.load_rule_pack(str(f))

    def test_pyyaml_unavailable_raises(self, monkeypatch, tmp_path):
        monkeypatch.setattr(grps, "yaml", None)
        f = tmp_path / "x.yaml"
        f.write_text("x: 1", encoding="utf-8")
        with pytest.raises(RuntimeError):
            grps.load_rule_pack(str(f))


# ---------------------------------------------------------------------------
# dump_yaml
# ---------------------------------------------------------------------------
class TestDumpYaml:
    def test_basic_dump(self):
        out = grps.dump_yaml({"a": 1, "b": 2})
        assert "a:" in out
        assert "b:" in out

    def test_gitops_sorts_keys(self):
        # gitops=True → sort_keys=True (b after a regardless of input order)
        out = grps.dump_yaml({"b": 1, "a": 2}, gitops=True)
        assert out.index("a:") < out.index("b:")

    def test_unicode_preserved(self):
        out = grps.dump_yaml({"label": "中文"})
        assert "中文" in out

    def test_yaml_unavailable_raises(self, monkeypatch):
        monkeypatch.setattr(grps, "yaml", None)
        with pytest.raises(RuntimeError):
            grps.dump_yaml({"x": 1})


# ---------------------------------------------------------------------------
# process_rule_packs — orchestrator
# ---------------------------------------------------------------------------
class TestProcessRulePacks:
    def _make_pack(self, dir_path: Path, name: str, body: str) -> Path:
        f = dir_path / name
        f.write_text(body, encoding="utf-8")
        return f

    def test_no_packs_returns_error(self, tmp_path):
        rp = tmp_path / "rule-packs"
        rp.mkdir()
        out = tmp_path / "out"
        report = grps.process_rule_packs(str(rp), str(out))
        assert report["status"] == "error"
        assert any("No rule pack files" in e or "未找到" in e
                   for e in report["errors"])

    def test_dry_run_does_not_create_dirs_or_files(self, tmp_path):
        rp = tmp_path / "rule-packs"
        rp.mkdir()
        self._make_pack(rp, "rule-pack-x.yaml", (
            "groups:\n"
            "  - name: x-normalization\n"
            "    rules:\n"
            "      - record: metric_a\n"
            "        expr: sum(raw_metric)\n"
        ))
        out = tmp_path / "out"
        grps.process_rule_packs(str(rp), str(out), dry_run=True)
        assert not (out / "edge-rules").exists()
        assert not (out / "central-rules").exists()
        assert not (out / "validation-report.json").exists()

    def test_happy_path_writes_edge_central_and_report(self, tmp_path):
        rp = tmp_path / "rule-packs"
        rp.mkdir()
        self._make_pack(rp, "rule-pack-x.yaml", (
            "groups:\n"
            "  - name: x-normalization\n"
            "    rules:\n"
            "      - record: metric_a\n"
            "        expr: rate(raw_a[5m])\n"
            "  - name: x-threshold-normalization\n"
            "    rules:\n"
            "      - record: metric_a_threshold\n"
            "        expr: metric_a > 100\n"
            "  - name: x-alerts\n"
            "    rules:\n"
            "      - alert: HighMetricA\n"
            "        expr: metric_a_threshold > 0\n"
        ))
        out = tmp_path / "out"
        report = grps.process_rule_packs(str(rp), str(out))
        # Files written.
        assert (out / "edge-rules" / "rule-pack-x.yaml").exists()
        assert (out / "central-rules" / "rule-pack-x.yaml").exists()
        assert (out / "validation-report.json").exists()
        # Report shape.
        assert report["status"] == "success"
        assert report["validation"]["total_packs"] == 1
        assert report["validation"]["edge_rules"] == 1
        assert report["validation"]["central_rules"] == 2
        assert len(report["processed_files"]) == 1
        assert report["processed_files"][0]["edge_groups"] == 1
        assert report["processed_files"][0]["central_groups"] == 2

    def test_missing_metric_recorded_in_report(self, tmp_path):
        rp = tmp_path / "rule-packs"
        rp.mkdir()
        # Central references metric not produced by edge.
        self._make_pack(rp, "rule-pack-y.yaml", (
            "groups:\n"
            "  - name: y-normalization\n"
            "    rules:\n"
            "      - record: only_metric\n"
            "        expr: rate(raw[5m])\n"
            "  - name: y-alerts\n"
            "    rules:\n"
            "      - alert: HighX\n"
            "        expr: missing_metric > 0\n"
        ))
        out = tmp_path / "out"
        report = grps.process_rule_packs(str(rp), str(out))
        mismatches = report["validation"]["metric_mismatches"]
        assert len(mismatches) == 1
        assert mismatches[0]["file"] == "rule-pack-y.yaml"
        assert "missing_metric" in mismatches[0]["missing_in_edge"]
        # processed_files marks invalid.
        assert report["processed_files"][0]["valid"] is False

    def test_no_groups_warns(self, tmp_path):
        rp = tmp_path / "rule-packs"
        rp.mkdir()
        self._make_pack(rp, "rule-pack-empty.yaml", "groups: []\n")
        out = tmp_path / "out"
        report = grps.process_rule_packs(str(rp), str(out))
        assert any("no groups found" in w for w in report["warnings"])

    def test_yaml_load_error_recorded_per_file(self, tmp_path):
        rp = tmp_path / "rule-packs"
        rp.mkdir()
        self._make_pack(rp, "rule-pack-broken.yaml", "groups: [unterminated")
        self._make_pack(rp, "rule-pack-good.yaml", (
            "groups:\n"
            "  - name: g-normalization\n"
            "    rules: []\n"
        ))
        out = tmp_path / "out"
        report = grps.process_rule_packs(str(rp), str(out))
        # Good file processes; broken file logs error but doesn't abort.
        assert report["status"] == "error"
        assert any("rule-pack-broken.yaml" in e for e in report["errors"])

    def test_operator_mode_emits_crd_filenames(self, tmp_path):
        rp = tmp_path / "rule-packs"
        rp.mkdir()
        self._make_pack(rp, "rule-pack-op.yaml", (
            "groups:\n"
            "  - name: op-normalization\n"
            "    rules:\n"
            "      - record: m\n"
            "        expr: x\n"
            "  - name: op-alerts\n"
            "    rules:\n"
            "      - alert: A\n"
            "        expr: m > 0\n"
        ))
        out = tmp_path / "out"
        grps.process_rule_packs(str(rp), str(out), operator=True, namespace="ns")
        # Operator mode prefixes with edge-/central-.
        assert (out / "edge-rules" / "edge-rule-pack-op.yaml").exists()
        assert (out / "central-rules" / "central-rule-pack-op.yaml").exists()
        # Verify CRD shape in one of the files.
        import yaml
        crd = yaml.safe_load(
            (out / "edge-rules" / "edge-rule-pack-op.yaml").read_text("utf-8"),
        )
        assert crd["kind"] == "PrometheusRule"
        assert crd["metadata"]["namespace"] == "ns"


# ---------------------------------------------------------------------------
# main — CLI
# ---------------------------------------------------------------------------
class TestMain:
    def test_success_exits_zero(self, tmp_path, monkeypatch, capsys, cli_argv):
        # Stub process_rule_packs to return clean report.
        monkeypatch.setattr(grps, "process_rule_packs", lambda **kw: {
            "status": "success",
            "errors": [],
            "warnings": [],
            "processed_files": [],
            "validation": {
                "total_packs": 1, "edge_rules": 0, "central_rules": 0,
                "metric_mismatches": [],
            },
        })
        cli_argv("generate_rule_pack_split.py")
        with pytest.raises(SystemExit) as exc:
            grps.main()
        assert exc.value.code == 0

    def test_metric_mismatch_exits_one(self, monkeypatch, cli_argv):
        monkeypatch.setattr(grps, "process_rule_packs", lambda **kw: {
            "status": "success",
            "errors": [],
            "warnings": [],
            "processed_files": [],
            "validation": {
                "total_packs": 1, "edge_rules": 0, "central_rules": 0,
                "metric_mismatches": [{"file": "x.yaml", "missing_in_edge": ["m"]}],
            },
        })
        cli_argv("generate_rule_pack_split.py")
        with pytest.raises(SystemExit) as exc:
            grps.main()
        assert exc.value.code == 1

    def test_error_status_exits_two(self, monkeypatch, cli_argv):
        monkeypatch.setattr(grps, "process_rule_packs", lambda **kw: {
            "status": "error",
            "errors": ["YAML parse failed"],
            "warnings": [],
            "processed_files": [],
            "validation": {
                "total_packs": 0, "edge_rules": 0, "central_rules": 0,
                "metric_mismatches": [],
            },
        })
        cli_argv("generate_rule_pack_split.py")
        with pytest.raises(SystemExit) as exc:
            grps.main()
        assert exc.value.code == 2

    def test_json_flag_emits_json(self, monkeypatch, capsys, cli_argv):
        monkeypatch.setattr(grps, "process_rule_packs", lambda **kw: {
            "status": "success",
            "errors": [],
            "warnings": [],
            "processed_files": [],
            "validation": {
                "total_packs": 0, "edge_rules": 0, "central_rules": 0,
                "metric_mismatches": [],
            },
        })
        cli_argv("generate_rule_pack_split.py", "--json")
        with pytest.raises(SystemExit):
            grps.main()
        out = capsys.readouterr().out
        # Output is parseable JSON.
        payload = json.loads(out)
        assert payload["status"] == "success"

    def test_text_output_shows_warnings_and_mismatches(self, monkeypatch, capsys, cli_argv):
        monkeypatch.setattr(grps, "process_rule_packs", lambda **kw: {
            "status": "success",
            "errors": [],
            "warnings": ["wrong-name: no groups"],
            "processed_files": [],
            "validation": {
                "total_packs": 1, "edge_rules": 0, "central_rules": 0,
                "metric_mismatches": [
                    {"file": "y.yaml", "missing_in_edge": ["foo"]},
                ],
            },
        })
        cli_argv("generate_rule_pack_split.py")
        with pytest.raises(SystemExit):
            grps.main()
        out = capsys.readouterr().out
        assert "WARN: wrong-name: no groups" in out
        assert "y.yaml" in out
        assert "foo" in out

    def test_text_output_error_branch(self, monkeypatch, capsys, cli_argv):
        monkeypatch.setattr(grps, "process_rule_packs", lambda **kw: {
            "status": "error",
            "errors": ["parse failed"],
            "warnings": [],
            "processed_files": [],
            "validation": {
                "total_packs": 0, "edge_rules": 0, "central_rules": 0,
                "metric_mismatches": [],
            },
        })
        cli_argv("generate_rule_pack_split.py")
        with pytest.raises(SystemExit):
            grps.main()
        out = capsys.readouterr().out
        assert "ERROR: parse failed" in out
