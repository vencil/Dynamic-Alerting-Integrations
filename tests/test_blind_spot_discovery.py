#!/usr/bin/env python3
"""test_blind_spot_discovery.py — Blind Spot Discovery 測試套件。"""

import json
import os
import tempfile
import unittest
from unittest import mock

import yaml


import blind_spot_discovery as bsd  # noqa: E402


# ── 1. DB Type Inference ────────────────────────────────────────────

class TestInferDbType(unittest.TestCase):
    """Test _infer_db_type_from_job and _infer_db_type_from_metric."""

    def test_exact_match(self):
        self.assertEqual(bsd._infer_db_type_from_job("mysql"), "mariadb")
        self.assertEqual(bsd._infer_db_type_from_job("postgres"), "postgresql")
        self.assertEqual(bsd._infer_db_type_from_job("redis"), "redis")

    def test_segment_match(self):
        self.assertEqual(bsd._infer_db_type_from_job("mysql-exporter-prod"), "mariadb")
        self.assertEqual(bsd._infer_db_type_from_job("my-kafka-cluster"), "kafka")
        self.assertEqual(bsd._infer_db_type_from_job("kafka-exporter"), "kafka")
        self.assertEqual(bsd._infer_db_type_from_job("pg-exporter"), "postgresql")

    def test_case_insensitive(self):
        self.assertEqual(bsd._infer_db_type_from_job("MySQL"), "mariadb")
        self.assertEqual(bsd._infer_db_type_from_job("PostgreSQL"), "postgresql")

    def test_unknown_job(self):
        self.assertEqual(bsd._infer_db_type_from_job("nginx"), "unknown")
        self.assertEqual(bsd._infer_db_type_from_job("node-exporter"), "unknown")

    def test_no_false_positive_on_substring(self):
        """Ensure short keywords don't false-match inside longer words."""
        self.assertEqual(bsd._infer_db_type_from_job("prometheus"), "unknown")
        self.assertEqual(bsd._infer_db_type_from_job("process-exporter"), "unknown")
        self.assertEqual(bsd._infer_db_type_from_job("thanos-sidecar"), "unknown")

    def test_metric_prefix_match(self):
        self.assertEqual(bsd._infer_db_type_from_metric("mysql_connections"), "mariadb")
        self.assertEqual(bsd._infer_db_type_from_metric("pg_cache_hit"), "postgresql")
        self.assertEqual(bsd._infer_db_type_from_metric("redis_memory_used"), "redis")

    def test_metric_no_match(self):
        self.assertIsNone(bsd._infer_db_type_from_metric("cpu_usage"))
        self.assertIsNone(bsd._infer_db_type_from_metric("_reserved_key"))


# ── 2. Extract DB Instances ─────────────────────────────────────────

class TestExtractDbInstances(unittest.TestCase):
    """Test extract_db_instances()."""

    def test_basic_extraction(self):
        targets = [
            {"job": "mysql", "instance": "10.0.0.1:9104", "namespace": "db-a", "labels": {}},
            {"job": "redis", "instance": "10.0.0.2:9121", "namespace": "db-a", "labels": {}},
            {"job": "mysql", "instance": "10.0.0.3:9104", "namespace": "db-b", "labels": {}},
        ]
        result = bsd.extract_db_instances(targets)
        self.assertEqual(len(result["mariadb"]), 2)
        self.assertEqual(len(result["redis"]), 1)

    def test_exclude_jobs(self):
        targets = [
            {"job": "mysql", "instance": "10.0.0.1:9104", "namespace": "db-a", "labels": {}},
            {"job": "node-exporter", "instance": "10.0.0.2:9100", "namespace": "", "labels": {}},
        ]
        result = bsd.extract_db_instances(targets, exclude_jobs=["node-exporter"])
        self.assertIn("mariadb", result)
        self.assertNotIn("unknown", result)

    def test_empty_targets(self):
        result = bsd.extract_db_instances([])
        self.assertEqual(result, {})

    def test_no_namespace_uses_instance_only(self):
        targets = [
            {"job": "redis", "instance": "redis:6379", "namespace": "", "labels": {}},
        ]
        result = bsd.extract_db_instances(targets)
        self.assertIn("redis:6379", result["redis"])


# ── 3. Load Monitored DB Types ──────────────────────────────────────

class TestLoadMonitoredDbTypes(unittest.TestCase):
    """Test load_monitored_db_types()."""

    def test_basic_loading_flat(self):
        """Flat format (legacy): {metric: value}."""
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "db-a.yaml"), "w", encoding="utf-8") as f:
                yaml.dump({"mysql_connections": 50, "redis_memory": 1024}, f)
            result = bsd.load_monitored_db_types(d)
            self.assertIn("mariadb", result)
            self.assertIn("redis", result)
            self.assertIn("db-a", result["mariadb"])

    def test_basic_loading_wrapped(self):
        """Wrapped format (actual conf.d/): {tenants: {name: {metric: value}}}."""
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "db-a.yaml"), "w", encoding="utf-8") as f:
                yaml.dump({"tenants": {"db-a": {
                    "mysql_connections": "70",
                    "_routing": {"receiver": {"type": "webhook"}},
                }}}, f)
            result = bsd.load_monitored_db_types(d)
            self.assertIn("mariadb", result)
            self.assertIn("db-a", result["mariadb"])

    def test_skips_reserved_keys(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "db-a.yaml"), "w", encoding="utf-8") as f:
                yaml.dump({"_routing": {}, "_severity_dedup": "enable",
                           "mysql_connections": 50}, f)
            result = bsd.load_monitored_db_types(d)
            self.assertIn("mariadb", result)
            self.assertEqual(len(result), 1)  # only mariadb, not _routing

    def test_skips_defaults_file(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "_defaults.yaml"), "w", encoding="utf-8") as f:
                yaml.dump({"mysql_connections": 99}, f)
            result = bsd.load_monitored_db_types(d)
            self.assertEqual(result, {})

    def test_missing_dir(self):
        result = bsd.load_monitored_db_types("/nonexistent")
        self.assertEqual(result, {})


# ── 4. Find Blind Spots ─────────────────────────────────────────────

class TestFindBlindSpots(unittest.TestCase):
    """Test find_blind_spots()."""

    def test_all_covered(self):
        live = {"mariadb": {"db-a/10.0.0.1:9104"}}
        monitored = {"mariadb": {"db-a"}}
        results = bsd.find_blind_spots(live, monitored)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "covered")

    def test_blind_spot_detected(self):
        live = {"mariadb": {"db-a/10.0.0.1:9104"}, "postgresql": {"db-b/10.0.0.2:5432"}}
        monitored = {"mariadb": {"db-a"}}
        results = bsd.find_blind_spots(live, monitored)
        statuses = {r["db_type"]: r["status"] for r in results}
        self.assertEqual(statuses["mariadb"], "covered")
        self.assertEqual(statuses["postgresql"], "blind_spot")

    def test_monitored_but_not_live(self):
        """Tenant has config but no live target — still listed as covered."""
        live = {}
        monitored = {"redis": {"db-a"}}
        results = bsd.find_blind_spots(live, monitored)
        self.assertEqual(results[0]["db_type"], "redis")
        self.assertEqual(results[0]["live_count"], 0)
        self.assertEqual(results[0]["status"], "covered")

    def test_unknown_targets_separate(self):
        live = {"unknown": {"ns/nginx:80"}}
        monitored = {}
        results = bsd.find_blind_spots(live, monitored)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "unrecognized")

    def test_empty_inputs(self):
        results = bsd.find_blind_spots({}, {})
        self.assertEqual(results, [])


# ── 5. Report Rendering ──────────────────────────────────────────────

class TestRenderReport(unittest.TestCase):
    """Test render_report()."""

    def test_report_contains_blind_spot(self):
        results = [{"db_type": "postgresql", "live_count": 3,
                     "live_instances": ["a", "b", "c"],
                     "monitored_tenants": [], "monitored_count": 0,
                     "status": "blind_spot"}]
        report = bsd.render_report(results)
        self.assertIn("BLIND SPOTS", report)
        self.assertIn("postgresql", report)
        self.assertIn("3 instance(s)", report)

    def test_report_contains_covered(self):
        results = [{"db_type": "mariadb", "live_count": 2,
                     "live_instances": ["x", "y"],
                     "monitored_tenants": ["db-a"], "monitored_count": 1,
                     "status": "covered"}]
        report = bsd.render_report(results)
        self.assertIn("COVERED", report)
        self.assertIn("db-a", report)

    def test_report_summary(self):
        results = [
            {"db_type": "mariadb", "live_count": 2, "live_instances": [],
             "monitored_tenants": ["t1"], "monitored_count": 1, "status": "covered"},
            {"db_type": "redis", "live_count": 1, "live_instances": [],
             "monitored_tenants": [], "monitored_count": 0, "status": "blind_spot"},
        ]
        report = bsd.render_report(results)
        self.assertIn("1 DB type(s) covered", report)
        self.assertIn("1 blind spot(s)", report)


# ── 6. CLI ───────────────────────────────────────────────────────────

class TestCLI(unittest.TestCase):
    """Test CLI argument parsing."""

    def test_required_args(self):
        parser = bsd.build_parser()
        args = parser.parse_args(["--config-dir", "/tmp/conf.d"])
        self.assertEqual(args.config_dir, "/tmp/conf.d")

    def test_exclude_jobs(self):
        parser = bsd.build_parser()
        args = parser.parse_args(["--config-dir", "/tmp", "--exclude-jobs", "a,b,c"])
        self.assertEqual(args.exclude_jobs, "a,b,c")

    def test_missing_config_dir(self):
        parser = bsd.build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args([])


if __name__ == "__main__":
    unittest.main()
