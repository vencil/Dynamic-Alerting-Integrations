#!/usr/bin/env python3
"""test_analyze_gaps.py — analyze_rule_pack_gaps.py 測試。

pytest style：使用 plain assert + conftest fixtures。

驗證:
  1. extract_custom_metrics() — custom_ prefix 抽取
  2. match_by_prefix() — Rule Pack prefix 匹配
  3. token_overlap_score() — Jaccard token 相似度
  4. tokenize() — metric name 分詞
  5. analyze_gaps() — 完整 gap 分析流程（含 fuzzy match）
  6. load_tenant_configs() — 配置載入
  7. load_metric_dictionary() — metric dictionary 載入與反向索引
  8. print_report() — 報表輸出格式
"""

import os

import pytest
import yaml

from factories import write_yaml

import analyze_rule_pack_gaps as ag


# ============================================================
# extract_custom_metrics
# ============================================================

class TestExtractCustomMetrics:
    """extract_custom_metrics() 從租戶配置抽取 custom_ prefix 指標。"""

    def test_basic_extraction(self):
        """基本 custom_ 前綴正確抽取。"""
        configs = {"db-a": {"custom_mysql_connections": 50, "mysql_cpu": 80}}
        result = ag.extract_custom_metrics(configs)
        assert len(result) == 1
        assert result[0]["original_metric"] == "mysql_connections"

    def test_skip_reserved_keys(self):
        """_ 前綴保留鍵被忽略。"""
        configs = {"db-a": {"_silent_mode": "warning", "custom_test": 1}}
        result = ag.extract_custom_metrics(configs)
        assert len(result) == 1
        assert result[0]["metric_key"] == "custom_test"

    def test_empty_config(self):
        """空配置回傳空清單。"""
        assert ag.extract_custom_metrics({}) == []

    def test_multiple_tenants(self):
        """多租戶正確抽取各自的 custom_ 指標。"""
        configs = {
            "db-a": {"custom_mysql_conn": 50},
            "db-b": {"custom_pg_conn": 100},
        }
        result = ag.extract_custom_metrics(configs)
        assert len(result) == 2


# ============================================================
# match_by_prefix
# ============================================================

class TestMatchByPrefix:
    """match_by_prefix() Rule Pack 前綴匹配。"""

    def test_mysql_prefix(self):
        """mysql_ 前綴匹配到 mariadb pack。"""
        pack, conf = ag.match_by_prefix("mysql_connections")
        assert pack == "mariadb"
        assert conf > 0

    def test_pg_prefix(self):
        """pg_ 前綴匹配到 postgresql pack。"""
        pack, conf = ag.match_by_prefix("pg_stat_activity")
        assert pack == "postgresql"

    def test_redis_prefix(self):
        """redis_ 前綴匹配到 redis pack。"""
        pack, conf = ag.match_by_prefix("redis_memory_used")
        assert pack == "redis"

    def test_no_match(self):
        """無匹配前綴回傳 (None, 0.0)。"""
        pack, conf = ag.match_by_prefix("custom_unknown_metric")
        assert pack is None
        assert conf == 0.0

    def test_case_insensitive(self):
        """大小寫不敏感匹配。"""
        pack, _ = ag.match_by_prefix("MySQL_Connections")
        assert pack == "mariadb"


# ============================================================
# tokenize
# ============================================================

class TestTokenize:
    """tokenize() metric name 分詞。"""

    def test_underscore_split(self):
        """底線分隔正確分詞。"""
        tokens = ag.tokenize("mysql_connections_total")
        assert tokens == {"mysql", "connections", "total"}

    def test_hyphen_split(self):
        """連字號分隔正確分詞。"""
        tokens = ag.tokenize("redis-memory-used")
        assert tokens == {"redis", "memory", "used"}

    def test_lowercase(self):
        """分詞結果全部小寫。"""
        tokens = ag.tokenize("MySQL_Connections")
        assert all(t == t.lower() for t in tokens)

    def test_single_word(self):
        """單一詞彙正確分詞。"""
        tokens = ag.tokenize("uptime")
        assert tokens == {"uptime"}


# ============================================================
# token_overlap_score
# ============================================================

class TestTokenOverlapScore:
    """token_overlap_score() Jaccard token 相似度。"""

    def test_identical(self):
        """完全相同 metric name 回傳 1.0。"""
        assert ag.token_overlap_score("mysql_connections", "mysql_connections") == 1.0

    def test_partial_overlap(self):
        """部分重疊回傳 0 < score < 1。"""
        score = ag.token_overlap_score("mysql_connections", "mysql_threads")
        assert 0.0 < score < 1.0

    def test_no_overlap(self):
        """無重疊回傳 0.0。"""
        assert ag.token_overlap_score("redis_memory", "kafka_topic") == 0.0

    def test_empty_name(self):
        """空名稱回傳 0.0。"""
        assert ag.token_overlap_score("", "mysql") == 0.0


# ============================================================
# analyze_gaps
# ============================================================

class TestAnalyzeGaps:
    """analyze_gaps() 完整 gap 分析流程。"""

    def test_exact_match(self):
        """字典精確匹配回傳 confidence=1.0。"""
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
        assert len(results) == 1
        assert results[0]["match_type"] == "exact"
        assert results[0]["confidence"] == 1.0
        assert results[0]["best_match_pack"] == "mariadb"

    def test_prefix_match(self):
        """前綴匹配回傳 confidence=0.7。"""
        custom_metrics = [{
            "tenant": "db-a",
            "metric_key": "custom_mysql_slow_queries",
            "value": 100,
            "original_metric": "mysql_slow_queries",
        }]
        results = ag.analyze_gaps(custom_metrics, {})
        assert len(results) == 1
        assert results[0]["match_type"] == "prefix"
        assert results[0]["best_match_pack"] == "mariadb"

    def test_no_match(self):
        """完全無匹配回傳 match_type=none。"""
        custom_metrics = [{
            "tenant": "db-a",
            "metric_key": "custom_unknown_thing",
            "value": 1,
            "original_metric": "unknown_thing",
        }]
        results = ag.analyze_gaps(custom_metrics, {})
        assert len(results) == 1
        assert results[0]["match_type"] == "none"
        assert "No official substitute" in results[0]["recommendation"]

    def test_fuzzy_match(self):
        """Token 重疊觸發 fuzzy match。"""
        custom_metrics = [{
            "tenant": "db-a",
            "metric_key": "custom_mysql_conn_idle",
            "value": 10,
            "original_metric": "mysql_conn_idle",
        }]
        # 字典含共享 token 的 metric → 觸發 fuzzy 或 prefix
        metric_dict = {
            "mysql_conn_total": {
                "pack": "mariadb",
                "description": "Total connections",
                "golden_name": "mysql_conn_total",
            }
        }
        results = ag.analyze_gaps(custom_metrics, metric_dict)
        assert len(results) == 1
        assert results[0]["confidence"] >= 0.4

    def test_empty_custom_metrics(self):
        """空輸入回傳空結果。"""
        assert ag.analyze_gaps([], {}) == []

    def test_high_confidence_recommendation(self):
        """高信心度產生 'Consider official Rule Pack' 建議。"""
        custom_metrics = [{
            "tenant": "db-a",
            "metric_key": "custom_mysql_uptime",
            "value": 1,
            "original_metric": "mysql_uptime",
        }]
        metric_dict = {
            "mysql_uptime": {
                "pack": "mariadb",
                "description": "Uptime",
                "golden_name": "mysql_uptime",
            }
        }
        results = ag.analyze_gaps(custom_metrics, metric_dict)
        assert "Consider official Rule Pack" in results[0]["recommendation"]


# ============================================================
# load_tenant_configs
# ============================================================

class TestLoadTenantConfigs:
    """load_tenant_configs() 配置載入。"""

    def test_load_single_file(self, config_dir):
        """單檔載入正確解析。"""
        write_yaml(config_dir, "test.yaml", "mysql_connections: 50\n")
        path = os.path.join(config_dir, "test.yaml")
        configs = ag.load_tenant_configs(tenant_config=path)
        assert len(configs) == 1
        assert "test" in configs

    def test_load_directory(self, config_dir):
        """目錄載入跳過 _ 前綴檔案。"""
        write_yaml(config_dir, "db-a.yaml", "mysql_connections: 50\n")
        write_yaml(config_dir, "_defaults.yaml", "defaults: {}\n")
        configs = ag.load_tenant_configs(config_dir=config_dir)
        assert len(configs) == 1
        assert "db-a" in configs

    def test_no_args_returns_empty(self):
        """不帶參數回傳空字典。"""
        assert ag.load_tenant_configs() == {}


# ============================================================
# load_metric_dictionary
# ============================================================

class TestLoadMetricDictionary:
    """load_metric_dictionary() metric dictionary 載入與反向索引。"""

    def test_basic_load(self, config_dir):
        """基本字典載入建立正確索引。"""
        data = {
            "mysql_connections": {
                "rule_pack": "mariadb",
                "description": "Active connections",
                "original_metric": "mysql_global_status_connections",
            }
        }
        path = write_yaml(config_dir, "dict.yaml",
                          yaml.dump(data, default_flow_style=False))
        result = ag.load_metric_dictionary(path)
        assert "mysql_connections" in result
        assert result["mysql_connections"]["pack"] == "mariadb"
        # 反向索引原始 metric
        assert "mysql_global_status_connections" in result
        assert result["mysql_global_status_connections"]["golden_name"] == "mysql_connections"

    def test_non_dict_entries_skipped(self, config_dir):
        """非 dict 類型的條目被忽略。"""
        data = {
            "valid_metric": {"rule_pack": "test", "description": "OK"},
            "invalid_metric": "just a string",
        }
        path = write_yaml(config_dir, "dict.yaml",
                          yaml.dump(data, default_flow_style=False))
        result = ag.load_metric_dictionary(path)
        assert "valid_metric" in result
        assert "invalid_metric" not in result

    def test_missing_file_returns_empty(self):
        """檔案不存在回傳空字典。"""
        assert ag.load_metric_dictionary("/nonexistent/dict.yaml") == {}

    def test_same_golden_and_original_no_duplicate(self, config_dir):
        """golden_name 與 original_metric 相同時不重複索引。"""
        data = {
            "cpu_usage": {
                "rule_pack": "kubernetes",
                "description": "CPU usage",
                "original_metric": "cpu_usage",
            }
        }
        path = write_yaml(config_dir, "dict.yaml",
                          yaml.dump(data, default_flow_style=False))
        result = ag.load_metric_dictionary(path)
        assert "cpu_usage" in result
        assert len(result) == 1  # 不重複


# ============================================================
# print_report
# ============================================================

class TestPrintReport:
    """print_report() 報表輸出格式。"""

    def test_empty_results(self, capsys):
        """空結果輸出提示訊息。"""
        ag.print_report([])
        out = capsys.readouterr().out
        assert "No custom_ metrics found" in out

    def test_report_contains_summary(self, capsys):
        """報表包含統計摘要。"""
        results = [{
            "tenant": "db-a",
            "custom_metric": "custom_cpu",
            "original_metric": "cpu",
            "current_value": 80,
            "best_match_pack": "kubernetes",
            "golden_name": "container_cpu",
            "confidence": 1.0,
            "match_type": "exact",
            "recommendation": "Consider official Rule Pack 'kubernetes'",
        }]
        ag.print_report(results)
        out = capsys.readouterr().out
        assert "Rule Pack Gap Analysis" in out
        assert "Exact match:" in out

    def test_report_grouped_by_pack(self, capsys):
        """報表按 pack 分組顯示。"""
        results = [
            {
                "tenant": "db-a", "custom_metric": "custom_mysql_conn",
                "original_metric": "mysql_conn", "current_value": 50,
                "best_match_pack": "mariadb", "golden_name": "mysql_conn",
                "confidence": 1.0, "match_type": "exact",
                "recommendation": "Consider official Rule Pack 'mariadb'",
            },
            {
                "tenant": "db-b", "custom_metric": "custom_redis_mem",
                "original_metric": "redis_mem", "current_value": 70,
                "best_match_pack": "redis", "golden_name": None,
                "confidence": 0.7, "match_type": "prefix",
                "recommendation": "Consider official Rule Pack 'redis'",
            },
        ]
        ag.print_report(results)
        out = capsys.readouterr().out
        assert "[mariadb]" in out
        assert "[redis]" in out

    def test_report_migratable_count(self, capsys):
        """報表顯示可遷移 metric 數量。"""
        results = [{
            "tenant": "db-a", "custom_metric": "custom_test",
            "original_metric": "test", "current_value": 1,
            "best_match_pack": "mariadb", "golden_name": "test",
            "confidence": 0.8, "match_type": "prefix",
            "recommendation": "Consider official Rule Pack 'mariadb'",
        }]
        ag.print_report(results)
        out = capsys.readouterr().out
        assert "can be replaced" in out
