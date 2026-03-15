#!/usr/bin/env python3
"""test_lib_python.py — _lib_python.py 共用函式庫測試。

pytest style：使用 plain assert + conftest fixtures。

驗證:
  1. parse_duration_seconds() — Prometheus duration 解析
  2. format_duration() — seconds → duration 格式化
  3. is_disabled() — 三態 disable 判定
  4. load_yaml_file() — YAML 載入 + 錯誤處理
  5. validate_and_clamp() — timing guardrails
  6. http_get_json / http_post_json / http_request_with_retry — HTTP helpers
"""

import json
import os
import pathlib
import stat
import tempfile
from unittest.mock import patch, MagicMock

import pytest
import urllib.error

import _lib_python as lib
from factories import mock_http_response


# ============================================================
# parse_duration_seconds
# ============================================================

class TestParseDurationSeconds:
    """parse_duration_seconds() 測試。"""

    @pytest.mark.parametrize("duration,expected", [
        ("30s", 30), ("5m", 300), ("4h", 14400), ("1d", 86400),
        ("1.5h", 5400), (60, 60), (3.14, 3),
    ], ids=["30s", "5m", "4h", "1d", "1.5h", "int-60", "float-3.14"])
    def test_valid_durations(self, duration, expected):
        """有效 duration 字串正確解析。"""
        assert lib.parse_duration_seconds(duration) == expected

    @pytest.mark.parametrize("bad", ["abc", "", None, "5x", "s", [], {}])
    def test_invalid_returns_none(self, bad):
        """無效輸入回傳 None。"""
        assert lib.parse_duration_seconds(bad) is None


# ============================================================
# format_duration
# ============================================================

class TestFormatDuration:
    """format_duration() 測試。"""

    @pytest.mark.parametrize("seconds,expected", [
        (30, "30s"), (300, "5m"), (3600, "1h"),
        (86400, "24h"), (259200, "72h"),
    ], ids=["30s", "5m", "1h", "24h-no-day", "72h-no-day"])
    def test_format_values(self, seconds, expected):
        """秒數正確格式化為 duration 字串。"""
        assert lib.format_duration(seconds) == expected


# ============================================================
# is_disabled
# ============================================================

class TestIsDisabled:
    """is_disabled() 三態判定測試。"""

    @pytest.mark.parametrize("val", [
        "disable", "disabled", "off", "false",
        "Disable", "DISABLED", " OFF ", "False",
    ])
    def test_disable_variants(self, val):
        """各種 disable 變體正確判定為停用。"""
        assert lib.is_disabled(val) is True

    @pytest.mark.parametrize("val", ["enable", "warning", "critical", "all", "true", "on"])
    def test_non_disabled(self, val):
        """非 disable 值正確判定為未停用。"""
        assert lib.is_disabled(val) is False

    def test_empty_and_none(self):
        """空字串、None 及非字串型別皆判定為未停用。"""
        assert lib.is_disabled("") is False
        assert lib.is_disabled(None) is False
        assert lib.is_disabled(42) is False


# ============================================================
# load_yaml_file
# ============================================================

class TestLoadYamlFile:
    """load_yaml_file() 測試。"""

    def test_load_valid_yaml(self, config_dir):
        """有效 YAML 檔案正確載入並解析。"""
        path = os.path.join(config_dir, "test.yaml")
        with open(path, "w", encoding="utf-8") as f:
            f.write("key: value\nlist:\n  - a\n  - b\n")
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        assert lib.load_yaml_file(path) == {"key": "value", "list": ["a", "b"]}

    def test_missing_file_returns_default(self):
        """檔案不存在時回傳預設值。"""
        assert lib.load_yaml_file("/nonexistent/file.yaml") is None
        assert lib.load_yaml_file("/nonexistent/file.yaml", default={}) == {}

    def test_empty_file_returns_default(self, config_dir):
        """空檔案時回傳預設值。"""
        path = os.path.join(config_dir, "empty.yaml")
        with open(path, "w", encoding="utf-8") as f:
            f.write("")
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        assert lib.load_yaml_file(path) is None
        assert lib.load_yaml_file(path, default=[]) == []

    def test_none_path(self):
        """路徑為 None 時回傳 None。"""
        assert lib.load_yaml_file(None) is None


# ============================================================
# iter_yaml_files
# ============================================================

class TestIterYamlFiles:
    """iter_yaml_files() config directory walking."""

    def test_lists_yaml_files(self, config_dir):
        """列舉 YAML 檔案，跳過保留檔和隱藏檔。"""
        for name in ["a.yaml", "b.yml", "c.txt", "_defaults.yaml", ".hidden.yaml"]:
            path = os.path.join(config_dir, name)
            with open(path, "w", encoding="utf-8") as f:
                f.write("x: 1\n")
            os.chmod(path, 0o600)
        result = lib.iter_yaml_files(config_dir)
        names = [fname for fname, _ in result]
        assert names == ["a.yaml", "b.yml"]

    def test_skip_reserved_false_includes_underscored(self, config_dir):
        """skip_reserved=False 時包含底線開頭檔案。"""
        for name in ["a.yaml", "_defaults.yaml"]:
            path = os.path.join(config_dir, name)
            with open(path, "w", encoding="utf-8") as f:
                f.write("x: 1\n")
            os.chmod(path, 0o600)
        result = lib.iter_yaml_files(config_dir, skip_reserved=False)
        names = [fname for fname, _ in result]
        assert names == ["_defaults.yaml", "a.yaml"]

    def test_missing_dir_returns_empty(self):
        """目錄不存在時回傳空清單。"""
        assert lib.iter_yaml_files("/nonexistent") == []


# ============================================================
# load_tenant_configs
# ============================================================

class TestLoadTenantConfigs:
    """load_tenant_configs() tenant config loading."""

    def test_wrapped_format(self, config_dir):
        """tenants 包裝格式正確解析。"""
        path = os.path.join(config_dir, "db-a.yaml")
        with open(path, "w", encoding="utf-8") as f:
            f.write("tenants:\n  db-a:\n    metric_a: '100'\n  db-b:\n    metric_b: '200'\n")
        os.chmod(path, 0o600)
        result = lib.load_tenant_configs(config_dir)
        assert "db-a" in result
        assert "db-b" in result
        assert result["db-a"]["metric_a"] == "100"

    def test_flat_format(self, config_dir):
        """平面格式檔名推導租戶名稱。"""
        path = os.path.join(config_dir, "db-a.yaml")
        with open(path, "w", encoding="utf-8") as f:
            f.write("metric_a: '100'\n")
        os.chmod(path, 0o600)
        result = lib.load_tenant_configs(config_dir)
        assert "db-a" in result
        assert result["db-a"]["metric_a"] == "100"

    def test_skips_reserved_files(self, config_dir):
        """跳過保留檔案不納入租戶配置。"""
        for name, content in [("db-a.yaml", "metric: '1'\n"),
                              ("_defaults.yaml", "x: 1\n")]:
            path = os.path.join(config_dir, name)
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            os.chmod(path, 0o600)
        result = lib.load_tenant_configs(config_dir)
        assert "db-a" in result
        assert "_defaults" not in result

    def test_empty_dir_returns_empty(self, config_dir):
        """空目錄回傳空字典。"""
        assert lib.load_tenant_configs(config_dir) == {}


# ============================================================
# validate_and_clamp — Guardrails
# ============================================================

class TestValidateAndClamp:
    """validate_and_clamp() timing guardrails 直接測試。"""

    def test_within_bounds_no_warning(self):
        """範圍內值：原值回傳，無警告。"""
        val, warnings = lib.validate_and_clamp("group_wait", "30s", "db-a")
        assert val == "30s"
        assert warnings == []

    def test_below_minimum_clamped(self):
        """低於下限：clamp 到最小值 + 警告。"""
        val, warnings = lib.validate_and_clamp("group_wait", "1s", "db-a")
        assert val == "5s"
        assert len(warnings) == 1
        assert "below" in warnings[0]
        assert "db-a" in warnings[0]

    def test_above_maximum_clamped(self):
        """超過上限：clamp 到最大值 + 警告。"""
        val, warnings = lib.validate_and_clamp("repeat_interval", "100h", "db-b")
        assert val == "72h"
        assert len(warnings) == 1
        assert "above" in warnings[0]

    def test_boundary_minimum_exact(self):
        """恰好等於下限：通過。"""
        val, warnings = lib.validate_and_clamp("group_wait", "5s", "db-a")
        assert val == "5s"
        assert warnings == []

    def test_boundary_maximum_exact(self):
        """恰好等於上限：通過。"""
        val, warnings = lib.validate_and_clamp("group_interval", "5m", "db-a")
        assert val == "5m"
        assert warnings == []

    def test_repeat_interval_boundary_min(self):
        """repeat_interval 下限 1m (60s)。"""
        val, warnings = lib.validate_and_clamp("repeat_interval", "30s", "db-a")
        assert val == "1m"
        assert "below" in warnings[0]

    def test_repeat_interval_boundary_max(self):
        """repeat_interval 上限 72h (259200s)。"""
        val, warnings = lib.validate_and_clamp("repeat_interval", "72h", "db-a")
        assert val == "72h"
        assert warnings == []

    def test_invalid_duration_uses_default(self):
        """無法解析的 duration：使用 platform default + 警告。"""
        val, warnings = lib.validate_and_clamp("group_wait", "invalid", "db-c")
        assert val == "30s"
        assert len(warnings) == 1
        assert "invalid" in warnings[0]

    def test_unknown_param_passthrough(self):
        """未知參數名：原值回傳，無警告。"""
        val, warnings = lib.validate_and_clamp("unknown_param", "10s", "db-a")
        assert val == "10s"
        assert warnings == []

    def test_numeric_seconds_input(self):
        """接受 int 型別的秒數。"""
        val, warnings = lib.validate_and_clamp("group_wait", 30, "db-a")
        assert val == 30
        assert warnings == []

    def test_numeric_below_minimum(self):
        """int 型別低於下限：clamp。"""
        val, warnings = lib.validate_and_clamp("group_wait", 2, "db-a")
        assert val == "5s"
        assert "below" in warnings[0]


# ============================================================
# HTTP helpers
# ============================================================

# _mock_response 已移至 factories.py（mock_http_response）


class TestHttpGetJson:
    """http_get_json() 測試。"""

    @patch("_lib_python.urllib.request.urlopen")
    def test_success(self, mock_urlopen):
        """成功回傳 JSON dict。"""
        mock_urlopen.return_value = mock_http_response({"status": "ok"})
        data, err = lib.http_get_json("http://localhost:9090/api/v1/query")
        assert data == {"status": "ok"}
        assert err is None

    @patch("_lib_python.urllib.request.urlopen")
    def test_empty_body(self, mock_urlopen):
        """空 body 回傳空 dict。"""
        mock_urlopen.return_value = mock_http_response(body=b"")
        data, err = lib.http_get_json("http://localhost/api")
        assert data == {}
        assert err is None

    @patch("_lib_python.urllib.request.urlopen")
    def test_network_error(self, mock_urlopen):
        """連線錯誤回傳 (None, error_msg)。"""
        mock_urlopen.side_effect = urllib.error.URLError("Connection refused")
        data, err = lib.http_get_json("http://unreachable:9090/")
        assert data is None
        assert "Connection refused" in err

    @patch("_lib_python.urllib.request.urlopen")
    def test_http_error(self, mock_urlopen):
        """HTTP 錯誤回傳 (None, error_msg)。"""
        mock_urlopen.side_effect = urllib.error.HTTPError(
            "http://test/", 500, "Internal Server Error", {}, None)
        data, err = lib.http_get_json("http://test/")
        assert data is None
        assert "500" in err

    @patch("_lib_python.urllib.request.urlopen")
    def test_custom_headers(self, mock_urlopen):
        """自訂 headers 正確傳遞。"""
        mock_urlopen.return_value = mock_http_response({"ok": True})
        lib.http_get_json("http://test/", headers={"Authorization": "Bearer token"})
        call_args = mock_urlopen.call_args
        req = call_args[0][0]
        assert req.get_header("Authorization") == "Bearer token"


class TestHttpPostJson:
    """http_post_json() 測試。"""

    @patch("_lib_python.urllib.request.urlopen")
    def test_success_with_payload(self, mock_urlopen):
        """成功 POST JSON payload。"""
        mock_urlopen.return_value = mock_http_response({"id": "123"})
        data, err = lib.http_post_json(
            "http://localhost:9093/api/v2/silences",
            payload={"matchers": [], "comment": "test"})
        assert data == {"id": "123"}
        assert err is None
        call_args = mock_urlopen.call_args
        sent_data = call_args[1].get("data") or call_args[0][1]
        parsed = json.loads(sent_data.decode("utf-8"))
        assert parsed["comment"] == "test"

    @patch("_lib_python.urllib.request.urlopen")
    def test_none_payload(self, mock_urlopen):
        """payload=None 時不送 body。"""
        mock_urlopen.return_value = mock_http_response({})
        lib.http_post_json("http://test/", payload=None)
        call_args = mock_urlopen.call_args
        sent_data = call_args[1].get("data") or call_args[0][1] if len(call_args[0]) > 1 else None
        assert sent_data is None

    @patch("_lib_python.urllib.request.urlopen")
    def test_http_error_formatted(self, mock_urlopen):
        """HTTP 錯誤格式化為 'HTTP {code}: {reason}'。"""
        mock_urlopen.side_effect = urllib.error.HTTPError(
            "http://test/", 400, "Bad Request", {}, None)
        data, err = lib.http_post_json("http://test/", payload={})
        assert data is None
        assert "400" in err
        assert "Bad Request" in err

    @patch("_lib_python.urllib.request.urlopen")
    def test_custom_method(self, mock_urlopen):
        """支援自訂 HTTP method。"""
        mock_urlopen.return_value = mock_http_response({})
        lib.http_post_json("http://test/", method="DELETE")
        call_args = mock_urlopen.call_args
        req = call_args[0][0]
        assert req.get_method() == "DELETE"


class TestHttpRequestWithRetry:
    """http_request_with_retry() 重試邏輯測試。"""

    @patch("time.sleep")
    @patch("_lib_python.urllib.request.urlopen")
    def test_success_first_attempt(self, mock_urlopen, mock_sleep):
        """第一次就成功，不重試。"""
        mock_urlopen.return_value = mock_http_response({"status": "ok"})
        result = lib.http_request_with_retry("http://test/")
        assert result == {"status": "ok"}
        mock_sleep.assert_not_called()

    @patch("time.sleep")
    @patch("_lib_python.urllib.request.urlopen")
    def test_retry_on_5xx(self, mock_urlopen, mock_sleep):
        """5xx 錯誤觸發重試，第二次成功。"""
        mock_urlopen.side_effect = [
            urllib.error.HTTPError("http://test/", 503, "Unavailable", {}, None),
            mock_http_response({"status": "recovered"}),
        ]
        result = lib.http_request_with_retry("http://test/", max_retries=3)
        assert result == {"status": "recovered"}
        mock_sleep.assert_called_once_with(1)

    @patch("time.sleep")
    @patch("_lib_python.urllib.request.urlopen")
    def test_no_retry_on_4xx(self, mock_urlopen, mock_sleep):
        """4xx 錯誤不重試，立即 raise。"""
        mock_urlopen.side_effect = urllib.error.HTTPError(
            "http://test/", 404, "Not Found", {}, None)
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            lib.http_request_with_retry("http://test/")
        assert exc_info.value.code == 404
        mock_sleep.assert_not_called()

    @patch("time.sleep")
    @patch("_lib_python.urllib.request.urlopen")
    def test_retry_exhausted_raises(self, mock_urlopen, mock_sleep):
        """重試耗盡後 raise 最後一個錯誤。"""
        mock_urlopen.side_effect = urllib.error.HTTPError(
            "http://test/", 500, "Server Error", {}, None)
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            lib.http_request_with_retry("http://test/", max_retries=2)
        assert exc_info.value.code == 500
        assert mock_sleep.call_count == 1

    @patch("time.sleep")
    @patch("_lib_python.urllib.request.urlopen")
    def test_retry_on_connection_error(self, mock_urlopen, mock_sleep):
        """連線錯誤觸發重試。"""
        mock_urlopen.side_effect = [
            urllib.error.URLError("Connection refused"),
            mock_http_response({"ok": True}),
        ]
        result = lib.http_request_with_retry("http://test/", max_retries=3)
        assert result == {"ok": True}

    @patch("time.sleep")
    @patch("_lib_python.urllib.request.urlopen")
    def test_exponential_backoff(self, mock_urlopen, mock_sleep):
        """重試間隔遵循指數退避 (2^0, 2^1)。"""
        mock_urlopen.side_effect = [
            urllib.error.HTTPError("http://test/", 502, "Bad Gateway", {}, None),
            urllib.error.HTTPError("http://test/", 502, "Bad Gateway", {}, None),
            mock_http_response({"ok": True}),
        ]
        lib.http_request_with_retry("http://test/", max_retries=3)
        assert mock_sleep.call_count == 2
        mock_sleep.assert_any_call(1)
        mock_sleep.assert_any_call(2)


# ============================================================
# write_onboard_hints / read_onboard_hints round-trip
# ============================================================

class TestOnboardHintsRoundTrip:
    """write_onboard_hints → read_onboard_hints 往返測試。"""

    def test_round_trip_basic(self, config_dir):
        """基本 dict 寫入後讀回應完全一致。"""
        hints = {"tenants": ["db-a", "db-b"], "db_types": {"db-a": "mysql"}}
        path = lib.write_onboard_hints(str(config_dir), hints)
        result = lib.read_onboard_hints(path)
        assert result == hints

    def test_round_trip_unicode(self, config_dir):
        """含 Unicode（繁體中文）的值正確往返。"""
        hints = {"note": "租戶設定完成", "tags": ["告警", "監控"]}
        path = lib.write_onboard_hints(str(config_dir), hints)
        result = lib.read_onboard_hints(path)
        assert result == hints
        # 確認 ensure_ascii=False 寫入原生 Unicode
        raw = pathlib.Path(path).read_text(encoding="utf-8")
        assert "租戶" in raw

    def test_round_trip_nested(self, config_dir):
        """深層巢狀結構正確往返。"""
        hints = {
            "routing_hints": {
                "db-a": {"receiver": "webhook", "url": "https://example.com"},
                "db-b": {"receiver": "slack", "channel": "#alerts"},
            },
            "metadata": {"version": "2.0.0", "generated": True},
        }
        path = lib.write_onboard_hints(str(config_dir), hints)
        assert lib.read_onboard_hints(path) == hints

    def test_round_trip_empty_dict(self, config_dir):
        """空 dict 正確往返。"""
        path = lib.write_onboard_hints(str(config_dir), {})
        assert lib.read_onboard_hints(path) == {}

    def test_round_trip_list_values(self, config_dir):
        """含 list / numeric / boolean 值正確往返。"""
        hints = {"counts": [1, 2, 3], "enabled": True, "ratio": 0.95}
        path = lib.write_onboard_hints(str(config_dir), hints)
        assert lib.read_onboard_hints(path) == hints

    def test_write_creates_file(self, config_dir):
        """write_onboard_hints 建立 onboard-hints.json 檔案。"""
        path = lib.write_onboard_hints(str(config_dir), {"test": 1})
        assert os.path.isfile(path)
        assert path.endswith("onboard-hints.json")

    def test_write_file_permissions(self, config_dir):
        """寫入檔案權限為 0o600（SAST 規範）。"""
        path = lib.write_onboard_hints(str(config_dir), {"x": 1})
        mode = os.stat(path).st_mode & 0o777
        assert mode == 0o600

    @pytest.mark.parametrize("path", [
        "/nonexistent/path.json", None, "",
    ], ids=["missing-file", "none", "empty-string"])
    def test_read_invalid_path_returns_none(self, path):
        """無效路徑（不存在 / None / 空字串）回傳 None。"""
        assert lib.read_onboard_hints(path) is None

    def test_overwrite_existing(self, config_dir):
        """重複寫入同目錄會覆蓋舊檔。"""
        lib.write_onboard_hints(str(config_dir), {"v": 1})
        path = lib.write_onboard_hints(str(config_dir), {"v": 2})
        assert lib.read_onboard_hints(path) == {"v": 2}
