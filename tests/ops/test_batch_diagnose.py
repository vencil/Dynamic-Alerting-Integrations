#!/usr/bin/env python3
"""test_batch_diagnose.py — batch_diagnose.py pytest 風格測試。

驗證:
  1. discover_tenants() — ConfigMap key 解析
  2. run_diagnose_for_tenant() — 單租戶診斷執行
  3. generate_report() — 報告產出 + health score
  4. print_text_report() — 文字報告格式
"""

import json
import subprocess
import sys
from unittest.mock import patch, MagicMock

import pytest

import batch_diagnose as bd  # noqa: E402


class TestDiscoverTenants:
    """discover_tenants() 測試。"""

    @patch("batch_diagnose.subprocess.run")
    def test_normal_discovery(self, mock_run):
        """正常 ConfigMap 應返回排序的 tenant 列表。"""
        cm_data = {
            "data": {
                "_defaults.yaml": "defaults: {}",
                "db-b.yaml": "tenants: {}",
                "db-a.yaml": "tenants: {}",
            }
        }
        mock_run.return_value = MagicMock(
            returncode=0, stdout=json.dumps(cm_data),
        )
        result = bd.discover_tenants()
        assert result == ["db-a", "db-b"]

    @patch("batch_diagnose.subprocess.run")
    def test_skip_underscore_keys(self, mock_run):
        """_ 前綴的 key 應被忽略。"""
        cm_data = {
            "data": {
                "_defaults.yaml": "defaults: {}",
                "_internal.yaml": "stuff",
                "db-a.yaml": "tenants: {}",
            }
        }
        mock_run.return_value = MagicMock(
            returncode=0, stdout=json.dumps(cm_data),
        )
        result = bd.discover_tenants()
        assert result == ["db-a"]

    @patch("batch_diagnose.subprocess.run")
    def test_kubectl_failure(self, mock_run):
        """kubectl 失敗應返回空列表。"""
        mock_run.return_value = MagicMock(returncode=1, stderr="error")
        result = bd.discover_tenants()
        assert result == []

    @patch("batch_diagnose.subprocess.run")
    def test_kubectl_timeout(self, mock_run):
        """kubectl timeout 應返回空列表。"""
        import subprocess
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="kubectl", timeout=15)
        result = bd.discover_tenants()
        assert result == []

    @patch("batch_diagnose.subprocess.run")
    def test_empty_configmap(self, mock_run):
        """空 ConfigMap 應返回空列表。"""
        cm_data = {"data": {}}
        mock_run.return_value = MagicMock(
            returncode=0, stdout=json.dumps(cm_data),
        )
        result = bd.discover_tenants()
        assert result == []


class TestGenerateReport:
    """generate_report() 測試。"""

    def test_all_healthy(self):
        """全部健康時 health_score = 1.0。"""
        results = [
            {"tenant": "db-a", "status": "healthy"},
            {"tenant": "db-b", "status": "healthy"},
        ]
        report = bd.generate_report(results, "http://localhost:9090")
        assert report["health_score"] == 1.0
        assert report["healthy_count"] == 2
        assert report["issue_count"] == 0

    def test_partial_health(self):
        """部分健康時 health_score 正確計算。"""
        results = [
            {"tenant": "db-a", "status": "healthy"},
            {"tenant": "db-b", "status": "error", "issues": ["Pod not found"]},
        ]
        report = bd.generate_report(results, "http://localhost:9090")
        assert report["health_score"] == 0.5
        assert report["healthy_count"] == 1
        assert report["issue_count"] == 1

    def test_recommendations_pod_issue(self):
        """Pod 相關 issue 應產生 kubectl get pods 建議。"""
        results = [
            {"tenant": "db-a", "status": "error", "issues": ["Pod not found"]},
        ]
        report = bd.generate_report(results, "http://localhost:9090")
        assert any("kubectl get pods" in r for r in report["recommendations"])

    def test_recommendations_exporter_issue(self):
        """Exporter 相關 issue 應產生 kubectl logs 建議。"""
        results = [
            {"tenant": "db-a", "status": "error", "issues": ["Exporter DOWN"]},
        ]
        report = bd.generate_report(results, "http://localhost:9090")
        assert any("kubectl logs" in r for r in report["recommendations"])

    def test_empty_results(self):
        """空結果應返回 0 health_score。"""
        report = bd.generate_report([], "http://localhost:9090")
        assert report["health_score"] == 0.0
        assert report["total_tenants"] == 0

    def test_report_has_timestamp(self):
        """報告應包含 timestamp。"""
        results = [{"tenant": "db-a", "status": "healthy"}]
        report = bd.generate_report(results, "http://localhost:9090")
        assert "timestamp" in report


class TestPrintTextReport:
    """print_text_report() 格式驗證。"""

    def test_no_crash_on_healthy(self):
        """全部健康的報告不應崩潰。"""
        report = bd.generate_report(
            [{"tenant": "db-a", "status": "healthy", "operational_mode": "normal",
              "elapsed_seconds": 1.2}],
            "http://localhost:9090",
        )
        # Should not raise
        bd.print_text_report(report)

    def test_healthy_section(self, capsys):
        """健康 tenant 區塊正確顯示。"""
        report = bd.generate_report(
            [{"tenant": "db-a", "status": "healthy", "operational_mode": "normal",
              "elapsed_seconds": 1.2}],
            "http://localhost:9090",
        )
        bd.print_text_report(report)
        out = capsys.readouterr().out
        assert "Health Score" in out
        assert "db-a" in out
        assert "100%" in out

    def test_issues_section(self, capsys):
        """問題 tenant 區塊顯示 issue 清單。"""
        report = bd.generate_report(
            [{"tenant": "db-a", "status": "error", "issues": ["Pod missing"]}],
            "http://prom",
        )
        bd.print_text_report(report)
        out = capsys.readouterr().out
        assert "Issues" in out
        assert "Pod missing" in out

    def test_recommendations_section(self, capsys):
        """補救步驟區塊正確顯示。"""
        report = bd.generate_report(
            [{"tenant": "db-a", "status": "error", "issues": ["Prometheus timeout"]}],
            "http://prom",
        )
        bd.print_text_report(report)
        out = capsys.readouterr().out
        assert "Remediation" in out

    def test_silent_mode_suffix(self, capsys):
        """非 normal 模式顯示 [mode] 後綴。"""
        report = bd.generate_report(
            [{"tenant": "db-a", "status": "healthy",
              "operational_mode": "maintenance", "elapsed_seconds": 0.5}],
            "http://prom",
        )
        bd.print_text_report(report)
        out = capsys.readouterr().out
        assert "[maintenance]" in out


# ── generate_report recommendations 分支覆蓋 ─────────────────────


class TestRecommendations:
    """generate_report() recommendations 分支完整覆蓋。"""

    def test_prometheus_issue(self):
        """Prometheus 相關 issue 建議連線檢查。"""
        results = [
            {"tenant": "db-a", "status": "error",
             "issues": ["Prometheus query timeout"]},
        ]
        report = bd.generate_report(results, "http://prom")
        assert any("Prometheus" in r for r in report["recommendations"])

    def test_down_issue(self):
        """DOWN 狀態 issue 建議查看 exporter logs。"""
        results = [
            {"tenant": "db-a", "status": "error",
             "issues": ["Service DOWN"]},
        ]
        report = bd.generate_report(results, "http://prom")
        assert any("kubectl logs" in r for r in report["recommendations"])

    def test_generic_issue(self):
        """未分類 issue 直接顯示原文。"""
        results = [
            {"tenant": "db-a", "status": "error",
             "issues": ["some random error"]},
        ]
        report = bd.generate_report(results, "http://prom")
        assert any("some random error" in r for r in report["recommendations"])

    def test_multiple_issues(self):
        """多個 issue 產生多條建議。"""
        results = [
            {"tenant": "db-a", "status": "error",
             "issues": ["Pod not ready", "Exporter DOWN"]},
        ]
        report = bd.generate_report(results, "http://prom")
        assert len(report["recommendations"]) == 2


# ── run_diagnose_for_tenant（mock diagnose_check）─────────────────


class TestRunDiagnoseForTenant:
    """run_diagnose_for_tenant() 單租戶診斷。"""

    def test_healthy_result(self, monkeypatch):
        """正常診斷回傳 JSON 結果。"""
        def mock_check(tenant, prom_url):
            import sys
            sys.stdout.write(json.dumps({"tenant": tenant, "status": "healthy"}))
        monkeypatch.setattr(bd, "diagnose_check", mock_check)
        result = bd.run_diagnose_for_tenant("db-a", "http://prom")
        assert result["tenant"] == "db-a"
        assert result["status"] == "healthy"
        assert "elapsed_seconds" in result

    def test_empty_output(self, monkeypatch):
        """空輸出回傳 error 狀態。"""
        monkeypatch.setattr(bd, "diagnose_check", lambda t, p: None)
        result = bd.run_diagnose_for_tenant("db-a", "http://prom")
        assert result["status"] == "error"
        assert "empty output" in result["issues"][0]

    def test_exception_caught(self, monkeypatch):
        """例外回傳 error 狀態。"""
        def mock_check(tenant, prom_url):
            raise OSError("connection refused")
        monkeypatch.setattr(bd, "diagnose_check", mock_check)
        result = bd.run_diagnose_for_tenant("db-a", "http://prom")
        assert result["status"] == "error"
        assert "connection refused" in result["issues"][0]


# ── discover_tenants JSON decode ──────────────────────────────────


class TestDiscoverTenantsEdge:
    """discover_tenants() 邊際案例。"""

    @patch("batch_diagnose.subprocess.run")
    def test_invalid_json(self, mock_run):
        """無效 JSON 回傳空清單。"""
        mock_run.return_value = MagicMock(returncode=0, stdout="not json")
        result = bd.discover_tenants()
        assert result == []

    @patch("batch_diagnose.subprocess.run")
    def test_non_yaml_keys_ignored(self, mock_run):
        """非 .yaml 結尾的 key 被忽略。"""
        cm_data = {"data": {"readme.txt": "info", "db-a.yaml": "tenants: {}"}}
        mock_run.return_value = MagicMock(
            returncode=0, stdout=json.dumps(cm_data))
        result = bd.discover_tenants()
        assert result == ["db-a"]


# ── main() CLI 測試 ─────────────────────────────────────────────


class TestMainCLI:
    """main() CLI 整合測試。"""

    def test_tenants_flag(self, monkeypatch, capsys):
        """--tenants 直接指定 tenant 清單。"""
        monkeypatch.setattr(sys, "argv", [
            "batch_diagnose", "--tenants", "db-a,db-b",
            "--prometheus", "http://prom:9090", "--dry-run",
        ])
        bd.main()
        out = capsys.readouterr().out
        assert "db-a" in out
        assert "db-b" in out
        assert "2 tenants" in out

    def test_dry_run_single(self, monkeypatch, capsys):
        """--dry-run 列出 tenant 但不執行檢查。"""
        monkeypatch.setattr(sys, "argv", [
            "batch_diagnose", "--tenants", "db-a", "--dry-run",
        ])
        bd.main()
        out = capsys.readouterr().out
        assert "db-a" in out
        assert "dry-run" in out.lower() or "without --dry-run" in out

    def test_no_tenants_exits(self, monkeypatch):
        """沒有 tenant 時 exit 1。"""
        monkeypatch.setattr(sys, "argv", [
            "batch_diagnose", "--tenants", "",
        ])
        # Empty tenants string → discover from ConfigMap → mock empty
        with patch.object(bd, "discover_tenants", return_value=[]):
            with pytest.raises(SystemExit) as exc_info:
                bd.main()
            assert exc_info.value.code == 1

    def test_json_output(self, monkeypatch, capsys):
        """--json 輸出 JSON 格式。"""
        monkeypatch.setattr(sys, "argv", [
            "batch_diagnose", "--tenants", "db-a",
            "--prometheus", "http://prom:9090", "--json",
        ])

        def mock_run_for_tenant(tenant, prom_url, timeout=30):
            return {"tenant": tenant, "status": "healthy",
                    "elapsed_seconds": 0.1}

        monkeypatch.setattr(bd, "run_diagnose_for_tenant",
                            mock_run_for_tenant)
        bd.main()
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data["health_score"] == 1.0
        assert data["total_tenants"] == 1

    def test_text_output(self, monkeypatch, capsys):
        """預設文字輸出。"""
        monkeypatch.setattr(sys, "argv", [
            "batch_diagnose", "--tenants", "db-a",
            "--prometheus", "http://prom:9090",
        ])

        def mock_run_for_tenant(tenant, prom_url, timeout=30):
            return {"tenant": tenant, "status": "healthy",
                    "operational_mode": "normal", "elapsed_seconds": 0.5}

        monkeypatch.setattr(bd, "run_diagnose_for_tenant",
                            mock_run_for_tenant)
        bd.main()
        out = capsys.readouterr().out
        assert "Health Score" in out
        assert "db-a" in out

    def test_output_file(self, monkeypatch, tmp_path, capsys):
        """--output 寫入 JSON 檔案。"""
        out_file = tmp_path / "report.json"
        monkeypatch.setattr(sys, "argv", [
            "batch_diagnose", "--tenants", "db-a",
            "--prometheus", "http://prom:9090",
            "--output", str(out_file),
        ])

        def mock_run_for_tenant(tenant, prom_url, timeout=30):
            return {"tenant": tenant, "status": "healthy",
                    "operational_mode": "normal", "elapsed_seconds": 0.5}

        monkeypatch.setattr(bd, "run_diagnose_for_tenant",
                            mock_run_for_tenant)
        bd.main()
        assert out_file.exists()
        data = json.loads(out_file.read_text(encoding="utf-8"))
        assert data["total_tenants"] == 1

    def test_executor_exception(self, monkeypatch, capsys):
        """ThreadPoolExecutor 例外被捕獲。"""
        monkeypatch.setattr(sys, "argv", [
            "batch_diagnose", "--tenants", "db-a",
            "--prometheus", "http://prom:9090", "--json",
        ])

        def mock_run_for_tenant(tenant, prom_url, timeout=30):
            raise RuntimeError("boom")

        monkeypatch.setattr(bd, "run_diagnose_for_tenant",
                            mock_run_for_tenant)
        bd.main()
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data["issue_count"] == 1

    def test_auto_discover(self, monkeypatch, capsys):
        """不指定 --tenants 時自動探索。"""
        monkeypatch.setattr(sys, "argv", [
            "batch_diagnose", "--prometheus", "http://prom:9090",
            "--dry-run",
        ])
        monkeypatch.setattr(bd, "discover_tenants",
                            lambda **kw: ["db-a", "db-b"])
        bd.main()
        out = capsys.readouterr().out
        assert "db-a" in out
        assert "db-b" in out
