#!/usr/bin/env python3
"""backtest_threshold：一個叢集一條序列，各自重放（#1200，ADR-006）。

rule pack 把 `tenant:*` 逐 (tenant, da_cluster) 記錄，告警逐叢集觸發。兩個
叢集的資料在同一台 Prometheus 相遇時，`<series>{tenant="t"}` 每個叢集回一列；
先前把各列樣本串成一條，分母變成兩倍、最吵的那個叢集被稀釋。這裡釘住：

  - 票上的三個數字例子（確切數字）
  - 兩個叢集同時超標不會加倍
  - 有 label 與沒 label 的列各自成組
  - 同一個叢集兩列（或多列都沒 label）不重放、也不相加
  - 只有一列時，結果與先前逐欄相同
"""

import json

import pytest

import backtest_threshold as bt  # noqa: E402
from _lib_cluster_identity import CLUSTER_IDENTITY_LABEL  # noqa: E402
from _lib_exitcodes import emit_result  # noqa: E402

_KEY = "mysql_connection_usage_percent"          # 告警比 `>`
_SERIES = "tenant:mysql_connection_usage:percent"
_QUERY = _SERIES + '{tenant="t-x"}'


def _row(cluster, vals):
    """一個叢集的一列；`cluster=None` 是沒有 da_cluster label 的列。"""
    metric = {"__name__": _SERIES, "tenant": "t-x"}
    if cluster is not None:
        metric[CLUSTER_IDENTITY_LABEL] = cluster
    return {"metric": metric, "values": [(i, str(v)) for i, v in enumerate(vals)]}


def _backtest(monkeypatch, rows, old, new, metric=_KEY):
    monkeypatch.setattr(bt, "query_range", lambda *a, **kw: rows)
    change = {"tenant": "t-x", "metric": metric, "old_value": old, "new_value": new}
    return bt.backtest_change("http://prom", change, 86400)


_EAST_QUIET = [50] * 100
_WEST_15 = [90] * 15 + [50] * 85


class TestTheThreeExamples:
    """票上的三個例子，數字照寫。"""

    def test_newly_enabled_is_the_worst_clusters_share(self, monkeypatch):
        """100 步，east 恆為 50，west 前 15 步為 90，新門檻 80：先前報
        `15/200`（7.5%，MEDIUM）；真正會響的是 west 的告警，15/100（HIGH）。"""
        r = _backtest(monkeypatch, [_row("east", _EAST_QUIET), _row("west", _WEST_15)],
                      None, "80")
        assert r == {
            "tenant": "t-x", "metric": _KEY, "old_value": None, "new_value": "80",
            "status": "analyzed", "query": _QUERY, "risk": "HIGH",
            "data_points": 100, "old_breach_count": 0, "new_breach_count": 15,
            "impact_pct": None, "impact_kind": "newly_enabled",
            "message": ('Metric newly enabled — 15/100 points would fire '
                        '[da_cluster="west", the worst of 2 clusters]'),
            "worst_cluster": "west",
            "clusters": [
                {"da_cluster": "east", "status": "analyzed", "risk": "LOW",
                 "data_points": 100, "old_breach_count": 0, "new_breach_count": 0,
                 "impact_pct": None, "impact_kind": "newly_enabled",
                 "message": "Metric newly enabled — 0/100 points would fire"},
                {"da_cluster": "west", "status": "analyzed", "risk": "HIGH",
                 "data_points": 100, "old_breach_count": 0, "new_breach_count": 15,
                 "impact_pct": None, "impact_kind": "newly_enabled",
                 "message": "Metric newly enabled — 15/100 points would fire"},
            ],
        }

    def test_both_clusters_breaching_is_not_doubled(self, monkeypatch):
        """兩個叢集同一段時間超標：`data_points` 與觸發點數是「一個叢集的時間
        步數」，不是 200 與 30。"""
        r = _backtest(monkeypatch, [_row("east", _WEST_15), _row("west", _WEST_15)],
                      None, "80")
        assert (r["data_points"], r["new_breach_count"]) == (100, 15)
        assert r["message"].startswith("Metric newly enabled — 15/100 points would fire")
        assert [(c["data_points"], c["new_breach_count"]) for c in r["clusters"]] == [
            (100, 15), (100, 15)]
        assert r["worst_cluster"] == "east"   # 同分：依名稱取第一個

    def test_a_cluster_whose_alert_disappears_is_not_diluted(self, monkeypatch):
        """相對變更：east 100→100、west 10→0。串起來是 110→100（−9.1%，LOW）；
        west 的告警整個消失（−100%，HIGH）。"""
        east = [99] * 100                      # 新舊門檻下都響
        west = [90] * 10 + [50] * 90           # 只在舊門檻 80 下響
        r = _backtest(monkeypatch, [_row("east", east), _row("west", west)], "80", "95")
        assert (r["risk"], r["worst_cluster"]) == ("HIGH", "west")
        assert (r["data_points"], r["old_breach_count"], r["new_breach_count"]) == (100, 10, 0)
        assert (r["impact_pct"], r["impact_kind"]) == (-100.0, "relative")
        assert r["message"] == ('10 fewer firing points (-100.0%) '
                                '[da_cluster="west", the worst of 2 clusters]')
        east_entry, west_entry = r["clusters"]
        assert (east_entry["risk"], east_entry["old_breach_count"],
                east_entry["new_breach_count"], east_entry["impact_pct"]) == ("LOW", 100, 100, 0.0)
        assert (west_entry["risk"], west_entry["impact_pct"]) == ("HIGH", -100.0)


class TestWorstCluster:
    def test_the_worst_is_not_the_first(self, monkeypatch):
        """最糟的叢集排在後面、中間、前面，整體都取它。"""
        quiet, noisy = _row("b", _EAST_QUIET), _WEST_15
        for name in ("a", "c"):
            r = _backtest(monkeypatch, [quiet, _row(name, noisy)], None, "80")
            assert (r["risk"], r["worst_cluster"], r["new_breach_count"]) == ("HIGH", name, 15)

    def test_equal_risk_takes_the_larger_change_in_firing_steps(self, monkeypatch):
        """停用：每個叢集都是 MEDIUM，取被靜默最多觸發點的那個。"""
        r = _backtest(monkeypatch, [_row("east", [90] * 3 + [50] * 97),
                                    _row("west", [90] * 40 + [50] * 60)], "80", None)
        assert (r["risk"], r["worst_cluster"], r["old_breach_count"]) == ("MEDIUM", "west", 40)

    def test_lower_bound_key_is_replayed_per_cluster_too(self, monkeypatch):
        """`<` 的告警：方向照 observed-map，逐叢集。"""
        r = _backtest(monkeypatch, [_row("east", [99.5] * 100), _row("west", [90] * 100)],
                      "95", "99.9", metric="db2_bufferpool_hit_ratio")
        assert [(c["da_cluster"], c["old_breach_count"], c["new_breach_count"], c["risk"])
                for c in r["clusters"]] == [("east", 0, 100, "HIGH"), ("west", 100, 100, "LOW")]
        assert (r["worst_cluster"], r["data_points"], r["new_breach_count"]) == ("east", 100, 100)

    def test_a_cluster_with_only_nan_is_listed_and_not_the_worst(self, monkeypatch):
        r = _backtest(monkeypatch, [_row("east", ["NaN"] * 100), _row("west", _WEST_15)],
                      None, "80")
        assert (r["status"], r["worst_cluster"], r["data_points"]) == ("analyzed", "west", 100)
        assert r["clusters"][0] == {
            "da_cluster": "east", "status": "no_data", "risk": "UNKNOWN",
            "message": "Only NaN samples in lookback window"}

    def test_no_cluster_with_data_is_no_data(self, monkeypatch):
        r = _backtest(monkeypatch, [_row("east", ["NaN"] * 5), _row("west", ["NaN"] * 5)],
                      None, "80")
        assert (r["status"], r["risk"]) == ("no_data", "UNKNOWN")
        assert "worst_cluster" not in r and "data_points" not in r
        assert [c["status"] for c in r["clusters"]] == ["no_data", "no_data"]


class TestRowsTheLabelDoesNotTellApart:
    def test_mixed_labelled_and_unlabelled_are_two_groups(self, monkeypatch):
        """沒有 label 的列自成一組，不併進任何叢集。"""
        r = _backtest(monkeypatch, [_row("west", _EAST_QUIET), _row(None, _WEST_15)],
                      None, "80")
        assert [c["da_cluster"] for c in r["clusters"]] == ["", "west"]
        assert (r["worst_cluster"], r["data_points"], r["new_breach_count"]) == ("", 100, 15)
        assert r["message"].endswith("[no da_cluster, the worst of 2 clusters]")

    @pytest.mark.parametrize("rows,named", [
        ([_row("west", _EAST_QUIET), _row("west", _WEST_15)],
         '2 series with da_cluster="west"'),
        ([_row(None, _EAST_QUIET), _row(None, _WEST_15)],
         "2 series without a da_cluster label"),
        ([_row("east", _EAST_QUIET), _row("west", _EAST_QUIET), _row("west", _WEST_15)],
         '2 series with da_cluster="west"'),
        ([_row("", _EAST_QUIET), _row(None, _WEST_15)],       # 空值與沒有 label 相同
         "2 series without a da_cluster label"),
    ], ids=["one-cluster-twice", "unlabelled-twice", "beside-a-good-cluster", "empty-label"])
    def test_two_rows_of_one_cluster_are_not_backtested(self, monkeypatch, rows, named):
        """租戶 recording rule 不該有這種列：不重放、不相加，照既有的
        `not_backtested` 路徑報出原因。"""
        r = _backtest(monkeypatch, rows, None, "80")
        assert (r["status"], r["risk"]) == ("not_backtested", "UNKNOWN")
        assert named in r["message"] and r["message"].startswith("Not backtested: ")
        assert r["backtest"] == "skipped: " + r["message"].removeprefix("Not backtested: ")
        assert r["query"] == _QUERY
        assert "data_points" not in r and "clusters" not in r

    def test_a_row_without_float_samples_is_no_series(self, monkeypatch):
        """native histogram 列沒有 `values`：照舊略過，不算第二條序列。"""
        histogram = {"metric": {"tenant": "t-x"}, "histograms": [[1, {"count": "3"}]]}
        r = _backtest(monkeypatch, [_row(None, _WEST_15), histogram], None, "80")
        assert (r["status"], r["data_points"]) == ("analyzed", 100)
        assert "clusters" not in r


class TestOneRowIsWhatItWas:
    """只有一列（有沒有 da_cluster 都一樣）：欄位與先前逐一相同，沒有新欄位。"""

    _EXPECTED = {
        "tenant": "t-x", "metric": _KEY, "old_value": "70", "new_value": "85",
        "status": "analyzed", "query": _QUERY, "risk": "MEDIUM",
        "data_points": 100, "old_breach_count": 15, "new_breach_count": 10,
        "impact_pct": -33.3, "impact_kind": "relative",
        "message": "5 fewer firing points (-33.3%)",
    }
    _VALS = [90] * 10 + [80] * 5 + [50] * 85

    @pytest.mark.parametrize("cluster", [None, "", "west"])
    def test_fields_and_their_order(self, monkeypatch, cluster):
        r = _backtest(monkeypatch, [_row(cluster, self._VALS)], "70", "85")
        assert r == self._EXPECTED
        assert list(r) == list(self._EXPECTED)

    def test_no_row_is_no_data(self, monkeypatch):
        r = _backtest(monkeypatch, [], "70", "85")
        assert r == {"tenant": "t-x", "metric": _KEY, "old_value": "70", "new_value": "85",
                     "status": "no_data", "risk": "UNKNOWN", "query": _QUERY,
                     "message": "No historical data found in Prometheus"}


class TestReports:
    """文字、Markdown、JSON 與結束碼：超過一個叢集時點名各叢集。"""

    def _cli(self, monkeypatch, capsys, cli_argv, tmp_path, rows, *flags):
        monkeypatch.setattr(bt, "prometheus_available", lambda url, timeout=5: True)
        monkeypatch.setattr(bt, "query_range", lambda *a, **kw: rows)
        md = tmp_path / "out.md"
        cli_argv("backtest_threshold", "--tenant", "t-x", "--metric", _KEY,
                 "--new-value", "80", "--prometheus", "http://prom:9090",
                 "--markdown-output", str(md), *flags)
        with pytest.raises(SystemExit) as ei:
            raise SystemExit(emit_result(bt.main()))
        return ei.value.code, capsys.readouterr().out, md.read_text(encoding="utf-8")

    _TWO = [_row("east", _EAST_QUIET), _row("west", _WEST_15)]

    def test_text_names_the_clusters_and_high_is_rc_1(
            self, monkeypatch, capsys, cli_argv, tmp_path):
        """先前這個輸入是 MEDIUM、結束碼 0。"""
        rc, out, _ = self._cli(monkeypatch, capsys, cli_argv, tmp_path, self._TWO)
        assert rc == 1
        assert "[HIGH  ] t-x/" + _KEY in out
        assert ('Metric newly enabled — 15/100 points would fire '
                '[da_cluster="west", the worst of 2 clusters]') in out
        assert 'da_cluster="east": LOW (0 -> 0 of 100 points fire)' in out
        assert 'da_cluster="west": HIGH (0 -> 15 of 100 points fire)' in out

    def test_markdown_names_the_clusters_in_the_row(
            self, monkeypatch, capsys, cli_argv, tmp_path):
        _, _, md = self._cli(monkeypatch, capsys, cli_argv, tmp_path, self._TWO)
        row = next(line for line in md.splitlines() if line.startswith("| HIGH"))
        assert ('<br>per cluster: `da_cluster="east"` LOW (0 -> 0 of 100 points fire); '
                '`da_cluster="west"` HIGH (0 -> 15 of 100 points fire) |') in row
        assert row.count(" | ") == 6          # 仍是七欄的一列

    def test_json_carries_clusters(self, monkeypatch, capsys, cli_argv, tmp_path):
        rc, out, _ = self._cli(monkeypatch, capsys, cli_argv, tmp_path, self._TWO, "--json")
        doc = json.loads(out)
        assert rc == 1 and doc["risk_summary"] == {"HIGH": 1, "MEDIUM": 0, "LOW": 0}
        change = doc["changes"][0]
        assert change["worst_cluster"] == "west"
        assert [c["da_cluster"] for c in change["clusters"]] == ["east", "west"]

    def test_one_row_reports_have_no_cluster_line(
            self, monkeypatch, capsys, cli_argv, tmp_path):
        rc, out, md = self._cli(monkeypatch, capsys, cli_argv, tmp_path,
                                [_row("west", _WEST_15)])
        assert rc == 1                         # 15/100，本來就是 HIGH
        assert "da_cluster" not in out.replace(_QUERY, "") and "per cluster" not in md

    def test_duplicate_rows_are_reported_as_not_backtested(
            self, monkeypatch, capsys, cli_argv, tmp_path):
        rc, out, md = self._cli(monkeypatch, capsys, cli_argv, tmp_path,
                                [_row("west", _EAST_QUIET), _row("west", _WEST_15)])
        assert rc == 0
        assert 'Not backtested: 1 (2 series with da_cluster="west"' in out
        assert "[UNKNOWN]" in out and "| UNKNOWN |" in md

    def test_a_cluster_name_cannot_break_the_markdown_row(
            self, monkeypatch, capsys, cli_argv, tmp_path):
        """叢集名稱來自 Prometheus：`|` 與換行不得拆開表格列。"""
        rows = [_row("a|b\nc", _EAST_QUIET), _row("west", _WEST_15)]
        _, out, md = self._cli(monkeypatch, capsys, cli_argv, tmp_path, rows)
        row = next(line for line in md.splitlines() if line.startswith("| HIGH"))
        assert "a\\|b?c" in row and row.count(" | ") == 6
        assert 'da_cluster="a|b?c": LOW' in out
