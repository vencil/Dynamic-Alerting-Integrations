#!/usr/bin/env python3
"""test_patch_config.py — patch_config.py --diff 模式測試。

驗證:
  1. get_current_value() — 從 ConfigMap 讀取當前值
  2. diff_preview() — 變更預覽邏輯
  3. find_affected_alerts() — Alert 影響分析
  4. print_diff() — 格式化輸出
"""

import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts", "tools"))

import patch_config as pc  # noqa: E402


class TestGetCurrentValue(unittest.TestCase):
    """get_current_value() 測試。"""

    def test_multifile_tenant_value(self):
        """應從 tenant YAML 讀取值。"""
        cm_data = {
            "data": {
                "_defaults.yaml": "defaults:\n  mysql_connections: 70",
                "db-a.yaml": "tenants:\n  db-a:\n    mysql_connections: 50",
            }
        }
        val, source = pc.get_current_value(cm_data, "multi-file", "db-a", "mysql_connections")
        self.assertEqual(val, 50)
        self.assertEqual(source, "tenant")

    def test_multifile_defaults_fallback(self):
        """Tenant 無值時應 fallback 到 defaults。"""
        cm_data = {
            "data": {
                "_defaults.yaml": "defaults:\n  mysql_connections: 70",
                "db-a.yaml": "tenants:\n  db-a: {}",
            }
        }
        val, source = pc.get_current_value(cm_data, "multi-file", "db-a", "mysql_connections")
        self.assertEqual(val, 70)
        self.assertEqual(source, "defaults")

    def test_multifile_not_found(self):
        """完全找不到值時應返回 None。"""
        cm_data = {"data": {"_defaults.yaml": "defaults: {}"}}
        val, source = pc.get_current_value(cm_data, "multi-file", "db-a", "unknown_metric")
        self.assertIsNone(val)
        self.assertEqual(source, "none")

    def test_legacy_tenant_value(self):
        """Legacy 模式應從 config.yaml 讀取。"""
        cm_data = {
            "data": {
                "config.yaml": "tenants:\n  db-a:\n    mysql_connections: 50\ndefaults:\n  mysql_connections: 70",
            }
        }
        val, source = pc.get_current_value(cm_data, "legacy", "db-a", "mysql_connections")
        self.assertEqual(val, 50)
        self.assertEqual(source, "tenant")


class TestDiffPreview(unittest.TestCase):
    """diff_preview() 測試。"""

    def test_custom_to_custom(self):
        """Custom → Custom 變更。"""
        cm_data = {
            "data": {
                "_defaults.yaml": "defaults: {}",
                "db-a.yaml": "tenants:\n  db-a:\n    mysql_connections: 70",
            }
        }
        diff = pc.diff_preview(cm_data, "multi-file", "db-a", "mysql_connections", "50")
        self.assertTrue(diff["changed"])
        self.assertIn("custom: 70", diff["before"]["state"])
        self.assertIn("custom: 50", diff["after"]["state"])

    def test_custom_to_default(self):
        """Custom → Default (刪除) 變更。"""
        cm_data = {
            "data": {
                "_defaults.yaml": "defaults: {}",
                "db-a.yaml": "tenants:\n  db-a:\n    mysql_connections: 70",
            }
        }
        diff = pc.diff_preview(cm_data, "multi-file", "db-a", "mysql_connections", "default")
        self.assertTrue(diff["changed"])
        self.assertIn("default", diff["after"]["state"])

    def test_custom_to_disable(self):
        """Custom → Disable 變更。"""
        cm_data = {
            "data": {
                "_defaults.yaml": "defaults: {}",
                "db-a.yaml": "tenants:\n  db-a:\n    mysql_connections: 70",
            }
        }
        diff = pc.diff_preview(cm_data, "multi-file", "db-a", "mysql_connections", "disable")
        self.assertTrue(diff["changed"])
        self.assertIn("disabled", diff["after"]["state"])

    def test_no_change(self):
        """值未變更時 changed=False。"""
        cm_data = {
            "data": {
                "_defaults.yaml": "defaults: {}",
                "db-a.yaml": "tenants:\n  db-a:\n    mysql_connections: 50",
            }
        }
        diff = pc.diff_preview(cm_data, "multi-file", "db-a", "mysql_connections", "50")
        self.assertFalse(diff["changed"])


class TestFindAffectedAlerts(unittest.TestCase):
    """find_affected_alerts() 測試。"""

    def test_normal_metric(self):
        alerts = pc.find_affected_alerts("mysql_connections")
        self.assertTrue(len(alerts) > 0)

    def test_dimensional_metric(self):
        """帶維度的 metric 應 strip {} 後匹配。"""
        alerts = pc.find_affected_alerts('redis_queue_length{queue="tasks"}')
        self.assertTrue(len(alerts) > 0)


class TestDetectMode(unittest.TestCase):
    """detect_mode() 測試。"""

    def test_multifile(self):
        cm_data = {"data": {"_defaults.yaml": "stuff"}}
        self.assertEqual(pc.detect_mode(cm_data), "multi-file")

    def test_legacy(self):
        cm_data = {"data": {"config.yaml": "stuff"}}
        self.assertEqual(pc.detect_mode(cm_data), "legacy")


class TestPrintDiff(unittest.TestCase):
    """print_diff() 不崩潰測試。"""

    def test_changed_diff(self):
        diff = {
            "tenant": "db-a", "metric_key": "mysql_connections",
            "configmap_mode": "multi-file", "changed": True,
            "before": {"value": 70, "source": "tenant", "state": "custom: 70"},
            "after": {"value": 50, "state": "custom: 50"},
            "affected_alerts": ["*MysqlConnections*"],
        }
        pc.print_diff(diff)  # Should not raise

    def test_unchanged_diff(self):
        diff = {
            "tenant": "db-a", "metric_key": "mysql_connections",
            "configmap_mode": "multi-file", "changed": False,
            "before": {"value": 50, "source": "tenant", "state": "custom: 50"},
            "after": {"value": 50, "state": "custom: 50"},
            "affected_alerts": [],
        }
        pc.print_diff(diff)  # Should not raise


if __name__ == "__main__":
    unittest.main()
