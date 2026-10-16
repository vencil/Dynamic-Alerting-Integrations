#!/usr/bin/env python3
"""test_backtest_threshold.py — backtest_threshold.py pytest 風格測試。

驗證:
  1. parse_lookback() — 時間窗口解析
  2. count_threshold_breaches() — 超閾值計數
  3. extract_changes_from_dirs() — 目錄比對（值來自 da-guard served-values，#2119）
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
    ], ids=["days", "hours", "minutes"])
    def test_parse_lookback(self, lookback_str, expected):
        """各種 lookback 字串正確解析為秒數。"""
        assert bt.parse_lookback(lookback_str) == expected

    @pytest.mark.parametrize("lookback_str", ["invalid", "7", "", "banana"],
                             ids=["word", "bare-number", "empty", "banana"])
    def test_parse_lookback_unusable_raises(self, lookback_str):
        """#1625: 不符合 `<數字><d|h|m>` 的值 → ValueError，不再靜默回退 7d。

        ⛔ 這條原本是 parametrize 裡的 `("invalid", 7 * 86400)`（id
        `invalid-fallback`），把「打錯的值被當成預設」當成契約在守。裸數字
        `7` 是舊文件教的寫法——最需要被抓的一種。
        """
        with pytest.raises(ValueError) as exc:
            bt.parse_lookback(lookback_str)
        assert repr(lookback_str) in str(exc.value)
        assert "<number><d|h|m>" in str(exc.value)


class TestGitDiffPrecondition:
    """#1556 item 4 (D-06): `--git-diff` 供了但 git 不可用 → exit 2，不是「沒變更」。

    da-tools 映像檔沒裝 git、WORKDIR 也不是 repo，所以 `--git-diff` 在那裡永遠
    像「你的 PR 沒改東西」。這裡把 `_git_bytes` 換成 None 來重現；control
    列用 b""（git 正常、diff 空）證明原本的 "No threshold changes found." 路徑
    還在。
    """

    def _argv(self, cli_argv, *extra):
        cli_argv("backtest_threshold", "--git-diff",
                 "--prometheus", "http://prom:9090", *extra)

    def test_git_unusable_exits_caller_error(self, monkeypatch, capsys, cli_argv):
        monkeypatch.setattr(bt, "_git_bytes", lambda *a, **k: None)
        # Prometheus 可達與否都不該影響：這裡故意讓它「不可達」並帶
        # --skip-if-unavailable，證明 git 前置檢查不被那個旗標跳過。
        monkeypatch.setattr(bt, "prometheus_available", lambda url, timeout=5: False)
        self._argv(cli_argv, "--skip-if-unavailable")
        with pytest.raises(SystemExit) as exc_info:
            bt.main()
        assert exc_info.value.code == 2
        captured = capsys.readouterr()
        assert "--git-diff" in captured.err
        assert "could not run" in captured.err
        assert "Do not switch to --config-dir" in captured.err
        assert captured.out == ""

    def test_git_unusable_json_envelope(self, monkeypatch, capsys, cli_argv):
        monkeypatch.setattr(bt, "_git_bytes", lambda *a, **k: None)
        monkeypatch.setattr(bt, "prometheus_available", lambda url, timeout=5: True)
        self._argv(cli_argv, "--json")
        with pytest.raises(SystemExit) as exc_info:
            bt.main()
        assert exc_info.value.code == 2
        captured = capsys.readouterr()
        doc = json.loads(captured.out)           # 全文 parse ⇒ stdout 恰一份 JSON
        assert doc["status"] == "caller_error"
        assert doc["reason"] == "git_diff_unavailable"
        assert set(doc) == set(bt.generate_report([], doc["lookback"])) | {"status", "reason"}
        assert "could not run" in captured.err

    def test_empty_diff_is_still_no_changes(self, monkeypatch, capsys, cli_argv):
        """control：git 正常、diff 空（b""）→ 原本的 "No threshold changes found." exit 0。"""
        monkeypatch.setattr(bt, "_git_bytes", lambda *a, **k: b"")
        monkeypatch.setattr(bt, "prometheus_available", lambda url, timeout=5: True)
        self._argv(cli_argv, "--json")
        with pytest.raises(SystemExit) as exc_info:
            bt.main()
        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert "No threshold changes found." in captured.err
        doc = json.loads(captured.out)
        assert doc["status"] == "no_changes"


class TestLookbackCli:
    """#1625: `--lookback` 供了但不可用 → argparse 出口 exit 2 並指名格式。

    負向列只證明「壞值被擋」；兩條 control 列證明「好值與省略仍可用」——
    一個把所有 `--lookback` 都拒絕的修法，光靠負向列也會全綠。
    """

    def _skip_argv(self, cli_argv, *extra):
        cli_argv("backtest_threshold",
                 "--tenant", "tenant-one", "--metric", "max_connections",
                 "--old-value", "100", "--new-value", "150",
                 "--prometheus", "http://prom:9090",
                 "--skip-if-unavailable", "--json", *extra)

    @pytest.mark.parametrize("bad", ["banana", "7", ""],
                             ids=["banana", "bare-number", "empty"])
    def test_unusable_lookback_exits_caller_error(self, monkeypatch, capsys,
                                                  cli_argv, bad):
        monkeypatch.setattr(bt, "prometheus_available", lambda url, timeout=5: False)
        self._skip_argv(cli_argv, "--lookback", bad)
        with pytest.raises(SystemExit) as exc_info:
            bt.main()
        assert exc_info.value.code == 2
        err = capsys.readouterr().err
        assert "<number><d|h|m>" in err
        assert repr(bad) in err                  # `!r` ⇒ '' 看得見

    def test_valid_lookback_accepted(self, monkeypatch, capsys, cli_argv):
        """control：`--lookback 30d` 照常接受並被回音。"""
        monkeypatch.setattr(bt, "prometheus_available", lambda url, timeout=5: False)
        self._skip_argv(cli_argv, "--lookback", "30d")
        with pytest.raises(SystemExit) as exc_info:
            bt.main()
        assert exc_info.value.code == 0
        doc = json.loads(capsys.readouterr().out)
        assert doc["lookback"] == "30d"

    def test_omitted_lookback_uses_default(self, monkeypatch, capsys, cli_argv):
        """control：省略 `--lookback` → 預設 `7d`（argparse 對字串預設也跑 type）。"""
        monkeypatch.setattr(bt, "prometheus_available", lambda url, timeout=5: False)
        self._skip_argv(cli_argv)
        with pytest.raises(SystemExit) as exc_info:
            bt.main()
        assert exc_info.value.code == 0
        doc = json.loads(capsys.readouterr().out)
        assert doc["lookback"] == "7d"


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


_DEFAULTS = "defaults:\n  mysql_connections: 80\n"


def _confd(root, files):
    """A conf.d tree at `root`: {relative name: body}."""
    os.makedirs(root, exist_ok=True)
    for name, body in files.items():
        with open(os.path.join(root, name), "w", encoding="utf-8") as f:
            f.write(body)
    return root


@pytest.mark.usefixtures("da_guard_env")
class TestExtractChangesFromDirs:
    """extract_changes_from_dirs() 測試：兩棵樹的值都來自 da-guard served-values（#2119）。

    完整的語意矩陣（平台層、defaults 層、profile、子目錄、排程）在
    tests/shared/test_backtest_served_values_matrix.py。
    """

    def test_detect_changes(self, tmp_path):
        """應偵測到閾值變更，metric 是門檻 key，不是 `tenants`。"""
        cur = _confd(tmp_path / "cur", {"_defaults.yaml": _DEFAULTS,
                                        "db-a.yaml": "tenants:\n  db-a:\n    mysql_connections: 50\n"})
        base = _confd(tmp_path / "base", {"_defaults.yaml": _DEFAULTS,
                                          "db-a.yaml": "tenants:\n  db-a:\n    mysql_connections: 70\n"})
        changes = bt.extract_changes_from_dirs(str(cur), str(base))
        # #2750：每筆帶 served-values 給的 /metrics series（矩陣測試逐格比對）。
        assert [s.metric_key for s in changes[0].pop("series")] == ["mysql_connections"]
        assert changes == [{"tenant": "db-a", "metric": "mysql_connections",
                            "old_value": "70", "new_value": "50"}]

    def test_skip_underscore_keys(self, tmp_path):
        """_ 前綴的 key 不是門檻，改了不算門檻變更。"""
        cur = _confd(tmp_path / "cur", {"_defaults.yaml": _DEFAULTS,
                                        "db-a.yaml": "tenants:\n  db-a:\n    _silent_mode: warning\n"
                                                     "    mysql_connections: 50\n"})
        base = _confd(tmp_path / "base", {"_defaults.yaml": _DEFAULTS,
                                          "db-a.yaml": "tenants:\n  db-a:\n    mysql_connections: 50\n"})
        assert bt.extract_changes_from_dirs(str(cur), str(base)) == []

    def test_defaults_file_change_is_a_change(self, tmp_path):
        """`_defaults.yaml` 改了，沒覆寫該 key 的租戶送出的值就變了（#2119；以前略過 `_` 檔）。"""
        tenant = {"db-a.yaml": "tenants:\n  db-a: {}\n"}
        cur = _confd(tmp_path / "cur", {"_defaults.yaml": "defaults:\n  mysql_connections: 90\n", **tenant})
        base = _confd(tmp_path / "base", {"_defaults.yaml": _DEFAULTS, **tenant})
        changes = bt.extract_changes_from_dirs(str(cur), str(base))
        assert [{k: v for k, v in c.items() if k != "series"} for c in changes] == [
            {"tenant": "db-a", "metric": "mysql_connections", "old_value": "80", "new_value": "90"}]

    def test_no_changes(self, tmp_path):
        """相同配置不應有變更。"""
        files = {"_defaults.yaml": _DEFAULTS, "db-a.yaml": "tenants:\n  db-a:\n    mysql_connections: 50\n"}
        cur, base = _confd(tmp_path / "cur", files), _confd(tmp_path / "base", files)
        assert bt.extract_changes_from_dirs(str(cur), str(base)) == []


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

    def test_pipe_in_message_stays_in_one_cell(self):
        """#3059：query_error 的訊息帶 Prometheus 原文，可能含 `|`；跳脫後仍是 7 欄。"""
        results = [
            {"status": "query_error", "risk": "UNKNOWN", "tenant": "t-x",
             "metric": "m", "old_value": "70", "new_value": "50",
             "query": 'q{tenant="t-x"}',
             "message": "Prometheus query failed: parse error: unexpected | in a | b"},
        ]
        md = bt.generate_markdown(bt.generate_report(results, "7d"))
        row = next(line for line in md.splitlines() if line.startswith("| UNKNOWN |"))
        assert row.replace("\\|", "").count("|") == 8
        assert "unexpected \\| in a \\| b" in row


# ── backtest_change（mock query_range）────────────────────────────


def _map_with(monkeypatch, **entries):
    """真的 observed-map 加上這些假條目（#3017：map 沒收錄的 key 不回測，
    所以用假 key 的測試要先給它一個 alert）。"""
    keys = {**bt._observed_map(), **entries}
    monkeypatch.setattr(bt, "_observed_map", lambda: keys)


def _upper(series):
    """一個 `series > threshold` 的 per-tenant 條目。"""
    return {"scope": "tenant", "direction": ">", "observed_series": series}


@pytest.fixture
def cpu_mapped(monkeypatch):
    """`cpu` 不是出廠 key；給它一個 `tenant:cpu:max > threshold` 的 alert。"""
    _map_with(monkeypatch, cpu=_upper("tenant:cpu:max"))


@pytest.mark.usefixtures("cpu_mapped")
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


@pytest.mark.usefixtures("cpu_mapped")
class TestBacktestChangeNaN:
    """#2963：全 NaN 不得報成 analyzed／LOW；分母仍含 NaN（owner 裁定的方案 P）。

    #3002 後只有一個查詢，全 NaN 沒有「下一個 pattern」可換，維持 no_data。"""

    def _run(self, monkeypatch, vals, old, new):
        values = [(i, v) for i, v in enumerate(vals)]
        monkeypatch.setattr(bt, "query_range",
                            lambda *a, **kw: [{"values": values}])
        change = {"tenant": "t-x", "metric": "cpu", "old_value": old, "new_value": new}
        return bt.backtest_change("http://prom", change, 86400)

    @pytest.mark.parametrize("old,new", [
        ("70", "65"),          # 一般變更（main 上是 analyzed / LOW）
        ("disable", "70"),     # 新啟用（main 上是 analyzed / LOW）
        ("70", "disable"),     # 停用（main 上是 analyzed / MEDIUM）
    ])
    def test_all_nan_is_no_data(self, monkeypatch, old, new):
        result = self._run(monkeypatch, ["NaN"] * 50, old, new)
        assert result["status"] == "no_data"
        assert result["risk"] == "UNKNOWN"
        assert result["message"] == "Only NaN samples in lookback window"
        assert "data_points" not in result

    def test_truly_empty_keeps_not_found_message(self, monkeypatch):
        monkeypatch.setattr(bt, "query_range", lambda *a, **kw: [])
        change = {"tenant": "t-x", "metric": "cpu", "old_value": "70", "new_value": "50"}
        result = bt.backtest_change("http://prom", change, 86400)
        assert result["message"] == "No historical data found in Prometheus"

    def test_nan_counts_in_denominator_for_new_enable(self, monkeypatch):
        """900 NaN + 100 會觸發：分母 1000 → 10% → MEDIUM（不是 HIGH）。

        釘住 owner 的語意選擇：pct = 佔整段回看時間的比例；NaN 那段告警不會響。
        """
        result = self._run(monkeypatch, ["NaN"] * 900 + ["95"] * 100,
                           "disable", "70")
        assert result["status"] == "analyzed"
        assert result["risk"] == "MEDIUM"
        assert result["data_points"] == 1000
        assert result["new_breach_count"] == 100

    def test_only_hot_points_new_enable_is_high(self, monkeypatch):
        result = self._run(monkeypatch, ["95"] * 100, "disable", "70")
        assert result["risk"] == "HIGH"
        assert result["data_points"] == 100

    @pytest.mark.parametrize("inf", ["+Inf", "-Inf"])
    def test_inf_is_not_missing_data(self, monkeypatch, inf):
        """±Inf 不是 NaN：全 ±Inf 仍是 analyzed，不得走 no_data。"""
        result = self._run(monkeypatch, [inf] * 10, "70", "65")
        assert result["status"] == "analyzed"
        assert result["data_points"] == 10

    def test_inf_mixed_with_nan_counts(self, monkeypatch):
        """NaN 混 +Inf 的新啟用：+Inf 算觸發、也留在分母。"""
        result = self._run(monkeypatch, ["NaN"] * 9 + ["+Inf"], "disable", "70")
        assert result["status"] == "analyzed"
        assert result["data_points"] == 10
        assert result["new_breach_count"] == 1
        assert result["risk"] == "MEDIUM"

    def test_unparseable_values_are_not_nan(self, monkeypatch):
        """讀不懂的值不算 NaN：不得報成「Only NaN」（交給 count_threshold_breaches 跳過）。"""
        result = self._run(monkeypatch, ["oops", "n/a"], "70", "65")
        assert result.get("message") != "Only NaN samples in lookback window"
        assert result["status"] == "analyzed"

    def test_all_finite_unchanged(self, monkeypatch):
        result = self._run(monkeypatch, ["60", "80", "90"], "85", "70")
        assert result["status"] == "analyzed"
        assert result["data_points"] == 3
        assert (result["old_breach_count"], result["new_breach_count"]) == (1, 2)
        assert result["risk"] == "HIGH"

    def test_nan_mixed_normal_change(self, monkeypatch):
        result = self._run(monkeypatch, ["NaN", "80", "NaN", "90"], "85", "70")
        assert result["status"] == "analyzed"
        assert result["data_points"] == 4
        assert (result["old_breach_count"], result["new_breach_count"]) == (1, 2)


# ── query_range（mock http_get_json）──────────────────────────────
# W1: query_range 的 fetch core 收斂進 _lib_prometheus.query_prometheus_range，
# 故 HTTP seam 改 patch _lib_prometheus.http_get_json（工具模組層的
# http_get_json 已不在此路徑上）。


class TestQueryRange:
    """query_range() Prometheus range query。"""

    def test_success(self, monkeypatch):
        """成功查詢回傳結果。"""
        def mock_get(url, timeout=30):
            return {
                "status": "success",
                "data": {"result": [{"values": [[1, "42"]]}]},
            }, None
        monkeypatch.setattr("_lib_prometheus.http_get_json", mock_get)
        result = bt.query_range("http://prom", "up", 3600)
        assert len(result) == 1

    def test_http_error_raises(self, monkeypatch):
        """#3059：HTTP 錯誤丟 PrometheusQueryError、帶錯誤原文，不再回空清單
        （以前回 []，被報成 no_data、rc 0）。"""
        def mock_get(url, timeout=30):
            return None, "connection refused"
        monkeypatch.setattr("_lib_prometheus.http_get_json", mock_get)
        with pytest.raises(bt.PrometheusQueryError, match="^connection refused$"):
            bt.query_range("http://prom", "up", 3600)

    def test_api_error_raises(self, monkeypatch):
        """#3059：API 錯誤狀態（含沒有 error 欄位）丟 PrometheusQueryError。"""
        def mock_get(url, timeout=30):
            return {"status": "error"}, None
        monkeypatch.setattr("_lib_prometheus.http_get_json", mock_get)
        with pytest.raises(bt.PrometheusQueryError, match="Unknown Prometheus error"):
            bt.query_range("http://prom", "bad{", 3600)

    def test_empty_result_is_still_empty(self, monkeypatch):
        """control：查詢成功但沒有 series 仍是 []（no_data 那條路）。"""
        def mock_get(url, timeout=30):
            return {"status": "success", "data": {"result": []}}, None
        monkeypatch.setattr("_lib_prometheus.http_get_json", mock_get)
        assert bt.query_range("http://prom", "up", 3600) == []


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

    def test_no_data_wording_is_neutral(self, capsys):
        """#2963：no_data 也涵蓋全 NaN，摘要行不得宣稱 metric 不存在。"""
        results = [
            {"status": "no_data", "risk": "UNKNOWN", "tenant": "a",
             "metric": "m", "old_value": "70", "new_value": "50",
             "message": "Only NaN samples in lookback window"},
        ]
        bt.print_text_report(bt.generate_report(results, "7d"))
        out = capsys.readouterr().out
        assert "No data: 1 (no usable data in Prometheus)" in out
        assert "metric not found" not in out


# ── extract_changes_from_git_diff（mock subprocess）───────────────


class TestExtractChangesFromGitDiff:
    """extract_changes_from_git_diff() git diff 解析。"""

    def test_normal_diff(self, monkeypatch):
        """正常 diff 解析出閾值變更。"""
        # ⚠️ BYTES since #1634: every path-carrying git call in the tool runs
        # without `text=True`, because a conf.d name is not required to be
        # valid UTF-8 and strict decoding raised inside `subprocess.run`,
        # outside the tool's own `except`. A fake that still answers `str`
        # describes an interface the tool no longer has.
        diff_output = (
            b"+++ b/conf.d/db-a.yaml\n"
            b"-  mysql_connections: 70\n"
            b"+  mysql_connections: 50\n"
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
            b"+++ b/conf.d/db-a.yaml\n"
            b"-  _silent_mode: normal\n"
            b"+  _silent_mode: warning\n"
        )
        def mock_run(*args, **kwargs):
            return type("R", (), {"returncode": 0, "stdout": diff_output})()
        monkeypatch.setattr(subprocess, "run", mock_run)
        changes = bt.extract_changes_from_git_diff()
        assert len(changes) == 0

    def test_git_failure_returns_none(self, monkeypatch):
        """git 命令失敗回傳 None（#1556 item 4：不再與「空 diff」同值）。

        ⛔ 原名 `test_git_failure_returns_empty`、斷言 `== []`——把「git 跑不了」
        和「PR 沒改東西」釘成同一個值，正是 main() 靜默 rc 0 的根源。
        """
        def mock_run(*args, **kwargs):
            return type("R", (), {"returncode": 1, "stdout": ""})()
        monkeypatch.setattr(subprocess, "run", mock_run)
        assert bt.extract_changes_from_git_diff() is None

    def test_git_timeout_returns_none(self, monkeypatch):
        """git 命令逾時回傳 None。"""
        def mock_run(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="git", timeout=15)
        monkeypatch.setattr(subprocess, "run", mock_run)
        assert bt.extract_changes_from_git_diff() is None

    def test_git_bytes_none_vs_empty(self, monkeypatch):
        """直接分辨兩個值：_git_bytes → None ⇒ None；→ b"" ⇒ []（git 正常、diff 空）。"""
        monkeypatch.setattr(bt, "_git_bytes", lambda *a, **k: None)
        assert bt.extract_changes_from_git_diff() is None
        monkeypatch.setattr(bt, "_git_bytes", lambda *a, **k: b"")
        assert bt.extract_changes_from_git_diff() == []

    def test_new_key_only(self, monkeypatch):
        """只有新增 key（無對應 old）。"""
        diff_output = (
            b"+++ b/conf.d/db-a.yaml\n"
            b"+  mysql_connections: 50\n"
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


# ── --json envelope 形狀（#1112）─────────────────────────────────


class TestJsonEnvelope:
    """兩條「沒跑回測」的終端路徑，其 `--json` envelope 的**形狀**。

    subprocess gate（tests/shared/test_json_stdout_contract.py）只斷言
    json.loads(stdout) 成功——status 值被改一個字、reason 鍵被拿掉，它照樣
    全綠。這裡逐鍵釘住 envelope，並用 key-set 比對 happy path 的
    generate_report()，讓「新增欄位只加在 happy path、忘了加進 empty_report」
    這種 drift 直接紅。
    """

    def _empty_report_keys(self, lookback):
        """happy-path generate_report() 的 key set（zero results）。"""
        return set(bt.generate_report([], lookback))

    def test_skip_if_unavailable_json_envelope(self, monkeypatch, capsys, cli_argv):
        """Prometheus 不可達 + --skip-if-unavailable + --json → skipped envelope、exit 0。"""
        monkeypatch.setattr(bt, "prometheus_available", lambda url, timeout=5: False)
        cli_argv("backtest_threshold",
                 "--tenant", "tenant-one", "--metric", "max_connections",
                 "--old-value", "100", "--new-value", "150",
                 "--prometheus", "http://prom:9090",
                 "--lookback", "3d",
                 "--skip-if-unavailable", "--json")

        with pytest.raises(SystemExit) as exc_info:
            bt.main()
        assert exc_info.value.code == 0          # graceful skip 仍是 EXIT_OK

        captured = capsys.readouterr()
        doc = json.loads(captured.out)           # 全文 parse ⇒ stdout 只有 JSON

        assert doc["status"] == "skipped"
        assert doc["reason"] == "prometheus_unavailable"
        assert doc["lookback"] == "3d"           # 回音 caller 給的窗口
        # 設計上歸零的計數確實是 0 / []
        assert doc["total_changes"] == 0
        assert doc["analyzed"] == 0
        assert doc["no_data"] == 0
        assert doc["risk_summary"] == {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
        assert doc["changes"] == []
        # 與 happy path 同 schema（consumer 讀 .risk_summary.HIGH 兩條路都通）
        assert set(doc) == self._empty_report_keys("3d") | {"status", "reason"}

        assert "Prometheus unavailable" in captured.err
        assert "Prometheus" not in captured.out

    def test_no_changes_json_envelope(self, monkeypatch, tmp_path, capsys, cli_argv, da_guard_env):
        """Prometheus 可達但零變更 + --json → no_changes envelope、exit 0。"""
        monkeypatch.setattr(bt, "prometheus_available", lambda url, timeout=5: True)
        conf_d = tmp_path / "conf.d"
        conf_d.mkdir()
        (conf_d / "tenant-one.yaml").write_text(
            "tenants:\n  tenant-one:\n    mysql_connections: 100\n", encoding="utf-8")

        # 同一個目錄同時當 current 與 baseline ⇒ 必然零變更
        cli_argv("backtest_threshold",
                 "--config-dir", str(conf_d), "--baseline", str(conf_d),
                 "--prometheus", "http://prom:9090", "--json")

        with pytest.raises(SystemExit) as exc_info:
            bt.main()
        assert exc_info.value.code == 0

        captured = capsys.readouterr()
        doc = json.loads(captured.out)

        assert doc["status"] == "no_changes"
        assert doc["reason"] == "no_threshold_changes_detected"
        assert doc["total_changes"] == 0
        assert doc["analyzed"] == 0
        assert doc["no_data"] == 0
        assert doc["risk_summary"] == {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
        assert doc["changes"] == []
        assert set(doc) == self._empty_report_keys(doc["lookback"]) | {"status", "reason"}

        assert "No threshold changes found." in captured.err
        assert "No threshold changes" not in captured.out


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
        with open(os.path.join(d, name), "w", encoding="utf-8") as f:
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

    def test_keep_flat_removal_of_real_threshold_kept(self, monkeypatch):
        """純量閾值被移除（HEAD~1 有該 top-level key）→ 保留（disable transition，#657）。"""
        monkeypatch.setattr(bt, "_flat_keys_at_head1",
                            lambda t: {"mysql_connections", "mysql_threads_running"})
        changes = [{"tenant": "db-b", "metric": "mysql_connections",
                    "old_value": "70", "new_value": None}]
        assert bt.keep_flat_threshold_changes(changes, {}) == changes

    def test_keep_flat_removal_of_recipe_field_dropped(self, monkeypatch):
        """recipe 內層欄位被移除（HEAD~1 top-level 沒有它）→ 丟棄。"""
        monkeypatch.setattr(bt, "_flat_keys_at_head1", lambda t: {"mysql_connections"})
        changes = [{"tenant": "db-b", "metric": "threshold",
                    "old_value": "150:warning", "new_value": None}]
        assert bt.keep_flat_threshold_changes(changes, {}) == []

    def test_keep_flat_removal_head1_unavailable_dropped(self, monkeypatch):
        """HEAD~1 不可得（空集合）→ 移除型保守丟棄，不臆測。"""
        monkeypatch.setattr(bt, "_flat_keys_at_head1", lambda t: set())
        changes = [{"tenant": "gone", "metric": "mysql_connections",
                    "old_value": "70", "new_value": None}]
        assert bt.keep_flat_threshold_changes(changes, {}) == []

    def test_changed_conf_files_reduces_repo_root_to_cwd_relative(self, monkeypatch):
        """git diff 的 repo-root 路徑被縮成 conf.d/<basename>（CI 在子目錄跑也載得到）。"""
        # ⚠️ NUL-delimited BYTES since #1634: this call now passes `-z`, which
        # is git's own contract for "never quote anything" and is what carries
        # a non-ASCII (or newline-bearing) name through intact. A newline-
        # delimited `str` fake describes the pre-#1634 interface.
        out = (b"components/threshold-exporter/config/conf.d/db-b.yaml\0"
               b"components/threshold-exporter/config/conf.d/_defaults.yaml\0")
        monkeypatch.setattr(subprocess, "run",
                            lambda *a, **k: type("R", (), {"returncode": 0, "stdout": out})())
        assert bt.changed_conf_files() == ["conf.d/db-b.yaml", "conf.d/_defaults.yaml"]

    def test_flat_keys_at_head1_parses_tenants_wrapper(self, monkeypatch):
        """從 HEAD~1 的 tenants-wrapper 抽 top-level 純量閾值 key（排除 _ 與 recipe 區塊）。"""
        # ⚠️ BYTES since #1634. This one was GREEN with a `str` fake even after
        # the tool switched, because `yaml.safe_load` accepts both — i.e. the
        # fake described an interface the tool no longer had and nothing said
        # so. Pinned to bytes so the fake cannot drift away from the call again.
        yaml_text = (b"tenants:\n  db-b:\n    mysql_connections: '100'\n    mysql_threads_running: '60'\n"
                     b"    _silent_mode: warning\n    _custom_alerts:\n      - recipe: threshold\n")
        monkeypatch.setattr(subprocess, "run",
                            lambda *a, **k: type("R", (), {"returncode": 0, "stdout": yaml_text})())
        assert bt._flat_keys_at_head1("db-b") == {"mysql_connections", "mysql_threads_running"}

    def test_flat_keys_at_head1_duplicate_key_is_like_bad_syntax(self, monkeypatch):
        """#2231：HEAD~1 的檔有重複鍵時，exporter 整份丟掉，比照語法錯回空集合。"""
        yaml_text = (b"tenants:\n  db-b:\n    mysql_connections: '5'\n"
                     b"    mysql_connections: '6'\n    mysql_threads_running: '1'\n")
        monkeypatch.setattr(subprocess, "run",
                            lambda *a, **k: type("R", (), {"returncode": 0, "stdout": yaml_text})())
        assert bt._flat_keys_at_head1("db-b") == set()

    def test_notice_empty_when_no_recipes(self):
        """無 recipe 租戶 → notice 為空字串。"""
        assert bt.custom_alert_notice([]) == ""

    def test_notice_and_markdown_point_at_657(self):
        """notice / markdown 都列出租戶並指向 #657。"""
        note = bt.custom_alert_notice(["db-a", "db-b"])
        assert "db-a" in note and "db-b" in note and "#657" in note
        md = bt.custom_alert_markdown(["db-b"])
        assert "#657" in md and "`db-b`" in md


# ---------------------------------------------------------------------------
# --prometheus env-var fallback (add_prometheus_arg / README §6.1)
# ---------------------------------------------------------------------------
class TestPrometheusEnvFallback:
    """`--prometheus` resolves $PROMETHEUS_URL at the argparse layer.

    backtest 先前硬編 http://localhost:9090（standalone 直跑讀不到 env）。
    add_prometheus_arg 現在於 argparse 層 resolve（env → 否則 localhost）。
    """

    def _capture_prom_url(self, monkeypatch, cli_argv, argv):
        captured = {}

        def fake_available(prom_url, timeout=5):
            captured["url"] = prom_url
            return False  # unreachable → --skip-if-unavailable graceful exit(0)

        monkeypatch.setattr(bt, "prometheus_available", fake_available)
        # #3059: a change source is required before the Prometheus check, so
        # the bare argv these rows used would exit 2 without asking.
        cli_argv(*argv, "--tenant", "t", "--metric", "mysql_connections", "--new-value", "1")
        with pytest.raises(SystemExit) as ei:
            bt.main()
        assert ei.value.code == 0
        return captured["url"]

    def test_uses_env_when_flag_absent(self, monkeypatch, cli_argv):
        """--prometheus omitted + $PROMETHEUS_URL set → uses the env value."""
        monkeypatch.setenv("PROMETHEUS_URL", "http://test:1234")
        url = self._capture_prom_url(
            monkeypatch, cli_argv, ["backtest_threshold", "--skip-if-unavailable"])
        assert url == "http://test:1234"

    def test_falls_back_to_localhost_when_env_unset(self, monkeypatch, cli_argv):
        """--prometheus omitted + env unset → byte-identical old default."""
        monkeypatch.delenv("PROMETHEUS_URL", raising=False)
        url = self._capture_prom_url(
            monkeypatch, cli_argv, ["backtest_threshold", "--skip-if-unavailable"])
        assert url == "http://localhost:9090"

    def test_explicit_flag_overrides_env(self, monkeypatch, cli_argv):
        """Explicit --prometheus always wins over $PROMETHEUS_URL."""
        monkeypatch.setenv("PROMETHEUS_URL", "http://test:1234")
        url = self._capture_prom_url(
            monkeypatch, cli_argv,
            ["backtest_threshold", "--skip-if-unavailable", "--prometheus", "http://cli:9099"])
        assert url == "http://cli:9099"


def _bytes_git(monkeypatch, stdout: bytes, returncode: int = 0):
    """Replace `subprocess.run` with a fake answering BYTES.

    Matches the existing fakes in this file: every path-carrying git call in
    the tool runs without `text=True` (#1634), so a `str` fake would describe
    an interface the tool no longer has.
    """
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: type("R", (), {"returncode": returncode, "stdout": stdout})())


class TestConfdSpellingParityWithTheExporter:
    """#1603 — every conf.d reader in this file accepts BOTH spellings.

    The exporter's scanner suffixes on `.yaml` AND `.yml`
    (`components/threshold-exporter/app/pkg/config/hierarchy.go`), so a
    `conf.d/db-b.yml` is a live tenant it is serving. Five sites here used
    to pass `suffixes=(".yaml",)` and could not see it: a real threshold
    change on that tenant produced an empty backtest, rc=0, and no message
    naming what had been skipped.

    Every test below is three-sided on purpose:

      * LOWER   — the `.yml` carrier must be seen. This is what #1603
                  buys; each of these assertions was RED before the
                  argument was dropped (measured, both directions).
      * UPPER   — a name the exporter does NOT accept (`.json`, `.yang`,
                  `.txt`) must still be ignored, so "widened" cannot
                  quietly become "accepts anything".
      * CONTROL — the `.yaml` twin with a byte-identical body must behave
                  exactly as before. If a CONTROL row stops firing, the
                  fixture is broken and the other two rows prove nothing.

    ⚠️ SCOPE — the SPELLING axis only. Case folding (`DB-A.YAML`, #1588)
    and the `is_file` axis (a DIRECTORY named `notes.yaml/`, #1607) are
    pinned elsewhere and were measured unchanged by the commit that added
    this class; do not read a green run here as covering them.
    """

    # ── site 1 of 5: extract_changes_from_dirs ────────────────────────
    # #2119: the trees are read by da-guard, so the spelling rule is the
    # exporter's own; these rows pin that it reaches this tool's answer.
    def test_dir_comparison_sees_a_yml_carrier(self, tmp_path, da_guard_env):
        """LOWER + CONTROL: `.yml` and `.yaml` twins both yield a change."""
        cur, base = tmp_path / "cur", tmp_path / "base"
        for where, value in ((cur, 50), (base, 70)):
            where.mkdir()
            (where / "_defaults.yaml").write_text("defaults:\n  mysql_connections: 80\n",
                                                  encoding="utf-8")
            for name, tenant in (("db-a.yaml", "db-a"), ("db-b.yml", "db-b")):
                (where / name).write_text(
                    f"tenants:\n  {tenant}:\n    mysql_connections: {value}\n", encoding="utf-8")

        tenants = {c["tenant"] for c in bt.extract_changes_from_dirs(str(cur), str(base))}
        assert tenants == {"db-a", "db-b"}, (
            "db-a is the CONTROL and must always be here; db-b is the #1603 "
            "row and was missing before the widening"
        )

    def test_dir_comparison_still_ignores_non_config_extensions(self, tmp_path, da_guard_env):
        """UPPER: widened to CONFIG_SUFFIXES, not to 'any file'."""
        cur, base = tmp_path / "cur", tmp_path / "base"
        for where, value in ((cur, 50), (base, 70)):
            where.mkdir()
            (where / "_defaults.yaml").write_text("defaults:\n  mysql_connections: 80\n",
                                                  encoding="utf-8")
            # CONTROL row proves the fixture can produce a change at all.
            for name, tenant in (("db-a.yaml", "db-a"), ("db-c.json", "db-c"),
                                 ("db-d.yang", "db-d"), ("db-e.txt", "db-e")):
                (where / name).write_text(
                    f"tenants:\n  {tenant}:\n    mysql_connections: {value}\n", encoding="utf-8")

        tenants = {c["tenant"] for c in bt.extract_changes_from_dirs(str(cur), str(base))}
        assert tenants == {"db-a"}

    # ── site 2 of 5: extract_changes_from_git_diff ────────────────────
    def test_git_diff_parser_sees_a_yml_carrier(self, monkeypatch):
        """LOWER + CONTROL, and UPPER in the same diff."""
        _bytes_git(monkeypatch, (
            b"+++ b/conf.d/db-a.yaml\n"
            b"-  mysql_connections: 70\n"
            b"+  mysql_connections: 50\n"
            b"+++ b/conf.d/db-b.yml\n"
            b"-  mysql_connections: 70\n"
            b"+  mysql_connections: 50\n"
            b"+++ b/conf.d/db-c.json\n"
            b"-  mysql_connections: 70\n"
            b"+  mysql_connections: 50\n"
        ))
        tenants = {c["tenant"] for c in bt.extract_changes_from_git_diff()}
        assert tenants == {"db-a", "db-b"}

    # ── site 3 of 5: changed_conf_files ───────────────────────────────
    def test_changed_conf_files_keeps_yml_and_drops_other_extensions(self, monkeypatch):
        """LOWER + UPPER + CONTROL on one NUL-delimited listing."""
        _bytes_git(monkeypatch, (
            b"components/threshold-exporter/config/conf.d/db-a.yaml\0"
            b"components/threshold-exporter/config/conf.d/db-b.yml\0"
            b"components/threshold-exporter/config/conf.d/notes.txt\0"
        ))
        assert bt.changed_conf_files() == ["conf.d/db-a.yaml", "conf.d/db-b.yml"]

    # ── site 4 of 5: _carrier_at_head1 ────────────────────────────────
    def test_carrier_at_head1_resolves_a_yml_carrier(self, monkeypatch):
        """LOWER: the tenant id came off a `.yml`, so resolution must too."""
        _bytes_git(monkeypatch, b"conf.d/db-a.yaml\0conf.d/db-b.yml\0")
        assert bt._carrier_at_head1("db-b") == "./conf.d/db-b.yml"
        # CONTROL — the spelling this function always resolved.
        assert bt._carrier_at_head1("db-a") == "./conf.d/db-a.yaml"

    def test_carrier_at_head1_ignores_non_config_extensions(self, monkeypatch):
        """UPPER: a `.json` stem must not answer for a tenant."""
        _bytes_git(monkeypatch, b"conf.d/db-c.json\0")
        assert bt._carrier_at_head1("db-c") is None

    # ── site 5 of 5: the recipe notice in main() ──────────────────────
    def test_recipe_notice_counts_a_tenant_declared_in_a_yml_carrier(
        self, monkeypatch, tmp_path, capsys, cli_argv
    ):
        """LOWER + UPPER through `main()`'s own `_confd_entries` filter.

        The operator-facing NOTE is built from `find_custom_alert_tenants`,
        which reads whatever this filter admits — so a recipe-only tenant
        living in a `.yml` used to be absent from a notice whose whole job
        is "there are recipe tenants this flat backtest does not cover".
        """
        monkeypatch.setattr(bt, "prometheus_available", lambda url, timeout=5: True)
        conf_d = tmp_path / "conf.d"
        conf_d.mkdir()
        recipe = ("tenants:\n  {t}:\n    _custom_alerts:\n"
                  "      - recipe: threshold\n")
        (conf_d / "acme.yaml").write_text(recipe.format(t="acme"), encoding="utf-8")
        (conf_d / "beta.yml").write_text(recipe.format(t="beta"), encoding="utf-8")
        # UPPER — same body, an extension the exporter never loads.
        (conf_d / "ghost.txt").write_text(recipe.format(t="ghost"), encoding="utf-8")

        cli_argv("backtest_threshold",
                 "--config-dir", str(conf_d), "--baseline", str(conf_d),
                 "--prometheus", "http://prom:9090")
        with pytest.raises(SystemExit):
            bt.main()

        err = capsys.readouterr().err
        assert "acme" in err, "CONTROL: the .yaml recipe tenant was always named"
        assert "beta" in err, "#1603: the .yml recipe tenant is a real tenant"
        assert "ghost" not in err


# ── 回放 alert 實際比較的那條 series（#3017 / #3002）──────────────


def _boom(*a, **kw):
    raise AssertionError("must not query Prometheus")


class TestAlertComparison:
    """#3017：越界方向照 alert 的比較方向（observed-map 的 `direction`），不再
    一律 `>`。#3002：只查一次——alert 比較的那條 recording rule（observed-map
    的 `observed_series`）加 `{tenant=...}`，不再猜三種 pattern。map 沒收錄或
    無法回放的 key 判 not_backtested、不查 Prometheus。

    下界 key、查詢名稱都用 repo 內真的 map（SoT），不 mock：map 改了這裡會紅。
    """

    def _run(self, monkeypatch, metric, vals, old, new, tenant="t-x"):
        asked = []

        def query_range(url, q, lookback, step=bt.DEFAULT_STEP):
            asked.append(q)
            return [{"values": [(i, str(v)) for i, v in enumerate(vals)]}] if vals else []
        monkeypatch.setattr(bt, "query_range", query_range)
        change = {"tenant": tenant, "metric": metric, "old_value": old, "new_value": new}
        return bt.backtest_change("http://prom", change, 86400), asked

    # ── 方向（#3017）──
    def test_lower_bound_key_counts_points_below(self, monkeypatch):
        """票上的重現：`< 95` 一點都不觸發，`< 99.9` 全部 100 點觸發。"""
        r, _ = self._run(monkeypatch, "db2_bufferpool_hit_ratio", [99.5] * 100, "95", "99.9")
        assert r["status"] == "analyzed"
        assert (r["old_breach_count"], r["new_breach_count"]) == (0, 100)
        assert r["risk"] == "HIGH"
        assert "START firing" in r["message"]

    def test_lower_bound_key_loosening_is_fewer(self, monkeypatch):
        """下界門檻調低是放寬：觸發點變少，方向不得算反。"""
        r, _ = self._run(monkeypatch, "kafka_broker_count", [2] * 6 + [3] * 4, "3", "2")
        assert (r["old_breach_count"], r["new_breach_count"]) == (6, 0)
        assert r["message"].startswith("6 fewer firing points")

    def test_lower_bound_disable_counts_old_below(self, monkeypatch):
        r, _ = self._run(monkeypatch, "rabbitmq_consumers", [0, 0, 1, 5], "1", "disable")
        assert (r["risk"], r["old_breach_count"]) == ("MEDIUM", 2)

    def test_lower_bound_enable_counts_new_below(self, monkeypatch):
        r, _ = self._run(monkeypatch, "kafka_active_controllers", [0, 1, 1, 1], "disable", "1")
        assert r["new_breach_count"] == 1

    def test_upper_bound_key_unchanged(self, monkeypatch):
        r, _ = self._run(monkeypatch, "mysql_connections", [60, 80, 90], "70", "85")
        assert (r["old_breach_count"], r["new_breach_count"]) == (2, 1)

    # ── 查詢（#3002）──
    @pytest.mark.parametrize("metric,query", [
        # 告警比的是 threads_connected，不是 `mysql_connections` 這個 key 名。
        ("mysql_connections", 'tenant:mysql_threads_connected:max{tenant="t-x"}'),
        # 比的是使用率 ratio，不是連線數。
        ("pg_connections", 'tenant:pg_connection_usage:ratio{tenant="t-x"}'),
        # 退役拼法（`--git-diff` 讀到的是檔裡寫的）先過 alias 表。
        ("mysql_cpu", 'tenant:mysql_threads_running:avg1m{tenant="t-x"}'),
        ("mysql_cpu_critical", 'tenant:mysql_threads_running:avg1m{tenant="t-x"}'),
    ])
    def test_one_query_of_the_series_the_alert_compares(self, monkeypatch, metric, query):
        r, asked = self._run(monkeypatch, metric, [1, 2], "70", "85")
        assert asked == [query]
        assert r["query"] == query

    @pytest.mark.parametrize("vals,message", [
        ([], "No historical data found in Prometheus"),
        (["NaN"] * 3, "Only NaN samples in lookback window"),
    ])
    def test_no_data_names_the_one_query(self, monkeypatch, vals, message):
        """只問一次：查不到或全 NaN 都是 no_data，`query` 指出問的是什麼。"""
        r, asked = self._run(monkeypatch, "mysql_connections", vals, "70", "85")
        assert (r["status"], r["message"]) == ("no_data", message)
        assert asked == [r["query"]] == ['tenant:mysql_threads_connected:max{tenant="t-x"}']

    @pytest.mark.parametrize("old,new", [("70", "85"), ("70", "disable"), ("disable", "70")])
    def test_every_analyzed_result_carries_its_query(self, monkeypatch, old, new):
        r, asked = self._run(monkeypatch, "mysql_connections", [60, 80, 90], old, new)
        assert r["status"] == "analyzed" and r["query"] == asked[0]

    def test_dimensioned_key_resolves_to_its_base(self):
        assert bt.alert_comparison({"tenant": "t", "metric": 'db2_bufferpool_hit_ratio{db="a"}',
                                    "old_value": "90", "new_value": "95"}) \
            == ("tenant:db2_bufferpool_hit_ratio:min", "below", None)

    # ── 不回測（#3017）──
    def test_unmapped_and_deferred_keys_have_distinct_reasons(self, monkeypatch):
        monkeypatch.setattr(bt, "query_range", _boom)
        reasons = {}
        for key in ("es_index_doc_count", "container_cpu"):
            change = {"tenant": "t-x", "metric": key, "old_value": "70", "new_value": "50"}
            r = bt.backtest_change("http://prom", change, 86400)
            assert (r["status"], r["risk"]) == ("not_backtested", "UNKNOWN")
            assert "query" not in r
            assert r["message"] == "Not backtested: " + r["backtest"].removeprefix("skipped: ")
            assert bt.not_backtestable(change) == r["backtest"].removeprefix("skipped: ")
            reasons[key] = bt.not_backtestable(change)
        assert reasons["es_index_doc_count"].startswith("no shipped alert compares this key")
        assert "KNOWN_DEFERRED" in reasons["container_cpu"]
        assert "no shipped alert" not in reasons["container_cpu"]

    @pytest.mark.parametrize("entry,why", [
        ({"scope": "tenant", "needs_review": True, "reason": "composite"}, "needs review"),
        ({"scope": "tenant", "direction": None, "observed_series": "tenant:k:max"}, "direction"),
        ({"scope": "tenant", "direction": ">"}, "composite"),
    ])
    def test_unusable_entry_is_not_backtested(self, monkeypatch, entry, why):
        _map_with(monkeypatch, k=entry)
        monkeypatch.setattr(bt, "query_range", _boom)
        r = bt.backtest_change("http://prom", {"tenant": "t", "metric": "k", "old_value": "1",
                                               "new_value": "2"}, 86400)
        assert r["status"] == "not_backtested" and why in r["backtest"]

    # ── served-values 的 row（#2750 × #3017）──
    def _served(self, metric, *rows):
        import _lib_tenant_values as tv
        series = [tv.Series("user_threshold", {"severity": sev}, key, twin, {}, {})
                  for key, sev, twin in rows]
        return {"tenant": "t-x", "metric": metric, "old_value": "1", "new_value": "2",
                "series": series}

    def test_served_critical_row_uses_the_critical_alert(self, monkeypatch):
        _map_with(monkeypatch, k=_upper("tenant:k:max"),
                  k_critical={"scope": "tenant", "direction": "<",
                              "observed_series": "tenant:k_crit:min"})
        assert bt.alert_comparison(self._served("k", ("k", "critical", False))) == (
            "tenant:k_crit:min", "below", None)
        assert bt.alert_comparison(self._served("k", ("k", "warning", False))) == (
            "tenant:k:max", "above", None)

    def test_rows_compared_differently_are_not_backtested(self, monkeypatch):
        _map_with(monkeypatch, k=_upper("tenant:k:max"), k_critical=_upper("tenant:k_crit:max"))
        monkeypatch.setattr(bt, "query_range", _boom)
        change = self._served("k", ("k", "warning", False), ("k", "critical", False))
        r = bt.backtest_change("http://prom", change, 86400)
        assert r["status"] == "not_backtested"
        assert r["backtest"].startswith("skipped: rows compared differently")

    def test_rows_compared_alike_are_backtested_once(self, monkeypatch):
        """mysql_connections 與其 _critical 比同一條 series、同方向：照常回測。"""
        change = self._served("mysql_connections", ("mysql_connections", "warning", False),
                              ("mysql_connections", "critical", False))
        assert bt.alert_comparison(change) == ("tenant:mysql_threads_connected:max", "above", None)

    def test_mongodb_critical_row_has_no_alert(self, monkeypatch):
        """mongodb 的門檻 recording rule 只正規化 severity="warning"：覆寫成
        `N:critical` 的時段沒有 alert 在比，不借 base 的。"""
        monkeypatch.setattr(bt, "query_range", _boom)
        change = self._served("mongodb_connections_current",
                              ("mongodb_connections_current", "critical", False))
        reason = bt.not_backtestable(change)
        assert reason.startswith("mongodb_connections_current_critical: no shipped alert")
        assert bt.backtest_change("http://prom", change, 86400)["status"] == "not_backtested"

    def test_legacy_twin_row_is_not_read(self):
        change = self._served("mysql_threads_running",
                              ("mysql_threads_running", "warning", False),
                              ("mysql_cpu", "warning", True))
        assert bt._alert_keys(change) == (["mysql_threads_running"], None)


class TestAlertComparisonCli:
    """#3017 對結束碼的影響（owner 同意）：未收錄 key 不再判 HIGH；規則本身
    （rc 1 若且唯若有 HIGH）不變。"""

    def _main(self, monkeypatch, capsys, cli_argv, metric, old, new, vals, *extra):
        monkeypatch.setattr(bt, "prometheus_available", lambda url, timeout=5: True)
        monkeypatch.setattr(bt, "query_range", lambda *a, **kw: [
            {"values": [(i, str(v)) for i, v in enumerate(vals)]}])
        cli_argv("backtest_threshold", "--tenant", "t-x", "--metric", metric,
                 "--old-value", old, "--new-value", new,
                 "--prometheus", "http://prom:9090", *extra)
        with pytest.raises(SystemExit) as ei:
            bt.main()
        return ei.value.code, capsys.readouterr()

    def test_unmapped_key_is_listed_rc0(self, monkeypatch, capsys, cli_argv):
        """以前會判 HIGH（rc 1）的未收錄 key：列為 Not backtested、rc 0。"""
        rc, out = self._main(monkeypatch, capsys, cli_argv, "es_index_doc_count",
                             "100", "50", [90] * 10, "--json")
        doc = json.loads(out.out)
        assert rc == 0 and doc["risk_summary"]["HIGH"] == 0
        assert doc["changes"][0]["status"] == "not_backtested"

    @pytest.mark.parametrize("old,new,risk,rc", [
        ("95", "99.9", "HIGH", 1),   # 下界收緊到 99.5 之上：開始觸發
        ("95", "96", "LOW", 0),      # 兩邊都不觸發
    ])
    def test_rc_is_1_iff_high(self, monkeypatch, capsys, cli_argv, old, new, risk, rc):
        got, out = self._main(monkeypatch, capsys, cli_argv, "db2_bufferpool_hit_ratio",
                              old, new, [99.5] * 100, "--json")
        doc = json.loads(out.out)
        assert (doc["changes"][0]["risk"], got) == (risk, rc)
        assert doc["changes"][0]["query"] == 'tenant:db2_bufferpool_hit_ratio:min{tenant="t-x"}'

    def test_text_and_markdown_show_the_query(self, monkeypatch, capsys, cli_argv, tmp_path):
        md = tmp_path / "out.md"
        _, out = self._main(monkeypatch, capsys, cli_argv, "mysql_connections", "70", "85",
                            [60, 80, 90], "--markdown-output", str(md))
        q = 'tenant:mysql_threads_connected:max{tenant="t-x"}'
        assert f"query: {q}" in out.out
        assert f"| `{q}` |" in md.read_text(encoding="utf-8")


class TestObservedMapMissing:
    """#3017：observed-map 不能用 → rc 2（環境錯），在 Prometheus 檢查之前，
    `--skip-if-unavailable` 也蓋不掉；`--json` 照樣拿到 caller_error envelope。"""

    def _argv(self, cli_argv, *extra):
        cli_argv("backtest_threshold", "--tenant", "t-x", "--metric", "mysql_connections",
                 "--old-value", "70", "--new-value", "85",
                 "--prometheus", "http://prom:9090", "--skip-if-unavailable", *extra)

    def _missing(self, monkeypatch, tmp_path):
        monkeypatch.setattr(bt, "_OBSERVED_MAP", None)
        monkeypatch.setattr(bt.observed_map_lib, "DEFAULT_MAP_PATH", str(tmp_path / "nope.yaml"))
        monkeypatch.setattr(bt, "prometheus_available", lambda url, timeout=5: False)

    def test_missing_map_exits_2(self, monkeypatch, tmp_path, capsys, cli_argv):
        self._missing(monkeypatch, tmp_path)
        self._argv(cli_argv)
        with pytest.raises(SystemExit) as ei:
            bt.main()
        assert ei.value.code == 2
        err = capsys.readouterr().err
        assert "ERROR:" in err and "observed-map not found" in err and "nope.yaml" in err

    def test_missing_map_json_envelope(self, monkeypatch, tmp_path, capsys, cli_argv):
        self._missing(monkeypatch, tmp_path)
        self._argv(cli_argv, "--json")
        with pytest.raises(SystemExit) as ei:
            bt.main()
        assert ei.value.code == 2
        doc = json.loads(capsys.readouterr().out)
        assert (doc["status"], doc["reason"]) == ("caller_error", "observed_map_unavailable")

    def test_a_later_load_failure_is_still_rc_2(self, monkeypatch, capsys, cli_argv):
        """後援：main 的 decorator 讓任何一處 ObservedMapError 都是 rc 2，不是 traceback。"""
        calls = []

        def flaky():
            calls.append(1)
            if len(calls) > 1:
                raise bt.observed_map_lib.ObservedMapError("gone", "missing")
            return {}
        monkeypatch.setattr(bt, "_observed_map", flaky)
        monkeypatch.setattr(bt, "prometheus_available", lambda url, timeout=5: True)
        self._argv(cli_argv)
        with pytest.raises(SystemExit) as ei:
            bt.main()
        assert ei.value.code == 2 and "ERROR: gone" in capsys.readouterr().err


# ── 寫在檔裡的值：`N:critical`、讀不懂的值（#3017 review）────────────


class TestWrittenValues:
    """`--git-diff`／`--tenant` 給的是檔裡寫的值。exporter 對 `70:critical` 的
    讀法（`pkg/config/resolve.go`：切第一個 `:`，前段是數字、後段是 severity）
    在這裡照抄；以前 `float("95:critical")` 失敗被當成 0 個越界點，判出假的
    HIGH（rc 1）或假的 LOW。"""

    _VALS = [60, 80, 90, 100]

    def _main(self, monkeypatch, capsys, cli_argv, argv):
        asked = []

        def query_range(url, q, lookback, step=bt.DEFAULT_STEP):
            asked.append(q)
            return [{"values": [(i, str(v)) for i, v in enumerate(self._VALS)]}]
        monkeypatch.setattr(bt, "prometheus_available", lambda url, timeout=5: True)
        monkeypatch.setattr(bt, "query_range", query_range)
        cli_argv("backtest_threshold", *argv, "--prometheus", "http://prom:9090", "--json")
        with pytest.raises(SystemExit) as ei:
            bt.main()
        return ei.value.code, json.loads(capsys.readouterr().out)["changes"], asked

    def _tenant(self, monkeypatch, capsys, cli_argv, metric, old, new):
        return self._main(monkeypatch, capsys, cli_argv,
                          ["--tenant", "t-x", "--metric", metric,
                           "--old-value", old, "--new-value", new])

    @pytest.mark.parametrize("key,raw,want", [
        ("mysql_connections", "70", (70.0, "warning")),
        ("mysql_connections", " 70 : critical ", (70.0, "critical")),
        ("mysql_connections", "70:info", (70.0, "info")),
        ("mysql_connections_critical", "95", (95.0, "critical")),
        ("mysql_connections", "disable", None),
        ("mysql_connections", None, None),
        ("mysql_connections", "NaN", (float("nan"), "warning")),
        # Spellings Go's ParseFloat accepts too (measured).
        ("mysql_connections", "Inf", (float("inf"), "warning")),
        ("mysql_connections", "-inf:critical", (float("-inf"), "critical")),
        ("mysql_connections_critical", "+Inf", (float("inf"), "critical")),
        ("mysql_connections", "infinity", (float("inf"), "warning")),
        ("mysql_connections", "1e-400", (0.0, "warning")),
    ])
    def test_parse_threshold_value(self, key, raw, want):
        got = bt.parse_threshold_value(key, raw)
        if want is None or want[0] == want[0]:
            assert got == want
        else:
            assert got[0] != got[0] and got[1] == want[1]

    @pytest.mark.parametrize("key,raw", [
        ("mysql_connections", "7O"), ("mysql_connections", "1_000"),
        ("mysql_connections", "١٢"), ("mysql_connections_critical", "95:critical"),
        ("mysql_connections", "+nan"), ("mysql_connections", "-nan:critical"),
        ("mysql_connections", "1e400"), ("mysql_connections_critical", "-1e400"),
        ("mysql_connections", "0x1p4"),
    ], ids=["typo", "underscore", "non-ascii-digit", "suffix-on-critical-key",
            "signed-nan", "signed-nan-critical", "overflow", "negative-overflow", "hex-float"])
    def test_values_this_backtest_cannot_read_are_refused(self, key, raw):
        """Go 也拒絕：typo、非 ASCII 數字、帶號 NaN、溢位（ErrRange）。Go 接受但
        這裡保守拒絕：`1_000`、`0x1p4`（實測 go run strconv.ParseFloat）。"""
        with pytest.raises(ValueError):
            bt.parse_threshold_value(key, raw)

    def test_severity_change_is_not_backtested(self, monkeypatch, capsys, cli_argv):
        """審查的重現：70 → 95:critical 以前判 HIGH「3 fewer」、rc 1。"""
        rc, rows, asked = self._tenant(monkeypatch, capsys, cli_argv,
                                       "mysql_connections", "70", "95:critical")
        assert (rc, asked) == (0, [])
        assert rows[0]["status"] == "not_backtested"
        assert rows[0]["backtest"] == ("skipped: severity changes (warning -> critical): "
                                       "the two values are compared by different alerts")

    def test_both_critical_compare_the_numbers_on_the_critical_alert(
            self, monkeypatch, capsys, cli_argv):
        rc, rows, asked = self._tenant(monkeypatch, capsys, cli_argv,
                                       "mysql_connections", "70:critical", "95:critical")
        assert asked == ['tenant:mysql_threads_connected:max{tenant="t-x"}']
        assert (rows[0]["old_breach_count"], rows[0]["new_breach_count"]) == (3, 1)

    def test_enable_as_critical(self, monkeypatch, capsys, cli_argv):
        """停用 → 啟用：只有一邊有值，severity 取那一邊的。"""
        _, rows, asked = self._tenant(monkeypatch, capsys, cli_argv,
                                      "mysql_connections", "disable", "95:critical")
        assert rows[0]["status"] == "analyzed" and rows[0]["new_breach_count"] == 1
        assert asked == ['tenant:mysql_threads_connected:max{tenant="t-x"}']

    def test_critical_value_with_no_critical_alert(self, monkeypatch, capsys, cli_argv):
        """mongodb 的門檻只正規化 warning：`N:critical` 沒有 alert 在比。"""
        _, rows, asked = self._tenant(monkeypatch, capsys, cli_argv,
                                      "mongodb_connections_current", "70:critical", "80:critical")
        assert asked == []
        assert rows[0]["backtest"].startswith(
            "skipped: mongodb_connections_current_critical: no shipped alert compares")

    @pytest.mark.parametrize("old,new,why", [
        ("70", "7O", "value '7O': this backtest cannot read it the way the exporter does"),
        ("70:info", "80:info", "severity 'info' is selected by no threshold rule"),
    ])
    def test_unreadable_values_are_not_backtested(self, monkeypatch, capsys, cli_argv,
                                                   old, new, why):
        rc, rows, asked = self._tenant(monkeypatch, capsys, cli_argv, "mysql_connections", old, new)
        assert (rc, asked) == (0, [])
        assert rows[0]["backtest"].startswith("skipped: " + why)

    def test_git_diff_reads_the_critical_suffix(self, monkeypatch, capsys, cli_argv):
        """`--git-diff` 這條路一樣：diff 裡的 `"95:critical"` 不是 0 個越界點。"""
        diff = (b"+++ b/conf.d/t-x.yaml\n"
                b"-    mysql_connections: '70:critical'\n"
                b"+    mysql_connections: '95:critical'\n"
                b"-    pg_connections: '70'\n"
                b"+    pg_connections: '95:critical'\n")
        monkeypatch.setattr(bt, "_git_bytes",
                            lambda args, quotepath_off=False: diff if "--unified=0" in args else b"")
        rc, rows, asked = self._main(monkeypatch, capsys, cli_argv, ["--git-diff"])
        got = {r["metric"]: r for r in rows}
        assert (got["mysql_connections"]["old_breach_count"],
                got["mysql_connections"]["new_breach_count"]) == (3, 1)
        assert got["pg_connections"]["status"] == "not_backtested"
        assert "severity changes" in got["pg_connections"]["backtest"]
        assert asked == ['tenant:mysql_threads_connected:max{tenant="t-x"}']


class TestMalformedObservedMap:
    def test_malformed_map_exits_2(self, monkeypatch, tmp_path, capsys, cli_argv):
        """#3017 review：`keys: {mysql_connections: 5}` 以前是 AttributeError
        traceback（rc 1，讀起來像 HIGH）；現在是 rc 2、一行指名 key。"""
        bad = tmp_path / "metric_observed_map.yaml"
        bad.write_text("keys:\n  mysql_connections: 5\n", encoding="utf-8")
        monkeypatch.setattr(bt, "_OBSERVED_MAP", None)
        monkeypatch.setattr(bt.observed_map_lib, "DEFAULT_MAP_PATH", str(bad))
        monkeypatch.setattr(bt, "prometheus_available", lambda url, timeout=5: True)
        cli_argv("backtest_threshold", "--tenant", "t-x", "--metric", "mysql_connections",
                 "--old-value", "70", "--new-value", "85", "--prometheus", "http://prom:9090")
        with pytest.raises(SystemExit) as ei:
            bt.main()
        assert ei.value.code == 2
        err = capsys.readouterr().err
        assert "ERROR:" in err and "'mysql_connections'" in err and "not a mapping" in err


# ── 查詢錯誤與空結果分開、rc 2 路徑的 envelope（#3059）──────────────


class _FakeProm:
    """本機假 Prometheus：buildinfo 200；`query_range` 依 `{tenant="..."}`
    回應——`errors` 裡的租戶回該 HTTP 狀態碼，其他租戶回 `values`。

    請求沒有 body（GET），仍照 Content-Length 讀完；回應後先
    `shutdown(SHUT_WR)` 再關線。"""

    def __init__(self, errors, values):
        import http.server
        import socket
        import threading
        import urllib.parse

        fake = self
        fake.asked = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                n = int(self.headers.get("Content-Length") or 0)
                if n:
                    self.rfile.read(n)
                url = urllib.parse.urlparse(self.path)
                if url.path == "/api/v1/status/buildinfo":
                    return self._send(200, {"status": "success", "data": {"version": "2.51.0"}})
                if url.path == "/api/v1/query_range":
                    q = urllib.parse.parse_qs(url.query)["query"][0]
                    fake.asked.append(q)
                    for tenant, code in errors.items():
                        if f'tenant="{tenant}"' in q:
                            return self._send(code, {"status": "error", "errorType": "bad_data",
                                                     "error": "boom"})
                    return self._send(200, {"status": "success", "data": {
                        "resultType": "matrix",
                        "result": [{"metric": {}, "values": [[i, str(v)]
                                                             for i, v in enumerate(values)]}]}})
                return self._send(404, {})

            def _send(self, code, obj):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def finish(self):
                try:
                    super().finish()
                    self.request.shutdown(socket.SHUT_WR)
                except OSError:
                    pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=10)


@pytest.fixture
def fake_prom():
    started = []

    def start(errors=None, values=(90,) * 10):
        p = _FakeProm(errors or {}, list(values))
        started.append(p)
        return p
    yield start
    for p in started:
        p.close()


def _diff_for(*tenants):
    """一段 `--git-diff`：每個租戶的 mysql_connections 100 → 50。"""
    out = b""
    for t in tenants:
        out += (f"+++ b/conf.d/{t}.yaml\n"
                "-    mysql_connections: '100'\n"
                "+    mysql_connections: '50'\n").encode()
    return out


def _run_main(cli_argv, capsys, *argv):
    cli_argv("backtest_threshold", *argv)
    with pytest.raises(SystemExit) as ei:
        bt.main()
    return ei.value.code, capsys.readouterr()


class TestQueryError:
    """#3059：Prometheus 連得上、但查詢失敗（400／503）的那一筆是
    `query_error`，不是「沒資料」；有任何一筆就 rc 2，優先於 HIGH 的 rc 1，
    `--skip-if-unavailable` 也不把它轉成 0（Prometheus 不是 unavailable）。"""

    @pytest.mark.parametrize("code", [400, 503])
    def test_single_query_error_is_rc2_partial(self, fake_prom, cli_argv, capsys, code):
        p = fake_prom(errors={"t-bad": code})
        rc, out = _run_main(cli_argv, capsys, "--tenant", "t-bad", "--metric", "mysql_connections",
                            "--old-value", "70", "--new-value", "50",
                            "--prometheus", p.url, "--json")
        doc = json.loads(out.out)
        assert rc == 2
        assert (doc["status"], doc["reason"]) == ("partial", "query_error")
        assert doc["query_error"] == 1 and doc["no_data"] == 0
        row = doc["changes"][0]
        assert (row["status"], row["risk"]) == ("query_error", "UNKNOWN")
        assert f"HTTP Error {code}" in row["message"]
        assert row["query"] == 'tenant:mysql_threads_connected:max{tenant="t-bad"}'
        assert "not measured" in out.err

    def _git(self, monkeypatch, *tenants):
        diff = _diff_for(*tenants)
        monkeypatch.setattr(bt, "_git_bytes",
                            lambda args, quotepath_off=False: diff if "--unified=0" in args else b"")

    def test_other_changes_still_run_and_query_error_beats_high(
            self, fake_prom, monkeypatch, cli_argv, capsys):
        p = fake_prom(errors={"t-bad": 400})
        self._git(monkeypatch, "t-ok", "t-bad")
        rc, out = _run_main(cli_argv, capsys, "--git-diff", "--prometheus", p.url, "--json")
        doc = json.loads(out.out)
        got = {r["tenant"]: r for r in doc["changes"]}
        assert got["t-ok"]["status"] == "analyzed" and got["t-ok"]["risk"] == "HIGH"
        assert got["t-bad"]["status"] == "query_error"
        assert doc["risk_summary"]["HIGH"] == 1
        assert rc == 2, "a change not measured outranks HIGH (owner ruling)"
        assert len(p.asked) == 2

    def test_high_alone_is_still_rc1(self, fake_prom, monkeypatch, cli_argv, capsys):
        """control：沒有 query_error 時 HIGH 照舊 rc 1，頂層不帶 status。"""
        p = fake_prom()
        self._git(monkeypatch, "t-ok")
        rc, out = _run_main(cli_argv, capsys, "--git-diff", "--prometheus", p.url, "--json")
        doc = json.loads(out.out)
        assert rc == 1 and "status" not in doc and doc["query_error"] == 0

    def test_skip_if_unavailable_does_not_hide_query_error(
            self, fake_prom, monkeypatch, cli_argv, capsys):
        p = fake_prom(errors={"t-bad": 503})
        self._git(monkeypatch, "t-bad")
        rc, out = _run_main(cli_argv, capsys, "--git-diff", "--prometheus", p.url,
                            "--skip-if-unavailable", "--json")
        assert rc == 2
        assert json.loads(out.out)["status"] == "partial"

    def test_text_and_markdown_name_the_unmeasured(
            self, fake_prom, monkeypatch, cli_argv, capsys, tmp_path):
        p = fake_prom(errors={"t-bad": 400})
        self._git(monkeypatch, "t-ok", "t-bad")
        md = tmp_path / "out.md"
        rc, out = _run_main(cli_argv, capsys, "--git-diff", "--prometheus", p.url,
                            "--markdown-output", str(md))
        assert rc == 2
        assert "Query error: 1" in out.out
        assert "1 change(s) not measured" in md.read_text(encoding="utf-8")

    def test_message_is_escaped_and_truncated(self, monkeypatch, cpu_mapped):
        def failing(*a, **kw):
            raise bt.PrometheusQueryError("line1\nFAKE [PASS]\x1b[2J" + "x" * 1000)
        monkeypatch.setattr(bt, "query_range", failing)
        r = bt.backtest_change("http://prom", {"tenant": "t", "metric": "cpu",
                                               "old_value": "70", "new_value": "50"}, 86400)
        assert r["status"] == "query_error"
        assert "\n" not in r["message"] and "\x1b" not in r["message"]
        assert len(r["message"]) < 400


class TestCallerErrorEnvelopes:
    """#3059：三條 rc 2 路徑在 `--json` 下 stdout 恰一份 caller_error
    envelope；參數錯在 Prometheus 檢查之前，`--skip-if-unavailable` 吃不掉。"""

    def _envelope(self, out, reason):
        doc = json.loads(out.out)
        assert (doc["status"], doc["reason"]) == ("caller_error", reason)
        assert set(doc) == set(bt.generate_report([], doc["lookback"])) | {"status", "reason"}

    def test_prometheus_unreachable_envelope(self, monkeypatch, cli_argv, capsys):
        monkeypatch.setattr(bt, "prometheus_available", lambda url, timeout=5: False)
        rc, out = _run_main(cli_argv, capsys, "--tenant", "t", "--metric", "mysql_connections",
                            "--old-value", "70", "--new-value", "50",
                            "--prometheus", "http://prom:9090", "--json")
        assert rc == 2
        self._envelope(out, "prometheus_unavailable")
        assert "not reachable" in out.err

    @pytest.mark.parametrize("skip", [False, True], ids=["plain", "skip-if-unavailable"])
    def test_tenant_args_missing(self, monkeypatch, cli_argv, capsys, skip):
        monkeypatch.setattr(bt, "prometheus_available", lambda url, timeout=5: False)
        rc, out = _run_main(cli_argv, capsys, "--tenant", "t", "--prometheus", "http://prom:9090",
                            "--json", *(["--skip-if-unavailable"] if skip else []))
        assert rc == 2
        self._envelope(out, "tenant_args_missing")

    @pytest.mark.parametrize("tenant", ['a"b', "a\\b", "Upper", "a" * 64, "a-", "a\x1b[2Jb"])
    @pytest.mark.parametrize("skip", [False, True], ids=["plain", "skip-if-unavailable"])
    def test_invalid_tenant_id(self, monkeypatch, cli_argv, capsys, skip, tenant):
        """#3066: `--tenant` is the tenant id rule's (`is_valid_tenant_id`), not
        only "encodes as UTF-8": `a"b` used to go into `{tenant="a"b"}`, an
        invalid query, with rc 0. Refused before Prometheus is asked: with
        `--skip-if-unavailable` Prometheus is unreachable, which used to be rc 0."""
        monkeypatch.setattr(bt, "prometheus_available", lambda url, timeout=5: not skip)
        monkeypatch.setattr(bt, "query_range", _boom)
        rc, out = _run_main(cli_argv, capsys, "--tenant", tenant, "--metric", "mysql_connections",
                            "--old-value", "70", "--new-value", "50",
                            "--prometheus", "http://prom:9090", "--json",
                            *(["--skip-if-unavailable"] if skip else []))
        assert rc == 2
        self._envelope(out, "invalid_tenant_id")
        assert "is not a valid tenant id" in out.err
        assert "\x1b" not in out.err  # the id is printed through safe_label

    def test_a_valid_tenant_id_is_queried(self, monkeypatch, cli_argv, capsys):
        """CONTROL for the above: a DNS-1123 label reaches the query as written."""
        asked = []
        monkeypatch.setattr(bt, "prometheus_available", lambda url, timeout=5: True)
        monkeypatch.setattr(bt, "query_range", lambda url, q, *a, **kw: asked.append(q) or [])
        rc, _ = _run_main(cli_argv, capsys, "--tenant", "ok-1", "--metric", "mysql_connections",
                          "--old-value", "70", "--new-value", "50",
                          "--prometheus", "http://prom:9090", "--json")
        assert rc == 0
        assert asked and all('{tenant="ok-1"}' in q for q in asked)

    @pytest.mark.parametrize("skip", [False, True], ids=["plain", "skip-if-unavailable"])
    def test_no_change_source(self, monkeypatch, cli_argv, capsys, skip):
        monkeypatch.setattr(bt, "prometheus_available", lambda url, timeout=5: False)
        rc, out = _run_main(cli_argv, capsys, "--prometheus", "http://prom:9090",
                            "--json", *(["--skip-if-unavailable"] if skip else []))
        assert rc == 2
        self._envelope(out, "no_change_source")
