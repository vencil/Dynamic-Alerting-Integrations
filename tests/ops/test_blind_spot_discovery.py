#!/usr/bin/env python3
"""pytest style tests for blind_spot_discovery.py — Blind Spot Discovery 測試套件。"""

import json
import os
import sys
import tempfile
from unittest import mock
from unittest.mock import patch

import pytest
import yaml


import blind_spot_discovery as bsd  # noqa: E402


# ── 1. DB Type Inference ────────────────────────────────────────────

class TestInferDbType:
    """Test _infer_db_type_from_job and _infer_db_type_from_metric。"""

    @pytest.mark.parametrize("job_name,expected", [
        ("mysql", "mariadb"),
        ("postgres", "postgresql"),
        ("redis", "redis"),
        ("mysql-exporter-prod", "mariadb"),
        ("my-kafka-cluster", "kafka"),
        ("kafka-exporter", "kafka"),
        ("pg-exporter", "postgresql"),
        ("MySQL", "mariadb"),
        ("PostgreSQL", "postgresql"),
        ("nginx", "unknown"),
        ("node-exporter", "unknown"),
        ("prometheus", "unknown"),
        ("process-exporter", "unknown"),
        ("thanos-sidecar", "unknown"),
    ], ids=["exact-mysql", "exact-postgres", "exact-redis",
            "segment-mysql", "segment-kafka", "segment-kafka2", "segment-pg",
            "case-MySQL", "case-PostgreSQL",
            "unknown-nginx", "unknown-node", "unknown-prometheus",
            "unknown-process", "unknown-thanos"])
    def test_infer_db_from_job(self, job_name, expected):
        """job 名稱正確推斷 DB 類型。"""
        assert bsd._infer_db_type_from_job(job_name) == expected

    @pytest.mark.parametrize("metric,expected", [
        ("mysql_connections", "mariadb"),
        ("pg_cache_hit", "postgresql"),
        ("redis_memory_used", "redis"),
        ("cpu_usage", None),
        ("_reserved_key", None),
    ], ids=["mysql-metric", "pg-metric", "redis-metric", "unrelated", "reserved"])
    def test_infer_db_from_metric(self, metric, expected):
        """metric 名稱正確推斷 DB 類型。"""
        assert bsd._infer_db_type_from_metric(metric) == expected


# ── 2. Extract DB Instances ─────────────────────────────────────────

class TestExtractDbInstances:
    """Test extract_db_instances()。"""

    def test_basic_extraction(self):
        """基本提取 DB 實例。"""
        targets = [
            {"job": "mysql", "instance": "10.0.0.1:9104", "namespace": "db-a", "labels": {}},
            {"job": "redis", "instance": "10.0.0.2:9121", "namespace": "db-a", "labels": {}},
            {"job": "mysql", "instance": "10.0.0.3:9104", "namespace": "db-b", "labels": {}},
        ]
        result = bsd.extract_db_instances(targets)
        assert len(result["mariadb"]) == 2
        assert len(result["redis"]) == 1

    def test_exclude_jobs(self):
        """排除特定 job。"""
        targets = [
            {"job": "mysql", "instance": "10.0.0.1:9104", "namespace": "db-a", "labels": {}},
            {"job": "node-exporter", "instance": "10.0.0.2:9100", "namespace": "", "labels": {}},
        ]
        result = bsd.extract_db_instances(targets, exclude_jobs=["node-exporter"])
        assert "mariadb" in result
        assert "unknown" not in result

    def test_empty_targets(self):
        """空 targets 列表返回空字典。"""
        result = bsd.extract_db_instances([])
        assert result == {}

    def test_no_namespace_uses_instance_only(self):
        """無 namespace 時僅用 instance。"""
        targets = [
            {"job": "redis", "instance": "redis:6379", "namespace": "", "labels": {}},
        ]
        result = bsd.extract_db_instances(targets)
        assert "redis:6379" in result["redis"]


# ── 3. Load Monitored DB Types ──────────────────────────────────────

class TestLoadMonitoredDbTypes:
    """Test load_monitored_db_types()。"""

    def test_basic_loading_flat(self):
        """平坦格式 (legacy): {metric: value}。"""
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "db-a.yaml"), "w", encoding="utf-8") as f:
                yaml.dump({"mysql_connections": 50, "redis_memory": 1024}, f)
            result = bsd.load_monitored_db_types(d)
            assert "mariadb" in result
            assert "redis" in result
            assert "db-a" in result["mariadb"]

    def test_basic_loading_wrapped(self):
        """包裝格式 (actual conf.d/): {tenants: {name: {metric: value}}}。"""
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "db-a.yaml"), "w", encoding="utf-8") as f:
                yaml.dump({"tenants": {"db-a": {
                    "mysql_connections": "70",
                    "_routing": {"receiver": {"type": "webhook"}},
                }}}, f)
            result = bsd.load_monitored_db_types(d)
            assert "mariadb" in result
            assert "db-a" in result["mariadb"]

    def test_skips_reserved_keys(self):
        """跳過保留鍵。"""
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "db-a.yaml"), "w", encoding="utf-8") as f:
                yaml.dump({"_routing": {}, "_severity_dedup": "enable",
                           "mysql_connections": 50}, f)
            result = bsd.load_monitored_db_types(d)
            assert "mariadb" in result
            assert len(result) == 1  # only mariadb, not _routing

    def test_skips_defaults_file(self):
        """跳過 _defaults.yaml。"""
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "_defaults.yaml"), "w", encoding="utf-8") as f:
                yaml.dump({"mysql_connections": 99}, f)
            result = bsd.load_monitored_db_types(d)
            assert result == {}

    def test_missing_dir(self):
        """缺失目錄返回空字典。"""
        result = bsd.load_monitored_db_types("/nonexistent")
        assert result == {}


# ── 4. Find Blind Spots ─────────────────────────────────────────────

class TestFindBlindSpots:
    """Test find_blind_spots()。"""

    def test_all_covered(self):
        """所有 DB 都有監控。"""
        live = {"mariadb": {"db-a/10.0.0.1:9104"}}
        monitored = {"mariadb": {"db-a"}}
        results = bsd.find_blind_spots(live, monitored)
        assert len(results) == 1
        assert results[0]["status"] == "covered"

    def test_blind_spot_detected(self):
        """偵測到盲點。"""
        live = {"mariadb": {"db-a/10.0.0.1:9104"}, "postgresql": {"db-b/10.0.0.2:5432"}}
        monitored = {"mariadb": {"db-a"}}
        results = bsd.find_blind_spots(live, monitored)
        statuses = {r["db_type"]: r["status"] for r in results}
        assert statuses["mariadb"] == "covered"
        assert statuses["postgresql"] == "blind_spot"

    def test_monitored_but_not_live(self):
        """Tenant 有設定但無實況 target — 仍標記為 covered。"""
        live = {}
        monitored = {"redis": {"db-a"}}
        results = bsd.find_blind_spots(live, monitored)
        assert results[0]["db_type"] == "redis"
        assert results[0]["live_count"] == 0
        assert results[0]["status"] == "covered"

    def test_unknown_targets_separate(self):
        """未知 target 分開標記。"""
        live = {"unknown": {"ns/nginx:80"}}
        monitored = {}
        results = bsd.find_blind_spots(live, monitored)
        assert len(results) == 1
        assert results[0]["status"] == "unrecognized"

    def test_empty_inputs(self):
        """空輸入返回空列表。"""
        results = bsd.find_blind_spots({}, {})
        assert results == []


# ── 5. Report Rendering ──────────────────────────────────────────────

class TestRenderReport:
    """Test render_report()。"""

    def test_report_contains_blind_spot(self):
        """報告包含盲點標記。"""
        results = [{"db_type": "postgresql", "live_count": 3,
                     "live_instances": ["a", "b", "c"],
                     "monitored_tenants": [], "monitored_count": 0,
                     "status": "blind_spot"}]
        report = bsd.render_report(results)
        assert "BLIND SPOTS" in report
        assert "postgresql" in report
        assert "3 instance(s)" in report

    def test_report_contains_covered(self):
        """報告包含已涵蓋標記。"""
        results = [{"db_type": "mariadb", "live_count": 2,
                     "live_instances": ["x", "y"],
                     "monitored_tenants": ["db-a"], "monitored_count": 1,
                     "status": "covered"}]
        report = bsd.render_report(results)
        assert "COVERED" in report
        assert "db-a" in report

    def test_report_summary(self):
        """報告摘要統計正確。"""
        results = [
            {"db_type": "mariadb", "live_count": 2, "live_instances": [],
             "monitored_tenants": ["t1"], "monitored_count": 1, "status": "covered"},
            {"db_type": "redis", "live_count": 1, "live_instances": [],
             "monitored_tenants": [], "monitored_count": 0, "status": "blind_spot"},
        ]
        report = bsd.render_report(results)
        assert "1 DB type(s) covered" in report
        assert "1 blind spot(s)" in report


# ── 6. CLI ───────────────────────────────────────────────────────────

class TestCLI:
    """Test CLI argument parsing。"""

    def test_required_args(self):
        """必要引數正確解析。"""
        parser = bsd.build_parser()
        args = parser.parse_args(["--config-dir", "/tmp/conf.d"])
        assert args.config_dir == "/tmp/conf.d"

    def test_exclude_jobs(self):
        """--exclude-jobs 參數正確解析。"""
        parser = bsd.build_parser()
        args = parser.parse_args(["--config-dir", "/tmp", "--exclude-jobs", "a,b,c"])
        assert args.exclude_jobs == "a,b,c"

    def test_missing_config_dir(self):
        """缺失必要引數時退出。"""
        parser = bsd.build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args([])


# ── 7. query_prometheus_targets ───────────────────────────────────

class TestQueryPrometheusTargets:
    """query_prometheus_targets() 測試。"""

    @patch("blind_spot_discovery.http_get_json")
    def test_success(self, mock_http):
        """正常回傳 targets。"""
        mock_http.return_value = (
            {
                "status": "success",
                "data": {
                    "activeTargets": [
                        {"labels": {"job": "mysqld", "instance": "10.0.0.1:9104",
                                    "namespace": "db-a"}},
                        {"labels": {"job": "redis", "instance": "10.0.0.2:9121",
                                    "namespace": "db-b"}},
                    ]
                }
            },
            None,
        )
        result = bsd.query_prometheus_targets("http://prom:9090")
        assert len(result) == 2
        assert result[0]["job"] == "mysqld"
        assert result[1]["namespace"] == "db-b"

    @patch("blind_spot_discovery.http_get_json")
    def test_http_error(self, mock_http):
        """HTTP 錯誤回傳空列表。"""
        mock_http.return_value = (None, "connection refused")
        result = bsd.query_prometheus_targets("http://prom:9090")
        assert result == []

    @patch("blind_spot_discovery.http_get_json")
    def test_non_success_status(self, mock_http):
        """非 success 狀態回傳空列表。"""
        mock_http.return_value = (
            {"status": "error", "error": "bad query"},
            None,
        )
        result = bsd.query_prometheus_targets("http://prom:9090")
        assert result == []

    @patch("blind_spot_discovery.http_get_json")
    def test_empty_targets(self, mock_http):
        """空 targets 回傳空列表。"""
        mock_http.return_value = (
            {"status": "success", "data": {"activeTargets": []}},
            None,
        )
        result = bsd.query_prometheus_targets("http://prom:9090")
        assert result == []


# ── 8. render_report 進階分支 ─────────────────────────────────────

class TestRenderReportAdvanced:
    """render_report() 進階分支覆蓋。"""

    def test_blind_spot_more_than_5(self):
        """Blind spot 超過 5 個 instance 顯示 '... and N more'。"""
        instances = [f"inst-{i}" for i in range(8)]
        results = [{"db_type": "redis", "live_count": 8,
                     "live_instances": instances,
                     "monitored_tenants": [], "monitored_count": 0,
                     "status": "blind_spot"}]
        report = bsd.render_report(results)
        assert "and 3 more" in report

    def test_unknown_status(self):
        """Unrecognized 狀態正確渲染。"""
        results = [{"db_type": "unknown", "live_count": 2,
                     "live_instances": ["u1", "u2"],
                     "monitored_tenants": [], "monitored_count": 0,
                     "status": "unrecognized"}]
        report = bsd.render_report(results)
        assert "UNRECOGNIZED" in report

    def test_unknown_more_than_5(self):
        """Unrecognized 超過 5 個 instance 顯示 '... and N more'。"""
        instances = [f"unknown-{i}" for i in range(7)]
        results = [{"db_type": "unknown", "live_count": 7,
                     "live_instances": instances,
                     "monitored_tenants": [], "monitored_count": 0,
                     "status": "unrecognized"}]
        report = bsd.render_report(results)
        assert "and 2 more" in report


# ── 9. main() 整合測試 ───────────────────────────────────────────

class TestMainIntegration:
    """main() CLI 整合測試。"""

    def test_text_output(self, monkeypatch, capsys, tmp_path):
        """main() 預設文字輸出。"""
        conf_dir = tmp_path / "conf.d"
        conf_dir.mkdir()
        (conf_dir / "db-a.yaml").write_text(
            "mariadb:\n  connection_count:\n    warning: 100\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(sys, "argv", [
            "blind_spot_discovery",
            "--config-dir", str(conf_dir),
            "--prometheus", "http://prom:9090",
        ])
        monkeypatch.setattr(bsd, "query_prometheus_targets", lambda url: [
            {"job": "mysqld_exporter", "instance": "10.0.0.1:9104",
             "namespace": "db-a", "labels": {}},
        ])
        bsd.main()
        out = capsys.readouterr().out
        assert "Summary" in out

    def test_json_output(self, monkeypatch, capsys, tmp_path):
        """--json-output 輸出 JSON。"""
        conf_dir = tmp_path / "conf.d"
        conf_dir.mkdir()
        (conf_dir / "db-a.yaml").write_text(
            "mariadb:\n  connection_count:\n    warning: 100\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(sys, "argv", [
            "blind_spot_discovery",
            "--config-dir", str(conf_dir),
            "--prometheus", "http://prom:9090",
            "--json-output",
        ])
        monkeypatch.setattr(bsd, "query_prometheus_targets", lambda url: [])
        bsd.main()
        out = capsys.readouterr().out
        data = json.loads(out)
        assert isinstance(data, list)

    def test_exclude_jobs(self, monkeypatch, capsys, tmp_path):
        """--exclude-jobs 正確排除 job。"""
        conf_dir = tmp_path / "conf.d"
        conf_dir.mkdir()
        (conf_dir / "db-a.yaml").write_text(
            "mariadb:\n  connection_count:\n    warning: 100\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(sys, "argv", [
            "blind_spot_discovery",
            "--config-dir", str(conf_dir),
            "--prometheus", "http://prom:9090",
            "--exclude-jobs", "prometheus,node-exporter",
        ])
        monkeypatch.setattr(bsd, "query_prometheus_targets", lambda url: [
            {"job": "prometheus", "instance": "localhost:9090",
             "namespace": "monitoring", "labels": {}},
        ])
        bsd.main()
        out = capsys.readouterr().out
        assert "Summary" in out

    def test_prom_env_fallback(self, monkeypatch, capsys, tmp_path):
        """PROMETHEUS_URL env var 作為 fallback。"""
        conf_dir = tmp_path / "conf.d"
        conf_dir.mkdir()
        (conf_dir / "t1.yaml").write_text("redis:\n  x: {}\n",
                                          encoding="utf-8")
        monkeypatch.setenv("PROMETHEUS_URL", "http://env-prom:9090")
        monkeypatch.setattr(sys, "argv", [
            "blind_spot_discovery", "--config-dir", str(conf_dir),
        ])
        captured_url = []
        def mock_targets(url):
            captured_url.append(url)
            return []
        monkeypatch.setattr(bsd, "query_prometheus_targets", mock_targets)
        bsd.main()
        assert captured_url[0] == "http://env-prom:9090"
