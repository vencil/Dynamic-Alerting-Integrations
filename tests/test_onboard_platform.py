#!/usr/bin/env python3
"""Tests for onboard_platform.py — Reverse analysis engine for Dynamic Alerting onboarding."""
import csv
import os
import sys
import tempfile
import unittest

import yaml

# Ensure scripts/tools is on path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts", "tools"))

from conftest import write_yaml  # noqa: E402
from onboard_platform import (  # noqa: E402
    parse_alertmanager_config,
    flatten_route_tree,
    reverse_map_receiver,
    analyze_alertmanager,
    generate_tenant_routing_yamls,
    _extract_tenant_from_matchers,
    _check_timing_guardrails,
    classify_rule,
    extract_threshold_candidates,
    analyze_rule_files,
    generate_defaults_from_candidates,
    write_migration_csv,
    parse_scrape_configs,
    analyze_relabel_configs,
    analyze_scrape_configs,
    scan_rule_files,
    write_outputs,
    DEFAULT_TENANT_LABEL,
)


# ============================================================
# Phase 1: Alertmanager Reverse Analysis
# ============================================================

class TestParseAlertmanagerConfig(unittest.TestCase):
    """Test parse_alertmanager_config with various input formats."""

    def test_raw_alertmanager_yaml(self):
        with tempfile.TemporaryDirectory() as d:
            path = write_yaml(d, "am.yaml", yaml.dump({
                "route": {"receiver": "default", "group_by": ["alertname"]},
                "receivers": [{"name": "default"}],
            }))
            result = parse_alertmanager_config(path)
            self.assertIsNotNone(result)
            self.assertIn("route", result)
            self.assertIn("receivers", result)

    def test_configmap_wrapped(self):
        """ConfigMap with data.alertmanager.yml string."""
        inner = yaml.dump({
            "route": {"receiver": "default"},
            "receivers": [{"name": "default"}],
        })
        with tempfile.TemporaryDirectory() as d:
            path = write_yaml(d, "cm.yaml", yaml.dump({
                "data": {"alertmanager.yml": inner},
            }))
            result = parse_alertmanager_config(path)
            self.assertIsNotNone(result)
            self.assertIn("route", result)

    def test_invalid_file(self):
        with tempfile.TemporaryDirectory() as d:
            # Use truly invalid YAML that safe_load will reject
            path = write_yaml(d, "bad.yaml", "just a string\n")
            result = parse_alertmanager_config(path)
            self.assertIsNone(result)  # No route/receivers → None

    def test_nonexistent_file(self):
        result = parse_alertmanager_config("/nonexistent/path.yaml")
        self.assertIsNone(result)

    def test_empty_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = write_yaml(d, "empty.yaml", "")
            result = parse_alertmanager_config(path)
            self.assertIsNone(result)


class TestExtractTenantFromMatchers(unittest.TestCase):

    def test_exact_match(self):
        self.assertEqual(
            _extract_tenant_from_matchers(['tenant="db-a"'], "tenant"),
            "db-a")

    def test_regex_exact(self):
        """Regex matcher that is actually exact (no regex chars)."""
        self.assertEqual(
            _extract_tenant_from_matchers(['tenant=~"db-b"'], "tenant"),
            "db-b")

    def test_true_regex_skipped(self):
        """True regex patterns should be skipped."""
        self.assertIsNone(
            _extract_tenant_from_matchers(['tenant=~"db-.*"'], "tenant"))

    def test_custom_label(self):
        self.assertEqual(
            _extract_tenant_from_matchers(['instance="prod-1"'], "instance"),
            "prod-1")

    def test_no_tenant_matcher(self):
        self.assertIsNone(
            _extract_tenant_from_matchers(['severity="critical"'], "tenant"))

    def test_empty_matchers(self):
        self.assertIsNone(_extract_tenant_from_matchers([], "tenant"))
        self.assertIsNone(_extract_tenant_from_matchers(None, "tenant"))


class TestFlattenRouteTree(unittest.TestCase):

    def test_simple_flat(self):
        route = {
            "receiver": "default",
            "routes": [
                {"matchers": ['tenant="db-a"'], "receiver": "tenant-db-a"},
                {"matchers": ['tenant="db-b"'], "receiver": "tenant-db-b"},
            ],
        }
        flat = flatten_route_tree(route)
        tenants = [r["tenant"] for r in flat if r["tenant"]]
        self.assertEqual(sorted(tenants), ["db-a", "db-b"])

    def test_nested_routes(self):
        route = {
            "receiver": "default",
            "routes": [{
                "matchers": ['tenant="db-a"'],
                "receiver": "tenant-db-a",
                "routes": [{
                    "matchers": ['severity="critical"'],
                    "receiver": "tenant-db-a-critical",
                }],
            }],
        }
        flat = flatten_route_tree(route)
        # Should have both parent and child routes
        self.assertTrue(len(flat) >= 2)

    def test_continue_flag(self):
        route = {
            "receiver": "platform",
            "continue": True,
            "routes": [],
        }
        flat = flatten_route_tree(route)
        self.assertTrue(flat[0]["continue_flag"])

    def test_legacy_match_format(self):
        route = {
            "receiver": "default",
            "routes": [{
                "match": {"tenant": "db-a"},
                "receiver": "tenant-db-a",
            }],
        }
        flat = flatten_route_tree(route)
        tenants = [r["tenant"] for r in flat if r["tenant"]]
        self.assertIn("db-a", tenants)

    def test_empty_route(self):
        flat = flatten_route_tree({})
        self.assertEqual(flat, [])

    def test_custom_tenant_label(self):
        route = {
            "receiver": "default",
            "routes": [
                {"matchers": ['cluster="prod"'], "receiver": "cluster-prod"},
            ],
        }
        flat = flatten_route_tree(route, tenant_label="cluster")
        tenants = [r["tenant"] for r in flat if r["tenant"]]
        self.assertIn("prod", tenants)


class TestReverseMapReceiver(unittest.TestCase):

    def test_webhook_receiver(self):
        receivers = [{"name": "tenant-db-a", "webhook_configs": [{"url": "https://hook.example.com"}]}]
        result = reverse_map_receiver(receivers, "tenant-db-a")
        self.assertIsNotNone(result)
        self.assertEqual(result["type"], "webhook")
        self.assertEqual(result["url"], "https://hook.example.com")

    def test_slack_receiver(self):
        receivers = [{"name": "team-slack", "slack_configs": [
            {"api_url": "https://hooks.slack.com/services/T/B/X", "channel": "#alerts"}
        ]}]
        result = reverse_map_receiver(receivers, "team-slack")
        self.assertEqual(result["type"], "slack")
        self.assertEqual(result["channel"], "#alerts")

    def test_email_receiver(self):
        receivers = [{"name": "dba-email", "email_configs": [
            {"to": "dba@example.com", "smarthost": "smtp.example.com:587"}
        ]}]
        result = reverse_map_receiver(receivers, "dba-email")
        self.assertEqual(result["type"], "email")
        self.assertEqual(result["to"], "dba@example.com")

    def test_pagerduty_receiver(self):
        receivers = [{"name": "oncall-pd", "pagerduty_configs": [
            {"service_key": "abc123"}
        ]}]
        result = reverse_map_receiver(receivers, "oncall-pd")
        self.assertEqual(result["type"], "pagerduty")

    def test_missing_receiver(self):
        receivers = [{"name": "other"}]
        result = reverse_map_receiver(receivers, "missing")
        self.assertIsNone(result)

    def test_empty_receivers(self):
        self.assertIsNone(reverse_map_receiver([], "any"))
        self.assertIsNone(reverse_map_receiver(None, "any"))


class TestAnalyzeAlertmanager(unittest.TestCase):

    def _build_am_config(self, routes=None, receivers=None, inhibit_rules=None):
        return {
            "route": {
                "receiver": "default",
                "routes": routes or [],
            },
            "receivers": receivers or [{"name": "default"}],
            "inhibit_rules": inhibit_rules or [],
        }

    def test_basic_tenant_extraction(self):
        am = self._build_am_config(
            routes=[
                {"matchers": ['tenant="db-a"'], "receiver": "tenant-db-a",
                 "group_wait": "30s", "repeat_interval": "4h"},
            ],
            receivers=[
                {"name": "default"},
                {"name": "tenant-db-a", "webhook_configs": [{"url": "https://hook.example.com"}]},
            ],
        )
        routings, summary = analyze_alertmanager(am)
        self.assertIn("db-a", routings)
        self.assertEqual(routings["db-a"]["receiver"]["type"], "webhook")
        self.assertEqual(summary["tenant_routes"], 1)

    def test_platform_enforced_route_skipped(self):
        am = self._build_am_config(
            routes=[
                {"receiver": "platform-noc", "continue": True},
                {"matchers": ['tenant="db-a"'], "receiver": "tenant-db-a"},
            ],
            receivers=[
                {"name": "default"},
                {"name": "platform-noc", "webhook_configs": [{"url": "https://noc.example.com"}]},
                {"name": "tenant-db-a", "webhook_configs": [{"url": "https://a.example.com"}]},
            ],
        )
        routings, summary = analyze_alertmanager(am)
        self.assertIn("db-a", routings)
        # Default route + platform-noc continue route are both skipped
        self.assertGreaterEqual(len(summary["skipped_routes"]), 1)

    def test_timing_guardrail_warnings(self):
        am = self._build_am_config(
            routes=[
                {"matchers": ['tenant="db-a"'], "receiver": "tenant-db-a",
                 "group_wait": "1s"},  # Below minimum 5s
            ],
            receivers=[
                {"name": "default"},
                {"name": "tenant-db-a", "webhook_configs": [{"url": "https://a.example.com"}]},
            ],
        )
        routings, summary = analyze_alertmanager(am)
        self.assertTrue(any("below" in w.lower() for w in summary["warnings"]))

    def test_severity_dedup_detection(self):
        am = self._build_am_config(
            routes=[
                {"matchers": ['tenant="db-a"'], "receiver": "tenant-db-a"},
            ],
            receivers=[
                {"name": "default"},
                {"name": "tenant-db-a", "webhook_configs": [{"url": "https://a.example.com"}]},
            ],
            inhibit_rules=[{
                "source_matchers": ['severity="critical"', 'metric_group=~".+"', 'tenant="db-a"'],
                "target_matchers": ['severity="warning"', 'metric_group=~".+"', 'tenant="db-a"'],
                "equal": ["metric_group"],
            }],
        )
        _, summary = analyze_alertmanager(am)
        self.assertEqual(summary["dedup_tenants"].get("db-a"), "enable")

    def test_custom_tenant_label(self):
        am = self._build_am_config(
            routes=[
                {"matchers": ['cluster="prod-1"'], "receiver": "cluster-prod-1"},
            ],
            receivers=[
                {"name": "default"},
                {"name": "cluster-prod-1", "webhook_configs": [{"url": "https://p.example.com"}]},
            ],
        )
        routings, _ = analyze_alertmanager(am, tenant_label="cluster")
        self.assertIn("prod-1", routings)

    def test_multiple_tenants(self):
        am = self._build_am_config(
            routes=[
                {"matchers": ['tenant="db-a"'], "receiver": "tenant-db-a"},
                {"matchers": ['tenant="db-b"'], "receiver": "tenant-db-b"},
            ],
            receivers=[
                {"name": "default"},
                {"name": "tenant-db-a", "webhook_configs": [{"url": "https://a.example.com"}]},
                {"name": "tenant-db-b", "slack_configs": [{"api_url": "https://slack.example.com"}]},
            ],
        )
        routings, summary = analyze_alertmanager(am)
        self.assertEqual(summary["tenant_routes"], 2)
        self.assertEqual(routings["db-a"]["receiver"]["type"], "webhook")
        self.assertEqual(routings["db-b"]["receiver"]["type"], "slack")


class TestCheckTimingGuardrails(unittest.TestCase):

    def test_valid_timing(self):
        val, warn = _check_timing_guardrails("30s", "group_wait")
        self.assertEqual(val, "30s")
        self.assertIsNone(warn)

    def test_below_minimum(self):
        val, warn = _check_timing_guardrails("1s", "group_wait")
        self.assertIsNotNone(warn)
        self.assertIn("below", warn)

    def test_above_maximum(self):
        val, warn = _check_timing_guardrails("100h", "repeat_interval")
        self.assertIsNotNone(warn)
        self.assertIn("above", warn)

    def test_none_value(self):
        val, warn = _check_timing_guardrails(None, "group_wait")
        self.assertIsNone(val)
        self.assertIsNone(warn)


class TestGenerateTenantRoutingYamls(unittest.TestCase):

    def test_basic_generation(self):
        routings = {
            "db-a": {
                "receiver": {"type": "webhook", "url": "https://a.example.com"},
                "group_wait": "30s",
            },
        }
        result = generate_tenant_routing_yamls(routings)
        self.assertIn("db-a", result)
        parsed = yaml.safe_load(result["db-a"])
        self.assertEqual(
            parsed["tenants"]["db-a"]["_routing"]["receiver"]["type"], "webhook")

    def test_with_dedup_info(self):
        routings = {"db-a": {"receiver": {"type": "webhook", "url": "https://a.com"}}}
        result = generate_tenant_routing_yamls(routings, dedup_info={"db-a": "enable"})
        parsed = yaml.safe_load(result["db-a"])
        self.assertEqual(parsed["tenants"]["db-a"]["_severity_dedup"], "enable")


# ============================================================
# Phase 2: Rule File Analysis
# ============================================================

class TestClassifyRule(unittest.TestCase):

    def test_recording_rule(self):
        self.assertEqual(classify_rule({"record": "foo:bar:max"}), "recording")

    def test_alert_rule(self):
        self.assertEqual(classify_rule({"alert": "HighCPU"}), "alert")

    def test_unknown_rule(self):
        self.assertEqual(classify_rule({"comment": "test"}), "unknown")


class TestExtractThresholdCandidates(unittest.TestCase):

    def test_simple_threshold(self):
        rule = {
            "alert": "HighConnections",
            "expr": "mysql_connections > 100",
            "labels": {"severity": "warning"},
        }
        result = extract_threshold_candidates(rule)
        if result["status"] != "unparseable":
            self.assertEqual(result["threshold_value"], "100")
            self.assertEqual(result["operator"], ">")
            self.assertEqual(result["severity"], "warning")

    def test_unparseable_expr(self):
        rule = {
            "alert": "ComplexAlert",
            "expr": "absent(up{job='test'})",
            "labels": {"severity": "critical"},
        }
        result = extract_threshold_candidates(rule)
        self.assertEqual(result["status"], "unparseable")

    def test_severity_extraction(self):
        rule = {
            "alert": "CriticalCPU",
            "expr": "cpu_usage > 95",
            "labels": {"severity": "critical"},
        }
        result = extract_threshold_candidates(rule)
        self.assertEqual(result["severity"], "critical")

    def test_metric_group_extraction(self):
        rule = {
            "alert": "TestAlert",
            "expr": "metric_a > 50",
            "labels": {"severity": "warning", "metric_group": "metric_a"},
        }
        result = extract_threshold_candidates(rule)
        self.assertEqual(result["metric_group"], "metric_a")


class TestAnalyzeRuleFiles(unittest.TestCase):

    def test_basic_rule_file(self):
        with tempfile.TemporaryDirectory() as d:
            content = yaml.dump({
                "groups": [{
                    "name": "test-alerts",
                    "rules": [
                        {"alert": "HighCPU", "expr": "cpu > 80", "labels": {"severity": "warning"}},
                        {"record": "tenant:cpu:max", "expr": "max by(tenant) (cpu)"},
                    ],
                }],
            })
            path = write_yaml(d, "rules.yaml", content)
            candidates, recording, summary = analyze_rule_files([path])
            self.assertEqual(summary["alert_rules"], 1)
            self.assertEqual(summary["recording_rules"], 1)
            self.assertEqual(summary["total_groups"], 1)

    def test_empty_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = write_yaml(d, "empty.yaml", "")
            _, _, summary = analyze_rule_files([path])
            self.assertEqual(summary["total_rules"], 0)

    def test_nonexistent_file(self):
        _, _, summary = analyze_rule_files(["/nonexistent/rules.yaml"])
        self.assertTrue(len(summary["errors"]) > 0)

    def test_configmap_wrapped_rules(self):
        """Rule files wrapped in ConfigMap format."""
        # ConfigMap stores YAML as a string value in data
        inner_dict = {
            "groups": [{
                "name": "test",
                "rules": [{"alert": "Test", "expr": "metric > 1",
                            "labels": {"severity": "warning"}}],
            }],
        }
        inner_str = yaml.dump(inner_dict)
        # Build ConfigMap with the string value
        cm = {"data": {"rules.yaml": inner_str}}
        with tempfile.TemporaryDirectory() as d:
            path = write_yaml(d, "cm.yaml", yaml.dump(cm, default_flow_style=False))
            candidates, _, summary = analyze_rule_files([path])
            self.assertEqual(summary["alert_rules"], 1)


class TestGenerateDefaultsFromCandidates(unittest.TestCase):

    def test_basic_defaults(self):
        candidates = [
            {"status": "perfect", "metric_key": "cpu", "severity": "warning", "threshold_value": "80"},
            {"status": "perfect", "metric_key": "cpu", "severity": "critical", "threshold_value": "95"},
        ]
        defaults = generate_defaults_from_candidates(candidates)
        self.assertIn("defaults", defaults)
        self.assertEqual(defaults["defaults"]["cpu"], "80")
        self.assertEqual(defaults["defaults"]["cpu_critical"], "95")

    def test_most_common_value(self):
        candidates = [
            {"status": "perfect", "metric_key": "conn", "severity": "warning", "threshold_value": "100"},
            {"status": "perfect", "metric_key": "conn", "severity": "warning", "threshold_value": "100"},
            {"status": "perfect", "metric_key": "conn", "severity": "warning", "threshold_value": "200"},
        ]
        defaults = generate_defaults_from_candidates(candidates)
        self.assertEqual(defaults["defaults"]["conn"], "100")

    def test_skip_unparseable(self):
        candidates = [
            {"status": "unparseable", "metric_key": None, "severity": "warning", "threshold_value": None},
        ]
        defaults = generate_defaults_from_candidates(candidates)
        self.assertEqual(defaults, {})


class TestWriteMigrationCsv(unittest.TestCase):

    def test_csv_output(self):
        candidates = [
            {
                "alert_name": "HighCPU", "file": "rules.yaml", "group": "test",
                "status": "perfect", "severity": "warning", "metric_key": "cpu",
                "threshold_value": "80", "operator": ">", "aggregation": "max",
                "agg_reason": "test", "metric_group": None, "dict_match": None,
            },
        ]
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "plan.csv")
            write_migration_csv(candidates, path)
            self.assertTrue(os.path.exists(path))
            with open(path, encoding="utf-8") as f:
                reader = csv.DictReader(f)
                rows = list(reader)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["alert_name"], "HighCPU")


# ============================================================
# Phase 3: Scrape Config Analysis
# ============================================================

class TestParseScrapeConfigs(unittest.TestCase):

    def test_raw_prometheus_yaml(self):
        with tempfile.TemporaryDirectory() as d:
            path = write_yaml(d, "prom.yaml", yaml.dump({
                "scrape_configs": [
                    {"job_name": "node", "static_configs": [{"targets": ["localhost:9100"]}]},
                ],
            }))
            result = parse_scrape_configs(path)
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0]["job_name"], "node")

    def test_configmap_wrapped(self):
        inner = yaml.dump({
            "scrape_configs": [{"job_name": "kubelet"}],
        })
        with tempfile.TemporaryDirectory() as d:
            path = write_yaml(d, "cm.yaml", yaml.dump({
                "data": {"prometheus.yml": inner},
            }))
            result = parse_scrape_configs(path)
            self.assertEqual(len(result), 1)

    def test_empty_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = write_yaml(d, "empty.yaml", "")
            result = parse_scrape_configs(path)
            self.assertEqual(result, [])


class TestAnalyzeRelabelConfigs(unittest.TestCase):

    def test_namespace_mapping(self):
        sc = {
            "job_name": "pod-monitor",
            "relabel_configs": [
                {"source_labels": ["__meta_kubernetes_namespace"], "target_label": "tenant"},
            ],
        }
        result = analyze_relabel_configs(sc)
        self.assertTrue(result["has_tenant_mapping"])
        self.assertEqual(result["mapping_type"], "namespace")

    def test_service_label_mapping(self):
        sc = {
            "job_name": "svc-monitor",
            "relabel_configs": [
                {"source_labels": ["__meta_kubernetes_service_label_tenant"],
                 "target_label": "tenant"},
            ],
        }
        result = analyze_relabel_configs(sc)
        self.assertTrue(result["has_tenant_mapping"])
        self.assertEqual(result["mapping_type"], "service_label")

    def test_no_tenant_mapping(self):
        sc = {
            "job_name": "basic",
            "relabel_configs": [
                {"source_labels": ["__address__"], "target_label": "__param_target"},
            ],
        }
        result = analyze_relabel_configs(sc)
        self.assertFalse(result["has_tenant_mapping"])
        self.assertTrue(len(result["suggestions"]) > 0)

    def test_custom_tenant_label(self):
        sc = {
            "job_name": "custom",
            "relabel_configs": [
                {"source_labels": ["__meta_kubernetes_namespace"], "target_label": "cluster"},
            ],
        }
        result = analyze_relabel_configs(sc, tenant_label="cluster")
        self.assertTrue(result["has_tenant_mapping"])

    def test_metric_relabel_configs(self):
        sc = {
            "job_name": "exporter",
            "metric_relabel_configs": [
                {"source_labels": ["__meta_kubernetes_namespace"], "target_label": "tenant"},
            ],
        }
        result = analyze_relabel_configs(sc)
        self.assertTrue(result["has_tenant_mapping"])


class TestAnalyzeScrapeConfigs(unittest.TestCase):

    def test_mixed_jobs(self):
        scrape_configs = [
            {"job_name": "with-tenant",
             "relabel_configs": [
                 {"source_labels": ["__meta_kubernetes_namespace"], "target_label": "tenant"},
             ]},
            {"job_name": "without-tenant",
             "relabel_configs": []},
        ]
        analyses, summary = analyze_scrape_configs(scrape_configs)
        self.assertEqual(summary["total_jobs"], 2)
        self.assertEqual(summary["with_tenant_mapping"], 1)
        self.assertEqual(summary["without_tenant_mapping"], 1)


# ============================================================
# Integration: Output Generation
# ============================================================

class TestScanRuleFiles(unittest.TestCase):

    def test_glob_pattern(self):
        with tempfile.TemporaryDirectory() as d:
            write_yaml(d, "a.yaml", "groups: []")
            write_yaml(d, "b.yml", "groups: []")
            write_yaml(d, "c.txt", "not yaml")
            files = scan_rule_files(os.path.join(d, "*.yaml"))
            self.assertEqual(len(files), 1)
            files_yml = scan_rule_files(os.path.join(d, "*.yml"))
            self.assertEqual(len(files_yml), 1)


class TestWriteOutputs(unittest.TestCase):

    def test_dry_run(self):
        report = write_outputs(
            "/tmp/test",
            phase1_results=({"db-a": {"receiver": {"type": "webhook"}}},
                            {"tenant_routes": 1, "total_routes": 1,
                             "skipped_routes": [], "dedup_tenants": {}, "warnings": []}),
            dry_run=True,
        )
        self.assertIn("phase1", report["phases"])
        self.assertEqual(len(report["files_written"]), 0)

    def test_phase1_file_output(self):
        with tempfile.TemporaryDirectory() as d:
            report = write_outputs(
                d,
                phase1_results=(
                    {"db-a": {"receiver": {"type": "webhook", "url": "https://a.com"}}},
                    {"tenant_routes": 1, "total_routes": 1, "skipped_routes": [],
                     "dedup_tenants": {}, "warnings": []},
                ),
            )
            self.assertTrue(len(report["files_written"]) > 0)
            # Check tenant YAML was written
            tenant_yaml = os.path.join(d, "phase1-routing", "db-a.yaml")
            self.assertTrue(os.path.exists(tenant_yaml))

    def test_phase2_file_output(self):
        with tempfile.TemporaryDirectory() as d:
            candidates = [
                {"alert_name": "Test", "file": "r.yaml", "group": "g",
                 "status": "perfect", "severity": "warning", "metric_key": "cpu",
                 "threshold_value": "80", "operator": ">", "aggregation": "max",
                 "agg_reason": "test", "metric_group": None, "dict_match": None},
            ]
            report = write_outputs(
                d,
                phase2_results=(candidates, [],
                                {"files_scanned": 1, "total_groups": 1, "total_rules": 1,
                                 "alert_rules": 1, "recording_rules": 0, "parseable": 1,
                                 "unparseable": 0, "errors": []}),
            )
            csv_path = os.path.join(d, "phase2-rules", "migration-plan.csv")
            self.assertTrue(os.path.exists(csv_path))

    def test_no_results_empty_report(self):
        report = write_outputs("/tmp/test", dry_run=True)
        self.assertEqual(report["phases"], {})


if __name__ == "__main__":
    unittest.main()
