#!/usr/bin/env python3
"""test_patch_config.py — patch_config.py --diff 模式 pytest 風格測試。

驗證:
  1. get_current_value() — 從 ConfigMap 讀取當前值
  2. diff_preview() — 變更預覽邏輯
  3. find_affected_alerts() — Alert 影響分析
  4. print_diff() — 格式化輸出
"""


import patch_config as pc  # noqa: E402


class TestGetCurrentValue:
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
        assert val == 50
        assert source == "tenant"

    def test_multifile_defaults_fallback(self):
        """Tenant 無值時應 fallback 到 defaults。"""
        cm_data = {
            "data": {
                "_defaults.yaml": "defaults:\n  mysql_connections: 70",
                "db-a.yaml": "tenants:\n  db-a: {}",
            }
        }
        val, source = pc.get_current_value(cm_data, "multi-file", "db-a", "mysql_connections")
        assert val == 70
        assert source == "defaults"

    def test_multifile_not_found(self):
        """完全找不到值時應返回 None。"""
        cm_data = {"data": {"_defaults.yaml": "defaults: {}"}}
        val, source = pc.get_current_value(cm_data, "multi-file", "db-a", "unknown_metric")
        assert val is None
        assert source == "none"

    def test_legacy_tenant_value(self):
        """Legacy 模式應從 config.yaml 讀取。"""
        cm_data = {
            "data": {
                "config.yaml": "tenants:\n  db-a:\n    mysql_connections: 50\ndefaults:\n  mysql_connections: 70",
            }
        }
        val, source = pc.get_current_value(cm_data, "legacy", "db-a", "mysql_connections")
        assert val == 50
        assert source == "tenant"


class TestDiffPreview:
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
        assert diff["changed"]
        assert "custom: 70" in diff["before"]["state"]
        assert "custom: 50" in diff["after"]["state"]

    def test_custom_to_default(self):
        """Custom → Default (刪除) 變更。"""
        cm_data = {
            "data": {
                "_defaults.yaml": "defaults: {}",
                "db-a.yaml": "tenants:\n  db-a:\n    mysql_connections: 70",
            }
        }
        diff = pc.diff_preview(cm_data, "multi-file", "db-a", "mysql_connections", "default")
        assert diff["changed"]
        assert "default" in diff["after"]["state"]

    def test_custom_to_disable(self):
        """Custom → Disable 變更。"""
        cm_data = {
            "data": {
                "_defaults.yaml": "defaults: {}",
                "db-a.yaml": "tenants:\n  db-a:\n    mysql_connections: 70",
            }
        }
        diff = pc.diff_preview(cm_data, "multi-file", "db-a", "mysql_connections", "disable")
        assert diff["changed"]
        assert "disabled" in diff["after"]["state"]

    def test_no_change(self):
        """值未變更時 changed=False。"""
        cm_data = {
            "data": {
                "_defaults.yaml": "defaults: {}",
                "db-a.yaml": "tenants:\n  db-a:\n    mysql_connections: 50",
            }
        }
        diff = pc.diff_preview(cm_data, "multi-file", "db-a", "mysql_connections", "50")
        assert not diff["changed"]


class TestFindAffectedAlerts:
    """find_affected_alerts() 測試。"""

    def test_normal_metric(self):
        """測試常規 metric 查詢。"""
        alerts = pc.find_affected_alerts("mysql_connections")
        assert len(alerts) > 0

    def test_dimensional_metric(self):
        """帶維度的 metric 應 strip {} 後匹配。"""
        alerts = pc.find_affected_alerts('redis_queue_length{queue="tasks"}')
        assert len(alerts) > 0


class TestDetectMode:
    """detect_mode() 測試。"""

    def test_multifile(self):
        """測試多檔案模式偵測。"""
        cm_data = {"data": {"_defaults.yaml": "stuff"}}
        assert pc.detect_mode(cm_data) == "multi-file"

    def test_legacy(self):
        """測試傳統模式偵測。"""
        cm_data = {"data": {"config.yaml": "stuff"}}
        assert pc.detect_mode(cm_data) == "legacy"


class TestPrintDiff:
    """print_diff() 不崩潰測試。"""

    def test_changed_diff(self):
        """測試已變更的 diff 不崩潰。"""
        diff = {
            "tenant": "db-a", "metric_key": "mysql_connections",
            "configmap_mode": "multi-file", "changed": True,
            "before": {"value": 70, "source": "tenant", "state": "custom: 70"},
            "after": {"value": 50, "state": "custom: 50"},
            "affected_alerts": ["*MysqlConnections*"],
        }
        pc.print_diff(diff)  # Should not raise

    def test_unchanged_diff(self):
        """測試未變更的 diff 不崩潰。"""
        diff = {
            "tenant": "db-a", "metric_key": "mysql_connections",
            "configmap_mode": "multi-file", "changed": False,
            "before": {"value": 50, "source": "tenant", "state": "custom: 50"},
            "after": {"value": 50, "state": "custom: 50"},
            "affected_alerts": [],
        }
        pc.print_diff(diff)  # Should not raise
