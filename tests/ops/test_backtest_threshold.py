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

import errno
import json
import os
import shutil
import subprocess
import sys
import tempfile
from unittest.mock import patch

import pytest

from _platform_fs import require_file_name, symlink_or_skip  # noqa: E402

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

    def test_unchanged_trees_are_still_no_changes(self, monkeypatch, capsys, cli_argv, tmp_path,
                                                   da_guard_env):
        """control：git 正常、兩棵 conf.d 送出的值一樣 → "No threshold changes found." exit 0。"""
        files = {"_defaults.yaml": _DEFAULTS, "db-a.yaml": "tenants:\n  db-a: {}\n"}
        monkeypatch.chdir(_git_tree_repo(tmp_path, files, files))
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
                            "old_value": "70", "new_value": "50", "keys": ["mysql_connections"]}]

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
            {"tenant": "db-a", "metric": "mysql_connections", "old_value": "80", "new_value": "90",
             "keys": ["mysql_connections"]}]

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


# ── materialize_head1_confd：git 答不出來 ⇒ None ─────────────────────


class TestMaterializeHead1Confd:
    """`--git-diff` 的 HEAD~1 那棵樹（#3111）。git 答不出來（失敗、逾時）是
    Head1Unavailable，與「HEAD~1 沒有 conf.d/」（""）分開：前者 rc 2，後者是一棵空樹。"""

    def test_git_failure_raises(self, monkeypatch, tmp_path):
        def mock_run(*args, **kwargs):
            return type("R", (), {"returncode": 1, "stdout": b""})()
        monkeypatch.setattr(subprocess, "run", mock_run)
        with pytest.raises(bt.Head1Unavailable, match="git could not run"):
            bt.materialize_head1_confd(str(tmp_path))

    def test_git_timeout_raises(self, monkeypatch, tmp_path):
        def mock_run(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="git", timeout=15)
        monkeypatch.setattr(subprocess, "run", mock_run)
        with pytest.raises(bt.Head1Unavailable):
            bt.materialize_head1_confd(str(tmp_path))

    def test_no_confd_at_head1_is_empty_not_none(self, tmp_path, monkeypatch):
        cfg = _git_tree_repo(tmp_path, None, {"x.yaml": "tenants: {}\n"})
        monkeypatch.chdir(cfg)
        out = tmp_path / "out"
        out.mkdir()
        assert bt.materialize_head1_confd(str(out)) == ""

    def test_writes_every_file_and_symlink_as_recorded(self, tmp_path, monkeypatch):
        """巢狀、各種檔名（換行、引號、反斜線、空白、非 UTF-8）都照 bytes 寫出；
        symlink 照它記錄的 target 重建。"""
        names = {b"a.yaml": b"A", b"sub/deep/b.yml": b"B", b'q"uote.yaml': b"Q",
                 b"new\nline.yaml": b"N", b"back\\slash.yaml": b"S", b"sp ace.yaml": b"P",
                 b"legacy-\xff.yaml": b"X", "İSTANBUL.YAML".encode("utf-8"): b"I"}
        for rel in names:
            require_file_name(rel.rsplit(b"/", 1)[-1])
        cfg = _git_tree_repo(tmp_path, names, names)
        root = os.fsencode(str(cfg / "conf.d"))
        symlink_or_skip(b"a.yaml", os.path.join(root, b"link.yaml"))
        _git_run(cfg.parent, "add", "-A")
        _git_run(cfg.parent, "commit", "-qm", "link")
        _git_run(cfg.parent, "commit", "-qm", "head", "--allow-empty")
        monkeypatch.chdir(cfg)
        out = tmp_path / "out"
        out.mkdir()
        got = os.fsencode(bt.materialize_head1_confd(str(out)))
        for rel, body in names.items():
            with open(os.path.join(got, rel), "rb") as f:
                assert f.read() == body, rel
        assert os.readlink(os.path.join(got, b"link.yaml")) == b"a.yaml"


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
    """find_custom_alert_tenants — 把 flat 工具對 _custom_alerts 的隱性
    無-recipe-path 補成顯性 fail-loud。conf.d 用
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

    # ── site 2 of 5: --git-diff (#3111: the two trees are read by da-guard) ──
    def test_git_diff_sees_a_yml_carrier(self, tmp_path, monkeypatch, cli_argv, capsys,
                                         fake_prom, da_guard_env):
        """LOWER + CONTROL, and UPPER in the same trees."""
        def tree(value):
            out = {"_defaults.yaml": "defaults:\n  mysql_connections: 80\n"}
            for name, tenant in (("db-a.yaml", "db-a"), ("db-b.yml", "db-b"),
                                 ("db-c.json", "db-c")):
                out[name] = f"tenants:\n  {tenant}:\n    mysql_connections: {value}\n"
            return out
        monkeypatch.chdir(_git_tree_repo(tmp_path, tree(70), tree(50)))
        p = fake_prom()
        _rc, out = _run_main(cli_argv, capsys, "--git-diff", "--prometheus", p.url, "--json")
        assert {c["tenant"] for c in json.loads(out.out)["changes"]} == {"db-a", "db-b"}

    # ── site 3 of 5: changed_conf_files ───────────────────────────────
    def test_changed_conf_files_keeps_yml_and_drops_other_extensions(self, monkeypatch):
        """LOWER + UPPER + CONTROL on one NUL-delimited listing."""
        _bytes_git(monkeypatch, (
            b"components/threshold-exporter/config/conf.d/db-a.yaml\0"
            b"components/threshold-exporter/config/conf.d/db-b.yml\0"
            b"components/threshold-exporter/config/conf.d/notes.txt\0"
        ))
        assert bt.changed_conf_files() == ["conf.d/db-a.yaml", "conf.d/db-b.yml"]

    # ── site 4 of 5: _carrier_at_head1 — gone with the diff parser (#3111) ──

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
        ("mysql_connections", "70:Critical", (70.0, "critical")),
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
        ("mysql_connections", "70:info"), ("mysql_connections", "70:"),
    ], ids=["typo", "underscore", "non-ascii-digit", "suffix-on-critical-key",
            "signed-nan", "signed-nan-critical", "overflow", "negative-overflow", "hex-float",
            "unselected-severity", "empty-severity"])
    def test_values_this_backtest_cannot_read_are_refused(self, key, raw):
        """Go 也拒絕：typo、非 ASCII 數字、帶號 NaN、溢位（ErrRange）、規則不選的
        severity（#3071：`70:info`、`70:` 整個值讀不懂）。Go 接受但這裡保守拒絕：
        `1_000`、`0x1p4`（實測 go run strconv.ParseFloat）。"""
        with pytest.raises(ValueError):
            bt.parse_threshold_value(key, raw)

    def test_a_severity_change_is_two_rows(self, monkeypatch, capsys, cli_argv):
        """#3111：70 → 95:critical 是兩條 /metrics row（同目錄比對）：warning 告警
        失去門檻（3 點不再響、MEDIUM），critical 告警開始比 95（1/4 點、HIGH，rc 1）。
        #3060 R1 把它當成一列照值回測（3 → 1 點）。"""
        rc, rows, asked = self._tenant(monkeypatch, capsys, cli_argv,
                                       "mysql_connections", "70", "95:critical")
        assert asked == ['tenant:mysql_threads_connected:max{tenant="t-x"}'] * 2
        assert [(r["metric"], r["old_value"], r["new_value"], r["risk"]) for r in rows] == [
            ("mysql_connections", "70", None, "MEDIUM"),
            ("mysql_connections_critical", None, "95", "HIGH")]
        assert all("old_severity" not in r for r in rows)
        assert rows[1]["keys"] == ["mysql_connections"]
        assert rc == 1

    def test_a_severity_no_rule_selects_is_unreadable(self, monkeypatch, capsys, cli_argv):
        """#3071：`70:high` 的 severity 沒有規則會選，exporter 把整個值當成讀不懂、改送
        平台預設值——那個值只看寫法看不出來，所以不回測（#3060 R1 曾把它判成靜默、
        MEDIUM、rc 0；exporter 已不再這樣送）。"""
        rc, rows, asked = self._tenant(monkeypatch, capsys, cli_argv,
                                       "mysql_connections", "70", "70:high")
        assert (rc, asked) == (0, [])
        assert rows[0]["backtest"] == (
            "skipped: value '70:high': this backtest cannot read it the way the exporter does")

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
        assert [(r["metric"], r["old_value"], r["new_value"]) for r in rows] == [
            ("mongodb_connections_current_critical", "70", "80")]
        assert rows[0]["backtest"].startswith("skipped: no shipped alert compares")

    @pytest.mark.parametrize("old,new,why", [
        ("70", "7O", "value '7O': this backtest cannot read it the way the exporter does"),
        ("70:info", "80:info", "value '70:info': this backtest cannot read it the way the exporter does"),
    ])
    def test_unreadable_values_are_not_backtested(self, monkeypatch, capsys, cli_argv,
                                                   old, new, why):
        rc, rows, asked = self._tenant(monkeypatch, capsys, cli_argv, "mysql_connections", old, new)
        assert (rc, asked) == (0, [])
        assert rows[0]["backtest"].startswith("skipped: " + why)

    def test_git_diff_reads_the_critical_suffix(self, monkeypatch, capsys, cli_argv, tmp_path,
                                                da_guard_env):
        """`--git-diff`：`"95:critical"` 不是 0 個越界點。#3111 起兩棵樹由 exporter
        讀，每條 /metrics row 一列：`70` → `95:critical` 是 warning 那條沒了、critical
        那條出現，各由自己的告警比。"""
        defaults = {"_defaults.yaml": "defaults:\n  mysql_connections: 80\n  pg_connections: 80\n"}
        before = "tenants:\n  t-x:\n    mysql_connections: '70:critical'\n    pg_connections: '70'\n"
        after = "tenants:\n  t-x:\n    mysql_connections: '95:critical'\n    pg_connections: '95:critical'\n"
        monkeypatch.chdir(_git_tree_repo(tmp_path, {**defaults, "t-x.yaml": before},
                                         {**defaults, "t-x.yaml": after}))
        rc, rows, asked = self._main(monkeypatch, capsys, cli_argv, ["--git-diff"])
        got = {(r["metric"], r["old_value"], r["new_value"]): r for r in rows}
        crit = got[("mysql_connections_critical", "70", "95")]
        assert (crit["old_breach_count"], crit["new_breach_count"]) == (3, 1)
        assert crit["query"] == 'tenant:mysql_threads_connected:max{tenant="t-x"}'
        assert ("pg_connections", "70", None) in got
        assert ("pg_connections_critical", None, "95") in got
        assert all("old_severity" not in r for r in rows)


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


def _repo_for(tmp_path, *tenants):
    """A `--git-diff` checkout (the dir to run from): each tenant's
    mysql_connections 100 → 50 between HEAD~1 and HEAD."""
    def tree(value):
        return {"_defaults.yaml": _DEFAULTS,
                **{f"{t}.yaml": f"tenants:\n  {t}:\n    mysql_connections: '{value}'\n"
                   for t in tenants}}
    return _git_tree_repo(tmp_path, tree(100), tree(50))


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
        monkeypatch.chdir(_repo_for(self._tmp, *tenants))

    @pytest.fixture(autouse=True)
    def _tmp_and_da_guard(self, tmp_path, da_guard_env):
        self._tmp = tmp_path

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


# ── 每一條結束路徑都寫出要求的檔、開工前先檢查輸出路徑（#3089）──────────


def _recipe_confd(root):
    d = root / "recipe-conf.d"
    d.mkdir()
    (d / "ta.yaml").write_text(
        "tenants:\n"
        "  ta:\n"
        "    mysql_connections: 100\n"
        "    _custom_alerts:\n"
        '      - {recipe: threshold, name: cpu_hot, metric: node_cpu, op: ">",\n'
        '         window: 5m, threshold: "80:warning"}\n',
        encoding="utf-8")
    return d


def _plain_confd(root):
    d = root / "plain-conf.d"
    d.mkdir()
    (d / "tenant-one.yaml").write_text(
        "tenants:\n  tenant-one:\n    mysql_connections: 100\n", encoding="utf-8")
    return d


_TENANT = ("--tenant", "t-x", "--metric", "mysql_connections",
           "--old-value", "100", "--new-value", "50")


def _path_skip(mp, tmp, prom):
    mp.setattr(bt, "prometheus_available", lambda url, timeout=5: False)
    return [*_TENANT, "--prometheus", "http://prom:9090", "--skip-if-unavailable"]


def _path_skip_with_recipe(mp, tmp, prom):
    mp.setattr(bt, "prometheus_available", lambda url, timeout=5: False)
    d = _recipe_confd(tmp)
    return ["--config-dir", str(d), "--baseline", str(d),
            "--prometheus", "http://prom:9090", "--skip-if-unavailable"]


def _path_no_changes(mp, tmp, prom):
    mp.setattr(bt, "prometheus_available", lambda url, timeout=5: True)
    d = _plain_confd(tmp)
    return ["--config-dir", str(d), "--baseline", str(d), "--prometheus", "http://prom:9090"]


def _path_prometheus_unavailable(mp, tmp, prom):
    mp.setattr(bt, "prometheus_available", lambda url, timeout=5: False)
    return [*_TENANT, "--prometheus", "http://prom:9090"]


def _path_observed_map(mp, tmp, prom):
    mp.setattr(bt, "_OBSERVED_MAP", None)
    mp.setattr(bt.observed_map_lib, "DEFAULT_MAP_PATH", str(tmp / "nope.yaml"))
    mp.setattr(bt, "prometheus_available", _boom)
    return [*_TENANT, "--prometheus", "http://prom:9090", "--skip-if-unavailable"]


def _path_git_diff(mp, tmp, prom):
    mp.setattr(bt, "_git_bytes", lambda *a, **k: None)
    mp.setattr(bt, "prometheus_available", _boom)
    return ["--git-diff", "--prometheus", "http://prom:9090"]


def _path_baseline_missing(mp, tmp, prom):
    mp.setattr(bt, "prometheus_available", _boom)
    return ["--config-dir", str(_plain_confd(tmp)), "--prometheus", "http://prom:9090"]


def _path_served_values(mp, tmp, prom):
    def missing(*a, **kw):
        raise bt.DaGuardNotFoundError("no da-guard")
    mp.setattr(bt, "extract_changes_from_dirs", missing)
    mp.setattr(bt, "prometheus_available", _boom)
    d = _plain_confd(tmp)
    return ["--config-dir", str(d), "--baseline", str(d), "--prometheus", "http://prom:9090"]


def _path_conf_unreadable(mp, tmp, prom):
    mp.setattr(bt, "prometheus_available", _boom)
    d = tmp / "bad-conf.d"
    d.mkdir()
    (d / "db.yaml").write_bytes(b"tenants:\n  db:\n    x: \xff\n")
    return ["--config-dir", str(d), "--baseline", str(d), "--prometheus", "http://prom:9090"]


def _path_query_error(mp, tmp, prom):
    p = prom(errors={"t-x": 400})
    return [*_TENANT, "--prometheus", p.url]


def _path_high(mp, tmp, prom):
    p = prom()
    return [*_TENANT, "--prometheus", p.url]


# (id, argv builder, rc, (status, reason) or None for a report with no status,
#  words the markdown must say)
_EVERY_PATH = [
    ("skip", _path_skip, 0, ("skipped", "prometheus_unavailable"),
     ("Backtest not run", "Prometheus")),
    ("skip-recipe", _path_skip_with_recipe, 0, ("skipped", "prometheus_unavailable"),
     ("Backtest not run", "Prometheus", "Custom-Alert Recipes", "`ta`")),
    ("no-changes", _path_no_changes, 0, ("no_changes", "no_threshold_changes_detected"),
     ("Backtest not run", "no threshold changes")),
    ("prometheus-unavailable", _path_prometheus_unavailable, 2,
     ("caller_error", "prometheus_unavailable"), ("Backtest not run", "prometheus_unavailable")),
    ("observed-map", _path_observed_map, 2, ("caller_error", "observed_map_unavailable"),
     ("Backtest not run", "observed_map_unavailable")),
    ("git-diff", _path_git_diff, 2, ("caller_error", "git_diff_unavailable"),
     ("Backtest not run", "git_diff_unavailable")),
    ("baseline-missing", _path_baseline_missing, 2, ("caller_error", "baseline_missing"),
     ("Backtest not run", "baseline_missing")),
    ("served-values", _path_served_values, 2, ("caller_error", "served_values_unavailable"),
     ("Backtest not run", "served_values_unavailable")),
    ("conf-unreadable", _path_conf_unreadable, 2, ("caller_error", "conf_file_unreadable"),
     ("Backtest not run", "conf_file_unreadable")),
    ("query-error", _path_query_error, 2, ("partial", "query_error"),
     ("Threshold Backtest Results", "not measured")),
    ("high", _path_high, 1, None, ("Threshold Backtest Results", "HIGH")),
]


@pytest.mark.usefixtures("da_guard_env")
class TestOutputsOnEveryPath:
    """#3089 (a)：preflight 之後每一條結束路徑都寫出 `-o` 與 `--markdown-output`。

    `-o` 是該路徑在 `--json` 時印的同一份 JSON；markdown 非空、寫出原因。
    rc 照舊（skip 0、no_changes 0、HIGH 1、其餘 2）。修前只有正常報告那條
    寫 `-o`，skip／no_changes 只在有 recipe 租戶時寫 markdown。"""

    @pytest.mark.parametrize("pid,build,rc,disc,words", _EVERY_PATH,
                             ids=[p[0] for p in _EVERY_PATH])
    def test_both_files_match_the_stdout_envelope(
            self, pid, build, rc, disc, words, monkeypatch, tmp_path, cli_argv, capsys,
            fake_prom):
        out_json, out_md = tmp_path / "out" / "r.json", tmp_path / "out" / "r.md"
        out_json.parent.mkdir()
        argv = build(monkeypatch, tmp_path, fake_prom)
        got_rc, out = _run_main(cli_argv, capsys, *argv, "--json",
                                "-o", str(out_json), "--markdown-output", str(out_md))
        assert got_rc == rc, out.err
        doc = json.loads(out.out)
        if disc is None:
            assert "status" not in doc
        else:
            assert (doc["status"], doc["reason"]) == disc
        assert out_json.is_file(), f"{pid}: -o was not written"
        assert json.loads(out_json.read_text(encoding="utf-8")) == doc
        assert out_md.is_file(), f"{pid}: --markdown-output was not written"
        md = out_md.read_text(encoding="utf-8")
        assert md.strip(), f"{pid}: the markdown is empty"
        for w in words:
            assert w in md, f"{pid}: the markdown does not say {w!r}:\n{md}"

    @pytest.mark.parametrize("pid,build,rc", [(p[0], p[1], p[2]) for p in _EVERY_PATH],
                             ids=[p[0] for p in _EVERY_PATH])
    def test_text_mode_writes_both_and_keeps_rc(
            self, pid, build, rc, monkeypatch, tmp_path, cli_argv, capsys, fake_prom):
        out_json, out_md = tmp_path / "r.json", tmp_path / "r.md"
        out_json.write_text("stale\n", encoding="utf-8")   # an existing file is fine
        argv = build(monkeypatch, tmp_path, fake_prom)
        got_rc, out = _run_main(cli_argv, capsys, *argv,
                                "-o", str(out_json), "--markdown-output", str(out_md))
        assert got_rc == rc, out.err
        assert "check the value given to" not in out.err   # no write error
        assert json.loads(out_json.read_text(encoding="utf-8"))["lookback"] == "7d"
        assert out_md.read_text(encoding="utf-8").strip()


def _unwritable(tmp, shape, name):
    if shape == "missing_parent":
        return tmp / "absent" / name
    if shape == "parent_is_file":
        (tmp / "blocker").write_text("file\n", encoding="utf-8")
        return tmp / "blocker" / name
    if shape == "dangling_symlink":   # #3089 review: a link to a missing directory
        link = tmp / name
        symlink_or_skip(tmp / "absent" / name, link)
        return link
    p = tmp / name          # target_is_dir
    p.mkdir()
    return p


@pytest.mark.usefixtures("da_guard_env")
class TestOutputPreflight:
    """#3089 (b)：參數檢查之後、git／served-values／observed-map／Prometheus
    之前，先確認 `-o` 與 `--markdown-output` 寫得進去。寫不進去 ⇒ 一行
    `ERROR: cannot write …`、rc 2、`--json` 印 `output_unwritable` envelope；
    什麼都還沒查，所以 stdout 不會先印一份正常報告。"""

    @pytest.mark.parametrize("flag,named", [("-o", "-o/--output"),
                                            ("--markdown-output", "--markdown-output")])
    @pytest.mark.parametrize("shape", ["missing_parent", "parent_is_file", "target_is_dir",
                                       "dangling_symlink"])
    @pytest.mark.parametrize("pid,build", [("skip", _path_skip), ("no-changes", _path_no_changes)])
    def test_skip_and_no_changes_are_rc2_output_unwritable(
            self, pid, build, shape, flag, named, monkeypatch, tmp_path, cli_argv, capsys,
            fake_prom):
        bad = _unwritable(tmp_path, shape, "r.out")
        argv = build(monkeypatch, tmp_path, fake_prom)
        rc, out = _run_main(cli_argv, capsys, *argv, "--json", flag, str(bad))
        assert rc == 2
        doc = json.loads(out.out)
        assert (doc["status"], doc["reason"]) == ("caller_error", "output_unwritable")
        assert set(doc) == set(bt.generate_report([], doc["lookback"])) | {"status", "reason"}
        lines = [ln for ln in out.err.splitlines() if ln.startswith("ERROR:")]
        assert len(lines) == 1, out.err
        assert lines[0].startswith(f"ERROR: cannot write {bad}: ")
        assert f"check the value given to {named}" in lines[0]
        assert "Traceback" not in out.err

    @pytest.mark.parametrize("flag", ["-o", "--markdown-output"])
    def test_report_path_stops_before_any_work(self, flag, monkeypatch, tmp_path, cli_argv,
                                               capsys):
        """git、observed-map、Prometheus 一個都沒碰就停（每個都換成 `_boom`）。"""
        for name in ("_git_bytes", "_observed_map", "prometheus_available", "query_range",
                     "extract_changes_from_dirs", "load_conf_files"):
            monkeypatch.setattr(bt, name, _boom)
        bad = _unwritable(tmp_path, "missing_parent", "r.out")
        rc, out = _run_main(cli_argv, capsys, "--git-diff", "--prometheus", "http://prom:9090",
                            "--json", flag, str(bad))
        assert rc == 2
        assert json.loads(out.out)["reason"] == "output_unwritable"
        assert not bad.parent.exists()

    def test_the_other_output_is_not_written_either(self, monkeypatch, tmp_path, cli_argv,
                                                    capsys):
        monkeypatch.setattr(bt, "prometheus_available", _boom)
        good = tmp_path / "r.md"
        rc, out = _run_main(cli_argv, capsys, *_TENANT, "--prometheus", "http://prom:9090",
                            "-o", str(_unwritable(tmp_path, "target_is_dir", "r.json")),
                            "--markdown-output", str(good))
        assert rc == 2 and out.out == ""
        assert not good.exists()

    @pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() == 0,
                        reason="as root the permission bits decide nothing")
    @pytest.mark.parametrize("where", ["file", "parent"])
    def test_permission_bits_are_read(self, where, tmp_path):
        """EACCES: a read-only existing file, or a read-only parent directory."""
        d = tmp_path / "d"
        d.mkdir()
        target = d / "r.json"
        if where == "file":
            target.write_text("old", encoding="utf-8")
            target.chmod(0o444)
        else:
            d.chmod(0o500)
        try:
            exc = bt._unwritable_output(str(target), "-o/--output")
            assert exc is not None and exc.errno == errno.EACCES
        finally:
            d.chmod(0o700)

    def test_a_regular_file_someone_else_owns_is_unwritable(self, tmp_path, monkeypatch):
        """The secure writer's chmod 0600 fails on a file another user owns
        (EPERM) after writing it: judged unwritable up front (#3105)."""
        target = tmp_path / "r.json"
        target.write_text("old", encoding="utf-8")
        target.chmod(0o666)
        owner = target.stat().st_uid
        monkeypatch.setattr(bt.os, "geteuid", lambda: owner + 1)
        exc = bt._unwritable_output(str(target), "-o/--output")
        assert exc is not None and exc.errno == errno.EPERM

    def test_a_device_is_not_judged_by_its_owner(self, tmp_path, monkeypatch):
        """`-o /dev/null`: the writer does not chmod a device (#3105), so who
        owns it does not matter."""
        monkeypatch.setattr(bt.os, "geteuid", lambda: 12345)
        assert bt._unwritable_output("/dev/null", "-o/--output") is None

    def test_argument_errors_still_come_first(self, monkeypatch, tmp_path, cli_argv, capsys):
        """參數錯先於 preflight：reason 仍是 tenant_args_missing。"""
        bad = _unwritable(tmp_path, "missing_parent", "r.json")
        rc, out = _run_main(cli_argv, capsys, "--tenant", "t", "--prometheus", "http://prom:9090",
                            "--json", "-o", str(bad))
        assert rc == 2
        assert json.loads(out.out)["reason"] == "tenant_args_missing"


# ── 以送出的 series 為單位分列（#3111）──────────────────────────────


def _rows(changes):
    """The fields a reader compares, without the Series objects."""
    return [{k: v for k, v in c.items() if k != "series"} for c in changes]


_CRIT_DEFAULTS = "defaults:\n  mysql_connections: 80\n  mongodb_connections_current: 300\n"


@pytest.mark.usefixtures("da_guard_env")
class TestServedSeriesGrouping:
    """#3111：變更以 /metrics 實際送出的 series 為單位，不以 threshold key 為單位。
    同一條 series 換一個 key 來寫（`90:critical` ↔ `disable` + `_critical: 90`），
    exporter 送出的東西沒變，就沒有變更。"""

    def _pair(self, tmp_path, old_body, new_body):
        base = _confd(tmp_path / "base", {"_defaults.yaml": _CRIT_DEFAULTS, "db-a.yaml": old_body})
        cur = _confd(tmp_path / "cur", {"_defaults.yaml": _CRIT_DEFAULTS, "db-a.yaml": new_body})
        return bt.extract_changes_from_dirs(str(cur), str(base))

    def test_same_series_written_by_another_key_is_no_change(self, tmp_path):
        """票上的 sb／sc：兩棵樹都只送一條 critical 90，舊版報兩列、rc 1。"""
        changes = self._pair(
            tmp_path,
            'tenants:\n  db-a:\n    mysql_connections: "90:critical"\n',
            'tenants:\n  db-a:\n    mysql_connections: disable\n'
            '    mysql_connections_critical: "90"\n')
        assert _rows(changes) == []

    def test_a_real_change_of_that_series_is_still_one_row(self, tmp_path):
        """同一條 critical series 90 → 95：一列，metric 是比它的告警 key，keys 列出供值的 key。"""
        changes = self._pair(
            tmp_path,
            'tenants:\n  db-a:\n    mysql_connections: "90:critical"\n',
            'tenants:\n  db-a:\n    mysql_connections: disable\n'
            '    mysql_connections_critical: "95"\n')
        assert _rows(changes) == [{
            "tenant": "db-a", "metric": "mysql_connections_critical",
            "old_value": "90", "new_value": "95",
            "keys": ["mysql_connections", "mysql_connections_critical"]}]
        assert [s.labels["severity"] for s in changes[0]["series"]] == ["critical"]

    def test_split_into_two_levels_reports_only_the_new_warning(self, tmp_path):
        """票上的「拆成兩級」：critical 90 → 90 沒變，warning None → 70 新啟用。"""
        changes = self._pair(
            tmp_path,
            'tenants:\n  db-a:\n    mysql_connections: "90:critical"\n',
            'tenants:\n  db-a:\n    mysql_connections: "70"\n'
            '    mysql_connections_critical: "90"\n')
        assert _rows(changes) == [{
            "tenant": "db-a", "metric": "mysql_connections",
            "old_value": None, "new_value": "70", "keys": ["mysql_connections"]}]

    def test_severity_move_is_two_series(self, tmp_path):
        """`70` → `70:critical`：warning 那條沒了、critical 那條出現，各一列。"""
        changes = self._pair(
            tmp_path,
            'tenants:\n  db-a:\n    mysql_connections: "70"\n',
            'tenants:\n  db-a:\n    mysql_connections: "70:critical"\n')
        assert _rows(changes) == [
            {"tenant": "db-a", "metric": "mysql_connections",
             "old_value": "70", "new_value": None, "keys": ["mysql_connections"]},
            {"tenant": "db-a", "metric": "mysql_connections_critical",
             "old_value": None, "new_value": "70", "keys": ["mysql_connections"]},
        ]


class TestNoAlertSideIsNoThreshold:
    """#3111 (e)：告警 key 不在 observed-map、也不是 KNOWN_DEFERRED ⇒ 沒有出廠告警
    比它。mongodb 沒有 `_critical` 告警，所以 `70` → `70:critical` 是 warning 告警
    失去門檻（靜默 MEDIUM），critical 那一列沒有告警比、不回測——不再整筆 UNKNOWN。"""

    _VALS = [60, 80, 90, 100]

    def _tenant(self, monkeypatch, capsys, cli_argv, metric, old, new):
        asked = []

        def query_range(url, q, lookback, step=bt.DEFAULT_STEP):
            asked.append(q)
            return [{"values": [(i, str(v)) for i, v in enumerate(self._VALS)]}]
        monkeypatch.setattr(bt, "prometheus_available", lambda url, timeout=5: True)
        monkeypatch.setattr(bt, "query_range", query_range)
        cli_argv("backtest_threshold", "--tenant", "t-x", "--metric", metric,
                 "--old-value", old, "--new-value", new,
                 "--prometheus", "http://prom:9090", "--json")
        with pytest.raises(SystemExit) as ei:
            bt.main()
        return ei.value.code, json.loads(capsys.readouterr().out)["changes"], asked

    def test_moving_to_a_severity_no_alert_compares_is_silenced(
            self, monkeypatch, capsys, cli_argv):
        rc, rows, asked = self._tenant(monkeypatch, capsys, cli_argv,
                                       "mongodb_connections_current", "70", "70:critical")
        assert asked == ['tenant:mongodb_connections_current:max{tenant="t-x"}']
        assert [(r["metric"], r["status"], r["risk"]) for r in rows] == [
            ("mongodb_connections_current", "analyzed", "MEDIUM"),
            ("mongodb_connections_current_critical", "not_backtested", "UNKNOWN")]
        assert rows[0]["message"] == "Metric disabled — all alerts silenced"
        assert (rows[0]["old_breach_count"], rows[0]["new_breach_count"]) == (3, 0)
        assert rc == 0

    def test_the_reverse_is_newly_enabled(self, monkeypatch, capsys, cli_argv):
        rc, rows, asked = self._tenant(monkeypatch, capsys, cli_argv,
                                       "mongodb_connections_current", "70:critical", "70")
        assert asked == ['tenant:mongodb_connections_current:max{tenant="t-x"}']
        row = rows[0]
        assert row["status"] == "analyzed" and row["new_breach_count"] == 3
        assert row["message"].startswith("Metric newly enabled")
        assert (row["risk"], rc) == ("HIGH", 1)

    def test_known_deferred_side_is_still_not_backtested(self, monkeypatch, capsys, cli_argv):
        """KNOWN_DEFERRED 不是「沒有告警」：它的告警比的是 map 不收錄的 recording rule。"""
        rc, rows, asked = self._tenant(monkeypatch, capsys, cli_argv,
                                       "container_cpu", "70", "80")
        assert (rc, asked, rows[0]["status"]) == (0, [], "not_backtested")
        assert "KNOWN_DEFERRED" in rows[0]["backtest"]


@pytest.mark.usefixtures("da_guard_env")
class TestTenantAndGitDiffAgree:
    """#3111 review：同一個編輯，`--tenant` 與 `--git-diff` 報同樣的列（一條規則）。"""

    @pytest.mark.parametrize("old,new", [("70", "70:critical"), ("70", "95:critical"),
                                         ("70:critical", "60"), ("70", "60"),
                                         ("70", "disable"), ("disable", "70:critical")])
    def test_same_rows(self, tmp_path, monkeypatch, cli_argv, capsys, fake_prom, old, new):
        p = fake_prom(values=(60, 80, 90, 100))
        _rc, out = _run_main(cli_argv, capsys, "--tenant", "db-a", "--metric",
                             "mysql_connections", "--old-value", old, "--new-value", new,
                             "--prometheus", p.url, "--json")
        tenant = [(r["metric"], r["old_value"], r["new_value"], r["risk"], r.get("message"))
                  for r in json.loads(out.out)["changes"]]
        cfg = _git_tree_repo(
            tmp_path, {**_D, "db-a.yaml": _tenant_file("db-a", f'    mysql_connections: "{old}"\n')},
            {**_D, "db-a.yaml": _tenant_file("db-a", f'    mysql_connections: "{new}"\n')})
        monkeypatch.chdir(cfg)
        _rc, out = _run_main(cli_argv, capsys, "--git-diff", "--prometheus", p.url, "--json")
        git = [(r["metric"], r["old_value"], r["new_value"], r["risk"], r.get("message"))
               for r in json.loads(out.out)["changes"]]
        assert tenant == git and tenant, (tenant, git)

    @pytest.mark.parametrize("old,new", [("70", "70:critical"), ("70", "60")])
    def test_same_rows_for_a_dimensioned_key(self, tmp_path, monkeypatch, cli_argv, capsys,
                                             fake_prom, old, new):
        """#3111 round 2: one naming for dimensioned rows — the alert key plus
        the dimension part — in both modes."""
        key = 'mysql_connections{db="a"}'
        p = fake_prom()
        _rc, out = _run_main(cli_argv, capsys, "--tenant", "db-a", "--metric", key,
                             "--old-value", old, "--new-value", new, "--prometheus", p.url,
                             "--json")
        tenant = [(r["metric"], r["old_value"], r["new_value"], r["risk"])
                  for r in json.loads(out.out)["changes"]]
        cfg = _git_tree_repo(
            tmp_path, {**_D, "db-a.yaml": _tenant_file("db-a", f"    '{key}': \"{old}\"\n")},
            {**_D, "db-a.yaml": _tenant_file("db-a", f"    '{key}': \"{new}\"\n")})
        monkeypatch.chdir(cfg)
        _rc, out = _run_main(cli_argv, capsys, "--git-diff", "--prometheus", p.url, "--json")
        git = [(r["metric"], r["old_value"], r["new_value"], r["risk"])
               for r in json.loads(out.out)["changes"]]
        assert tenant == git and tenant, (tenant, git)


# ── --git-diff 比的是 HEAD~1 與工作樹的兩棵 conf.d（#3111）─────────────


_GIT = shutil.which("git")


def _git_run(root, *args):
    return subprocess.run([_GIT, *args], cwd=str(root), capture_output=True, timeout=60,
                          check=True)


def _write_confd(confd, files):
    """`files`: {relative name (str or bytes): body (str or bytes)}; written as BYTES."""
    root = os.fsencode(str(confd))
    for name, body in files.items():
        rel = name if isinstance(name, bytes) else name.encode("utf-8")
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(body if isinstance(body, bytes) else body.encode("utf-8"))


def _git_tree_repo(tmp_path, before, after, commit_after=True):
    """A repo whose `cfg/conf.d` is `before` at HEAD~1 and `after` at HEAD (or only
    in the working tree when `commit_after` is False). None: no conf.d at all.
    Returns `cfg`, the directory the tool runs from (the repo root is above it,
    as in this repo's CI)."""
    repo = tmp_path / "repo"
    cfg = repo / "cfg"
    cfg.mkdir(parents=True)
    (repo / "README").write_text("x\n", encoding="utf-8")
    _git_run(repo, "init", "-q")
    for k, v in (("user.email", "t@example.invalid"), ("user.name", "t"),
                 ("core.quotepath", "true"), ("commit.gpgsign", "false")):
        _git_run(repo, "config", k, v)
    if before is not None:
        _write_confd(cfg / "conf.d", before)
    _git_run(repo, "add", "-A")
    _git_run(repo, "commit", "-qm", "before")
    shutil.rmtree(cfg / "conf.d", ignore_errors=True)
    if after is not None:
        _write_confd(cfg / "conf.d", after)
    if commit_after:
        _git_run(repo, "add", "-A")
        _git_run(repo, "commit", "-qm", "after", "--allow-empty")
    return cfg


_D = {"_defaults.yaml": _CRIT_DEFAULTS}


def _tenant_file(tenant, body):
    return f"tenants:\n  {tenant}:\n{body}"


@pytest.mark.usefixtures("da_guard_env")
class TestGitDiffComparesTrees:
    """#3111：`--git-diff` 不再解析 diff 文字，而是把 HEAD~1 的 conf.d 取成一棵樹，
    與工作樹的 conf.d 一起交給 exporter 讀（同 `--config-dir`）。租戶取自
    `tenants:`，不是檔名；子目錄、改名、刪除、新增、各種檔名都照 exporter 的讀法。"""

    def _run(self, monkeypatch, cli_argv, capsys, fake_prom, cfg, values=(90,) * 10):
        p = fake_prom(values=values)
        monkeypatch.chdir(cfg)
        rc, out = _run_main(cli_argv, capsys, "--git-diff", "--prometheus", p.url, "--json")
        return rc, json.loads(out.out), out.err

    def _changes(self, doc):
        return sorted((c["tenant"], c["metric"], c["old_value"], c["new_value"])
                      for c in doc["changes"])

    def test_spelling_only_change_is_no_change(self, tmp_path, monkeypatch, cli_argv, capsys,
                                               fake_prom):
        cfg = _git_tree_repo(
            tmp_path,
            {**_D, "db-a.yaml": _tenant_file("db-a", '    mysql_connections: "90:critical"\n')},
            {**_D, "db-a.yaml": _tenant_file("db-a", "    mysql_connections: disable\n"
                                                     '    mysql_connections_critical: "90"\n')})
        rc, doc, err = self._run(monkeypatch, cli_argv, capsys, fake_prom, cfg)
        assert (rc, doc["status"]) == (0, "no_changes"), doc

    def test_tenant_comes_from_tenants_not_the_file_name(self, tmp_path, monkeypatch, cli_argv,
                                                         capsys, fake_prom):
        """改名加改值：舊的 diff 解析把租戶當成新檔名 `renamed`。"""
        cfg = _git_tree_repo(
            tmp_path,
            {**_D, "db-a.yaml": _tenant_file("db-a", "    mysql_connections: 70\n")},
            {**_D, "renamed.yaml": _tenant_file("db-a", "    mysql_connections: 50\n")})
        rc, doc, _ = self._run(monkeypatch, cli_argv, capsys, fake_prom, cfg)
        assert self._changes(doc) == [("db-a", "mysql_connections", "70", "50")]

    def test_rename_alone_is_no_change(self, tmp_path, monkeypatch, cli_argv, capsys, fake_prom):
        body = _tenant_file("db-a", "    mysql_connections: 70\n")
        cfg = _git_tree_repo(tmp_path, {**_D, "db-a.yaml": body},
                             {**_D, "sub/moved.yml": body})
        rc, doc, _ = self._run(monkeypatch, cli_argv, capsys, fake_prom, cfg)
        assert (rc, doc["status"]) == (0, "no_changes")

    def test_deleted_added_and_nested_carriers(self, tmp_path, monkeypatch, cli_argv, capsys,
                                               fake_prom):
        cfg = _git_tree_repo(
            tmp_path,
            {**_D, "gone.yaml": _tenant_file("t-gone", "    mysql_connections: 70\n"),
             "deep/er/t.yaml": _tenant_file("t-deep", "    mysql_connections: 70\n")},
            {**_D, "deep/er/t.yaml": _tenant_file("t-deep", "    mysql_connections: 60\n"),
             "new/t.yaml": _tenant_file("t-new", "    mysql_connections: 50\n")})
        rc, doc, _ = self._run(monkeypatch, cli_argv, capsys, fake_prom, cfg)
        changes = self._changes(doc)
        assert ("t-deep", "mysql_connections", "70", "60") in changes
        # 刪掉的租戶：它送出的每一條 series 都沒了。
        assert ("t-gone", "mysql_connections", "70", None) in changes
        # 新租戶：每一條 series 都是新的（沒覆寫的 key 送的是預設值）。
        assert ("t-new", "mysql_connections", None, "50") in changes
        assert ("t-new", "mongodb_connections_current", None, "300") in changes

    def test_confd_missing_at_head1(self, tmp_path, monkeypatch, cli_argv, capsys, fake_prom):
        cfg = _git_tree_repo(
            tmp_path, None,
            {**_D, "db-a.yaml": _tenant_file("db-a", "    mysql_connections: 50\n")})
        rc, doc, _ = self._run(monkeypatch, cli_argv, capsys, fake_prom, cfg)
        assert ("db-a", "mysql_connections", None, "50") in self._changes(doc)
        assert all(c["old_value"] is None for c in doc["changes"])

    def test_a_confd_with_no_config_file_serves_nothing(self, tmp_path, monkeypatch, cli_argv,
                                                        capsys, fake_prom):
        """HEAD~1 的 conf.d/ 只有 README（這個 PR 才放進第一批檔）：那一邊沒有租戶，
        不是 da-guard 的「no .yaml files found」rc 2。"""
        cfg = _git_tree_repo(
            tmp_path, {"README.md": "x\n", "notes/a.txt": "y\n"},
            {**_D, "README.md": "x\n",
             "db-a.yaml": _tenant_file("db-a", "    mysql_connections: 50\n")})
        rc, doc, _ = self._run(monkeypatch, cli_argv, capsys, fake_prom, cfg)
        assert ("db-a", "mysql_connections", None, "50") in self._changes(doc)

    def test_no_confd_on_either_side_says_so(self, tmp_path, monkeypatch, cli_argv, capsys,
                                             fake_prom):
        cfg = _git_tree_repo(tmp_path, None, None)
        rc, doc, err = self._run(monkeypatch, cli_argv, capsys, fake_prom, cfg)
        assert (rc, doc["status"]) == (0, "no_changes")
        assert "no conf.d/ in this directory at HEAD~1 nor in the working tree" in err

    def test_confd_removed_from_the_working_tree(self, tmp_path, monkeypatch, cli_argv, capsys,
                                                 fake_prom):
        cfg = _git_tree_repo(
            tmp_path, {**_D, "db-a.yaml": _tenant_file("db-a", "    mysql_connections: 50\n")},
            None)
        rc, doc, _ = self._run(monkeypatch, cli_argv, capsys, fake_prom, cfg)
        assert ("db-a", "mysql_connections", "50", None) in self._changes(doc)
        assert all(c["new_value"] is None for c in doc["changes"])

    def test_uncommitted_working_tree_counts(self, tmp_path, monkeypatch, cli_argv, capsys,
                                             fake_prom):
        """`git diff HEAD~1` 比的是工作樹：這一點不變。"""
        cfg = _git_tree_repo(
            tmp_path, {**_D, "db-a.yaml": _tenant_file("db-a", "    mysql_connections: 70\n")},
            {**_D, "db-a.yaml": _tenant_file("db-a", "    mysql_connections: 50\n")},
            commit_after=False)
        _git_run(cfg.parent, "commit", "-qm", "unrelated", "--allow-empty")
        rc, doc, _ = self._run(monkeypatch, cli_argv, capsys, fake_prom, cfg)
        assert self._changes(doc) == [("db-a", "mysql_connections", "70", "50")]

    def test_no_parent_commit_is_rc2(self, tmp_path, monkeypatch, cli_argv, capsys):
        repo = tmp_path / "one"
        (repo / "conf.d").mkdir(parents=True)
        _write_confd(repo / "conf.d", {**_D})
        _git_run(repo, "init", "-q")
        _git_run(repo, "-c", "user.email=t@example.invalid", "-c", "user.name=t",
                 "-c", "commit.gpgsign=false", "add", "-A")
        _git_run(repo, "-c", "user.email=t@example.invalid", "-c", "user.name=t",
                 "-c", "commit.gpgsign=false", "commit", "-qm", "only")
        monkeypatch.chdir(repo)
        monkeypatch.setattr(bt, "prometheus_available", lambda url, timeout=5: True)
        rc, out = _run_main(cli_argv, capsys, "--git-diff", "--prometheus", "http://prom:9090",
                            "--json")
        assert rc == 2
        assert json.loads(out.out)["reason"] == "git_diff_unavailable"
        assert "fetch-depth" in out.err
        assert "git could not run to check HEAD~1 out: there is no parent commit" in out.err

    def test_an_object_the_clone_lacks_is_rc2_named(self, tmp_path, monkeypatch, cli_argv,
                                                    capsys):
        """#3111 review: a missing object is its own named cause."""
        cfg = _git_tree_repo(
            tmp_path, {**_D, "db-a.yaml": _tenant_file("db-a", "    mysql_connections: 70\n")},
            {**_D, "db-a.yaml": _tenant_file("db-a", "    mysql_connections: 50\n")})
        repo = cfg.parent
        oid = _git_run(repo, "rev-parse", "HEAD~1:cfg/conf.d/db-a.yaml").stdout.decode().strip()
        os.remove(repo / ".git" / "objects" / oid[:2] / oid[2:])
        monkeypatch.chdir(cfg)
        monkeypatch.setattr(bt, "prometheus_available", lambda url, timeout=5: True)
        rc, out = _run_main(cli_argv, capsys, "--git-diff", "--prometheus", "http://prom:9090",
                            "--json")
        assert (rc, json.loads(out.out)["reason"]) == (2, "git_diff_unavailable")
        assert "an object this clone does not have" in out.err

    def test_a_bare_repository_is_rc2_named(self, tmp_path, monkeypatch, cli_argv, capsys):
        """#3111 review: `--is-inside-work-tree` answers `false` (rc 0) in a bare
        repository; that answer is read, not only whether git ran."""
        cfg = _git_tree_repo(tmp_path, {**_D}, {**_D, "db-a.yaml": "tenants:\n  db-a: {}\n"})
        bare = tmp_path / "bare.git"
        subprocess.run([_GIT, "clone", "-q", "--bare", str(cfg.parent), str(bare)],
                       capture_output=True, timeout=60, check=True)
        monkeypatch.chdir(bare)
        monkeypatch.setattr(bt, "prometheus_available", lambda url, timeout=5: True)
        rc, out = _run_main(cli_argv, capsys, "--git-diff", "--prometheus", "http://prom:9090",
                            "--json")
        assert (rc, json.loads(out.out)["reason"]) == (2, "git_diff_unavailable")
        assert "this directory is not inside a git work tree (a bare repository?)" in out.err

    def test_outside_a_work_tree_is_rc2(self, tmp_path, monkeypatch, cli_argv, capsys):
        (tmp_path / "plain" / "conf.d").mkdir(parents=True)
        monkeypatch.chdir(tmp_path / "plain")
        monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
        monkeypatch.setattr(bt, "prometheus_available", lambda url, timeout=5: True)
        rc, out = _run_main(cli_argv, capsys, "--git-diff", "--prometheus", "http://prom:9090",
                            "--json")
        assert (rc, json.loads(out.out)["reason"]) == (2, "git_diff_unavailable")



def _plain_repo(root):
    root.mkdir(parents=True)
    _git_run(root, "init", "-q")
    for k, v in (("user.email", "t@example.invalid"), ("user.name", "t"),
                 ("commit.gpgsign", "false")):
        _git_run(root, "config", k, v)
    return root


def _commit_twice(root):
    """Commit what is there as HEAD~1, then an empty HEAD (nothing changed)."""
    _git_run(root, "add", "-A")
    _git_run(root, "commit", "-qm", "before")
    _git_run(root, "commit", "-qm", "after", "--allow-empty")


def _crafted_head1(root, confd_entries):
    """HEAD~1's tree is `conf.d` → a tree made of `confd_entries` (`git mktree`
    lines, which may be ones a checkout would never produce); HEAD is an
    empty commit on top. The working tree holds no conf.d/."""
    tree = subprocess.run([_GIT, "mktree"], cwd=str(root), input=confd_entries,
                          capture_output=True, timeout=60, check=True).stdout.strip()
    top = subprocess.run([_GIT, "mktree"], cwd=str(root),
                         input=b"040000 tree " + tree + b"\tconf.d\n",
                         capture_output=True, timeout=60, check=True).stdout.strip()
    c1 = subprocess.run([_GIT, "commit-tree", "-m", "before", top.decode()], cwd=str(root),
                        capture_output=True, timeout=60, check=True).stdout.strip()
    c2 = subprocess.run([_GIT, "commit-tree", "-m", "after", "-p", c1.decode(), top.decode()],
                        cwd=str(root), capture_output=True, timeout=60, check=True).stdout.strip()
    _git_run(root, "update-ref", "HEAD", c2.decode())


def _blob(root, data):
    return subprocess.run([_GIT, "hash-object", "-w", "--stdin"], cwd=str(root), input=data,
                          capture_output=True, timeout=60, check=True).stdout.strip()


@pytest.mark.usefixtures("da_guard_env")
class TestHead1TreeShapes:
    """#3111 review：HEAD~1 由 git 自己 checkout 到暫存目錄，所以 conf.d/ 本身是
    symlink、或裡面的 symlink 指出 conf.d/，都照 checkout 的樣子解析；submodule、
    不是目錄、解析後逃出 checkout 的 link 一律 rc 2 `git_diff_unavailable`、指名
    `HEAD~1:<path>`，不再有 traceback。"""

    def _run(self, monkeypatch, cli_argv, capsys, fake_prom, cwd):
        p = fake_prom()
        monkeypatch.chdir(cwd)
        rc, out = _run_main(cli_argv, capsys, "--git-diff", "--prometheus", p.url, "--json")
        return rc, json.loads(out.out), out.err

    def test_a_symlinked_confd_compares_as_unchanged(self, tmp_path, monkeypatch, cli_argv,
                                                     capsys, fake_prom):
        """舊版：HEAD~1 的 conf.d 是 symlink ⇒ 當成沒有 conf.d ⇒ 每個門檻「新啟用」、rc 1。"""
        repo = _plain_repo(tmp_path / "r")
        _write_confd(repo / "real", {**_D, "t.yaml": _tenant_file("t-a", "    mysql_connections: 70\n")})
        symlink_or_skip("real", repo / "conf.d")
        _commit_twice(repo)
        rc, doc, err = self._run(monkeypatch, cli_argv, capsys, fake_prom, repo)
        assert (rc, doc["status"]) == (0, "no_changes"), (doc, err)

    def test_a_link_out_of_confd_resolves_as_in_a_checkout(self, tmp_path, monkeypatch,
                                                            cli_argv, capsys, fake_prom):
        """舊版：`conf.d/t1.yaml -> ../shared/t1.yaml` 在 HEAD~1 那棵樹裡解析不到，rc 2、
        訊息還是暫存路徑。"""
        repo = _plain_repo(tmp_path / "r")
        _write_confd(repo / "shared", {"t1.yaml": _tenant_file("t-a", "    mysql_connections: 70\n")})
        _write_confd(repo / "conf.d", _D)
        symlink_or_skip("../shared/t1.yaml", repo / "conf.d" / "t1.yaml")
        _commit_twice(repo)
        rc, doc, err = self._run(monkeypatch, cli_argv, capsys, fake_prom, repo)
        assert (rc, doc["status"]) == (0, "no_changes"), (doc, err)

    @pytest.mark.parametrize("target", ["../../../outside.yaml", "/etc/hostname"],
                             ids=["leaves-the-repo", "absolute"])
    def test_a_link_leaving_the_repository_is_refused(self, tmp_path, monkeypatch, cli_argv,
                                                      capsys, fake_prom, target):
        repo = _plain_repo(tmp_path / "r")
        _write_confd(repo / "conf.d", _D)
        symlink_or_skip(target, repo / "conf.d" / "t1.yaml")
        _commit_twice(repo)
        rc, doc, err = self._run(monkeypatch, cli_argv, capsys, fake_prom, repo)
        assert (rc, doc["reason"]) == (2, "git_diff_unavailable"), (doc, err)
        assert "HEAD~1:conf.d/t1.yaml" in err and "Traceback" not in err
        assert "resolves outside the checked-out tree" in err, err
        assert "git could not run" not in err  # a refusal, not a git failure

    def test_a_refused_link_outside_confd_is_left_out(self, tmp_path, monkeypatch, cli_argv,
                                                      capsys, fake_prom):
        """conf.d/ 以外、沒有人引用的危險 link 不影響比對。"""
        repo = _plain_repo(tmp_path / "r")
        _write_confd(repo / "conf.d", {**_D, "t.yaml": _tenant_file("t-a", "    mysql_connections: 70\n")})
        symlink_or_skip("/etc/hostname", repo / "elsewhere")
        _commit_twice(repo)
        rc, doc, err = self._run(monkeypatch, cli_argv, capsys, fake_prom, repo)
        assert (rc, doc["status"]) == (0, "no_changes"), (doc, err)

    def test_a_submodule_confd_fails_closed(self, tmp_path, monkeypatch, cli_argv, capsys,
                                            fake_prom):
        repo = _plain_repo(tmp_path / "r")
        (repo / "README").write_text("x\n", encoding="utf-8")
        _git_run(repo, "add", "-A")
        _git_run(repo, "commit", "-qm", "base")
        head = _git_run(repo, "rev-parse", "HEAD").stdout.decode().strip()
        _git_run(repo, "update-index", "--add", "--cacheinfo", f"160000,{head},conf.d")
        _git_run(repo, "commit", "-qm", "submodule")
        _git_run(repo, "commit", "-qm", "after", "--allow-empty")
        rc, doc, err = self._run(monkeypatch, cli_argv, capsys, fake_prom, repo)
        assert (rc, doc["reason"]) == (2, "git_diff_unavailable"), (doc, err)
        assert "HEAD~1:conf.d is a submodule" in err

    def test_a_submodule_inside_confd_fails_closed(self, tmp_path, monkeypatch, cli_argv,
                                                   capsys, fake_prom):
        repo = _plain_repo(tmp_path / "r")
        _write_confd(repo / "conf.d", _D)
        _git_run(repo, "add", "-A")
        _git_run(repo, "commit", "-qm", "base")
        head = _git_run(repo, "rev-parse", "HEAD").stdout.decode().strip()
        _git_run(repo, "update-index", "--add", "--cacheinfo", f"160000,{head},conf.d/team")
        _git_run(repo, "commit", "-qm", "submodule")
        _git_run(repo, "commit", "-qm", "after", "--allow-empty")
        rc, doc, err = self._run(monkeypatch, cli_argv, capsys, fake_prom, repo)
        assert (rc, doc["reason"]) == (2, "git_diff_unavailable"), (doc, err)
        assert "HEAD~1:conf.d/team is a submodule" in err

    def test_a_confd_that_is_a_file_fails_closed(self, tmp_path, monkeypatch, cli_argv, capsys,
                                                 fake_prom):
        repo = _plain_repo(tmp_path / "r")
        (repo / "conf.d").write_text("not a directory\n", encoding="utf-8")
        _commit_twice(repo)
        rc, doc, err = self._run(monkeypatch, cli_argv, capsys, fake_prom, repo)
        assert (rc, doc["reason"]) == (2, "git_diff_unavailable"), (doc, err)
        assert "HEAD~1:conf.d is not a directory" in err

    @pytest.mark.parametrize("shape,want", [
        ("blob-and-tree", (0, "no_changes")), ("two-blobs", (0, "no_changes")),
        # git writes the link cut at the NUL (`a`), which dangles: the exporter
        # cannot read it, so the baseline is refused whole (#2119's rule).
        ("nul-link", (2, "served_values_unavailable"))])
    def test_a_crafted_tree_is_what_git_checks_out(self, tmp_path, monkeypatch, cli_argv,
                                                   capsys, fake_prom, shape, want):
        """4ca5dcdf：同名的兩個 entry ⇒ FileExistsError、link target 含 NUL ⇒
        ValueError，都是 traceback。現在由 git 自己 checkout：結果就是 checkout 的
        結果，一份 JSON、沒有 traceback、沒有暫存路徑。"""
        repo = _plain_repo(tmp_path / "r")
        a, b = _blob(repo, b"tenants: {}\n"), _blob(repo, b"x: 1\n")
        sub = subprocess.run([_GIT, "mktree"], cwd=str(repo), input=b"100644 blob " + a + b"\tz.yaml\n",
                             capture_output=True, timeout=60, check=True).stdout.strip()
        lines = {
            "blob-and-tree": [b"100644 blob " + a + b"\tdup.yaml", b"040000 tree " + sub + b"\tdup.yaml"],
            "two-blobs": [b"100644 blob " + a + b"\tdup.yaml", b"100644 blob " + b + b"\tdup.yaml"],
            "nul-link": [b"120000 blob " + _blob(repo, b"a\0b") + b"\tnul.yaml"],
        }[shape]
        _crafted_head1(repo, b"\n".join(lines) + b"\n")
        rc, doc, err = self._run(monkeypatch, cli_argv, capsys, fake_prom, repo)
        assert "Traceback" not in err and "backtest-head1-" not in err, err
        assert (rc, doc.get("reason") if rc else doc["status"]) == want, (doc, err)

    def test_hidden_only_confd_at_head1_serves_nothing(self, tmp_path, monkeypatch, cli_argv,
                                                       capsys, fake_prom):
        """舊版：`.old.yaml`、`.archive/old.yaml` 被當成 config 檔 ⇒ da-guard「no .yaml
        files found」rc 2。exporter 跳過隱藏的檔與目錄。"""
        cfg = _git_tree_repo(
            tmp_path, {"README.md": "x\n", ".old.yaml": "tenants: {}\n",
                       ".archive/old.yaml": "tenants: {}\n"},
            {**_D, "db-a.yaml": _tenant_file("db-a", "    mysql_connections: 50\n")})
        rc, doc, err = self._run(monkeypatch, cli_argv, capsys, fake_prom, cfg)
        assert any((c["tenant"], c["old_value"], c["new_value"]) == ("db-a", None, "50")
                   for c in doc["changes"]), (doc, err)



@pytest.mark.usefixtures("da_guard_env")
class TestHead1IsGitsOwnCheckout:
    """#3111 round 2：HEAD~1 由 git 自己 checkout（暫存 index 的 read-tree +
    checkout-index），所以 eol 轉換、link 的寫法、export-subst（checkout 不套用）
    都與真的 checkout 相同；這幾種未變更的 repo 都不報變更。"""

    def _run(self, monkeypatch, cli_argv, capsys, fake_prom, cwd):
        p = fake_prom()
        monkeypatch.chdir(cwd)
        rc, out = _run_main(cli_argv, capsys, "--git-diff", "--prometheus", p.url, "--json")
        return rc, json.loads(out.out), out.err

    _T = {"t.yaml": _tenant_file("t-a", "    mysql_connections: 70\n")}

    @pytest.mark.parametrize("how", ["autocrlf", "eol-crlf"])
    def test_crlf_checkouts_compare_as_unchanged(self, tmp_path, monkeypatch, cli_argv, capsys,
                                                 fake_prom, how):
        """f998d34b：archive 套了 working-tree 轉換、雜湊不符 ⇒ 必定 rc 2。"""
        repo = _plain_repo(tmp_path / "r")
        if how == "autocrlf":
            _git_run(repo, "config", "core.autocrlf", "true")
        else:
            (repo / ".gitattributes").write_text("* text eol=crlf\n", encoding="utf-8")
        _write_confd(repo / "conf.d", {**_D, **self._T})
        _commit_twice(repo)
        rc, doc, err = self._run(monkeypatch, cli_argv, capsys, fake_prom, repo)
        assert (rc, doc["status"]) == (0, "no_changes"), (doc, err)

    def test_links_written_with_dot_and_double_slash(self, tmp_path, monkeypatch, cli_argv,
                                                     capsys, fake_prom):
        """f998d34b：data filter 正規化 link target 後與 git 記錄不符 ⇒ rc 2。"""
        repo = _plain_repo(tmp_path / "r")
        _write_confd(repo / "shared", {"t2.yaml": _tenant_file("t-b", "    mysql_connections: 60\n")})
        _write_confd(repo / "conf.d", {**_D, "t1.real": _tenant_file("t-a", "    mysql_connections: 70\n")})
        symlink_or_skip("./t1.real", repo / "conf.d" / "t1.yaml")
        symlink_or_skip("../shared//t2.yaml", repo / "conf.d" / "t2.yaml")
        _commit_twice(repo)
        rc, doc, err = self._run(monkeypatch, cli_argv, capsys, fake_prom, repo)
        assert (rc, doc["status"]) == (0, "no_changes"), (doc, err)

    @pytest.mark.parametrize("layout", ["link-out-of-confd", "confd-is-a-link"])
    def test_export_subst_is_not_applied(self, tmp_path, monkeypatch, cli_argv, capsys,
                                         fake_prom, layout):
        """f998d34b：`git archive` 套 export-subst，conf.d 外的連結目標（或 conf.d
        本身是 link 時）被改寫，未變更的 PR 報 HIGH、rc 1。checkout 不套用它。"""
        repo = _plain_repo(tmp_path / "r")
        body = _tenant_file("t-a", '    mysql_connections: "$Format:%ct$"\n')
        if layout == "link-out-of-confd":
            (repo / ".gitattributes").write_text("shared/** export-subst\n", encoding="utf-8")
            _write_confd(repo / "shared", {"t.yaml": body})
            _write_confd(repo / "conf.d", _D)
            symlink_or_skip("../shared/t.yaml", repo / "conf.d" / "t.yaml")
        else:
            (repo / ".gitattributes").write_text("real/** export-subst\n", encoding="utf-8")
            _write_confd(repo / "real", {**_D, "t.yaml": body})
            symlink_or_skip("real", repo / "conf.d")
        _commit_twice(repo)
        rc, doc, err = self._run(monkeypatch, cli_argv, capsys, fake_prom, repo)
        assert (rc, doc["status"]) == (0, "no_changes"), (doc, err)

    @pytest.mark.parametrize("last", ["../../outside.yaml", "/etc/hostname"])
    def test_a_chain_of_links_leaving_the_checkout_is_refused(self, tmp_path, monkeypatch,
                                                              cli_argv, capsys, fake_prom, last):
        repo = _plain_repo(tmp_path / "r")
        _write_confd(repo / "conf.d", _D)
        (repo / "hop").mkdir()
        symlink_or_skip(last, repo / "hop" / "l1.yaml")
        symlink_or_skip("../hop/l1.yaml", repo / "conf.d" / "t.yaml")
        _commit_twice(repo)
        rc, doc, err = self._run(monkeypatch, cli_argv, capsys, fake_prom, repo)
        assert (rc, doc["reason"]) == (2, "git_diff_unavailable"), (doc, err)
        assert "HEAD~1:conf.d/t.yaml resolves outside the checked-out tree" in err, err
        assert "backtest-head1-" not in err

    def test_the_repository_is_not_touched(self, tmp_path, monkeypatch, cli_argv, capsys,
                                           fake_prom):
        """The repository's own index, work tree and `.git/worktrees` are not touched."""
        repo = _plain_repo(tmp_path / "r")
        _write_confd(repo / "conf.d", {**_D, **self._T})
        _commit_twice(repo)
        (repo / "conf.d" / "t.yaml").write_text(
            _tenant_file("t-a", "    mysql_connections: 50\n"), encoding="utf-8")
        before = (_git_run(repo, "status", "--porcelain").stdout,
                  (repo / ".git" / "index").read_bytes())
        rc, doc, _ = self._run(monkeypatch, cli_argv, capsys, fake_prom, repo)
        assert [(c["metric"], c["old_value"], c["new_value"]) for c in doc["changes"]] == [
            ("mysql_connections", "70", "50")]
        assert (_git_run(repo, "status", "--porcelain").stdout,
                (repo / ".git" / "index").read_bytes()) == before
        assert not (repo / ".git" / "worktrees").exists()


_SLOW_GIT = """#!{bash}
"{git}" "$@"
rc=$?
case " $* " in *" checkout-index "*) : > "{marker}"; exec sleep 60;; esac
exit $rc
"""


@pytest.mark.parametrize("sig", ["SIGTERM", "SIGINT"])
def test_a_signal_removes_the_head1_checkout(tmp_path, sig, da_guard_env):
    """#3111 round 2：SIGTERM 會留下約 60 MB 的暫存 checkout。兩種訊號下都清掉。"""
    import signal as _signal
    import time
    bash = shutil.which("bash")
    if bash is None or not hasattr(_signal, sig) or os.name != "posix":
        pytest.skip("needs bash and POSIX signals")
    repo = _plain_repo(tmp_path / "r")
    _write_confd(repo / "conf.d", {**_D, "t.yaml": _tenant_file("t-a", "    mysql_connections: 70\n")})
    _commit_twice(repo)
    bindir, scratch, marker = tmp_path / "bin", tmp_path / "tmp", tmp_path / "checked-out"
    bindir.mkdir()
    scratch.mkdir()
    wrapper = bindir / "git"
    with open(wrapper, "w", encoding="utf-8") as f:
        f.write(_SLOW_GIT.format(bash=bash, git=_GIT, marker=marker))
    os.chmod(wrapper, 0o755)
    env = {**os.environ, "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
           "TMPDIR": str(scratch)}
    env.pop("PYTHONUNBUFFERED", None)
    proc = subprocess.Popen([sys.executable, bt.__file__, "--git-diff", "--prometheus",
                             "http://127.0.0.1:9"], cwd=str(repo), env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 60
        while not marker.exists():
            assert proc.poll() is None and time.monotonic() < deadline, "never reached checkout"
            time.sleep(0.1)
        assert [p.name for p in scratch.iterdir() if p.name.startswith("backtest-head1-")]
        proc.send_signal(getattr(_signal, sig))
        proc.wait(timeout=60)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=60)
    assert [p.name for p in scratch.iterdir() if p.name.startswith("backtest-head1-")] == []
