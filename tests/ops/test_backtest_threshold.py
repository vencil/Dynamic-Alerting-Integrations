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


class TestGitDiffRemoved:
    """#3111: `--git-diff` was removed. An old invocation gets rc 2 and one
    ERROR line naming the replacement — before anything is read, and through
    `--skip-if-unavailable` too — not argparse's "unrecognized arguments"."""

    @pytest.mark.parametrize("extra", [(), ("--skip-if-unavailable",)])
    def test_rc2_naming_the_replacement(self, monkeypatch, capsys, cli_argv, extra):
        for name in ("_observed_map", "prometheus_available", "query_range",
                     "extract_changes_from_dirs", "load_trees"):
            monkeypatch.setattr(bt, name, _boom)
        cli_argv("backtest_threshold", "--git-diff", "--prometheus", "http://prom:9090", *extra)
        with pytest.raises(SystemExit) as exc_info:
            bt.main()
        assert exc_info.value.code == 2
        captured = capsys.readouterr()
        lines = [ln for ln in captured.err.splitlines() if ln.startswith("ERROR:")]
        assert lines == [bt.GIT_DIFF_REMOVED]
        assert "git worktree add" in lines[0] and "--baseline" in lines[0]
        assert captured.out == ""

    def test_json_envelope(self, monkeypatch, capsys, cli_argv):
        cli_argv("backtest_threshold", "--git-diff", "--prometheus", "http://prom:9090", "--json")
        with pytest.raises(SystemExit) as exc_info:
            bt.main()
        assert exc_info.value.code == 2
        doc = json.loads(capsys.readouterr().out)
        assert (doc["status"], doc["reason"]) == ("caller_error", "git_diff_removed")
        assert set(doc) == set(bt.generate_report([], doc["lookback"])) | {"status", "reason"}

    @pytest.mark.parametrize("argv", [
        ("--git-diff", "--config-dir", "a", "--baseline", "b"),
        ("--config-dir", "a", "--git-diff"),
        ("--git-diff", "--tenant", "t", "--metric", "m", "--new-value", "1"),
        ("--git-diff=x",),
        ("--git-diff", "--json", "--config-dir", "a"),
    ], ids=["with-config-dir", "after-config-dir", "with-tenant", "with-a-value", "json"])
    def test_it_answers_first_in_every_combination(self, capsys, cli_argv, argv):
        """73c1e2da: argparse's "not allowed with" (or "ignored explicit argument")
        answered first, so the replacement was never named."""
        cli_argv("backtest_threshold", *argv, "--prometheus", "http://prom:9090")
        with pytest.raises(SystemExit) as exc_info:
            bt.main()
        assert exc_info.value.code == 2
        captured = capsys.readouterr()
        assert bt.GIT_DIFF_REMOVED in captured.err.splitlines()
        assert "not allowed with" not in captured.err
        if "--json" in argv:
            assert json.loads(captured.out)["reason"] == "git_diff_removed"

    def test_hidden_from_help(self, capsys, cli_argv):
        cli_argv("backtest_threshold", "--help")
        with pytest.raises(SystemExit):
            bt.main()
        assert "--git-diff" not in capsys.readouterr().out


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
        """啟用→停用：MEDIUM 風險。變更的值是 exporter 送的（served-values 的讀法，
        #3140 PR2）：沒有 row 的一邊是 None，不是寫法上的 `disable`。"""
        values = self._make_values([80, 90])
        monkeypatch.setattr(bt, "query_range",
                            lambda *a, **kw: [{"values": values}])
        change = {"tenant": "db-a", "metric": "cpu", "old_value": "70", "new_value": None}
        result = bt.backtest_change("http://prom", change, 86400)
        assert result["risk"] == "MEDIUM"
        assert "disabled" in result["message"]

    def test_a_row_lost_to_a_refused_value_is_high(self, monkeypatch):
        """#3140 PR2：新的一邊沒有 row 是因為 exporter 拒收非有限值（`new_refused`，
        served-values 的答案）——設定錯誤讓告警失聲，判 HIGH，不是停用的 MEDIUM。"""
        values = self._make_values([80, 90])
        monkeypatch.setattr(bt, "query_range",
                            lambda *a, **kw: [{"values": values}])
        change = {"tenant": "db-a", "metric": "cpu", "old_value": "70", "new_value": None,
                  "new_refused": "non_finite"}
        result = bt.backtest_change("http://prom", change, 86400)
        assert (result["status"], result["risk"], result["new_breach_count"]) == ("analyzed", "HIGH", 0)
        assert result["message"].startswith("Value refused by the exporter (non_finite)")

    def test_enable_transition(self, monkeypatch):
        """停用→啟用：根據觸發比例決定風險。"""
        values = self._make_values([80, 90, 50, 60, 70, 85, 95, 75, 65, 55])
        monkeypatch.setattr(bt, "query_range",
                            lambda *a, **kw: [{"values": values}])
        change = {"tenant": "db-a", "metric": "cpu", "old_value": None, "new_value": "70"}
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
        (None, "70"),          # 新啟用（main 上是 analyzed / LOW）
        ("70", None),          # 停用（main 上是 analyzed / MEDIUM）
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
                           None, "70")
        assert result["status"] == "analyzed"
        assert result["risk"] == "MEDIUM"
        assert result["data_points"] == 1000
        assert result["new_breach_count"] == 100

    def test_only_hot_points_new_enable_is_high(self, monkeypatch):
        result = self._run(monkeypatch, ["95"] * 100, None, "70")
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
        result = self._run(monkeypatch, ["NaN"] * 9 + ["+Inf"], None, "70")
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
                "data": {"resultType": "matrix", "result": [{"values": [[1, "42"]]}]},
            }, None
        monkeypatch.setattr("_lib_prometheus.http_get_json", mock_get)
        result = bt.query_range("http://prom", "up", 3600)
        assert len(result) == 1

    def test_http_error_raises(self, monkeypatch):
        """#3059：HTTP 錯誤丟 QueryError、帶錯誤原文，不再回空清單
        （以前回 []，被報成 no_data、rc 0）。"""
        def mock_get(url, timeout=30):
            return None, "connection refused"
        monkeypatch.setattr("_lib_prometheus.http_get_json", mock_get)
        with pytest.raises(bt.QueryError, match="^connection refused$"):
            bt.query_range("http://prom", "up", 3600)

    def test_api_error_raises(self, monkeypatch):
        """#3059：API 錯誤狀態（含沒有 error 欄位）丟 QueryError。"""
        def mock_get(url, timeout=30):
            return {"status": "error"}, None
        monkeypatch.setattr("_lib_prometheus.http_get_json", mock_get)
        with pytest.raises(bt.QueryError, match="Unknown Prometheus error"):
            bt.query_range("http://prom", "bad{", 3600)

    def test_empty_result_is_still_empty(self, monkeypatch):
        """control：查詢成功但沒有 series 仍是 []（no_data 那條路）。"""
        def mock_get(url, timeout=30):
            return {"status": "success", "data": {"resultType": "matrix", "result": []}}, None
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
    """custom_alert_notice / custom_alert_markdown — 把 flat 工具對
    _custom_alerts 的隱性無-recipe-path 補成顯性 fail-loud（#657）。哪些租戶的
    recipe 變了見 TestRecipeNoteFromTheWholeTree。"""

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

    # ── site 1 of 2: extract_changes_from_dirs ────────────────────────
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

    # ── site 2 of 2: the recipe notice in main() ──────────────────────
    def test_recipe_notice_counts_a_tenant_declared_in_a_yml_carrier(
        self, monkeypatch, tmp_path, capsys, cli_argv, da_guard_env
    ):
        """LOWER + UPPER through `main()`'s recipe NOTE, which since #3111 is
        the tenants whose recipes changed as da-guard serves the two trees:
        a recipe tenant in a `.yml` carrier is a real tenant (#1603); one in a
        `.txt` file is none the exporter loads."""
        monkeypatch.setattr(bt, "prometheus_available", lambda url, timeout=5: True)
        files = {name: _tenant_file(t, "    mysql_connections: 70\n")
                 for name, t in (("acme.yaml", "acme"), ("beta.yml", "beta"),
                                 ("ghost.txt", "ghost"))}
        cur, base = _dirs(tmp_path, {**_D, **files},
                          {**_D, **{n: b + _RECIPE.format(v=150) for n, b in files.items()}})
        cli_argv("backtest_threshold", "--config-dir", cur, "--baseline", base,
                 "--prometheus", "http://prom:9090")
        with pytest.raises(SystemExit):
            bt.main()
        note = [ln for ln in capsys.readouterr().err.splitlines() if ln.startswith("NOTE:")]
        assert len(note) == 1 and note[0].endswith("(_custom_alerts): acme, beta"), note


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
        r, _ = self._run(monkeypatch, "rabbitmq_consumers", [0, 0, 1, 5], "1", None)
        assert (r["risk"], r["old_breach_count"]) == ("MEDIUM", 2)

    def test_lower_bound_enable_counts_new_below(self, monkeypatch):
        r, _ = self._run(monkeypatch, "kafka_active_controllers", [0, 1, 1, 1], None, "1")
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
        # 退役拼法（`--tenant` 給的是寫法）先過 alias 表。
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

    @pytest.mark.parametrize("old,new", [("70", "85"), ("70", None), (None, "70")])
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


@pytest.mark.usefixtures("da_guard_env")
class TestWrittenValues:
    """`--tenant` 給的是寫法上的值。exporter 對 `70:critical` 的
    讀法（`pkg/config/resolve.go`：切最後一個 `:`，前段是數字、後段是 severity）
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
        ("mysql_connections", "dİsable", None),
        ("mysql_connections", None, None),
        ("mysql_connections", "1e-400", (0.0, "warning")),
    ])
    def test_each_side_is_what_the_exporter_serves(self, key, raw, want):
        """#3140 PR2：每一邊是 exporter 對這個值送的 row（da-guard served-values），
        Python 不再自己切 severity、判停用、讀數字。"""
        assert bt._served_written(key, (raw,)) == [want]

    @pytest.mark.parametrize("key,raw,want", [
        # base key：exporter 改送平台預設值（:critical 跟著被拒的值走，#2377）。
        ("mysql_connections", "NaN", (80.0, "warning")),
        ("mysql_connections", "Inf", (80.0, "warning")),
        ("mysql_connections", "-inf:critical", (80.0, "warning")),
        ("mysql_connections", "infinity", (80.0, "warning")),
        ("mysql_connections", "NaN:critical", (80.0, "warning")),
        # `_critical`／optional_overrides key：沒有預設值可退，不送 row。
        ("mysql_connections_critical", "+Inf", bt._REFUSED_NO_ROW),
        ("oracle_process_count", "-Inf", bt._REFUSED_NO_ROW),
    ])
    def test_a_non_finite_number_is_read_from_the_exporters_refusal(self, key, raw, want):
        """#3140：Go 的 ParseFloat 讀得懂 NaN／Inf，但 exporter 拒收；拒收後送什麼是
        served-values 的 `keys[].refused` 與送出的值，不是這裡的鏡像判定。"""
        assert bt._served_written(key, (raw,)) == [want]

    @pytest.mark.parametrize("metric,old,new,want", [
        # base key: the exporter serves the platform default (80) instead.
        ("mysql_connections", "70", "NaN", [("mysql_connections", "70", "80", None)]),
        ("mysql_connections", "70", "-Inf", [("mysql_connections", "70", "80", None)]),
        # the `:critical` goes with the refused value (#2377): a warning row.
        ("mysql_connections", "70", "-inf:critical", [("mysql_connections", "70", "80", None)]),
        ("mysql_connections", "NaN", "70", [("mysql_connections", "80", "70", None)]),
        ("mysql_connections", "80", "+Inf", []),
        # no default to fall back to: no row, marked refused (judged HIGH).
        ("mysql_connections_critical", "95", "+Inf",
         [("mysql_connections_critical", "95", None, "non_finite")]),
        ("oracle_process_count", "70", "-Inf", [("oracle_process_count", "70", None, "non_finite")]),
        # refused on the OLD side: no row there, nothing marked (a newly served row).
        ("oracle_process_count", "-Inf", "70", [("oracle_process_count", None, "70", None)]),
    ])
    def test_tenant_reads_a_non_finite_side_as_what_the_exporter_serves(
            self, capsys, metric, old, new, want):
        """#3140：`--tenant` 的非有限值改用 exporter 實際送的值計算：base key
        是平台預設值，`_critical`／`optional_overrides:` key 沒有 row（帶
        `new_refused`）。stderr 說明改用了什麼。"""
        rows = bt.written_changes("t-x", metric, old, new)
        assert [(r["metric"], r["old_value"], r["new_value"], r.get("new_refused"))
                for r in rows] == want
        assert "is not a finite number" in capsys.readouterr().err

    def test_tenant_non_finite_on_a_key_without_a_known_default_is_not_read(self):
        """A key the registry does not know: its default is unknown here, so
        the change is the values as written (not backtested)."""
        assert bt.written_changes("t-x", "no_such_key", "70", "-Inf") == [
            {"tenant": "t-x", "metric": "no_such_key", "old_value": "70", "new_value": "-Inf",
             "unread": "value '-Inf': this backtest cannot read it the way the exporter does"}]

    def test_a_key_the_registry_does_not_know_is_served_as_written(self):
        """未登錄的 key 在 `--tenant` 的樹裡宣告成沒有預設值的 key，exporter 照讀。"""
        assert bt._served_written("no_such_key", ("70", "60:critical")) == [
            (70.0, "warning"), (60.0, "critical")]

    def test_a_non_finite_new_value_is_backtested_at_the_default(
            self, monkeypatch, capsys, cli_argv):
        """#3140：70 → NaN 回測的是 exporter 改送的預設值 80（2/4 點越界），
        不是 NaN（0 點，以前判 HIGH、rc 1）。"""
        rc, rows, asked = self._tenant(monkeypatch, capsys, cli_argv,
                                       "mysql_connections", "70", "NaN")
        assert [(r["metric"], r["old_value"], r["new_value"], r["new_breach_count"])
                for r in rows] == [("mysql_connections", "70", "80", 2)]
        assert rc == 0

    @pytest.mark.parametrize("metric,raw", [
        ("mysql_connections_critical", "+Inf"), ("oracle_process_count", "-Inf")])
    def test_a_row_lost_to_a_refused_non_finite_value_is_high(
            self, monkeypatch, capsys, cli_argv, metric, raw):
        """#3140 PR2：`_critical`／宣告 key 寫非有限值，exporter 拒收且不送 row——
        PR1 暫時判成「Metric disabled」MEDIUM（rc 0）；現在依 served-values 的
        `keys[].refused` 判 HIGH，rc 回到 1。"""
        rc, rows, _asked = self._main(monkeypatch, capsys, cli_argv,
                                      ["--tenant", "t-x", "--metric", metric,
                                       "--old-value", "70", f"--new-value={raw}"])
        assert [(r["new_value"], r["risk"]) for r in rows] == [(None, "HIGH")]
        assert rows[0]["message"].startswith("Value refused by the exporter (non_finite)")
        assert rc == 1

    def test_tenant_without_da_guard_is_rc2_named(self, monkeypatch, capsys, cli_argv, tmp_path):
        """#3140 PR2：`--tenant` 的值由 da-guard 讀，找不到 da-guard 是結束碼 2
        （`served_values_unavailable`），不是靜默回到 Python 自己讀。"""
        monkeypatch.setenv("DA_GUARD_BINARY", str(tmp_path / "no-such-da-guard"))
        monkeypatch.setattr(bt, "prometheus_available", lambda url, timeout=5: True)
        cli_argv("backtest_threshold", "--tenant", "t-x", "--metric", "mysql_connections",
                 "--old-value", "70", "--new-value", "50", "--prometheus", "http://prom:9090", "--json")
        with pytest.raises(SystemExit) as ei:
            bt.main()
        out = capsys.readouterr()
        assert (ei.value.code, json.loads(out.out)["reason"]) == (2, "served_values_unavailable"), out.err

    def test_disable_is_still_medium(self, monkeypatch, capsys, cli_argv):
        """對照組：寫 `disable` 是停用（`keys[].reason` "disabled"），維持 MEDIUM、rc 0。"""
        rc, rows, _asked = self._tenant(monkeypatch, capsys, cli_argv,
                                        "mysql_connections_critical", "70", "disable")
        assert [(r["new_value"], r["risk"]) for r in rows] == [(None, "MEDIUM")]
        assert rc == 0

    @pytest.mark.parametrize("key,raw", [
        ("mysql_connections", "7O"),
        ("mysql_connections", "١٢"), ("mysql_connections_critical", "95:critical"),
        ("mysql_connections", "+nan"), ("mysql_connections", "-nan:critical"),
        ("mysql_connections", "1e400"), ("mysql_connections_critical", "-1e400"),
        ("mysql_connections", "1e999"),
        ("mysql_connections", "70:info"), ("mysql_connections", "70:"),
        ("mysql_connections", "disable:critical"), ("mysql_connections", "disable\x1c"),
        ("no_such_key", "NaN"),
    ], ids=["typo", "non-ascii-digit", "suffix-on-critical-key",
            "signed-nan", "signed-nan-critical", "overflow", "negative-overflow", "overflow-1e999",
            "unselected-severity", "empty-severity", "disable-with-severity", "go-space",
            "unknown-key-non-finite"])
    def test_values_the_exporter_refuses_as_unreadable_are_not_backtested(self, key, raw):
        """served-values 說 exporter 讀不懂（`keys[].refused` "unparsed"，或沒有已知預設
        值的 key 寫非有限值）：不回測，結束碼與 PR2 前相同。"""
        with pytest.raises(bt._NotRead, match="this backtest cannot read it the way the exporter does"):
            bt._served_written(key, (raw,))

    @pytest.mark.parametrize("raw,served", [("1_000", 1000.0), ("0x1p4", 16.0)])
    def test_a_spelling_the_exporter_reads_but_this_backtest_does_not(
            self, tmp_path, raw, served):
        """`1_000`、`0x1p4`：exporter 讀得懂（served-values 送 1000／16），但 `--tenant`
        照 PR2 之前不回測（`_SPELLINGS_NOT_BACKTESTED`），不在本 PR 改結束碼。"""
        import _lib_tenant_values as tvlib
        bt._written_tree(str(tmp_path), "mysql_connections", (raw,))
        got = tvlib.load_served_tree(str(tmp_path)).tenants[bt._WRITTEN_TENANTS[0]]
        assert got.values["mysql_connections"] == served
        with pytest.raises(bt._NotRead):
            bt._served_written("mysql_connections", (raw,))

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

    def test_two_trees_read_the_critical_suffix(self, monkeypatch, capsys, cli_argv, tmp_path,
                                                da_guard_env):
        """`"95:critical"` 不是 0 個越界點；兩棵樹由 exporter 讀，每條 /metrics row
        一列：`70` → `95:critical` 是 warning 那條沒了、critical 那條出現。"""
        defaults = {"_defaults.yaml": "defaults:\n  mysql_connections: 80\n  pg_connections: 80\n"}
        before = "tenants:\n  t-x:\n    mysql_connections: '70:critical'\n    pg_connections: '70'\n"
        after = "tenants:\n  t-x:\n    mysql_connections: '95:critical'\n    pg_connections: '95:critical'\n"
        cur, base = _dirs(tmp_path, {**defaults, "t-x.yaml": before}, {**defaults, "t-x.yaml": after})
        rc, rows, asked = self._main(monkeypatch, capsys, cli_argv,
                                     ["--config-dir", cur, "--baseline", base])
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


def _dirs_for(tmp_path, *tenants):
    """Two conf.d trees: each tenant's mysql_connections 100 → 50."""
    def tree(value):
        return {"_defaults.yaml": _DEFAULTS,
                **{f"{t}.yaml": f"tenants:\n  {t}:\n    mysql_connections: '{value}'\n"
                   for t in tenants}}
    return _dirs(tmp_path, tree(100), tree(50))


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
        cur, base = _dirs_for(self._tmp, *tenants)
        self._src = ("--config-dir", cur, "--baseline", base)

    @pytest.fixture(autouse=True)
    def _tmp_and_da_guard(self, tmp_path, da_guard_env):
        self._tmp = tmp_path

    def test_other_changes_still_run_and_query_error_beats_high(
            self, fake_prom, monkeypatch, cli_argv, capsys):
        p = fake_prom(errors={"t-bad": 400})
        self._git(monkeypatch, "t-ok", "t-bad")
        rc, out = _run_main(cli_argv, capsys, *self._src, "--prometheus", p.url, "--json")
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
        rc, out = _run_main(cli_argv, capsys, *self._src, "--prometheus", p.url, "--json")
        doc = json.loads(out.out)
        assert rc == 1 and "status" not in doc and doc["query_error"] == 0

    def test_skip_if_unavailable_does_not_hide_query_error(
            self, fake_prom, monkeypatch, cli_argv, capsys):
        p = fake_prom(errors={"t-bad": 503})
        self._git(monkeypatch, "t-bad")
        rc, out = _run_main(cli_argv, capsys, *self._src, "--prometheus", p.url,
                            "--skip-if-unavailable", "--json")
        assert rc == 2
        assert json.loads(out.out)["status"] == "partial"

    def test_text_and_markdown_name_the_unmeasured(
            self, fake_prom, monkeypatch, cli_argv, capsys, tmp_path):
        p = fake_prom(errors={"t-bad": 400})
        self._git(monkeypatch, "t-ok", "t-bad")
        md = tmp_path / "out.md"
        rc, out = _run_main(cli_argv, capsys, *self._src, "--prometheus", p.url,
                            "--markdown-output", str(md))
        assert rc == 2
        assert "Query error: 1" in out.out
        assert "1 change(s) not measured" in md.read_text(encoding="utf-8")

    def test_message_is_escaped_and_truncated(self, monkeypatch, cpu_mapped):
        def failing(*a, **kw):
            raise bt.QueryError("line1\nFAKE [PASS]\x1b[2J" + "x" * 1000)
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
    base = tmp / "recipe-base"  # the same tenant before its recipe was added
    base.mkdir()
    (base / "ta.yaml").write_text("tenants:\n  ta:\n    mysql_connections: 100\n",
                                  encoding="utf-8")
    return ["--config-dir", str(d), "--baseline", str(base),
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


def _path_baseline_missing(mp, tmp, prom):
    mp.setattr(bt, "prometheus_available", _boom)
    return ["--config-dir", str(_plain_confd(tmp)), "--prometheus", "http://prom:9090"]


def _path_served_values(mp, tmp, prom):
    def missing(*a, **kw):
        raise bt.DaGuardNotFoundError("no da-guard")
    mp.setattr(bt, "load_trees", missing)
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
    ("baseline-missing", _path_baseline_missing, 2, ("caller_error", "baseline_missing"),
     ("Backtest not run", "baseline_missing")),
    ("served-values", _path_served_values, 2, ("caller_error", "served_values_unavailable"),
     ("Backtest not run", "served_values_unavailable")),
    # #3111: da-guard names an undecodable file on either side.
    ("conf-unreadable", _path_conf_unreadable, 2, ("caller_error", "served_values_unavailable"),
     ("Backtest not run", "served_values_unavailable")),
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
        """樹、observed-map、Prometheus 一個都沒碰就停（每個都換成 `_boom`）。"""
        for name in ("_observed_map", "prometheus_available", "query_range",
                     "extract_changes_from_dirs", "load_trees"):
            monkeypatch.setattr(bt, name, _boom)
        bad = _unwritable(tmp_path, "missing_parent", "r.out")
        rc, out = _run_main(cli_argv, capsys, "--config-dir", str(tmp_path), "--baseline",
                            str(tmp_path), "--prometheus", "http://prom:9090",
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


def _write_confd(confd, files):
    """`files`: {relative name (str or bytes): body (str or bytes)}; written as BYTES."""
    root = os.fsencode(str(confd))
    os.makedirs(root, exist_ok=True)
    for name, body in files.items():
        rel = name if isinstance(name, bytes) else name.encode("utf-8")
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(body if isinstance(body, bytes) else body.encode("utf-8"))


def _dirs(tmp_path, before, after):
    """(current, baseline): two conf.d trees, `after` and `before`, as strings."""
    base, cur = tmp_path / "base" / "conf.d", tmp_path / "cur" / "conf.d"
    _write_confd(base, before)
    _write_confd(cur, after)
    return str(cur), str(base)


_D = {"_defaults.yaml": _CRIT_DEFAULTS}


def _tenant_file(tenant, body):
    return f"tenants:\n  {tenant}:\n{body}"


@pytest.mark.usefixtures("da_guard_env")
class TestTenantAndTreesAgree:
    """#3111：同一個編輯，`--tenant` 與 `--config-dir/--baseline` 報同樣的列（一條規則）。"""

    def _rows(self, out, *fields):
        return [tuple(r.get(f) for f in fields) for r in json.loads(out.out)["changes"]]

    @pytest.mark.parametrize("key", ["mysql_connections", 'mysql_connections{db="a"}'])
    @pytest.mark.parametrize("old,new", [("70", "70:critical"), ("70", "95:critical"),
                                         ("70:critical", "60"), ("70", "60"),
                                         ("70", "disable"), ("disable", "70:critical")])
    def test_same_rows(self, tmp_path, cli_argv, capsys, fake_prom, key, old, new):
        fields = ("metric", "old_value", "new_value", "risk", "message")
        p = fake_prom(values=(60, 80, 90, 100))
        _rc, out = _run_main(cli_argv, capsys, "--tenant", "db-a", "--metric", key,
                             "--old-value", old, "--new-value", new,
                             "--prometheus", p.url, "--json")
        tenant = self._rows(out, *fields)
        cur, base = _dirs(tmp_path, {**_D, "db-a.yaml": _tenant_file("db-a", f"    '{key}': \"{old}\"\n")},
                          {**_D, "db-a.yaml": _tenant_file("db-a", f"    '{key}': \"{new}\"\n")})
        _rc, out = _run_main(cli_argv, capsys, "--config-dir", cur, "--baseline", base,
                             "--prometheus", p.url, "--json")
        assert tenant == self._rows(out, *fields) and tenant, (tenant, out.out)

    @pytest.mark.parametrize("key,old,new,risk", [
        ("mysql_connections_critical", "95", "+Inf", "HIGH"),
        ("mysql_connections_critical", "95", "NaN", "HIGH"),
        ("mysql_connections", "70", "NaN", "MEDIUM"),
        ("mysql_connections_critical", "95", "disable", "MEDIUM"),
    ])
    def test_a_refused_value_reads_the_same_from_both(self, tmp_path, cli_argv, capsys, fake_prom,
                                                     key, old, new, risk):
        """#3140 PR2：被拒收的非有限值，`--tenant` 讀 `keys[].refused`、兩棵樹讀
        schedule／keys 的 `value_non_finite`——同一個 exporter 答案、同一列、同一個風險。"""
        fields = ("metric", "old_value", "new_value", "risk", "message")
        p = fake_prom(values=(60, 80, 90, 100))
        rc_t, out = _run_main(cli_argv, capsys, "--tenant", "db-a", "--metric", key,
                              "--old-value", old, f"--new-value={new}",
                              "--prometheus", p.url, "--json")
        tenant = self._rows(out, *fields)
        cur, base = _dirs(tmp_path, {**_D, "db-a.yaml": _tenant_file("db-a", f"    '{key}': \"{old}\"\n")},
                          {**_D, "db-a.yaml": _tenant_file("db-a", f"    '{key}': \"{new}\"\n")})
        rc_d, out = _run_main(cli_argv, capsys, "--config-dir", cur, "--baseline", base,
                              "--prometheus", p.url, "--json")
        assert tenant == self._rows(out, *fields) and tenant, (tenant, out.out)
        assert [r[3] for r in tenant] == [risk]
        assert rc_t == rc_d == (1 if risk == "HIGH" else 0)


@pytest.mark.usefixtures("da_guard_env")
class TestRefusedRowsFromTwoTrees:
    """#3140 PR2：兩棵樹時，row 在 PR 側消失的原因讀 served-values：被拒收的非有限值
    （`value_non_finite`）判 HIGH；讀不懂的值（`value_unparsed`）與 `disable` 維持
    「Metric disabled」MEDIUM（結束碼與 PR2 前相同）。"""

    def _run(self, tmp_path, cli_argv, capsys, fake_prom, before, after):
        p = fake_prom(values=(60, 80, 90, 100))
        cur, base = _dirs(tmp_path, {**_D, "db-a.yaml": _tenant_file("db-a", before)},
                          {**_D, "db-a.yaml": _tenant_file("db-a", after)})
        rc, out = _run_main(cli_argv, capsys, "--config-dir", cur, "--baseline", base,
                            "--prometheus", p.url, "--json")
        return rc, [(r["metric"], r["old_value"], r["new_value"], r["risk"], r.get("window"))
                    for r in json.loads(out.out)["changes"]]

    @pytest.mark.parametrize("new,risk,rc", [
        ("'NaN'", "HIGH", 1), ("'-Inf'", "HIGH", 1),
        ("'7O'", "MEDIUM", 0), (".nan", "MEDIUM", 0), ("'disable'", "MEDIUM", 0)])
    def test_why_the_critical_row_is_gone(self, tmp_path, cli_argv, capsys, fake_prom, new, risk, rc):
        """`.nan` 不加引號：exporter 讀成讀不懂的字串（`value_unparsed`），不是非有限數。"""
        got = self._run(tmp_path, cli_argv, capsys, fake_prom,
                        "    mysql_connections_critical: '95'\n",
                        f"    mysql_connections_critical: {new}\n")
        assert got == (rc, [("mysql_connections_critical", "95", None, risk, None)])

    @pytest.mark.parametrize("root", [".nan", ".inf", "-.inf"])
    def test_a_row_lost_to_a_non_finite_root_default_is_high(
            self, tmp_path, cli_argv, capsys, fake_prom, root):
        """根 `defaults:` 改成非有限數、租戶沒寫這個 key：沒有預設列，served-values
        標 `root_default_non_finite`，判 HIGH（不是「Metric disabled」MEDIUM）。"""
        p = fake_prom(values=(60, 80, 90, 100))
        tenant = {"db-a.yaml": _tenant_file("db-a", "    _silent_mode: \"false\"\n")}
        cur, base = _dirs(tmp_path, {**_D, **tenant},
                          {"_defaults.yaml": _CRIT_DEFAULTS.replace("mysql_connections: 80",
                                                                    f"mysql_connections: {root}"),
                           **tenant})
        rc, out = _run_main(cli_argv, capsys, "--config-dir", cur, "--baseline", base,
                            "--prometheus", p.url, "--json")
        rows = [(r["metric"], r["old_value"], r["new_value"], r["risk"])
                for r in json.loads(out.out)["changes"]]
        assert (rc, rows) == (1, [("mysql_connections", "80", None, "HIGH")]), out.out

    def test_a_non_finite_root_default_under_a_retired_alias_is_high(
            self, tmp_path, cli_argv, capsys, fake_prom):
        """根檔仍寫退役別名（`mysql_cpu`，正名 `mysql_threads_running`）：served-values
        以正名標 `root_default_non_finite`，所以照樣判 HIGH（審查 Finding A）。"""
        p = fake_prom(values=(60, 80, 90, 100))
        tenant = {"db-a.yaml": _tenant_file("db-a", "    _silent_mode: \"false\"\n")}
        cur, base = _dirs(tmp_path,
                          {"_defaults.yaml": _CRIT_DEFAULTS + "  mysql_cpu: 80\n", **tenant},
                          {"_defaults.yaml": _CRIT_DEFAULTS + "  mysql_cpu: .nan\n", **tenant})
        rc, out = _run_main(cli_argv, capsys, "--config-dir", cur, "--baseline", base,
                            "--prometheus", p.url, "--json")
        rows = [(r["metric"], r["old_value"], r["new_value"], r["risk"])
                for r in json.loads(out.out)["changes"]]
        assert (rc, rows) == (1, [("mysql_threads_running", "80", None, "HIGH")]), out.out

    def test_a_refusal_in_part_of_the_day_is_high_in_that_window(
            self, tmp_path, cli_argv, capsys, fake_prom):
        """排程裡只有夜間時段寫 NaN：那段的 segment 帶 `value_non_finite`，只有那段判 HIGH。"""
        sched = ("    mysql_connections_critical:\n      default: \"95\"\n      overrides:\n"
                 "        - window: \"01:00-09:00\"\n          value: \"{night}\"\n")
        got = self._run(tmp_path, cli_argv, capsys, fake_prom,
                        sched.format(night="90"), sched.format(night="NaN"))
        assert got == (1, [("mysql_connections_critical", "90", None, "HIGH", ["01:00-09:00"])])


@pytest.mark.usefixtures("da_guard_env")
class TestTwoTreesCompare:
    """#3111：`--git-diff` 移除後，PR 與其 base 一律以兩棵 conf.d 比（呼叫端自己
    checkout base）。原本 `--git-diff` 測試對「比對本身」的涵蓋改在這裡：只換寫法、
    severity 移動、改名、子目錄、刪檔、隱藏檔、沒有 config 檔的樹。"""

    def _run(self, cli_argv, capsys, fake_prom, cur, base):
        p = fake_prom()
        rc, out = _run_main(cli_argv, capsys, "--config-dir", cur, "--baseline", base,
                            "--prometheus", p.url, "--json")
        return rc, json.loads(out.out), out.err

    def _changes(self, doc):
        return sorted((c["tenant"], c["metric"], c["old_value"], c["new_value"])
                      for c in doc["changes"])

    def test_spelling_only_change_is_no_change(self, tmp_path, cli_argv, capsys, fake_prom):
        cur, base = _dirs(
            tmp_path,
            {**_D, "db-a.yaml": _tenant_file("db-a", '    mysql_connections: "90:critical"\n')},
            {**_D, "db-a.yaml": _tenant_file("db-a", "    mysql_connections: disable\n"
                                                     '    mysql_connections_critical: "90"\n')})
        rc, doc, err = self._run(cli_argv, capsys, fake_prom, cur, base)
        assert (rc, doc["status"]) == (0, "no_changes"), doc

    def test_severity_move_is_two_rows(self, tmp_path, cli_argv, capsys, fake_prom):
        cur, base = _dirs(
            tmp_path, {**_D, "db-a.yaml": _tenant_file("db-a", '    mysql_connections: "70"\n')},
            {**_D, "db-a.yaml": _tenant_file("db-a", '    mysql_connections: "70:critical"\n')})
        rc, doc, _ = self._run(cli_argv, capsys, fake_prom, cur, base)
        assert self._changes(doc) == [("db-a", "mysql_connections", "70", None),
                                      ("db-a", "mysql_connections_critical", None, "70")]

    def test_tenant_comes_from_tenants_not_the_file_name(self, tmp_path, cli_argv, capsys,
                                                         fake_prom):
        cur, base = _dirs(
            tmp_path, {**_D, "db-a.yaml": _tenant_file("db-a", "    mysql_connections: 70\n")},
            {**_D, "renamed.yaml": _tenant_file("db-a", "    mysql_connections: 50\n")})
        rc, doc, _ = self._run(cli_argv, capsys, fake_prom, cur, base)
        assert self._changes(doc) == [("db-a", "mysql_connections", "70", "50")]

    def test_rename_alone_is_no_change(self, tmp_path, cli_argv, capsys, fake_prom):
        body = _tenant_file("db-a", "    mysql_connections: 70\n")
        cur, base = _dirs(tmp_path, {**_D, "db-a.yaml": body}, {**_D, "sub/moved.yml": body})
        rc, doc, _ = self._run(cli_argv, capsys, fake_prom, cur, base)
        assert (rc, doc["status"]) == (0, "no_changes")

    def test_deleted_added_and_nested_carriers(self, tmp_path, cli_argv, capsys, fake_prom):
        cur, base = _dirs(
            tmp_path,
            {**_D, "gone.yaml": _tenant_file("t-gone", "    mysql_connections: 70\n"),
             "deep/er/t.yaml": _tenant_file("t-deep", "    mysql_connections: 70\n")},
            {**_D, "deep/er/t.yaml": _tenant_file("t-deep", "    mysql_connections: 60\n"),
             "new/t.yaml": _tenant_file("t-new", "    mysql_connections: 50\n")})
        rc, doc, _ = self._run(cli_argv, capsys, fake_prom, cur, base)
        changes = self._changes(doc)
        assert ("t-deep", "mysql_connections", "70", "60") in changes
        assert ("t-gone", "mysql_connections", "70", None) in changes
        assert ("t-new", "mysql_connections", None, "50") in changes
        assert ("t-new", "mongodb_connections_current", None, "300") in changes

    def test_hidden_files_and_directories_are_not_read(self, tmp_path, cli_argv, capsys,
                                                      fake_prom):
        """The exporter skips `.`-prefixed files and directories."""
        hidden = {".old.yaml": _tenant_file("t-h", "    mysql_connections: 1\n"),
                  ".archive/x.yaml": _tenant_file("t-h2", "    mysql_connections: 1\n")}
        cur, base = _dirs(
            tmp_path, {**_D, "db-a.yaml": _tenant_file("db-a", "    mysql_connections: 70\n")},
            {**_D, **hidden, "db-a.yaml": _tenant_file("db-a", "    mysql_connections: 50\n")})
        rc, doc, _ = self._run(cli_argv, capsys, fake_prom, cur, base)
        assert self._changes(doc) == [("db-a", "mysql_connections", "70", "50")]

    @pytest.mark.parametrize("side", ["baseline", "current"])
    def test_a_tree_with_no_config_is_rc2_named(self, tmp_path, cli_argv, capsys, fake_prom,
                                                side):
        """A tree holding no config file is one the exporter refuses ("no .yaml
        files found"): rc 2, named — a PR that adds the first conf.d/ compares
        against a baseline holding at least a `_defaults.yaml`."""
        empty = {"README.md": "x\n"}
        full = {**_D, "db-a.yaml": _tenant_file("db-a", "    mysql_connections: 50\n")}
        cur, base = _dirs(tmp_path, empty if side == "baseline" else full,
                          empty if side == "current" else full)
        rc, doc, err = self._run(cli_argv, capsys, fake_prom, cur, base)
        assert (rc, doc["reason"]) == (2, "served_values_unavailable"), (doc, err)
        assert "no .yaml files found" in err

    @pytest.mark.parametrize("name", [
        b"has\nnewline.yaml", b'has"quote.yaml', b"has\\backslash.yaml", b"has space.yaml",
        "İSTANBUL.YAML".encode("utf-8"), b"legacy-\xff.yaml"])
    def test_any_file_name_is_read(self, tmp_path, cli_argv, capsys, fake_prom, name):
        """The tenant is the `tenants:` key the exporter reads; a file's name is
        only a path."""
        require_file_name(name)
        cur, base = _dirs(
            tmp_path, {**_D, name: _tenant_file("acme", "    mysql_connections: 80\n")},
            {**_D, name: _tenant_file("acme", "    mysql_connections: 50\n")})
        rc, doc, err = self._run(cli_argv, capsys, fake_prom, cur, base)
        assert self._changes(doc) == [("acme", "mysql_connections", "80", "50")], err


_RECIPE = ("    _custom_alerts:\n      - recipe: threshold\n        name: q\n"
           "        metric: mysql_global_status_threads_connected\n        op: '>'\n"
           "        window: 5m\n        threshold: '{v}:warning'\n")


@pytest.mark.usefixtures("da_guard_env")
class TestRecipeNoteFromTheWholeTree:
    """#657 / #3111 / #3155: the recipe NOTE names the tenants whose custom-alert
    recipes CHANGED between `--baseline` and `--config-dir`, read as the
    exporter serves them (any file it takes a tenant from, `_platform.yaml`
    and sub-directories included) — not every recipe tenant of the tree."""

    def _note(self, monkeypatch, cli_argv, capsys, tmp_path, before, after, *extra):
        cur, base = _dirs(tmp_path, {**_D, **before}, {**_D, **after})
        monkeypatch.setattr(bt, "prometheus_available", lambda url, timeout=5: False)
        rc, out = _run_main(cli_argv, capsys, "--config-dir", cur, "--baseline", base,
                            "--prometheus", "http://prom:9090", "--skip-if-unavailable", *extra)
        notes = [ln for ln in out.err.splitlines() if ln.startswith("NOTE:")]
        return rc, notes, out

    def _t(self, tenant, body):
        return _tenant_file(tenant, body)

    def test_only_changed_recipe_tenants_are_named(self, monkeypatch, cli_argv, capsys,
                                                   tmp_path):
        same = self._t("db-b", _RECIPE.format(v=150))
        before = {"db-a.yaml": self._t("db-a", "    mysql_connections: 70\n"),
                  "db-b.yaml": same,
                  "chg.yaml": self._t("chg", _RECIPE.format(v=150)),
                  "gone.yaml": self._t("gone", _RECIPE.format(v=150)),
                  "moved.yaml": self._t("moved", _RECIPE.format(v=150))}
        after = {"db-a.yaml": self._t("db-a", "    mysql_connections: 50\n"),
                 "db-b.yaml": same,
                 "chg.yaml": self._t("chg", _RECIPE.format(v=200)),
                 "gone.yaml": self._t("gone", "    mysql_connections: 70\n"),
                 "deep/moved.yaml": self._t("moved", _RECIPE.format(v=150)),
                 "team/new.yaml": self._t("new", _RECIPE.format(v=150))}
        _rc, notes, _ = self._note(monkeypatch, cli_argv, capsys, tmp_path, before, after)
        assert len(notes) == 1 and notes[0].endswith("(_custom_alerts): chg, gone, new"), notes

    def test_an_untouched_recipe_tenant_is_not_named(self, monkeypatch, cli_argv, capsys,
                                                     tmp_path):
        """Every conf.d PR's comment used to carry every recipe tenant."""
        same = {"db-b.yaml": self._t("db-b", _RECIPE.format(v=150))}
        _rc, notes, _ = self._note(
            monkeypatch, cli_argv, capsys, tmp_path,
            {**same, "db-a.yaml": self._t("db-a", "    mysql_connections: 70\n")},
            {**same, "db-a.yaml": self._t("db-a", "    mysql_connections: 50\n")})
        assert notes == []

    def test_a_tenant_added_or_removed_without_recipes_is_not_named(
            self, monkeypatch, cli_argv, capsys, tmp_path):
        """A tenant on one side only is compared with "no recipes"."""
        _rc, notes, out = self._note(
            monkeypatch, cli_argv, capsys, tmp_path,
            {"gone.yaml": self._t("gone", "    mysql_connections: 70\n")},
            {"new.yaml": self._t("new", "    mysql_connections: 70\n")})
        assert notes == [], out.err

    def test_a_recipe_in_the_platform_file_is_named(self, monkeypatch, cli_argv, capsys,
                                                    tmp_path):
        """The exporter reads `tenants.<id>._custom_alerts` from `_platform.yaml`."""
        tp = {"tp.yaml": "tenants:\n  tp: {}\n"}
        _rc, notes, _ = self._note(
            monkeypatch, cli_argv, capsys, tmp_path, tp,
            {**tp, "_platform.yaml": "tenants:\n  tp:\n" + _RECIPE.format(v=150)})
        assert len(notes) == 1 and notes[0].endswith("(_custom_alerts): tp"), notes

    def test_tenant_ids_are_escaped_in_the_note_and_the_markdown(
            self, monkeypatch, cli_argv, capsys, tmp_path):
        """A tenant id is the `tenants:` key, which may hold anything YAML allows."""
        tid = "a`b[x](http://e)\x1bc\nd"
        quoted = '"a`b[x](http://e)\\ec\\nd"'
        md = tmp_path / "r.md"
        _rc, notes, out = self._note(
            monkeypatch, cli_argv, capsys, tmp_path, {"t.yaml": f"tenants:\n  {quoted}: {{}}\n"},
            {"t.yaml": f"tenants:\n  {quoted}:\n" + _RECIPE.format(v=150)},
            "--markdown-output", str(md))
        assert "\x1b" not in out.err and tid not in out.err, out.err
        assert len(notes) == 1, out.err
        text = md.read_text(encoding="utf-8")
        assert "\x1b" not in text and "](http" in text
        line = next(ln for ln in text.splitlines() if "changed custom-alert recipes" in ln)
        # A fence longer than the id's backtick run, so the span holds the whole id.
        assert line.endswith(": ``a`b[x](http://e)?c?d``"), line

    def test_a_pipe_in_a_tenant_id_is_not_escaped_outside_a_table(
            self, monkeypatch, cli_argv, capsys, tmp_path):
        """0bd6f362: the recipe line is running text, not a table row, so the
        table cell's `\\|` was shown as written."""
        md = tmp_path / "r.md"
        self._note(monkeypatch, cli_argv, capsys, tmp_path, {"t.yaml": "tenants:\n  a|b: {}\n"},
                   {"t.yaml": "tenants:\n  a|b:\n" + _RECIPE.format(v=150)},
                   "--markdown-output", str(md))
        line = next(ln for ln in md.read_text(encoding="utf-8").splitlines()
                    if "changed custom-alert recipes" in ln)
        assert line.endswith(": `a|b`"), line

    _BOGUS = ("    _custom_alerts:\n      - recipe: {r}\n        name: x\n        metric: m\n"
              "        op: '>'\n        window: 5m\n        threshold: '1:warning'\n")

    @pytest.mark.parametrize("before, after, named", [
        # c6: a list the exporter refuses whole is added.
        ("", _BOGUS.format(r="bogus"), True),
        # c6 reversed: the refused list is removed.
        (_BOGUS.format(r="bogus"), "", True),
        # c3: refused on both sides, with different content.
        (_BOGUS.format(r="bogus1"), _BOGUS.format(r="bogus2"), True),
        # control: refused on both sides, unchanged.
        (_BOGUS.format(r="bogus"), _BOGUS.format(r="bogus"), False),
    ], ids=["refused-added", "refused-removed", "refused-changed", "refused-unchanged"])
    def test_a_recipe_list_the_exporter_refuses_is_compared(
            self, monkeypatch, cli_argv, capsys, tmp_path, before, after, named):
        """0bd6f362: only the served rows were compared, so a recipe change
        the exporter refuses whole (it serves no row either side) was not
        named."""
        base = "    mysql_connections: 70\n"
        _rc, notes, out = self._note(
            monkeypatch, cli_argv, capsys, tmp_path,
            {"db-a.yaml": self._t("db-a", base + before)},
            {"db-a.yaml": self._t("db-a", base + after)})
        if named:
            assert len(notes) == 1 and notes[0].endswith("(_custom_alerts): db-a"), out.err
        else:
            assert notes == [], out.err

    @pytest.mark.parametrize("extra", [
        # c7: an unknown recipe beside a valid one.
        _BOGUS.format(r="bogus").replace("    _custom_alerts:\n", ""),
        # c20: a duplicate of the valid one.
        "      - recipe: threshold\n        name: q2\n"
        "        metric: mysql_global_status_threads_connected\n        op: '>'\n"
        "        window: 5m\n        threshold: '150:warning'\n",
    ], ids=["unknown-recipe", "duplicate"])
    def test_a_row_refused_beside_served_rows_is_not_named(
            self, monkeypatch, cli_argv, capsys, tmp_path, extra):
        """Documented limit: a row the exporter refuses while it serves the
        others leaves the served rows and `unserved` as they were; da-guard
        reports it on stderr only (which is not parsed), so the tenant is not
        named. Its stderr line still reaches the log."""
        good = self._t("db-b", _RECIPE.format(v=150))
        _rc, notes, out = self._note(monkeypatch, cli_argv, capsys, tmp_path,
                                     {"db-b.yaml": good}, {"db-b.yaml": good + extra})
        assert notes == [], out.err
        assert "rejected" in out.err, out.err

    _R1 = ("    _custom_alerts:\n      - recipe: threshold\n        name: r1\n        metric: m1\n"
           "        op: '>'\n        window: 5m\n        threshold: '1:warning'\n")
    _R2 = ("      - recipe: threshold\n        name: r2\n        metric: m2\n"
           "        op: '>'\n        window: 5m\n        threshold: '{v}:warning'\n")

    def _capped(self, monkeypatch, cli_argv, capsys, tmp_path, before, after):
        # A limit of 1 metric per tenant: r1 is served, any later recipe is cut.
        capped = {"_defaults.yaml": _CRIT_DEFAULTS + "max_metrics_per_tenant: 1\n"}
        head = "    mysql_connections: '70'\n"
        return self._note(monkeypatch, cli_argv, capsys, tmp_path,
                          {**capped, "db-a.yaml": self._t("db-a", head + before)},
                          {**capped, "db-a.yaml": self._t("db-a", head + after)})

    def test_a_recipe_added_past_the_metric_limit_is_named(self, monkeypatch, cli_argv, capsys,
                                                         tmp_path):
        """bbf0421d: the served rows are what the limit leaves, so r2 added
        past it changed nothing compared; `custom_alert_rows` counts it."""
        _rc, notes, out = self._capped(monkeypatch, cli_argv, capsys, tmp_path,
                                       self._R1, self._R1 + self._R2.format(v=2))
        assert len(notes) == 1 and notes[0].endswith("(_custom_alerts): db-a"), out.err

    def test_a_value_changed_in_a_recipe_past_the_limit_is_not_named(
            self, monkeypatch, cli_argv, capsys, tmp_path):
        """Documented limit: served-values counts the cut rows but does not
        show them."""
        _rc, notes, out = self._capped(monkeypatch, cli_argv, capsys, tmp_path,
                                       self._R1 + self._R2.format(v=2),
                                       self._R1 + self._R2.format(v=3))
        assert (_rc, notes) == (0, []), out.err

    def test_a_recipe_at_the_top_of_the_root_defaults_is_not_named(
            self, monkeypatch, cli_argv, capsys, tmp_path):
        """Documented limit: the compiler reads a `_defaults.yaml`'s top-level
        `_custom_alerts` (root or sub-directory); the exporter serves no row
        for it."""
        tenant = {"db-a.yaml": self._t("db-a", "    mysql_connections: 70\n")}
        top = "_custom_alerts:\n" + self._R1.replace("    _custom_alerts:\n", "").replace(
            "\n    ", "\n")[4:]
        _rc, notes, out = self._note(monkeypatch, cli_argv, capsys, tmp_path, tenant,
                                     {**tenant, "_defaults.yaml": _CRIT_DEFAULTS + top})
        assert (_rc, notes) == (0, []), out.err

    @pytest.mark.parametrize("empty", ["[]", "", "null"])
    def test_an_empty_list_is_no_recipe(self, monkeypatch, cli_argv, capsys, tmp_path, empty):
        """bbf0421d: removing `_custom_alerts: []` named the tenant, though
        neither side serves or compiles a recipe."""
        head = "    mysql_connections: 70\n"
        _rc, notes, out = self._note(
            monkeypatch, cli_argv, capsys, tmp_path,
            {"db-a.yaml": self._t("db-a", head + f"    _custom_alerts: {empty}\n")},
            {"db-a.yaml": self._t("db-a", head)})
        assert (_rc, notes) == (0, []), out.err

    def test_each_tree_is_read_by_da_guard_once(self, monkeypatch, cli_argv, capsys, tmp_path):
        """0bd6f362 ran da-guard on each tree twice (recipes, then values)."""
        calls = []
        real = bt.load_served_tree
        monkeypatch.setattr(bt, "load_served_tree",
                            lambda *a, **kw: calls.append(a) or real(*a, **kw))
        self._note(monkeypatch, cli_argv, capsys, tmp_path,
                   {"db-a.yaml": self._t("db-a", "    mysql_connections: 70\n")},
                   {"db-a.yaml": self._t("db-a", "    mysql_connections: 50\n")})
        assert len(calls) == 2, calls

    @pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() == 0,
                        reason="as root the permission bits decide nothing")
    def test_an_unreadable_file_under_config_dir_is_named(self, monkeypatch, cli_argv, capsys,
                                                          tmp_path):
        """73c1e2da: a PermissionError traceback, rc 2 `internal_error`."""
        cur, base = _dirs(tmp_path, {**_D, "t.yaml": self._t("t", "    mysql_connections: 1\n")},
                          {**_D, "t.yaml": self._t("t", "    mysql_connections: 2\n")})
        os.chmod(os.path.join(cur, "t.yaml"), 0)
        try:
            rc, out = _run_main(cli_argv, capsys, "--config-dir", cur, "--baseline", base,
                                "--prometheus", "http://prom:9090", "--json")
        finally:
            os.chmod(os.path.join(cur, "t.yaml"), 0o600)
        assert (rc, json.loads(out.out)["reason"]) == (2, "served_values_unavailable")
        assert "Traceback" not in out.err and "t.yaml" in out.err



_WORKFLOW = os.path.join(os.path.dirname(__file__), "..", "..", ".github", "workflows",
                         "backtest.yaml")


def _workflow():
    import yaml
    with open(_WORKFLOW, encoding="utf-8") as f:
        return yaml.safe_load(f)


class TestBacktestWorkflow:
    """#3111: the workflow that runs this tool on a PR."""

    def test_the_paths_comment_matches_what_the_tool_says(self):
        """0bd6f362's comment said a tool-only PR gets "no threshold changes
        found."; without a reachable Prometheus the skip comes first."""
        with open(_WORKFLOW, encoding="utf-8") as f:
            text = f.read()
        assert "Not everything the result" in text
        assert bt._NOT_RUN_WHY[("skipped", "prometheus_unavailable")].startswith(
            "Prometheus is unavailable")
        assert '"Prometheus is unavailable"' in text

    def test_it_runs_when_the_tool_or_the_workflow_changes(self):
        """73c1e2da: `paths` listed only conf.d/**, so a PR changing how the
        backtest runs never ran it."""
        doc = _workflow()
        on = doc.get("on", doc.get(True))
        paths = on["pull_request"]["paths"]
        for needed in ("components/threshold-exporter/config/conf.d/**",
                       "scripts/tools/ops/backtest_threshold.py",
                       "scripts/tools/ops/_observed_map_lib.py",
                       ".github/workflows/backtest.yaml"):
            assert needed in paths, (needed, paths)

    # A layout of the base's conf.d/: a file `name`, a directory `name/`, a
    # symlink `name -> target` (target relative to conf.d/, possibly missing);
    # `@link` first makes conf.d/ itself a symlink to a sibling directory.
    _LAYOUTS = [
        (None, False),
        ([], False),
        # 0bd6f362: a conf.d/ holding no config file passed the check, and the
        # run ended rc 2 `served_values_unavailable` ("no .yaml files found").
        (["README.md"], False),
        ([".hidden.yaml"], False),
        ([".git/x.yaml"], False),
        (["notes.yamlx"], False),
        (["_defaults.yaml"], True),
        (["team/db-a.yml"], True),
        (["DB-A.YAML"], True),
        # bbf0421d: a directory or a dangling symlink named like a config file
        # passed the check; a conf.d/ that is a symlink was skipped.
        (["x.yaml/"], False),
        (["x.yaml -> missing.yaml"], False),
        (["real.txt", "x.yaml -> real.txt"], True),
        (["@link", "_defaults.yaml"], True),
    ]
    _LAYOUT_IDS = ["no-dir", "empty", "readme-only", "hidden-only", "hidden-dir", "not-yaml",
                   "yaml", "nested-yml", "upper-case", "dir-named-yaml", "dangling-link",
                   "link-to-file", "confd-is-link"]

    @staticmethod
    def _lay_out(tmp_path, entries):
        confd = tmp_path / ".backtest-base/components/threshold-exporter/config/conf.d"
        if entries is None:
            return confd
        entries = list(entries)
        where = confd
        if entries[:1] == ["@link"]:
            entries = entries[1:]
            where = confd.parent / "real-confd"
            where.mkdir(parents=True)
            symlink_or_skip("real-confd", str(confd))
        else:
            confd.mkdir(parents=True)
        for entry in entries:
            name, _, target = entry.partition(" -> ")
            path = where / name.rstrip("/")
            path.parent.mkdir(parents=True, exist_ok=True)
            if target:
                symlink_or_skip(target, str(path))
            elif name.endswith("/"):
                path.mkdir()
            else:
                path.write_text("defaults:\n  mysql_connections: 80\n", encoding="utf-8")
        return confd

    def _check(self, tmp_path):
        bash = shutil.which("bash")
        if bash is None:
            pytest.skip("needs bash")
        steps = _workflow()["jobs"]["backtest"]["steps"]
        check = next(st for st in steps if st.get("id") == "base")
        out, comment = tmp_path / "gh-output", tmp_path / "comment.md"
        script = check["run"].replace("/tmp/backtest-comment.md", str(comment))
        subprocess.run([bash, "-e", "-c", script], cwd=str(tmp_path), timeout=60, check=True,
                       env={**os.environ, "GITHUB_OUTPUT": str(out)})
        return (out.read_text(encoding="utf-8") if out.exists() else ""), comment

    @pytest.mark.parametrize("entries, has_confd", _LAYOUTS, ids=_LAYOUT_IDS)
    def test_a_base_without_confd_skips_with_a_comment(self, tmp_path, entries, has_confd):
        """73c1e2da: a PR adding the first conf.d/ ended rc 2
        `served_values_unavailable`."""
        steps = _workflow()["jobs"]["backtest"]["steps"]
        run = next(st for st in steps if st.get("id") == "backtest")
        assert run["if"] == "steps.base.outputs.has_confd == 'true'"
        self._lay_out(tmp_path, entries)
        got, comment = self._check(tmp_path)
        if has_confd:
            assert got == "has_confd=true\n" and not comment.exists()
        else:
            assert got == ""
            assert "the PR base has no `conf.d/` config file" in comment.read_text(
                encoding="utf-8")

    @pytest.mark.usefixtures("da_guard_env")
    @pytest.mark.parametrize("entries, has_confd", _LAYOUTS[1:], ids=_LAYOUT_IDS[1:])
    def test_the_check_agrees_with_the_exporter(self, tmp_path, entries, has_confd):
        """The check stands in for "the exporter reads this tree": a layout it
        passes is one `da-guard served-values` reads, and one it skips is one
        da-guard refuses."""
        confd = self._lay_out(tmp_path, entries)
        res = subprocess.run([os.environ["DA_GUARD_BINARY"], "served-values", "--config-dir",
                              str(confd)], capture_output=True, text=True, encoding="utf-8",
                             timeout=120)
        assert (res.returncode == 0) is has_confd, res.stderr
        got, _ = self._check(tmp_path)
        assert (got == "has_confd=true\n") is has_confd
