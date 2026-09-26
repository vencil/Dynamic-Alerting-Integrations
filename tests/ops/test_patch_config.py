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

import json
from unittest import mock

import pytest
import yaml

import patch_config as pc  # noqa: E402
from _patch_config_fake import FakeCluster  # noqa: E402


@pytest.fixture(autouse=True)
def _signals_reset():
    """apply keeps its signal handlers to the end of the process (see
    patch_config._Signals); give later tests the caller's handlers back."""
    yield
    pc.SIGNALS.reset()


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
        assert val == "50"
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
        assert val == "70"
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
        assert val == "50"
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
        cm_data = {
            "data": {
                "_defaults.yaml": "defaults: {}",
                "db-a.yaml": "tenants:\n  db-a:\n    mysql_connections: '50'\n",
            }
        }
        diff = pc.diff_preview(cm_data, "multi-file", "db-a", "mysql_connections", "50")
        assert not diff["changed"]


class TestDiffPreviewDeclaredTier:
    """⛔ 執行期輸出也是一種宣稱（#1321）。

    `--diff` 印出來的那兩行是 operator 套用前唯一會讀的東西，而它們原本無條件說
    「default」——等於承諾每個 key 背後都有平台值。對宣告層（`optional_overrides:`
    只有 key 名、沒有值）沒有：`default` 不是還原成平台值，而是**沒有值**，series
    停掉、告警下線（PREVENT #656 的形狀）。

    `get_current_value` 本來就算得出 `source`（而且就印在同一行），資訊在手上，
    只是字串沒用它。
    """

    DECLARED = "oracle_process_count"

    def _cm(self, tenant_yaml="tenants:\n  db-a: {}"):
        return {
            "data": {
                "_defaults.yaml": (
                    "defaults:\n  mysql_connections: 70\n"
                    f"optional_overrides:\n  - {self.DECLARED}\n"
                ),
                "db-a.yaml": tenant_yaml,
            }
        }

    def test_declared_key_is_its_own_source(self):
        """宣告 ≠ 未知：兩者現在都沒有值，但租戶能對前者做的事不同。"""
        val, source = pc.get_current_value(
            self._cm(), "multi-file", "db-a", self.DECLARED)
        assert val is None
        assert source == "declared"
        val, source = pc.get_current_value(
            self._cm(), "multi-file", "db-a", "totally_unknown_key")
        assert source == "none"

    def test_revert_of_a_declared_key_does_not_claim_a_default(self):
        """租戶設過、現在要 revert：刪掉＝沒有值，不是「回到平台預設」。"""
        diff = pc.diff_preview(
            self._cm(f"tenants:\n  db-a:\n    {self.DECLARED}: '400'"),
            "multi-file", "db-a", self.DECLARED, "default")
        after = diff["after"]["state"]
        assert "default (key removed)" not in after
        assert "no value" in after
        assert "silent" in after

    def test_unset_declared_key_does_not_claim_a_default(self):
        diff = pc.diff_preview(
            self._cm(), "multi-file", "db-a", self.DECLARED, "400")
        before = diff["before"]
        assert before["source"] == "declared"
        assert "default (not set)" not in before["state"]
        assert "no value" in before["state"]

    def test_key_with_a_platform_default_still_says_default(self):
        """⛔ 反向：有平台預設的 key，原本的說法本來就是對的，不能一起改掉。"""
        diff = pc.diff_preview(
            self._cm("tenants:\n  db-a:\n    mysql_connections: '90'"),
            "multi-file", "db-a", "mysql_connections", "default")
        assert diff["after"]["state"] == "default (key removed)"

    def test_platform_default_is_not_called_a_tenant_custom_value(self):
        diff = pc.diff_preview(
            self._cm(), "multi-file", "db-a", "mysql_connections", "90")
        assert diff["before"]["source"] == "defaults"
        assert diff["before"]["state"] == "platform default: 70"

    def test_unknown_key_claims_neither_a_default_nor_a_declaration(self):
        diff = pc.diff_preview(
            self._cm(), "multi-file", "db-a", "totally_unknown_key", "default")
        assert "no platform default" in diff["after"]["state"]
        assert "declare" not in diff["after"]["state"].lower()

    def test_legacy_mode_reads_the_same_two_tiers(self):
        cm_data = {
            "data": {
                "config.yaml": (
                    "tenants:\n  db-a: {}\n"
                    "defaults:\n  mysql_connections: 70\n"
                    f"optional_overrides:\n  - {self.DECLARED}\n"
                )
            }
        }
        assert pc.get_current_value(
            cm_data, "legacy", "db-a", self.DECLARED)[1] == "declared"
        assert pc.get_current_value(
            cm_data, "legacy", "db-a", "mysql_connections") == ("70", "defaults")


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
        cm_data = {"data": {"_defaults.yaml": "defaults: {}"}}
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

    def test_failure_raises(self):
        with pytest.raises(pc.KubectlError):
            pc.run_cmd(["false"])

    def test_missing_binary_raises(self):
        with pytest.raises(pc.KubectlError):
            pc.run_cmd(["/nonexistent/kubectl-for-test"])


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
        # 空區塊保留：刪掉會把租戶除名
        assert patched["tenants"]["db-a"] == {}

    def test_new_tenant(self):
        cm_data = {
            "data": {
                "config.yaml": yaml.dump({"tenants": {}}),
            }
        }
        result = pc.patch_legacy(cm_data, "db-new", "cpu", "90")
        patched = yaml.safe_load(result["data"]["config.yaml"])
        assert patched["tenants"]["db-new"]["cpu"] == "90"

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
        assert val == "70"
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
    """apply_patch() 測試（寫後驗收另見 test_patch_config_verify.py）。"""

    @pytest.mark.parametrize("mode,data", [
        ("legacy", {"config.yaml": yaml.dump({"tenants": {"db-a": {"cpu": "80"}}})}),
        ("multi-file", {"_defaults.yaml": "defaults: {}",
                        "db-a.yaml": "tenants:\n  db-a:\n    cpu: '80'"}),
    ])
    def test_writes_the_tenant_key_once(self, mode, data):
        cluster = FakeCluster(data)
        with mock.patch("patch_config.run_cmd", side_effect=cluster):
            pc.apply_patch({"metadata": {"resourceVersion": "1"}, "data": data},
                           mode, "db-a", "cpu", "90")
        assert len(cluster.patches) == 1
        (key, text), = cluster.patches[0]["data"].items()
        assert yaml.safe_load(text)["tenants"]["db-a"]["cpu"] == "90"


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

    def test_json_without_diff_applies_and_prints_one_envelope(self, capsys):
        """`--json` 無 `--diff` 是 apply：stdout 恰好一份 envelope，其餘走 stderr。"""
        cluster = FakeCluster({"_defaults.yaml": "defaults: {}",
                               "db-a.yaml": "tenants:\n  db-a:\n    cpu: 80"})
        with mock.patch("patch_config.run_cmd", side_effect=cluster), \
                mock.patch("sys.argv", ["patch_config.py", "--json",
                                        "db-a", "cpu", "90"]):
            pc.main()
        captured = capsys.readouterr()
        doc = json.loads(captured.out)
        assert (doc["status"], doc["exit_code"], doc["written"]) == (
            "applied", 0, True)
        assert "Success" in captured.err
        assert len(cluster.patches) == 1

    @pytest.mark.parametrize("flag", ["--js", "--j"])
    @mock.patch("patch_config.run_cmd")
    def test_abbreviated_json_flag_still_gets_an_envelope(self, mock_run, capsys, flag):
        """argparse 接受 `--js`＝`--json`；參數錯誤時也要吐 envelope。"""
        with mock.patch("sys.argv", ["patch_config.py", "tenant-x", "--diff", flag]):
            with pytest.raises(SystemExit) as exc_info:
                pc.main()
        assert exc_info.value.code == 2
        assert json.loads(capsys.readouterr().out)["reason"] == "bad_arguments"
        mock_run.assert_not_called()

    def test_apply_mode(self, capsys):
        cluster = FakeCluster({"_defaults.yaml": "defaults: {}",
                               "db-a.yaml": "tenants:\n  db-a:\n    cpu: 80"})
        with mock.patch("patch_config.run_cmd", side_effect=cluster), \
                mock.patch("sys.argv", ["patch_config.py", "db-a", "cpu", "90"]):
            pc.main()
        out = capsys.readouterr().out
        assert "Success" in out


# ---------------------------------------------------------------------------
# #1927 / #1928 — 租戶的載體 key 看內容、不看檔名
# ---------------------------------------------------------------------------
# 這一組測的是 `locate_tenant_key` 這個唯一述詞，以及讀／寫兩條路徑都經過它。
# ⛔ fixture 刻意用中性名稱（tenant-x / team-x / other-t），不用 repo 的範例
# 租戶 id；其中「檔名 ≠ 租戶」那格正是 stem 比對必然看錯的形狀。

_DEFAULTS = "defaults:\n  cpu: 70\n"
_DEEP = "a: " + "[" * 1000 + "]" * 1000 + "\n"


def _cm(**data):
    return {"data": data}


def _decl(tenant, **metrics):
    return yaml.safe_dump({"tenants": {tenant: metrics}})


class TestLocateTenantKey:
    """`locate_tenant_key`：哪一個 key 的 `tenants:` 宣告了這個租戶。"""

    def test_yml_carrier_is_found(self):
        cm = _cm(**{"_defaults.yaml": _DEFAULTS,
                    "tenant-x.yml": _decl("tenant-x", cpu="50")})
        assert pc.locate_tenant_key(cm, "tenant-x") == "tenant-x.yml"

    def test_uppercase_yaml_carrier_is_found(self):
        cm = _cm(**{"_defaults.yaml": _DEFAULTS,
                    "TENANT-X.YAML": _decl("tenant-x", cpu="50")})
        assert pc.locate_tenant_key(cm, "tenant-x") == "TENANT-X.YAML"

    def test_key_name_differs_from_tenant(self):
        cm = _cm(**{"_defaults.yaml": _DEFAULTS,
                    "team-x.yaml": _decl("tenant-x", cpu="50")})
        assert pc.locate_tenant_key(cm, "tenant-x") == "team-x.yaml"

    def test_same_named_key_that_does_not_declare_the_tenant_is_not_it(self):
        """`tenant-x.yaml` 存在但宣告的是別的租戶 ⇒ 它不是 tenant-x 的載體。"""
        cm = _cm(**{"_defaults.yaml": _DEFAULTS,
                    "tenant-x.yaml": _decl("other-t", cpu="1"),
                    "team-x.yaml": _decl("tenant-x", cpu="50")})
        assert pc.locate_tenant_key(cm, "tenant-x") == "team-x.yaml"

    def test_two_keys_declaring_one_tenant_is_refused_naming_both(self):
        cm = _cm(**{"_defaults.yaml": _DEFAULTS,
                    "tenant-x.yaml": _decl("tenant-x", cpu="50"),
                    "tenant-x.yml": _decl("tenant-x", cpu="60")})
        with pytest.raises(pc.ConfigMapShapeError) as exc:
            pc.locate_tenant_key(cm, "tenant-x")
        assert "tenant-x.yaml" in str(exc.value)
        assert "tenant-x.yml" in str(exc.value)

    @pytest.mark.parametrize("broken", [
        "tenants: {tenant-x: [unclosed\n",
        "tenants:\n  other-t: {}\n  other-t: {}\n",
        _DEEP,
        "tenants:\n  ? [a]\n  : {}\n",
    ], ids=["syntax", "tenants-duplicate", "too-deep", "tenants-non-scalar-key"])
    def test_other_key_read_past_does_not_block_the_target(self, broken):
        cm = _cm(**{"_defaults.yaml": _DEFAULTS,
                    "tenant-x.yaml": _decl("tenant-x", cpu="50"),
                    "broken.yaml": broken})
        assert pc.locate_tenant_key(cm, "tenant-x") == "tenant-x.yaml"
        assert pc.build_patch(cm, "multi-file", "tenant-x", "cpu", "6") is not None

    def test_merge_key_in_other_tenants_is_still_refused(self):
        """`<<` 可能藏宣告。"""
        cm = _cm(**{"_defaults.yaml": _DEFAULTS,
                    "tenant-x.yaml": _decl("tenant-x", cpu="50"),
                    "other.yaml": "b: &b {u: {}}\ntenants:\n  <<: *b\n"})
        with pytest.raises(pc.ConfigMapShapeError, match="merge"):
            pc.locate_tenant_key(cm, "tenant-x")

    def test_defaults_deep_duplicate_does_not_block_writes(self):
        cm = _cm(**{"_defaults.yaml": "defaults:\n  cpu: 1\n  cpu: 2\n",
                    "tenant-x.yaml": _decl("tenant-x", cpu="50")})
        assert pc.build_patch(cm, pc.detect_mode(cm), "tenant-x", "cpu", "6")

    @pytest.mark.parametrize("own", [
        "tenants: {tenant-x: [unclosed\n",
        "tenants:\n  tenant-x: {}\n  tenant-x: {}\n",
        _DEEP,
    ], ids=["syntax", "tenants-duplicate", "too-deep"])
    def test_broken_own_key_fails_read_and_write(self, own):
        cm = _cm(**{"_defaults.yaml": _DEFAULTS, "tenant-x.yaml": own})
        with pytest.raises(pc.ConfigMapShapeError, match="tenant-x.yaml"):
            pc.tenant_block(cm, "tenant-x")
        with pytest.raises(pc.ConfigMapShapeError, match="tenant-x.yaml"):
            pc.patch_multifile(cm, "tenant-x", "cpu", "6")

    def test_tenants_block_in_reserved_key_is_not_a_declaration(self):
        cm = _cm(**{"_defaults.yaml": _DEFAULTS + "tenants:\n  tenant-x: {cpu: 1}\n",
                    "_profiles.yaml": _decl("tenant-x", cpu="2")})
        assert pc.locate_tenant_key(cm, "tenant-x") is None

    def test_hidden_key_is_not_a_declaration(self):
        cm = _cm(**{"_defaults.yaml": _DEFAULTS,
                    ".tenant-x.yaml": _decl("tenant-x", cpu="1"),
                    "tenant-x.yaml": _decl("tenant-x", cpu="50")})
        assert pc.locate_tenant_key(cm, "tenant-x") == "tenant-x.yaml"

    def test_reserved_key_does_not_make_a_real_carrier_ambiguous(self):
        cm = _cm(**{"_defaults.yaml": _DEFAULTS + "tenants:\n  tenant-x: {cpu: 1}\n",
                    "tenant-x.yaml": _decl("tenant-x", cpu="50")})
        assert pc.locate_tenant_key(cm, "tenant-x") == "tenant-x.yaml"


class TestDefaultsKeyDetection:
    """`_defaults` 的偵測：大小寫摺疊、.yaml/.yml 兩種拼法。"""

    def test_defaults_yml_is_multi_file(self):
        cm = _cm(**{"_defaults.yml": _DEFAULTS,
                    "tenant-x.yaml": _decl("tenant-x", cpu="50")})
        assert pc.detect_mode(cm) == "multi-file"

    def test_uppercase_defaults_is_multi_file(self):
        cm = _cm(**{"_DEFAULTS.YAML": _DEFAULTS})
        assert pc.detect_mode(cm) == "multi-file"

    def test_platform_tiers_are_read_from_defaults_yml(self):
        cm = _cm(**{"_defaults.yml": "defaults:\n  cpu: 70\n"
                                     "optional_overrides: [mem]\n"})
        assert pc.read_platform_tiers(cm, "multi-file") == ({"cpu": "70"}, ["mem"])

    def test_two_keys_folding_to_defaults_is_refused(self):
        cm = _cm(**{"_defaults.yaml": _DEFAULTS, "_Defaults.yml": _DEFAULTS})
        with pytest.raises(pc.ConfigMapShapeError) as exc:
            pc.detect_mode(cm)
        assert "_defaults.yaml" in str(exc.value)
        assert "_Defaults.yml" in str(exc.value)

    def test_neither_defaults_nor_config_yaml_is_refused(self):
        with pytest.raises(pc.ConfigMapShapeError):
            pc.detect_mode(_cm(**{"tenant-x.yaml": _decl("tenant-x", cpu="1")}))

    def test_legacy_is_still_recognised(self):
        assert pc.detect_mode(_cm(**{"config.yaml": "tenants: {}\n"})) == "legacy"


class TestWritePathUsesTheLocator:
    """寫入路徑 patch 的是定位到的那個 key，不管它叫什麼。"""

    def test_existing_yml_carrier_is_patched_not_a_new_yaml(self):
        cm = _cm(**{"_defaults.yaml": _DEFAULTS,
                    "tenant-x.yml": _decl("tenant-x", cpu="50")})
        patch = pc.patch_multifile(cm, "tenant-x", "cpu", "90")
        assert list(patch["data"]) == ["tenant-x.yml"]
        assert yaml.safe_load(patch["data"]["tenant-x.yml"]) == {
            "tenants": {"tenant-x": {"cpu": "90"}}}

    def test_carrier_named_differently_is_patched_in_place(self):
        cm = _cm(**{"_defaults.yaml": _DEFAULTS,
                    "team-x.yaml": _decl("tenant-x", cpu="50")
                    + "# kept\n"})
        patch = pc.patch_multifile(cm, "tenant-x", "cpu", "default")
        assert list(patch["data"]) == ["team-x.yaml"]

    def test_default_for_undeclared_tenant_is_a_noop(self):
        cm = _cm(**{"_defaults.yaml": _DEFAULTS,
                    "other-t.yaml": _decl("other-t", cpu="1")})
        assert pc.patch_multifile(cm, "tenant-x", "cpu", "default") is None

    def test_concrete_value_for_undeclared_tenant_creates_its_key(self):
        cm = _cm(**{"_defaults.yaml": _DEFAULTS})
        patch = pc.patch_multifile(cm, "tenant-x", "cpu", "90")
        assert list(patch["data"]) == ["tenant-x.yaml"]

    def test_write_refuses_when_two_keys_declare_the_tenant(self):
        cm = _cm(**{"_defaults.yaml": _DEFAULTS,
                    "a.yaml": _decl("tenant-x", cpu="1"),
                    "b.yml": _decl("tenant-x", cpu="2")})
        with pytest.raises(pc.ConfigMapShapeError):
            pc.patch_multifile(cm, "tenant-x", "cpu", "90")

    @mock.patch("patch_config.run_cmd")
    def test_cli_default_for_undeclared_tenant_writes_nothing(self, mock_run,
                                                              capsys):
        """對一個沒讀到的租戶送 `default` 的呼叫端：不得順手建出新租戶。"""
        cm = _cm(**{"_defaults.yaml": _DEFAULTS})
        mock_run.side_effect = [json.dumps(cm)]
        with mock.patch("sys.argv",
                        ["patch_config.py", "tenant-x", "cpu", "default"]):
            pc.main()  # rc 0：不得 SystemExit
        assert mock_run.call_count == 1  # 只有 get，沒有 patch
        assert "No-op" in capsys.readouterr().err

    @mock.patch("patch_config.run_cmd")
    def test_cli_ambiguous_tenant_exits_2_and_applies_nothing(self, mock_run,
                                                              capsys):
        cm = _cm(**{"_defaults.yaml": _DEFAULTS,
                    "a.yaml": _decl("tenant-x", cpu="1"),
                    "b.yaml": _decl("tenant-x", cpu="2")})
        mock_run.side_effect = [json.dumps(cm)]
        with mock.patch("sys.argv",
                        ["patch_config.py", "tenant-x", "cpu", "90"]):
            with pytest.raises(SystemExit) as exc:
                pc.main()
        assert exc.value.code == 2
        assert mock_run.call_count == 1
        assert "a.yaml" in capsys.readouterr().err



# ---------------------------------------------------------------------------
# 載體與值的形狀
# ---------------------------------------------------------------------------

class TestCarrierAndValueShapes:

    def test_non_yaml_key_is_not_a_carrier(self):
        """副檔名是候選條件之一——`notes.txt` 裡的 `tenants:` 不算宣告。"""
        cm = _cm(**{"_defaults.yaml": _DEFAULTS,
                    "notes.txt": _decl("tenant-x", cpu="1"),
                    "tenant-x.yaml": _decl("tenant-x", cpu="50")})
        assert pc.locate_tenant_key(cm, "tenant-x") == "tenant-x.yaml"

    @pytest.mark.parametrize("tid", ["010", "yes"])
    def test_tenant_id_is_the_key_text(self, tid):
        cm = _cm(**{"_defaults.yaml": _DEFAULTS,
                    "tx.yaml": f"tenants:\n  {tid}:\n    cpu: '5'\n"})
        assert pc.locate_tenant_key(cm, tid) == "tx.yaml"
        assert pc.get_current_value(cm, "multi-file", tid, "cpu") == ("5", "tenant")

    def test_legacy_get_current_value_reads_a_carrier_other_than_config_yaml(self):
        """legacy 版面下租戶在 `extra.yaml`，不是 `config.yaml`。"""
        cm = _cm(**{"config.yaml": "defaults:\n  m: 1\n",
                    "extra.yaml": "tenants:\n  tb: {m: '2'}\n"})
        assert pc.get_current_value(cm, "legacy", "tb", "m") == ("2", "tenant")

    def test_legacy_default_keeps_the_empty_block_outside_config_yaml(self):
        """`config.yaml` 以外的載體裡，刪掉空區塊會把租戶除名。"""
        cm = _cm(**{"config.yaml": "tenants: {}\n",
                    "extra.yaml": "tenants:\n  tb: {m: '2'}\n"})
        patch = pc.patch_legacy(cm, "tb", "m", "default")
        assert yaml.safe_load(patch["data"]["extra.yaml"]) == {"tenants": {"tb": {}}}

    def test_legacy_default_keeps_the_empty_block_in_config_yaml_too(self):
        """還原成 default 後租戶仍被宣告（空區塊）。"""
        cm = _cm(**{"config.yaml": "tenants:\n  tb: {m: '2'}\n"})
        patch = pc.patch_legacy(cm, "tb", "m", "default")
        assert yaml.safe_load(patch["data"]["config.yaml"]) == {"tenants": {"tb": {}}}
        assert pc.tenant_block(_cm(**patch["data"]), "tb") == ("config.yaml", {})

    @pytest.mark.parametrize("raw,exporter_text", [
        ("'70'", "70"), ("70", "70"), ("0.5", "0.5"), ("disable", "disable"),
        ("010", "010"), ("1:30", "1:30"), ("0b11", "0b11"), ("1_000", "1_000"),
        ("true", "true"), ("yes", "yes"), ("'010'", "010"), ("|-\n      y", "y"),
        ("!!int 010", "010"), ("!!float 80", "80"), ("!!bool true", "true"),
    ])
    def test_scalar_is_read_as_exporter_text_and_survives_a_write_back(
            self, raw, exporter_text):
        """讀 → 用 patch-config 寫回 → 再讀：兩次都是 exporter 看到的原文。"""
        cm = _cm(**{"_defaults.yaml": _DEFAULTS,
                    "tenant-x.yaml": f"tenants:\n  tenant-x:\n    cpu: {raw}\n"})
        assert pc.get_current_value(cm, "multi-file", "tenant-x", "cpu") == (
            exporter_text, "tenant")
        written = pc.patch_multifile(cm, "tenant-x", "cpu", exporter_text)["data"]
        assert pc.get_current_value(_cm(**{**cm["data"], **written}), "multi-file",
                                    "tenant-x", "cpu") == (exporter_text, "tenant")

    @pytest.mark.parametrize("block", ["5", "[a]", "'str'"])
    def test_tenant_block_that_is_not_a_mapping_is_refused(self, block):
        """讀取不得當成空區塊、寫入不得靜默蓋成 {}。"""
        cm = _cm(**{"_defaults.yaml": _DEFAULTS,
                    "tenant-x.yaml": f"tenants:\n  tenant-x: {block}\n"})
        with pytest.raises(pc.ConfigMapShapeError, match="not a mapping"):
            pc.tenant_block(cm, "tenant-x")
        with pytest.raises(pc.ConfigMapShapeError, match="not a mapping"):
            pc.patch_multifile(cm, "tenant-x", "cpu", "9")

    def test_null_tenant_block_is_an_empty_registered_tenant(self):
        cm = _cm(**{"_defaults.yaml": _DEFAULTS,
                    "tenant-x.yaml": "tenants:\n  tenant-x:\n"})
        assert pc.tenant_block(cm, "tenant-x") == ("tenant-x.yaml", {})

    @pytest.mark.parametrize("defaults", ["defaults: [unclosed\n", "- a\n"])
    def test_unusable_defaults_is_refused_only_by_diff(self, defaults):
        cm = _cm(**{"_defaults.yaml": defaults,
                    "tenant-x.yaml": _decl("tenant-x", cpu="5")})
        assert pc.get_current_value(cm, "multi-file", "tenant-x", "cpu") == ("5", "tenant")
        assert pc.build_patch(cm, pc.detect_mode(cm), "tenant-x", "cpu", "9")
        with pytest.raises(pc.ConfigMapShapeError, match="_defaults.yaml"):
            pc.diff_preview(cm, "multi-file", "tenant-x", "cpu", "9")

    @pytest.mark.parametrize("tenant", ["_x", ".x", "_defaults"])
    def test_new_key_that_is_not_a_carrier_is_refused(self, tenant):
        """`_x.yaml`／`.x.yaml`／`_defaults.yaml` 不會被讀成租戶載體。"""
        with pytest.raises(pc.ConfigMapShapeError):
            pc.patch_multifile(_cm(**{"_defaults.yaml": _DEFAULTS}),
                               tenant, "cpu", "9")

    def test_legacy_empty_config_yaml_is_not_reported_missing(self):
        """key 在、內容空，不是「not found」。"""
        patch = pc.patch_legacy(_cm(**{"config.yaml": ""}), "tb", "m", "1")
        assert yaml.safe_load(patch["data"]["config.yaml"]) == {
            "tenants": {"tb": {"m": "1"}}}

    @pytest.mark.parametrize("word", ["Default", "DEFAULT"])
    def test_default_is_case_insensitive(self, word):
        cm = _cm(**{"_defaults.yaml": _DEFAULTS,
                    "tenant-x.yaml": _decl("tenant-x", cpu="5")})
        patch = pc.patch_multifile(cm, "tenant-x", "cpu", word)
        assert yaml.safe_load(patch["data"]["tenant-x.yaml"]) == {
            "tenants": {"tenant-x": {}}}
        assert pc.patch_multifile(_cm(**{"_defaults.yaml": _DEFAULTS}),
                                  "tenant-x", "cpu", word) is None


# ---------------------------------------------------------------------------
# 節點讀取：只讀原文、不建構；歧義一律拒絕
# ---------------------------------------------------------------------------

_T = "tenants:\n  tenant-x:\n    cpu: '5'\n"


def _two(text, other=None):
    data = {"_defaults.yaml": _DEFAULTS, "tenant-x.yaml": text}
    if other is not None:
        data["other-t.yaml"] = other
    return _cm(**data)


class TestNodeReader:

    @pytest.mark.parametrize("other", [
        "tenants:\n  other-t:\n    note: 2020-13-45\n",
        "tenants:\n  other-t:\n    cpu: !foo 80\n",
    ])
    def test_value_pyyaml_cannot_construct_does_not_block_other_tenants(
            self, other):
        cm = _two(_T, other)
        assert pc.get_current_value(cm, "multi-file", "tenant-x", "cpu") == (
            "5", "tenant")

    def test_custom_tag_value_is_its_text(self):
        cm = _two("tenants:\n  tenant-x:\n    cpu: !foo 80\n")
        assert pc.get_current_value(cm, "multi-file", "tenant-x", "cpu") == ("80", "tenant")

    @pytest.mark.parametrize("text", [
        "tenants:\n  tenant-x:\n    cpu: '1'\n    cpu: '2'\n",
        "misc:\n  a: 1\n  a: 2\ntenants:\n  tenant-x: {}\n",
    ], ids=["metric", "elsewhere"])
    def test_duplicate_key_is_refused(self, text):
        with pytest.raises(pc.ConfigMapShapeError, match="twice"):
            pc.tenant_block(_two(text), "tenant-x")

    @pytest.mark.parametrize("text", [
        "b: &b {cpu: '5'}\ntenants:\n  tenant-x:\n    <<: *b\n",
        "b: &b {tenant-x: {}}\ntenants:\n  <<: *b\n",
    ], ids=["in-block", "in-tenants"])
    def test_merge_key_on_the_lookup_path_is_refused(self, text):
        with pytest.raises(pc.ConfigMapShapeError, match="merge"):
            pc.tenant_block(_two(text), "tenant-x")

    def test_merge_key_off_the_lookup_path_is_read_past(self):
        """他租戶區塊裡的 `<<` 不擋 tenant-x。"""
        other = "b: &b {cpu: '1'}\ntenants:\n  other-t:\n    <<: *b\n"
        assert pc.get_current_value(_two(_T, other), "multi-file", "tenant-x",
                                    "cpu") == ("5", "tenant")

    def test_null_tenant_key_declares_nothing(self):
        """null key 不宣告任何租戶。"""
        cm = _two("tenants:\n  ~: {cpu: '1'}\n  tenant-x: {cpu: '5'}\n")
        assert pc.locate_tenant_key(cm, "~") is None
        assert pc.get_current_value(cm, "multi-file", "tenant-x", "cpu") == ("5", "tenant")

    def test_non_scalar_key_on_the_lookup_path_is_refused(self):
        text = "tenants:\n  ? [a]\n  : {}\n  tenant-x: {cpu: '5'}\n"
        with pytest.raises(pc.ConfigMapShapeError, match="non-scalar"):
            pc.tenant_block(_two(text), "tenant-x")

    def test_only_the_first_document_is_read(self):
        """第二份文件以後的宣告、重複 key、語法錯都不讀。"""
        cm = _two(_T + "---\ntenants:\n  other-t: {}\n  other-t: {}\n"
                       "---\nx: [unclosed\n")
        assert pc.get_current_value(cm, "multi-file", "tenant-x", "cpu") == ("5", "tenant")
        assert pc.locate_tenant_key(cm, "other-t") is None

    @pytest.mark.parametrize("text", ["\ufeff" + _T, _T.replace("\n", "\r\n")],
                             ids=["bom", "crlf"])
    def test_bom_and_crlf_carriers(self, text):
        cm = _two(text)
        assert pc.get_current_value(cm, "multi-file", "tenant-x", "cpu") == ("5", "tenant")
        written = pc.patch_multifile(cm, "tenant-x", "cpu", "6")["data"]
        assert pc.get_current_value(_cm(**{**cm["data"], **written}), "multi-file",
                                    "tenant-x", "cpu") == ("6", "tenant")


class TestDiffBeforeAndChanged:

    def test_before_value_is_source_text_in_both_tiers(self):
        cm = _cm(**{"_defaults.yaml": "defaults:\n  mem: !!float 80\n",
                    "tenant-x.yaml": "tenants:\n  tenant-x:\n    cpu: !!int 010\n"})
        for metric, text in (("cpu", "010"), ("mem", "80")):
            diff = pc.diff_preview(cm, "multi-file", "tenant-x", metric, "9")
            assert diff["before"]["value"] == text

    def test_null_tenant_value_is_none(self):
        cm = _two("tenants:\n  tenant-x:\n    cpu: ~\n")
        before = pc.diff_preview(cm, "multi-file", "tenant-x", "cpu", "9")["before"]
        assert (before["value"], before["source"]) == (None, "tenant")

    def test_mapping_value_is_its_yaml_text(self):
        cm = _two("tenants:\n  tenant-x:\n    cpu: {default: '5', overrides: []}\n")
        value = pc.diff_preview(cm, "multi-file", "tenant-x", "cpu",
                                "9")["before"]["value"]
        assert isinstance(value, str)
        assert yaml.safe_load(value) == {"default": "5", "overrides": []}

    @pytest.mark.parametrize("data", [
        {"_defaults.yaml": _DEFAULTS},
        {"_defaults.yaml": _DEFAULTS, "tenant-x.yaml": _decl("tenant-x", mem="1")},
    ], ids=["tenant-undeclared", "metric-absent"])
    def test_default_that_apply_skips_is_not_a_change(self, data):
        cm = _cm(**data)
        diff = pc.diff_preview(cm, "multi-file", "tenant-x", "cpu", "default")
        assert diff["changed"] is False
        assert pc.patch_multifile(cm, "tenant-x", "cpu", "default") is None

    def test_default_that_removes_a_value_is_a_change(self):
        cm = _two(_T)
        assert pc.diff_preview(cm, "multi-file", "tenant-x", "cpu",
                               "default")["changed"] is True
        assert pc.patch_multifile(cm, "tenant-x", "cpu", "default") is not None

    def test_apply_writes_despite_an_unparseable_defaults_key(self):
        cluster = FakeCluster({"_defaults.yaml": "defaults: [unclosed\n",
                               "tenant-x.yaml": _T})
        with mock.patch("patch_config.run_cmd", side_effect=cluster), \
                mock.patch("sys.argv", ["patch_config.py", "tenant-x", "cpu", "9"]):
            pc.main()
        assert len(cluster.patches) == 1


    @pytest.mark.parametrize("defaults,tenant,value", [
        ("defaults:\n  cpu: '70'\n", "tenants:\n  tenant-x: {}\n", "70"),
        (_DEFAULTS, "tenants:\n  tenant-x:\n    cpu: ~\n", "None"),
    ], ids=["inherited-same-number", "null-vs-None"])
    def test_changed_is_what_apply_would_write(self, defaults, tenant, value):
        cm = _cm(**{"_defaults.yaml": defaults, "tenant-x.yaml": tenant})
        diff = pc.diff_preview(cm, "multi-file", "tenant-x", "cpu", value)
        assert diff["changed"] is True
        assert pc.patch_multifile(cm, "tenant-x", "cpu", value)["data"] != cm["data"]


class TestWriteScope:
    """寫入只改宣告單一租戶的 key；legacy 的 `config.yaml` 例外。"""

    _SHARED = "tenants:\n  tenant-x: {cpu: '5'}\n  other-t: {cpu: '1'}\n"

    def test_key_declaring_two_tenants_is_refused_naming_it(self):
        cm = _cm(**{"_defaults.yaml": _DEFAULTS, "shared.yaml": self._SHARED})
        with pytest.raises(pc.ConfigMapShapeError, match="shared.yaml"):
            pc.patch_multifile(cm, "tenant-x", "cpu", "6")

    def test_legacy_non_config_yaml_key_declaring_two_tenants_is_refused(self):
        cm = _cm(**{"config.yaml": "defaults: {}\n", "extra.yaml": self._SHARED})
        with pytest.raises(pc.ConfigMapShapeError, match="extra.yaml"):
            pc.patch_legacy(cm, "tenant-x", "cpu", "6")

    def test_legacy_config_yaml_may_declare_several(self):
        """對照組：本組「必須成功」的成員。"""
        cm = _cm(**{"config.yaml": self._SHARED})
        patched = yaml.safe_load(pc.patch_legacy(cm, "tenant-x", "cpu", "6")
                                 ["data"]["config.yaml"])
        assert patched["tenants"] == {"tenant-x": {"cpu": "6"},
                                      "other-t": {"cpu": "1"}}

    def test_self_referential_alias_is_written_without_recursing(self):
        cm = _two("a: &x [*x]\n" + _T)
        patch = pc.patch_multifile(cm, "tenant-x", "cpu", "6")
        assert pc.get_current_value(_cm(**{**cm["data"], **patch["data"]}),
                                    "multi-file", "tenant-x", "cpu") == ("6", "tenant")


_UNREADABLE = {
    "tab": 'tenants:\n  tenant-x:\n    cpu:\t"70"\n',
    "syntax": "tenants: {tenant-x: [unclosed\n",
    "root-duplicate": "a: 1\na: 2\ntenants:\n  tenant-x: {cpu: '70'}\n",
    "tenants-duplicate": "tenants:\n  tenant-x: {}\n  tenant-x: {}\n",
    "too-deep": _DEEP,
}


class TestUnreadableKeyWithUndeclaredTenant:
    """找不到宣告、又有 key 讀不了 ⇒ 拒絕並點名該 key。"""

    @pytest.mark.parametrize("path", ["default", "diff", "concrete"])
    @pytest.mark.parametrize("text", list(_UNREADABLE.values()), ids=list(_UNREADABLE))
    def test_is_refused_naming_the_key(self, text, path):
        cm = _cm(**{"_defaults.yaml": _DEFAULTS, "team-x.yaml": text})
        with pytest.raises(pc.ConfigMapShapeError, match="team-x.yaml"):
            if path in ("default", "concrete"):
                pc.build_patch(cm, "multi-file", "tenant-x", "cpu",
                               "default" if path == "default" else "9")
            else:
                pc.diff_preview(cm, "multi-file", "tenant-x", "cpu", "9")

    @pytest.mark.parametrize("text", list(_UNREADABLE.values()), ids=list(_UNREADABLE))
    def test_declared_tenant_is_unaffected(self, text):
        """對照組：本組「必須成功」的成員。"""
        cm = _two(_T, text)
        assert pc.get_current_value(cm, "multi-file", "tenant-x", "cpu") == ("5", "tenant")
        assert pc.build_patch(cm, "multi-file", "tenant-x", "cpu", "default")
        assert pc.diff_preview(cm, "multi-file", "tenant-x", "cpu", "9")["changed"]


class TestReaderBounds:

    def test_duplicate_scan_visits_each_node_once(self, monkeypatch):
        """alias 扇出（billion laughs）：每條邊只走一次，不隨路徑數爆炸。"""
        levels = ["l0: &l0 [x, x, x, x, x, x, x, x, x, x]"] + [
            f"l{i}: &l{i} [{', '.join([f'*l{i - 1}'] * 10)}]" for i in range(1, 7)]
        text = "\n".join(levels) + "\n" + _T
        nodes, edges, stack = set(), 0, [yaml.compose(text)]
        while stack:
            n = stack.pop()
            if id(n) not in nodes:
                nodes.add(id(n))
                children = ([x for kv in n.value for x in kv]
                            if isinstance(n, yaml.MappingNode)
                            else n.value if isinstance(n, yaml.SequenceNode) else [])
                edges += len(children)
                stack += children
        visits = []
        real = pc._refuse_duplicate_keys
        monkeypatch.setattr(pc, "_refuse_duplicate_keys",
                            lambda node, label, seen: visits.append(id(node))
                            or real(node, label, seen))
        pc.read_node(text, "t.yaml")
        assert len(set(visits)) == len(nodes)
        assert len(visits) == edges + 1  # one call per edge, not per path

    def test_nesting_too_deep_is_a_shape_error(self):
        with pytest.raises(pc.ConfigMapShapeError, match="too deeply"):
            pc.read_node("[" * 5000 + "]" * 5000, "t.yaml")

    @pytest.mark.parametrize("cm", [{"data": ["x"]}, ["x"]])
    def test_data_that_is_not_a_mapping_is_a_shape_error(self, cm):
        with pytest.raises(pc.ConfigMapShapeError, match="not a mapping"):
            pc.detect_mode(cm)


class TestSameValueAndUnrelatedCarriers:

    def test_setting_the_current_value_is_not_a_change(self):
        cm = _two("tenants:\n  tenant-x:\n    cpu: 70  # note\n")
        assert pc.diff_preview(cm, "multi-file", "tenant-x", "cpu", "70")["changed"] is False
        assert pc.build_patch(cm, "multi-file", "tenant-x", "cpu", "70") is None

    @pytest.mark.parametrize("other", [
        "tenants:\n  other-t:\n    cpu: '1'\n    cpu: '2'\n",
        "? [a]\n: 1\ntenants:\n  other-t: {cpu: '1'}\n",
    ], ids=["nested-duplicate", "root-non-scalar-key"])
    def test_unrelated_carrier_shape_does_not_block_the_target(self, other):
        cm = _two(_T, other)
        assert pc.get_current_value(cm, "multi-file", "tenant-x", "cpu") == ("5", "tenant")
        assert pc.diff_preview(cm, "multi-file", "tenant-x", "cpu", "6")["changed"]
        assert pc.patch_multifile(cm, "tenant-x", "cpu", "6") is not None


class TestWriteAndCliEdges:

    @pytest.mark.parametrize("tenants", ["[a, b]", "5"])
    def test_tenants_that_is_not_a_mapping_is_not_overwritten(self, tenants):
        cm = _cm(**{"_defaults.yaml": _DEFAULTS, "ta.yaml": f"tenants: {tenants}\n"})
        with pytest.raises(pc.ConfigMapShapeError, match="not a YAML mapping"):
            pc.patch_multifile(cm, "ta", "k", "3")

    @pytest.mark.parametrize("tid", ["010", "yes"])
    def test_write_that_would_rename_the_tenant_is_refused(self, tid):
        cm = _cm(**{"_defaults.yaml": _DEFAULTS,
                    "t.yaml": f"tenants:\n  {tid}: {{m: '1', n: '2'}}\n"})
        with pytest.raises(pc.ConfigMapShapeError, match="tenants it declares"):
            pc.patch_multifile(cm, tid, "m", "5")

    def test_new_key_already_holding_another_tenant_is_refused(self):
        cm = _cm(**{"_defaults.yaml": _DEFAULTS,
                    "tenant-x.yaml": _decl("other-t", cpu="1")})
        with pytest.raises(pc.ConfigMapShapeError, match="tenant-x.yaml"):
            pc.patch_multifile(cm, "tenant-x", "cpu", "6")

    @pytest.mark.parametrize("argv", [["--diff", "--json"], ["--diff", "--json=x"]],
                             ids=["root-not-mapping", "json-equals"])
    @mock.patch("patch_config.run_cmd")
    def test_cli_caller_errors_keep_rc_and_reason(self, mock_run, capsys, argv):
        """root 不是 mapping ⇒ configmap_shape；
        `--json=x` ⇒ bad_arguments。"""
        mock_run.return_value = json.dumps(
            {"data": {"_defaults.yaml": _DEFAULTS, "tx.yaml": "- a\n"}})
        with mock.patch("sys.argv", ["patch_config.py", "tx", "m", "1", *argv]):
            with pytest.raises(SystemExit) as exc:
                pc.main()
        assert exc.value.code == 2
        reason = json.loads(capsys.readouterr().out)["reason"]
        assert reason == ("configmap_shape" if argv[-1] == "--json"
                          else "bad_arguments")

    def test_json_help_is_rc_0_and_one_document(self, capsys):
        with mock.patch("sys.argv", ["patch_config.py", "--json", "-h"]):
            with pytest.raises(SystemExit) as exc:
                pc.main()
        assert not exc.value.code
        out = capsys.readouterr()
        assert json.loads(out.out)["status"] == "help"
        assert "usage" in out.err and "rejected" not in out.err

    def test_non_scalar_key_in_the_tenant_block_is_refused(self):
        cm = _two("tenants:\n  tenant-x:\n    ? [a]\n    : 1\n    cpu: '5'\n")
        with pytest.raises(pc.ConfigMapShapeError, match="non-scalar"):
            pc.tenant_block(cm, "tenant-x")

    def test_default_removes_a_value_that_reads_default(self):
        cm = _two("tenants:\n  tenant-x:\n    _state_maintenance: default\n")
        patch = pc.build_patch(cm, "multi-file", "tenant-x",
                               "_state_maintenance", "default")
        assert yaml.safe_load(patch["data"]["tenant-x.yaml"]) == {
            "tenants": {"tenant-x": {}}}

    @mock.patch("patch_config.run_cmd", return_value="[]")
    def test_kubectl_json_that_is_not_an_object_is_kubectl_failed(self, _run, capsys):
        with mock.patch("sys.argv", ["patch_config.py", "--diff", "--json",
                                     "tx", "m", "1"]):
            with pytest.raises(SystemExit):
                pc.main()
        assert json.loads(capsys.readouterr().out)["reason"] == "kubectl_failed"
