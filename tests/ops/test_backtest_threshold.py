#!/usr/bin/env python3
"""test_backtest_threshold.py — backtest_threshold.py pytest 風格測試。

驗證:
  1. parse_lookback() — 時間窗口解析
  2. count_threshold_breaches() — 超閾值計數
  3. extract_changes_from_dirs() — 目錄比對
  4. backtest_change() — 單一變更回測
  5. generate_report() — 報告彙整
  6. generate_markdown() — Markdown 格式
"""

import json
import os
import subprocess
import sys
import tempfile
from unittest.mock import patch

import pytest

import backtest_threshold as bt  # noqa: E402


class TestParseLookback:
    """parse_lookback() 測試。"""

    @pytest.mark.parametrize("lookback_str,expected", [
        ("7d", 7 * 86400),
        ("24h", 24 * 3600),
        ("30m", 30 * 60),
        ("invalid", 7 * 86400),
    ], ids=["days", "hours", "minutes", "invalid-fallback"])
    def test_parse_lookback(self, lookback_str, expected):
        """各種 lookback 字串正確解析為秒數。"""
        assert bt.parse_lookback(lookback_str) == expected


class TestCountThresholdBreaches:
    """count_threshold_breaches() 測試。"""

    @pytest.mark.parametrize("values,threshold,direction,expected", [
        ([(1, "80"), (2, "50"), (3, "90"), (4, "70")], 70, "above", 2),
        ([(1, "20"), (2, "50"), (3, "10"), (4, "30")], 25, "below", 2),
        ([(1, "80"), (2, "NaN"), (3, "90")], 70, "above", 2),
        ([(1, "80"), (2, "90")], "70", "above", 2),
    ], ids=["above", "below", "invalid-skipped", "string-threshold"])
    def test_breach_counting(self, values, threshold, direction, expected):
        """各種閾值方向與邊際條件正確計數。"""
        assert bt.count_threshold_breaches(values, threshold, direction) == expected

    def test_none_threshold(self):
        """None 閾值應返回 0。"""
        values = [(1, "80")]
        assert bt.count_threshold_breaches(values, None) == 0

    def test_empty_values(self):
        """空值列表應返回 0。"""
        assert bt.count_threshold_breaches([], 70) == 0


class TestExtractChangesFromDirs:
    """extract_changes_from_dirs() 測試。"""

    def test_detect_changes(self):
        """應偵測到閾值變更。"""
        with tempfile.TemporaryDirectory() as current, \
             tempfile.TemporaryDirectory() as baseline:
            # Current: mysql_connections = 50
            with open(os.path.join(current, "db-a.yaml"), "w") as f:
                f.write("mysql_connections: 50\n")
            # Baseline: mysql_connections = 70
            with open(os.path.join(baseline, "db-a.yaml"), "w") as f:
                f.write("mysql_connections: 70\n")

            changes = bt.extract_changes_from_dirs(current, baseline)
            assert len(changes) == 1
            assert changes[0]["tenant"] == "db-a"
            assert changes[0]["metric"] == "mysql_connections"
            assert changes[0]["old_value"] == "70"
            assert changes[0]["new_value"] == "50"

    def test_skip_underscore_keys(self):
        """_ 前綴的 key 應被忽略。"""
        with tempfile.TemporaryDirectory() as current, \
             tempfile.TemporaryDirectory() as baseline:
            with open(os.path.join(current, "db-a.yaml"), "w") as f:
                f.write("_silent_mode: warning\nmysql_connections: 50\n")
            with open(os.path.join(baseline, "db-a.yaml"), "w") as f:
                f.write("mysql_connections: 50\n")

            changes = bt.extract_changes_from_dirs(current, baseline)
            assert len(changes) == 0

    def test_skip_underscore_files(self):
        """_ 前綴的檔案應被忽略。"""
        with tempfile.TemporaryDirectory() as current, \
             tempfile.TemporaryDirectory() as baseline:
            with open(os.path.join(current, "_defaults.yaml"), "w") as f:
                f.write("mysql_connections: 50\n")
            changes = bt.extract_changes_from_dirs(current, baseline)
            assert len(changes) == 0

    def test_no_changes(self):
        """相同配置不應有變更。"""
        with tempfile.TemporaryDirectory() as current, \
             tempfile.TemporaryDirectory() as baseline:
            for d in [current, baseline]:
                with open(os.path.join(d, "db-a.yaml"), "w") as f:
                    f.write("mysql_connections: 50\n")
            changes = bt.extract_changes_from_dirs(current, baseline)
            assert len(changes) == 0


class TestGenerateReport:
    """generate_report() 測試。"""

    def test_risk_summary(self):
        """測試風險摘要統計。"""
        results = [
            {"status": "analyzed", "risk": "HIGH", "tenant": "a", "metric": "m"},
            {"status": "analyzed", "risk": "LOW", "tenant": "b", "metric": "n"},
            {"status": "no_data", "risk": "UNKNOWN", "tenant": "c", "metric": "o"},
        ]
        report = bt.generate_report(results, "7d")
        assert report["risk_summary"]["HIGH"] == 1
        assert report["risk_summary"]["LOW"] == 1
        assert report["analyzed"] == 2
        assert report["no_data"] == 1

    def test_report_has_timestamp(self):
        """測試報告應包含時間戳。"""
        report = bt.generate_report([], "7d")
        assert "timestamp" in report


class TestGenerateMarkdown:
    """generate_markdown() 測試。"""

    def test_contains_header(self):
        """測試 Markdown 應包含標題。"""
        report = bt.generate_report([], "7d")
        md = bt.generate_markdown(report)
        assert "## Threshold Backtest Results" in md

    def test_high_risk_warning(self):
        """測試 Markdown 應包含高風險警告。"""
        results = [
            {"status": "analyzed", "risk": "HIGH", "tenant": "a",
             "metric": "m", "old_value": "70", "new_value": "50",
             "message": "test"},
        ]
        report = bt.generate_report(results, "7d")
        md = bt.generate_markdown(report)
        assert "HIGH risk" in md

    def test_table_format(self):
        """測試 Markdown 應包含表格格式。"""
        results = [
            {"status": "analyzed", "risk": "LOW", "tenant": "db-a",
             "metric": "mysql_conn", "old_value": "70", "new_value": "50",
             "message": "no firing"},
        ]
        report = bt.generate_report(results, "7d")
        md = bt.generate_markdown(report)
        assert "| LOW |" in md
        assert "`mysql_conn`" in md

    def test_null_values_shown_as_dash(self):
        """None 值在 Markdown 表格中顯示為 —。"""
        results = [
            {"status": "analyzed", "risk": "MEDIUM", "tenant": "db-a",
             "metric": "m", "old_value": None, "new_value": "50",
             "message": "newly enabled"},
        ]
        report = bt.generate_report(results, "7d")
        md = bt.generate_markdown(report)
        assert "—" in md


# ── backtest_change（mock query_range）────────────────────────────


class TestBacktestChange:
    """backtest_change() 單一變更回測分析。"""

    def _make_values(self, vals):
        """產生 Prometheus 格式的 [(ts, val_str)] 清單。"""
        return [(i, str(v)) for i, v in enumerate(vals)]

    def test_no_data(self, monkeypatch):
        """無歷史資料回傳 no_data 狀態。"""
        monkeypatch.setattr(bt, "query_range", lambda *a, **kw: [])
        change = {"tenant": "db-a", "metric": "cpu", "old_value": "70", "new_value": "50"}
        result = bt.backtest_change("http://prom", change, 86400)
        assert result["status"] == "no_data"
        assert result["risk"] == "UNKNOWN"

    def test_normal_change_low_risk(self, monkeypatch):
        """閾值從 70→65：兩邊都不觸發 → LOW。"""
        values = self._make_values([30, 40, 50, 60, 55, 45, 35])
        monkeypatch.setattr(bt, "query_range",
                            lambda *a, **kw: [{"values": values}])
        change = {"tenant": "db-a", "metric": "cpu", "old_value": "70", "new_value": "65"}
        result = bt.backtest_change("http://prom", change, 86400)
        assert result["status"] == "analyzed"
        assert result["risk"] == "LOW"

    def test_threshold_tighter_high_risk(self, monkeypatch):
        """閾值大幅收緊：大量新增觸發 → HIGH。"""
        values = self._make_values([60, 65, 70, 75, 80, 85, 90, 95, 100, 55])
        monkeypatch.setattr(bt, "query_range",
                            lambda *a, **kw: [{"values": values}])
        change = {"tenant": "db-a", "metric": "cpu", "old_value": "100", "new_value": "50"}
        result = bt.backtest_change("http://prom", change, 86400)
        assert result["status"] == "analyzed"
        # old: 0 breaches (>100), new: 9 breaches (>50) → should start firing → HIGH
        assert result["risk"] == "HIGH"

    def test_disable_transition(self, monkeypatch):
        """啟用→停用：MEDIUM 風險。"""
        values = self._make_values([80, 90])
        monkeypatch.setattr(bt, "query_range",
                            lambda *a, **kw: [{"values": values}])
        change = {"tenant": "db-a", "metric": "cpu", "old_value": "70", "new_value": "disable"}
        result = bt.backtest_change("http://prom", change, 86400)
        assert result["risk"] == "MEDIUM"
        assert "disabled" in result["message"]

    def test_enable_transition(self, monkeypatch):
        """停用→啟用：根據觸發比例決定風險。"""
        values = self._make_values([80, 90, 50, 60, 70, 85, 95, 75, 65, 55])
        monkeypatch.setattr(bt, "query_range",
                            lambda *a, **kw: [{"values": values}])
        change = {"tenant": "db-a", "metric": "cpu", "old_value": "disable", "new_value": "70"}
        result = bt.backtest_change("http://prom", change, 86400)
        assert result["status"] == "analyzed"
        assert result["new_breach_count"] > 0


# ── query_range（mock http_get_json）──────────────────────────────


class TestQueryRange:
    """query_range() Prometheus range query。"""

    def test_success(self, monkeypatch):
        """成功查詢回傳結果。"""
        def mock_get(url, timeout=30):
            return {
                "status": "success",
                "data": {"result": [{"values": [[1, "42"]]}]},
            }, None
        monkeypatch.setattr(bt, "http_get_json", mock_get)
        result = bt.query_range("http://prom", "up", 3600)
        assert len(result) == 1

    def test_http_error_returns_empty(self, monkeypatch):
        """HTTP 錯誤回傳空清單。"""
        def mock_get(url, timeout=30):
            return None, "connection refused"
        monkeypatch.setattr(bt, "http_get_json", mock_get)
        result = bt.query_range("http://prom", "up", 3600)
        assert result == []

    def test_api_error_returns_empty(self, monkeypatch):
        """API 錯誤狀態回傳空清單。"""
        def mock_get(url, timeout=30):
            return {"status": "error"}, None
        monkeypatch.setattr(bt, "http_get_json", mock_get)
        result = bt.query_range("http://prom", "bad{", 3600)
        assert result == []


# ── print_text_report ─────────────────────────────────────────────


class TestPrintTextReport:
    """print_text_report() 文字報告輸出。"""

    def test_contains_header(self, capsys):
        """報告包含標題。"""
        report = bt.generate_report([], "7d")
        bt.print_text_report(report)
        out = capsys.readouterr().out
        assert "Threshold Backtest Report" in out

    def test_shows_risk_counts(self, capsys):
        """報告顯示風險統計。"""
        results = [
            {"status": "analyzed", "risk": "HIGH", "tenant": "a",
             "metric": "m", "old_value": "70", "new_value": "50",
             "message": "50 more"},
        ]
        report = bt.generate_report(results, "7d")
        bt.print_text_report(report)
        out = capsys.readouterr().out
        assert "1 HIGH" in out

    def test_no_data_count(self, capsys):
        """報告顯示 no_data 計數。"""
        results = [
            {"status": "no_data", "risk": "UNKNOWN", "tenant": "a",
             "metric": "m", "old_value": "70", "new_value": "50",
             "message": "No historical data"},
        ]
        report = bt.generate_report(results, "7d")
        bt.print_text_report(report)
        out = capsys.readouterr().out
        assert "No data: 1" in out


# ── extract_changes_from_git_diff（mock subprocess）───────────────


class TestExtractChangesFromGitDiff:
    """extract_changes_from_git_diff() git diff 解析。"""

    def test_normal_diff(self, monkeypatch):
        """正常 diff 解析出閾值變更。"""
        diff_output = (
            "+++ b/conf.d/db-a.yaml\n"
            "-  mysql_connections: 70\n"
            "+  mysql_connections: 50\n"
        )
        def mock_run(*args, **kwargs):
            m = type("R", (), {"returncode": 0, "stdout": diff_output})()
            return m
        monkeypatch.setattr(subprocess, "run", mock_run)
        changes = bt.extract_changes_from_git_diff()
        assert len(changes) == 1
        assert changes[0]["tenant"] == "db-a"
        assert changes[0]["old_value"] == "70"
        assert changes[0]["new_value"] == "50"

    def test_underscore_keys_filtered(self, monkeypatch):
        """_ 前綴 key 被過濾。"""
        diff_output = (
            "+++ b/conf.d/db-a.yaml\n"
            "-  _silent_mode: normal\n"
            "+  _silent_mode: warning\n"
        )
        def mock_run(*args, **kwargs):
            return type("R", (), {"returncode": 0, "stdout": diff_output})()
        monkeypatch.setattr(subprocess, "run", mock_run)
        changes = bt.extract_changes_from_git_diff()
        assert len(changes) == 0

    def test_git_failure_returns_empty(self, monkeypatch):
        """git 命令失敗回傳空清單。"""
        def mock_run(*args, **kwargs):
            return type("R", (), {"returncode": 1, "stdout": ""})()
        monkeypatch.setattr(subprocess, "run", mock_run)
        changes = bt.extract_changes_from_git_diff()
        assert changes == []

    def test_git_timeout_returns_empty(self, monkeypatch):
        """git 命令逾時回傳空清單。"""
        def mock_run(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="git", timeout=15)
        monkeypatch.setattr(subprocess, "run", mock_run)
        changes = bt.extract_changes_from_git_diff()
        assert changes == []

    def test_new_key_only(self, monkeypatch):
        """只有新增 key（無對應 old）。"""
        diff_output = (
            "+++ b/conf.d/db-a.yaml\n"
            "+  mysql_connections: 50\n"
        )
        def mock_run(*args, **kwargs):
            return type("R", (), {"returncode": 0, "stdout": diff_output})()
        monkeypatch.setattr(subprocess, "run", mock_run)
        changes = bt.extract_changes_from_git_diff()
        assert len(changes) == 1
        assert changes[0]["old_value"] is None
        assert changes[0]["new_value"] == "50"


# ── prometheus_available ──────────────────────────────────────────


class TestPrometheusAvailable:
    """prometheus_available() 可達性檢查。"""

    def test_reachable(self, monkeypatch):
        """可達回傳 True。"""
        monkeypatch.setattr(bt, "http_get_json",
                            lambda url, timeout=5: ({"status": "ok"}, None))
        assert bt.prometheus_available("http://prom") is True

    def test_unreachable(self, monkeypatch):
        """不可達回傳 False。"""
        monkeypatch.setattr(bt, "http_get_json",
                            lambda url, timeout=5: (None, "refused"))
        assert bt.prometheus_available("http://prom") is False


# ── RISK_THRESHOLDS ──────────────────────────────────────────────


class TestRiskThresholds:
    """RISK_THRESHOLDS 常數驗證。"""

    def test_high_greater_than_medium(self):
        """HIGH > MEDIUM > LOW。"""
        assert bt.RISK_THRESHOLDS["HIGH"] > bt.RISK_THRESHOLDS["MEDIUM"]
        assert bt.RISK_THRESHOLDS["MEDIUM"] > bt.RISK_THRESHOLDS["LOW"]


# ── 自訂告警 recipe 感知（#657 fail-loud）─────────────────────────


class TestCustomAlertDetection:
    """find_custom_alert_tenants / keep_flat_threshold_changes — 把 flat 工具
    對 _custom_alerts 的隱性無-recipe-path 補成顯性 fail-loud。conf.d 用
    `tenants: {<id>: {<metric>: <value>, _custom_alerts: [...]}}` 包裹格式。"""

    def _write(self, d, name, text):
        with open(os.path.join(d, name), "w") as f:
            f.write(text)

    _RECIPE_BLOCK = (
        "    _custom_alerts:\n"
        "      - recipe: threshold\n"
        "        name: q\n"
        "        metric: mysql_global_status_threads_connected\n"
        "        op: '>'\n"
        "        window: 5m\n"
        "        threshold: '150:warning'\n"
    )

    def test_detects_recipe_tenant(self):
        """tenants.<id>._custom_alerts 非空 → 該租戶 id 被偵測，純 flat 租戶不誤報。"""
        with tempfile.TemporaryDirectory() as d:
            self._write(d, "db-b.yaml",
                        "tenants:\n  db-b:\n    mysql_connections: '100'\n" + self._RECIPE_BLOCK)
            self._write(d, "db-a.yaml",
                        "tenants:\n  db-a:\n    mysql_connections: '70'\n")
            parsed = bt.load_conf_files([
                os.path.join(d, "db-b.yaml"),
                os.path.join(d, "db-a.yaml"),
            ])
            assert bt.find_custom_alert_tenants(parsed) == ["db-b"]

    def test_empty_custom_alerts_not_flagged(self):
        """空 _custom_alerts（[]）不算 recipe 租戶。"""
        with tempfile.TemporaryDirectory() as d:
            self._write(d, "db-b.yaml",
                        "tenants:\n  db-b:\n    mysql_connections: '100'\n    _custom_alerts: []\n")
            parsed = bt.load_conf_files([os.path.join(d, "db-b.yaml")])
            assert bt.find_custom_alert_tenants(parsed) == []

    def test_multi_tenant_file_uses_key_not_filename(self):
        """一個檔含多租戶 → 只有帶 recipe 的那個 id 被列出（id 來自 key，非檔名）。"""
        with tempfile.TemporaryDirectory() as d:
            self._write(d, "shop.yaml",
                        "tenants:\n"
                        "  shop-a:\n    mysql_connections: '80'\n"
                        "  shop-b:\n    mysql_connections: '90'\n" + self._RECIPE_BLOCK)
            parsed = bt.load_conf_files([os.path.join(d, "shop.yaml")])
            assert bt.find_custom_alert_tenants(parsed) == ["shop-b"]

    def test_skips_underscore_platform_files(self):
        """_ 前綴平台檔（如 _defaults.yaml）不被當成租戶。"""
        with tempfile.TemporaryDirectory() as d:
            self._write(d, "_defaults.yaml", "defaults:\n  mysql_connections: '50'\n")
            parsed = bt.load_conf_files([os.path.join(d, "_defaults.yaml")])
            assert parsed == {}
            assert bt.find_custom_alert_tenants(parsed) == []

    def test_keep_flat_drops_recipe_inner_fields(self):
        """git-diff line parser 誤抽的 recipe 內層欄位（recipe/name/op/threshold/metric）
        被濾掉，只留真正在 tenants.<id> 下的純量閾值。"""
        parsed = {"db-b": {"tenants": {"db-b": {
            "mysql_connections": "100",
            "_custom_alerts": [{"recipe": "threshold", "name": "q",
                                "metric": "x", "op": ">", "threshold": "150:warning"}],
        }}}}
        changes = [
            {"tenant": "db-b", "metric": "mysql_connections", "old_value": "70", "new_value": "100"},
            {"tenant": "db-b", "metric": "threshold", "old_value": "150:warning", "new_value": "200:warning"},
            {"tenant": "db-b", "metric": "metric", "old_value": "x", "new_value": "y"},
            {"tenant": "db-b", "metric": "op", "old_value": ">", "new_value": "<"},
        ]
        kept = bt.keep_flat_threshold_changes(changes, parsed)
        assert [c["metric"] for c in kept] == ["mysql_connections"]

    def test_keep_flat_keeps_when_file_unavailable(self):
        """檔案不可得（如純移除）→ 保留，不臆測。"""
        changes = [{"tenant": "gone", "metric": "mysql_connections",
                    "old_value": "70", "new_value": None}]
        assert bt.keep_flat_threshold_changes(changes, {}) == changes

    def test_notice_empty_when_no_recipes(self):
        """無 recipe 租戶 → notice 為空字串。"""
        assert bt.custom_alert_notice([]) == ""

    def test_notice_and_markdown_point_at_657(self):
        """notice / markdown 都列出租戶並指向 #657。"""
        note = bt.custom_alert_notice(["db-a", "db-b"])
        assert "db-a" in note and "db-b" in note and "#657" in note
        md = bt.custom_alert_markdown(["db-b"])
        assert "#657" in md and "`db-b`" in md
