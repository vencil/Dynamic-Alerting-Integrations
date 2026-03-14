#!/usr/bin/env python3
"""test_analyze_gaps.py — analyze_rule_pack_gaps.py 測試。

驗證:
  1. extract_custom_metrics() — custom_ prefix 抽取
  2. match_by_prefix() — Rule Pack prefix 匹配
  3. token_overlap_score() — Jaccard token 相似度
  4. analyze_gaps() — 完整 gap 分析流程
  5. load_tenant_configs() — 配置載入
"""

import os
import tempfile
import unittest


import analyze_rule_pack_gaps as ag  # noqa: E402


class TestExtractCustomMetrics(unittest.TestCase):
    """extract_custom_metrics() 測試。"""

    def test_basic_extraction(self):
        configs = {"db-a": {"custom_mysql_connections": 50, "mysql_cpu": 80}}
        result = ag.extract_custom_metrics(configs)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["original_metric"], "mysql_connections")

    def test_skip_reserved_keys(self):
        """_ 前綴 key 應被忽略。"""
        configs = {"db-a": {"_silent_mode": "warning", "custom_test": 1}}
        result = ag.extract_custom_metrics(configs)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["metric_key"], "custom_test")

    def test_empty_config(self):
        result = ag.extract_custom_metrics({})
        self.assertEqual(result, [])

    def test_multiple_tenants(self):
        configs = {
            "db-a": {"custom_mysql_conn": 50},
            "db-b": {"custom_pg_conn": 100},
        }
        result = ag.extract_custom_metrics(configs)
        self.assertEqual(len(result), 2)


class TestMatchByPrefix(unittest.TestCase):
    """match_by_prefix() 測試。"""

    def test_mysql_prefix(self):
        pack, conf = ag.match_by_prefix("mysql_connections")
        self.assertEqual(pack, "mariadb")
        self.assertGreater(conf, 0)

    def test_pg_prefix(self):
        pack, conf = ag.match_by_prefix("pg_stat_activity")
        self.assertEqual(pack, "postgresql")

    def test_redis_prefix(self):
        pack, conf = ag.match_by_prefix("redis_memory_used")
        self.assertEqual(pack, "redis")

    def test_no_match(self):
        pack, conf = ag.match_by_prefix("custom_unknown_metric")
        self.assertIsNone(pack)
        self.assertEqual(conf, 0.0)

    def test_case_insensitive(self):
        pack, _ = ag.match_by_prefix("MySQL_Connections")
        self.assertEqual(pack, "mariadb")


class TestTokenOverlapScore(unittest.TestCase):
    """token_overlap_score() 測試。"""

    def test_identical(self):
        score = ag.token_overlap_score("mysql_connections", "mysql_connections")
        self.assertEqual(score, 1.0)

    def test_partial_overlap(self):
        score = ag.token_overlap_score("mysql_connections", "mysql_threads")
        self.assertGreater(score, 0.0)
        self.assertLess(score, 1.0)

    def test_no_overlap(self):
        score = ag.token_overlap_score("redis_memory", "kafka_topic")
        self.assertEqual(score, 0.0)

    def test_empty_name(self):
        score = ag.token_overlap_score("", "mysql")
        self.assertEqual(score, 0.0)


class TestAnalyzeGaps(unittest.TestCase):
    """analyze_gaps() 完整流程測試。"""

    def test_exact_match(self):
        custom_metrics = [{
            "tenant": "db-a",
            "metric_key": "custom_mysql_connections",
            "value": 50,
            "original_metric": "mysql_connections",
        }]
        metric_dict = {
            "mysql_connections": {
                "pack": "mariadb",
                "description": "Active connections",
                "golden_name": "mysql_connections",
            }
        }
        results = ag.analyze_gaps(custom_metrics, metric_dict)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["match_type"], "exact")
        self.assertEqual(results[0]["confidence"], 1.0)
        self.assertEqual(results[0]["best_match_pack"], "mariadb")

    def test_prefix_match(self):
        custom_metrics = [{
            "tenant": "db-a",
            "metric_key": "custom_mysql_slow_queries",
            "value": 100,
            "original_metric": "mysql_slow_queries",
        }]
        results = ag.analyze_gaps(custom_metrics, {})
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["match_type"], "prefix")
        self.assertEqual(results[0]["best_match_pack"], "mariadb")

    def test_no_match(self):
        custom_metrics = [{
            "tenant": "db-a",
            "metric_key": "custom_unknown_thing",
            "value": 1,
            "original_metric": "unknown_thing",
        }]
        results = ag.analyze_gaps(custom_metrics, {})
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["match_type"], "none")
        self.assertIn("No official substitute", results[0]["recommendation"])


class TestLoadTenantConfigs(unittest.TestCase):
    """load_tenant_configs() 測試。"""

    def test_load_single_file(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("mysql_connections: 50\n")
            f.flush()
            try:
                configs = ag.load_tenant_configs(tenant_config=f.name)
                self.assertEqual(len(configs), 1)
            finally:
                os.unlink(f.name)

    def test_load_directory(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "db-a.yaml"), "w") as f:
                f.write("mysql_connections: 50\n")
            with open(os.path.join(d, "_defaults.yaml"), "w") as f:
                f.write("defaults: {}\n")
            configs = ag.load_tenant_configs(config_dir=d)
            # _defaults.yaml should be skipped
            self.assertEqual(len(configs), 1)
            self.assertIn("db-a", configs)


if __name__ == "__main__":
    unittest.main()
