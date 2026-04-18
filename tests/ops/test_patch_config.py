#!/usr/bin/env python3
"""test_patch_config.py — patch_config.py pytest 風格測試。

驗證:
  1. get_current_value() — 從 ConfigMap 讀取當前值
  2. diff_preview() — 變更預覽邏輯
  3. find_affected_alerts() — Alert 影響分析
  4. print_diff() — 格式化輸出
  5. patch_legacy() / patch_multifile() — 實際 patch 邏輯
  6. run_cmd() — 指令執行
"""

from unittest import mock

import pytest
import yaml

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


# ---------------------------------------------------------------------------
# run_cmd
# ---------------------------------------------------------------------------

class TestRunCmd:
    """run_cmd() 測試。"""

    def test_success(self):
        result = pc.run_cmd(["echo", "hello"])
        assert result == "hello"

    def test_string_input_converts(self):
        """String input should be split via shlex."""
        result = pc.run_cmd("echo hello")
        assert result == "hello"

    def test_failure_exits(self):
        with pytest.raises(SystemExit):
            pc.run_cmd(["false"])


# ---------------------------------------------------------------------------
# patch_legacy
# ---------------------------------------------------------------------------

class TestPatchLegacy:
    """patch_legacy() 測試。"""

    def test_set_custom_value(self):
        cm_data = {
            "data": {
                "config.yaml": yaml.dump({
                    "tenants": {"db-a": {"mysql_connections": "70"}},
                }),
            }
        }
        result = pc.patch_legacy(cm_data, "db-a", "mysql_connections", "50")
        patched = yaml.safe_load(result["data"]["config.yaml"])
        assert patched["tenants"]["db-a"]["mysql_connections"] == "50"

    def test_set_default_removes_key(self):
        cm_data = {
            "data": {
                "config.yaml": yaml.dump({
                    "tenants": {"db-a": {"mysql_connections": "70", "cpu": "80"}},
                }),
            }
        }
        result = pc.patch_legacy(cm_data, "db-a", "mysql_connections", "default")
        patched = yaml.safe_load(result["data"]["config.yaml"])
        assert "mysql_connections" not in patched["tenants"]["db-a"]
        assert patched["tenants"]["db-a"]["cpu"] == "80"

    def test_set_default_removes_empty_tenant(self):
        cm_data = {
            "data": {
                "config.yaml": yaml.dump({
                    "tenants": {"db-a": {"mysql_connections": "70"}},
                }),
            }
        }
        result = pc.patch_legacy(cm_data, "db-a", "mysql_connections", "default")
        patched = yaml.safe_load(result["data"]["config.yaml"])
        assert "db-a" not in patched["tenants"]

    def test_new_tenant(self):
        cm_data = {
            "data": {
                "config.yaml": yaml.dump({"tenants": {}}),
            }
        }
        result = pc.patch_legacy(cm_data, "db-new", "cpu", "90")
        patched = yaml.safe_load(result["data"]["config.yaml"])
        assert patched["tenants"]["db-new"]["cpu"] == "90"

    def test_missing_config_yaml_exits(self):
        cm_data = {"data": {}}
        with pytest.raises(SystemExit):
            pc.patch_legacy(cm_data, "db-a", "cpu", "90")

    def test_no_tenants_key(self):
        cm_data = {"data": {"config.yaml": yaml.dump({"defaults": {"cpu": 50}})}}
        result = pc.patch_legacy(cm_data, "db-a", "cpu", "90")
        patched = yaml.safe_load(result["data"]["config.yaml"])
        assert patched["tenants"]["db-a"]["cpu"] == "90"


# ---------------------------------------------------------------------------
# patch_multifile
# ---------------------------------------------------------------------------

class TestPatchMultifile:
    """patch_multifile() 測試。"""

    def test_set_custom_value(self):
        cm_data = {
            "data": {
                "_defaults.yaml": "defaults: {}",
                "db-a.yaml": "tenants:\n  db-a:\n    mysql_connections: '70'",
            }
        }
        result = pc.patch_multifile(cm_data, "db-a", "mysql_connections", "50")
        patched = yaml.safe_load(result["data"]["db-a.yaml"])
        assert patched["tenants"]["db-a"]["mysql_connections"] == "50"

    def test_set_default_keeps_empty_tenant(self):
        cm_data = {
            "data": {
                "db-a.yaml": "tenants:\n  db-a:\n    mysql_connections: '70'",
            }
        }
        result = pc.patch_multifile(cm_data, "db-a", "mysql_connections", "default")
        patched = yaml.safe_load(result["data"]["db-a.yaml"])
        assert "mysql_connections" not in patched["tenants"]["db-a"]
        assert "db-a" in patched["tenants"]

    def test_new_tenant_file(self):
        cm_data = {"data": {}}
        result = pc.patch_multifile(cm_data, "db-new", "cpu", "90")
        patched = yaml.safe_load(result["data"]["db-new.yaml"])
        assert patched["tenants"]["db-new"]["cpu"] == "90"

    def test_empty_existing_tenant_yaml(self):
        cm_data = {"data": {"db-a.yaml": ""}}
        result = pc.patch_multifile(cm_data, "db-a", "cpu", "90")
        patched = yaml.safe_load(result["data"]["db-a.yaml"])
        assert patched["tenants"]["db-a"]["cpu"] == "90"


# ---------------------------------------------------------------------------
# get_current_value — additional cases
# ---------------------------------------------------------------------------

class TestGetCurrentValueExtended:
    """get_current_value() 額外測試。"""

    def test_legacy_defaults_fallback(self):
        cm_data = {
            "data": {
                "config.yaml": "tenants:\n  db-a: {}\ndefaults:\n  mysql_connections: 70",
            }
        }
        val, source = pc.get_current_value(cm_data, "legacy", "db-a", "mysql_connections")
        assert val == 70
        assert source == "defaults"

    def test_legacy_not_found(self):
        cm_data = {"data": {"config.yaml": "tenants: {}"}}
        val, source = pc.get_current_value(cm_data, "legacy", "db-a", "unknown")
        assert val is None
        assert source == "none"

    def test_legacy_empty_config(self):
        cm_data = {"data": {}}
        val, source = pc.get_current_value(cm_data, "legacy", "db-a", "cpu")
        assert val is None
        assert source == "none"

    def test_multifile_empty_defaults(self):
        cm_data = {"data": {"_defaults.yaml": ""}}
        val, source = pc.get_current_value(cm_data, "multi-file", "db-a", "cpu")
        assert val is None
        assert source == "none"


# ---------------------------------------------------------------------------
# diff_preview — additional cases
# ---------------------------------------------------------------------------

class TestDiffPreviewExtended:
    """diff_preview() 額外測試。"""

    def test_disabled_old_value(self):
        cm_data = {
            "data": {
                "_defaults.yaml": "defaults: {}",
                "db-a.yaml": "tenants:\n  db-a:\n    mysql_connections: disable",
            }
        }
        diff = pc.diff_preview(cm_data, "multi-file", "db-a", "mysql_connections", "50")
        assert "disabled" in diff["before"]["state"]
        assert diff["changed"]

    def test_not_set_to_default(self):
        """Metric not set -> set to 'default' (still no change since both are default)."""
        cm_data = {"data": {"_defaults.yaml": "defaults: {}"}}
        diff = pc.diff_preview(cm_data, "multi-file", "db-a", "unknown_metric", "default")
        assert "default" in diff["before"]["state"]
        assert "default" in diff["after"]["state"]


# ---------------------------------------------------------------------------
# apply_patch
# ---------------------------------------------------------------------------

class TestApplyPatch:
    """apply_patch() 測試。"""

    @mock.patch("patch_config.run_cmd")
    @mock.patch("patch_config.os.remove")
    def test_legacy_mode(self, mock_rm, mock_run):
        mock_run.return_value = ""
        cm_data = {
            "data": {
                "config.yaml": yaml.dump({"tenants": {"db-a": {"cpu": "80"}}}),
            }
        }
        pc.apply_patch(cm_data, "legacy", "db-a", "cpu", "90")
        mock_run.assert_called_once()
        mock_rm.assert_called_once()

    @mock.patch("patch_config.run_cmd")
    @mock.patch("patch_config.os.remove")
    def test_multifile_mode(self, mock_rm, mock_run):
        mock_run.return_value = ""
        cm_data = {
            "data": {
                "_defaults.yaml": "defaults: {}",
                "db-a.yaml": "tenants:\n  db-a:\n    cpu: '80'",
            }
        }
        pc.apply_patch(cm_data, "multi-file", "db-a", "cpu", "90")
        mock_run.assert_called_once()


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

class TestMainCLI:
    """main() CLI entry point 測試。"""

    @mock.patch("patch_config.run_cmd")
    def test_diff_mode(self, mock_run, capsys):
        cm_json = '{"data":{"_defaults.yaml":"defaults: {}","db-a.yaml":"tenants:\\n  db-a:\\n    cpu: 80"}}'
        mock_run.return_value = cm_json

        with mock.patch("sys.argv", [
            "patch_config.py", "--diff", "db-a", "cpu", "90",
        ]):
            pc.main()
        out = capsys.readouterr().out
        assert "Config Change Preview" in out

    @mock.patch("patch_config.run_cmd")
    def test_diff_json_mode(self, mock_run, capsys):
        cm_json = '{"data":{"_defaults.yaml":"defaults: {}","db-a.yaml":"tenants:\\n  db-a:\\n    cpu: 80"}}'
        mock_run.return_value = cm_json

        with mock.patch("sys.argv", [
            "patch_config.py", "--diff", "--json", "db-a", "cpu", "90",
        ]):
            pc.main()
        import json
        out = json.loads(capsys.readouterr().out)
        assert out["changed"] is True

    @mock.patch("patch_config.os.remove")
    @mock.patch("patch_config.run_cmd")
    def test_apply_mode(self, mock_run, mock_rm, capsys):
        cm_json = '{"data":{"_defaults.yaml":"defaults: {}","db-a.yaml":"tenants:\\n  db-a:\\n    cpu: 80"}}'
        mock_run.side_effect = [cm_json, ""]  # first call: get, second: patch

        with mock.patch("sys.argv", [
            "patch_config.py", "db-a", "cpu", "90",
        ]):
            pc.main()
        out = capsys.readouterr().out
        assert "Success" in out
