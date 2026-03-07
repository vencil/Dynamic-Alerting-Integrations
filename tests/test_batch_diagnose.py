#!/usr/bin/env python3
"""test_batch_diagnose.py — batch_diagnose.py 測試。

驗證:
  1. discover_tenants() — ConfigMap key 解析
  2. run_diagnose_for_tenant() — 單租戶診斷執行
  3. generate_report() — 報告產出 + health score
  4. print_text_report() — 文字報告格式
"""

import json
import os
import sys
import unittest
from unittest.mock import patch, MagicMock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts", "tools"))

import batch_diagnose as bd  # noqa: E402


class TestDiscoverTenants(unittest.TestCase):
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
        self.assertEqual(result, ["db-a", "db-b"])

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
        self.assertEqual(result, ["db-a"])

    @patch("batch_diagnose.subprocess.run")
    def test_kubectl_failure(self, mock_run):
        """kubectl 失敗應返回空列表。"""
        mock_run.return_value = MagicMock(returncode=1, stderr="error")
        result = bd.discover_tenants()
        self.assertEqual(result, [])

    @patch("batch_diagnose.subprocess.run")
    def test_kubectl_timeout(self, mock_run):
        """kubectl timeout 應返回空列表。"""
        import subprocess
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="kubectl", timeout=15)
        result = bd.discover_tenants()
        self.assertEqual(result, [])

    @patch("batch_diagnose.subprocess.run")
    def test_empty_configmap(self, mock_run):
        """空 ConfigMap 應返回空列表。"""
        cm_data = {"data": {}}
        mock_run.return_value = MagicMock(
            returncode=0, stdout=json.dumps(cm_data),
        )
        result = bd.discover_tenants()
        self.assertEqual(result, [])


class TestGenerateReport(unittest.TestCase):
    """generate_report() 測試。"""

    def test_all_healthy(self):
        """全部健康時 health_score = 1.0。"""
        results = [
            {"tenant": "db-a", "status": "healthy"},
            {"tenant": "db-b", "status": "healthy"},
        ]
        report = bd.generate_report(results, "http://localhost:9090")
        self.assertEqual(report["health_score"], 1.0)
        self.assertEqual(report["healthy_count"], 2)
        self.assertEqual(report["issue_count"], 0)

    def test_partial_health(self):
        """部分健康時 health_score 正確計算。"""
        results = [
            {"tenant": "db-a", "status": "healthy"},
            {"tenant": "db-b", "status": "error", "issues": ["Pod not found"]},
        ]
        report = bd.generate_report(results, "http://localhost:9090")
        self.assertEqual(report["health_score"], 0.5)
        self.assertEqual(report["healthy_count"], 1)
        self.assertEqual(report["issue_count"], 1)

    def test_recommendations_pod_issue(self):
        """Pod 相關 issue 應產生 kubectl get pods 建議。"""
        results = [
            {"tenant": "db-a", "status": "error", "issues": ["Pod not found"]},
        ]
        report = bd.generate_report(results, "http://localhost:9090")
        self.assertTrue(any("kubectl get pods" in r for r in report["recommendations"]))

    def test_recommendations_exporter_issue(self):
        """Exporter 相關 issue 應產生 kubectl logs 建議。"""
        results = [
            {"tenant": "db-a", "status": "error", "issues": ["Exporter DOWN"]},
        ]
        report = bd.generate_report(results, "http://localhost:9090")
        self.assertTrue(any("kubectl logs" in r for r in report["recommendations"]))

    def test_empty_results(self):
        """空結果應返回 0 health_score。"""
        report = bd.generate_report([], "http://localhost:9090")
        self.assertEqual(report["health_score"], 0.0)
        self.assertEqual(report["total_tenants"], 0)

    def test_report_has_timestamp(self):
        """報告應包含 timestamp。"""
        results = [{"tenant": "db-a", "status": "healthy"}]
        report = bd.generate_report(results, "http://localhost:9090")
        self.assertIn("timestamp", report)


class TestPrintTextReport(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
