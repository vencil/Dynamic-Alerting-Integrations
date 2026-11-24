#!/usr/bin/env python3
"""backtest_threshold：一個叢集一條序列，各自重放（#1200，ADR-006）。

rule pack 把 `tenant:*` 逐 (tenant, da_cluster) 記錄，告警逐叢集觸發。兩個
叢集的資料在同一台 Prometheus 相遇時，`<series>{tenant="t"}` 每個叢集回一列；
先前把各列樣本串成一條，分母變成兩倍、最吵的那個叢集被稀釋。這裡釘住：

  - 票上的三個數字例子（確切數字）
  - 兩個叢集同時超標不會加倍
  - 有 label 與沒 label 的列各自成組
  - 同一個叢集、時間重疊的兩列（或都沒 label，例如沒去重的 HA pair）不相加，
    算「查了但量不到」：query_error、結束碼 2，量得到的叢集照列（--json 也印）
  - 同一個叢集、時間不重疊的兩列是同一條序列的前後段：接起來，結果與一列相同
  - 只有一列時，結果與先前逐欄相同
"""

import json
import re

import pytest

import backtest_threshold as bt  # noqa: E402
from _lib_cluster_identity import CLUSTER_IDENTITY_LABEL  # noqa: E402
from _lib_exitcodes import CallerError, emit_result, exit_on_crash  # noqa: E402

_KEY = "mysql_connection_usage_percent"          # 告警比 `>`
_SERIES = "tenant:mysql_connection_usage:percent"
_QUERY = _SERIES + '{tenant="t-x"}'
_UNESCAPED_PIPE = re.compile(r"(?<!\\)\|")


def _row(cluster, vals, t0=0, **extra):
    """一個叢集的一列，樣本時間從 `t0` 起每步 1；`cluster=None` 是沒有
    da_cluster label 的列。"""
    metric = {"__name__": _SERIES, "tenant": "t-x", **extra}
    if cluster is not None:
        metric[CLUSTER_IDENTITY_LABEL] = cluster
    return {"metric": metric, "values": [(t0 + i, str(v)) for i, v in enumerate(vals)]}


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

    def test_equal_risk_compares_shares_not_step_counts(self, monkeypatch):
        """序列長度不同時比的是「自己步數的幾成」：120/1000（12%）不該贏過
        50/100（50%），雖然 120 步比 50 步多。"""
        r = _backtest(monkeypatch, [_row("long", [90] * 120 + [50] * 880),
                                    _row("short", [90] * 50 + [50] * 50)], None, "80")
        assert [c["risk"] for c in r["clusters"]] == ["HIGH", "HIGH"]
        assert (r["worst_cluster"], r["data_points"], r["new_breach_count"]) == ("short", 100, 50)

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

    _DEDUP = ("— not one series, and never added up; query a deduplicated endpoint "
              "(one series per cluster)")

    @pytest.mark.parametrize("rows,message", [
        ([_row("west", _WEST_15, replica="a"), _row("west", _WEST_15, replica="b")],
         'Not measured: da_cluster="west": 2 series at the same time that differ only in replica '),
        ([_row(None, _WEST_15, replica="a"), _row(None, _WEST_15, replica="b")],
         "Not measured: no da_cluster: 2 series at the same time that differ only in replica "),
        ([_row("", _EAST_QUIET, pod="x"), _row(None, _WEST_15, replica="b")],   # 空值＝沒有 label
         "Not measured: no da_cluster: 2 series at the same time that differ only in da_cluster, pod, replica "),
        ([_row("west", _WEST_15), _row("west", _WEST_15)],
         'Not measured: da_cluster="west": 2 series at the same time with identical labels '),
    ], ids=["ha-pair", "unlabelled-ha-pair", "empty-label", "identical"])
    def test_two_rows_of_one_cluster_are_not_measured(self, monkeypatch, rows, message):
        """沒去重的 HA pair 不是多叢集：先前串起來算（這裡是 HIGH、結束碼 1）；
        現在不相加，也不能當成通過——與查詢失敗同一類結果（結束碼 2）。"""
        r = _backtest(monkeypatch, rows, None, "80")
        assert (r["status"], r["risk"], r["query"]) == ("query_error", "UNKNOWN", _QUERY)
        assert r["message"] == message + self._DEDUP
        assert "data_points" not in r and "worst_cluster" not in r
        assert [c["status"] for c in r["clusters"]] == ["query_error"]

    def test_a_measurable_cluster_beside_it_is_still_reported(self, monkeypatch):
        """west 量得到（HIGH），east 重複：整筆是量不到，west 的重放照列。"""
        r = _backtest(monkeypatch, [_row("west", _WEST_15), _row("east", _EAST_QUIET, replica="a"),
                                    _row("east", _EAST_QUIET, replica="b")], None, "80")
        assert (r["status"], r["risk"]) == ("query_error", "UNKNOWN")
        assert r["message"] == ('Not measured: da_cluster="east": 2 series at the same time that differ only in '
                                "replica " + self._DEDUP)
        east, west = r["clusters"]
        assert east == {"da_cluster": "east", "status": "query_error", "risk": "UNKNOWN",
                        "message": "2 series at the same time that differ only in replica " + self._DEDUP}
        assert (west["status"], west["risk"], west["new_breach_count"], west["data_points"]) == (
            "analyzed", "HIGH", 15, 100)

    def test_rows_one_after_the_other_are_one_series(self, monkeypatch):
        """審查者的 `churn`：同一個叢集的 label 在視窗內換過一次，兩列時間不重疊
        ——同一條序列的前後段。接起來量；結果與一列逐欄相同（main 的結果），
        沒有 `clusters`。"""
        one = _backtest(monkeypatch, [_row("east", _WEST_15)], "70", "85")
        for rows in ([_row("east", _WEST_15[:40], prometheus="old-pod"),
                      _row("east", _WEST_15[40:], t0=40, prometheus="new-pod")],
                     [_row("east", _WEST_15[40:], t0=40, prometheus="new-pod"),
                      _row("east", _WEST_15[:40], prometheus="old-pod")],
                     [_row(None, _WEST_15[:40], prometheus="old-pod"),
                      _row(None, _WEST_15[40:], t0=40, prometheus="new-pod")]):
            r = _backtest(monkeypatch, rows, "70", "85")
            assert r == one and list(r) == list(one)
        assert (one["data_points"], one["old_breach_count"]) == (100, 15)

    def test_rows_one_after_the_other_in_one_of_two_clusters(self, monkeypatch):
        r = _backtest(monkeypatch, [_row("east", _EAST_QUIET[:30], pod="a"),
                                    _row("east", _EAST_QUIET[30:], t0=30, pod="b"),
                                    _row("west", _WEST_15)], None, "80")
        assert (r["status"], r["worst_cluster"]) == ("analyzed", "west")
        assert [(c["da_cluster"], c["data_points"]) for c in r["clusters"]] == [
            ("east", 100), ("west", 100)]

    def test_rows_sharing_one_timestamp_overlap(self, monkeypatch):
        """前一列的最後一點與後一列的第一點同一個時間：那一刻有兩條序列。"""
        r = _backtest(monkeypatch, [_row("east", _WEST_15[:41], pod="a"),
                                    _row("east", _WEST_15[40:], t0=40, pod="b")], None, "80")
        assert r["status"] == "query_error"
        assert 'da_cluster="east": 2 series at the same time that differ only in pod' in r["message"]

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
            try:
                result = bt.main()
            except CallerError as exc:   # #3617：run() 的結束方式
                exit_on_crash(exc)
            raise SystemExit(emit_result(result))
        captured = capsys.readouterr()   # one read: it empties both streams
        self.err = captured.err
        return ei.value.code, captured.out, md.read_text(encoding="utf-8")

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
        assert ('| Metric newly enabled — 15/100 points would fire '
                '\\[da\\_cluster="west", the worst of 2 clusters\\]'
                '<br>per cluster: da\\_cluster="east": LOW (0 -\\> 0 of 100 points fire); '
                'da\\_cluster="west": HIGH (0 -\\> 15 of 100 points fire) |') in row
        assert len(_UNESCAPED_PIPE.findall(row)) == 8          # 仍是七欄的一列

    def test_json_carries_clusters(self, monkeypatch, capsys, cli_argv, tmp_path):
        rc, out, _ = self._cli(monkeypatch, capsys, cli_argv, tmp_path, self._TWO, "--json")
        doc = json.loads(out)
        assert rc == 1 and doc["risk_summary"] == {"HIGH": 1, "MEDIUM": 0, "LOW": 0}
        change = doc["changes"][0]
        assert change["worst_cluster"] == "west"
        assert [c["da_cluster"] for c in change["clusters"]] == ["east", "west"]

    def test_a_run_with_no_partly_measured_change_has_no_such_field(
            self, monkeypatch, capsys, cli_argv, tmp_path):
        _, out, _ = self._cli(monkeypatch, capsys, cli_argv, tmp_path, self._TWO, "--json")
        assert "partly_measured_risk" not in json.loads(out)

    def test_one_row_reports_have_no_cluster_line(
            self, monkeypatch, capsys, cli_argv, tmp_path):
        rc, out, md = self._cli(monkeypatch, capsys, cli_argv, tmp_path,
                                [_row("west", _WEST_15)])
        assert rc == 1                         # 15/100，本來就是 HIGH
        assert "da_cluster" not in out.replace(_QUERY, "") and "per cluster" not in md

    def test_duplicate_rows_end_the_run_at_rc_2(
            self, monkeypatch, capsys, cli_argv, tmp_path):
        """量不到不是通過：結束碼 2（先前串起來是 HIGH、結束碼 1）。"""
        rc, out, md = self._cli(monkeypatch, capsys, cli_argv, tmp_path,
                                [_row("west", _WEST_15, replica="a"),
                                 _row("west", _WEST_15, replica="b")])
        assert rc == 2
        assert ("Query error: 1 (not measured: the query failed, or rows the da_cluster label "
                "does not tell apart)") in out and "[UNKNOWN]" in out
        assert "Clusters measured in those" not in out
        assert 'Not measured: da_cluster="west": 2 series at the same time that differ only in replica' in out
        assert "| UNKNOWN |" in md and ("> **1 change(s) not measured: the query failed, or rows "
                                         "the da_cluster label does not tell apart.**") in md
        rc, out, _ = self._cli(monkeypatch, capsys, cli_argv, tmp_path,
                               [_row("west", _WEST_15, replica="a"),
                                _row("west", _WEST_15, replica="b")], "--json")
        assert (rc, out) == (2, "")            # #3617：什麼都沒量到，沒有答案可印

    def test_could_not_measure_outranks_a_high_cluster(
            self, monkeypatch, capsys, cli_argv, tmp_path):
        """west 自己是 HIGH（結束碼 1），east 重複：結束碼是 2，west 仍列出。"""
        rows = [_row("west", _WEST_15), _row("east", _EAST_QUIET, replica="a"),
                _row("east", _EAST_QUIET, replica="b")]
        rc, out, md = self._cli(monkeypatch, capsys, cli_argv, tmp_path, rows)
        assert rc == 2
        assert 'da_cluster="west": HIGH (0 -> 15 of 100 points fire)' in out
        assert 'da_cluster="east": not measured (2 series at the same time that differ only in replica' in out
        assert 'da\\_cluster="west": HIGH (0 -\\> 15 of 100 points fire)' in md
        # 摘要不能在 west 是 HIGH 的時候只寫「0 HIGH」。
        assert "  Risk: 0 HIGH, 0 MEDIUM, 0 LOW\n" in out
        assert ("  Clusters measured in those: 1 HIGH, 0 MEDIUM, 0 LOW "
                "(not in the Risk line above)\n") in out
        assert ("> **Clusters measured in those: 1 HIGH, 0 MEDIUM, 0 LOW** "
                "(not counted as analyzed).") in md

    def test_json_is_printed_when_part_of_the_change_was_measured(
            self, monkeypatch, capsys, cli_argv, tmp_path):
        """審查者的 `dup_plus`：east 重複、west 量得到。量到了一部分就要印 JSON
        （結束碼仍是 2）；stderr 不能說「nothing was answered」。"""
        rows = [_row("west", _WEST_15), _row("east", _EAST_QUIET, replica="a"),
                _row("east", _EAST_QUIET, replica="b")]
        rc, out, _ = self._cli(monkeypatch, capsys, cli_argv, tmp_path, rows, "--json")
        err = self.err
        assert "not measured" in err   # the stream is really read
        doc = json.loads(out)
        assert rc == 2 and (doc["status"], doc["reason"], doc["query_error"]) == (
            "partial", "query_error", 1)
        assert doc["risk_summary"] == {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
        assert doc["partly_measured_risk"] == {"HIGH": 1, "MEDIUM": 0, "LOW": 0}
        change = doc["changes"][0]
        assert change["status"] == "query_error"
        assert [(c["da_cluster"], c["status"], c["risk"]) for c in change["clusters"]] == [
            ("east", "query_error", "UNKNOWN"), ("west", "analyzed", "HIGH")]
        assert "nothing was answered" not in err and "Prometheus query failed" not in err

    def test_a_cluster_name_cannot_break_the_markdown_row(
            self, monkeypatch, capsys, cli_argv, tmp_path):
        """叢集名稱來自 Prometheus：`|`、反引號、HTML、換行都不得變成標記——
        訊息本身與逐叢集那一段都一樣（名稱在最糟的叢集上，兩處都會出現）。"""
        evil = "a|b`c<b>d\ne"
        rows = [_row(evil, _WEST_15), _row("west", _EAST_QUIET)]
        _, out, md = self._cli(monkeypatch, capsys, cli_argv, tmp_path, rows)
        row = next(line for line in md.splitlines() if line.startswith("| HIGH"))
        shown = 'da\\_cluster="a\\|b\\`c\\<b\\>d?e"'
        assert f"\\[{shown}, the worst of 2 clusters\\]" in row
        assert f"<br>per cluster: {shown}: HIGH" in row
        assert len(_UNESCAPED_PIPE.findall(row)) == 8
        assert "`c" not in row.replace("\\`", "") and "<b>" not in row
        assert 'da_cluster="a|b`c<b>d?e": HIGH' in out
