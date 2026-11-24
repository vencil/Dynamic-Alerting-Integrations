#!/usr/bin/env python3
"""threshold_recommend：統計逐叢集算，建議取最保守的（#1200，ADR-006）。

門檻一個租戶只有一個值（契約 3），告警逐叢集觸發。先前把各叢集的樣本倒在一
起算百分位數，那個數字不描述任何一個叢集。這裡釘住：

  - 每個叢集各算各的，建議取「不讓任何叢集多響」的那個：上界 key 取最高，
    下界 key 取最低的 floor
  - 有叢集要人工審查、或沒有值可建議時，整個 key 跟著它
  - 信心等級取最弱的叢集
  - 同一個叢集兩列不相加、送人工審查
  - 只有一列時結果與先前相同，JSON 沒有新欄位
"""

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

import threshold_recommend as tr  # noqa: E402
from _lib_cluster_identity import CLUSTER_IDENTITY_LABEL  # noqa: E402

_UPPER_MAP = {"connections": {"scope": "tenant", "direction": ">",
                              "observed_series": "tenant:mysql_threads_connected:max"}}
_LOWER_MAP = {"hit_ratio": {"scope": "tenant", "direction": "<",
                            "observed_series": "tenant:db2_bufferpool_hit_ratio:min",
                            "recommendation_mode": "percentile-lower"}}
_START = 1_700_000_000.0


def _row(cluster, pairs):
    """Prometheus 回的一列；`cluster=None` 是沒有 da_cluster label 的列。"""
    metric = {"tenant": "t-x"}
    if cluster is not None:
        metric[CLUSTER_IDENTITY_LABEL] = cluster
    return {"metric": metric, "values": [[ts, str(v)] for ts, v in pairs]}


def _flat(values, step=100.0):
    """1000 點、step 100s ≈ 27.7 小時，過 24 小時的涵蓋門檻。"""
    return [(_START + i * step, float(v)) for i, v in enumerate(values)]


def _floor(floor, days=9, pts_per_day=200):
    """每天的 P5 落在 `floor` 的樣本（同 test_threshold_recommend._floor_samples）。"""
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    hi, step = min(0.9999, floor + 0.02), 86400 // pts_per_day
    return [((base + timedelta(days=d, seconds=i * step)).timestamp(),
             floor if i < pts_per_day * 0.10 else hi)
            for d in range(days) for i in range(pts_per_day)]


def _analyze(rows, config, observed_map):
    with patch("threshold_recommend.prometheus_instant", return_value=rows):
        report = tr.analyze_tenant("t-x", config, prometheus_url="http://prom:9090",
                                   observed_map=observed_map)
    return report, report.keys[0]


# 現值 50。east 恆為 10；west 九成 40、一成 90（P95 = 90，高於現值 → 放寬到 P99 = 90）。
_EAST = _flat([10] * 1000)
_WEST = _flat([40] * 900 + [90] * 100)


class TestUpperBound:
    def test_each_cluster_is_measured_and_the_highest_is_taken(self):
        """倒在一起：2000 點的 P95 是 42.5，低於現值，會建議**收緊**到 42——
        而 west 有一成時間在 90。逐叢集：east 收到 25，west 放寬到 90；
        不讓任何叢集多響的是 90。"""
        report, rec = _analyze([_row("east", _EAST), _row("west", _WEST)],
                               {"connections": 50}, _UPPER_MAP)
        assert isinstance(rec, tr.ClusteredKeyRecommendation)
        assert (rec.recommended, rec.delta_pct, rec.conservative_cluster) == (90, 80.0, "west")
        assert (rec.p50, rec.p95, rec.p99, rec.sample_count) == (40.0, 90.0, 90.0, 1000)
        assert rec.confidence == tr.CONFIDENCE_HIGH
        assert rec.reason == ('relaxed to P99 (increase 80.0%) '
                              '[da_cluster="west" of 2 clusters: highest value]')
        assert rec.clusters == [
            {"da_cluster": "east", "sample_count": 1000, "p50": 10.0, "p95": 10.0, "p99": 10.0,
             "recommended": 25, "delta_pct": -50.0, "confidence": "HIGH", "force_manual": False,
             "reason": ("tighten floored at 25 above P95 10 (decrease 50.0%): one round tightens at most 50% "
                        "(half of current, rounded up, is 25)")},
            {"da_cluster": "west", "sample_count": 1000, "p50": 40.0, "p95": 90.0, "p99": 90.0,
             "recommended": 90, "delta_pct": 80.0, "confidence": "HIGH", "force_manual": False,
             "reason": "relaxed to P99 (increase 80.0%)"},
        ]
        assert report.recommended_changes == 1

    def test_the_order_of_the_rows_does_not_matter(self):
        _, rec = _analyze([_row("west", _WEST), _row("east", _EAST)],
                          {"connections": 50}, _UPPER_MAP)
        assert (rec.recommended, rec.conservative_cluster) == (90, "west")
        assert [c["da_cluster"] for c in rec.clusters] == ["east", "west"]

    def test_two_tightens_take_the_one_that_tightens_less(self):
        """現值 80：east 可收到 40，west 只能收到 60 → 60。"""
        _, rec = _analyze([_row("east", _flat([40] * 1000)), _row("west", _flat([60] * 1000))],
                          {"connections": 80}, _UPPER_MAP)
        assert (rec.recommended, rec.conservative_cluster) == (60, "west")
        assert [c["recommended"] for c in rec.clusters] == [40, 60]

    def test_a_cluster_with_no_value_leaves_the_key_without_one(self):
        """east 整段為 0（P95 非正，引擎不給建議）：不變更是唯一對它安全的答案。"""
        report, rec = _analyze([_row("east", _flat([0] * 1000)), _row("west", _WEST)],
                               {"connections": 50}, _UPPER_MAP)
        assert rec.recommended is None and not rec.force_manual
        assert rec.conservative_cluster == "east"
        assert rec.reason.endswith('[da_cluster="east" of 2 clusters: has no value to recommend]')
        assert [c["recommended"] for c in rec.clusters] == [None, 90]
        assert report.recommended_changes == 0

    def test_confidence_is_the_weakest_clusters(self):
        """west 只有 150 點（MEDIUM）：被採用的是 east 的值，信心仍是 MEDIUM。"""
        east = _flat([60] * 1000)
        west = _flat([40] * 150, step=700.0)          # 150 點、約 29 小時
        _, rec = _analyze([_row("east", east), _row("west", west)],
                          {"connections": 80}, _UPPER_MAP)
        assert (rec.recommended, rec.conservative_cluster) == (60, "east")
        assert rec.confidence == tr.CONFIDENCE_MEDIUM
        assert rec.reason.endswith('[da_cluster="east" of 2 clusters: highest value; '
                                   'confidence MEDIUM from da_cluster="west"]')
        assert [c["confidence"] for c in rec.clusters] == ["HIGH", "MEDIUM"]

    def test_current_zero_in_one_cluster_routes_the_key_to_manual_review(self):
        """現值 0：兩個叢集都是人工審查；key 跟著第一個，原因點名叢集。"""
        _, rec = _analyze([_row("east", _EAST), _row("west", _WEST)],
                          {"connections": 0}, _UPPER_MAP)
        assert rec.force_manual and rec.recommended is None
        assert rec.guardrail_reason.endswith(
            '[da_cluster="east" of 2 clusters: needs manual review]')


class TestLowerBound:
    def test_the_lowest_floor_is_taken(self):
        """現值 0.95：east 的 P5 是 0.97，west 是 0.98。floor 抬到 0.98 會讓
        east 開始響；取最低的 0.97。"""
        report, rec = _analyze([_row("east", _floor(0.97)), _row("west", _floor(0.98))],
                               {"hit_ratio": "0.95"}, _LOWER_MAP)
        assert rec.recommended == pytest.approx(0.97, abs=1e-9)
        assert rec.conservative_cluster == "east"
        assert [c["recommended"] for c in rec.clusters] == [
            pytest.approx(0.97, abs=1e-9), pytest.approx(0.98, abs=1e-9)]
        assert rec.reason.endswith('[da_cluster="east" of 2 clusters: lowest floor]')
        assert "miss-rate" in rec.reason and report.recommended_changes == 1

    def test_a_cluster_that_would_relax_routes_the_key_to_manual_review(self):
        """west 的 P5 低於現值（放寬 floor 一律人工）：整個 key 人工審查。"""
        report, rec = _analyze([_row("east", _floor(0.97)), _row("west", _floor(0.93))],
                               {"hit_ratio": "0.95"}, _LOWER_MAP)
        assert rec.force_manual and rec.recommended is None
        assert rec.conservative_cluster == "west"
        assert "would relax the floor" in rec.guardrail_reason
        assert rec.guardrail_reason.endswith(
            '[da_cluster="west" of 2 clusters: needs manual review]')
        assert not tr._exportable(rec) and report.recommended_changes == 0

    def test_a_cluster_within_margin_means_no_change(self):
        """west 在 margin 內（不需要變更）：不因 east 可以收緊就動。"""
        _, rec = _analyze([_row("east", _floor(0.97)), _row("west", _floor(0.9520))],
                          {"hit_ratio": "0.95"}, _LOWER_MAP)
        assert rec.recommended is None and not rec.force_manual
        assert rec.conservative_cluster == "west"


class TestRowsTheLabelDoesNotTellApart:
    def test_mixed_labelled_and_unlabelled_are_two_groups(self):
        _, rec = _analyze([_row("west", _EAST), _row(None, _WEST)],
                          {"connections": 50}, _UPPER_MAP)
        assert [c["da_cluster"] for c in rec.clusters] == ["", "west"]
        assert (rec.recommended, rec.conservative_cluster) == (90, "")
        assert rec.reason.endswith("[no da_cluster of 2 clusters: highest value]")

    @pytest.mark.parametrize("rows,named", [
        ([_row("west", _EAST), _row("west", _WEST)], '2 series with da_cluster="west"'),
        ([_row(None, _EAST), _row(None, _WEST)], "2 series without a da_cluster label"),
    ], ids=["one-cluster-twice", "unlabelled-twice"])
    def test_two_rows_of_one_cluster_go_to_manual_review(self, rows, named):
        """不相加：沒有百分位數、沒有建議值，送人工審查並寫明原因。"""
        report, rec = _analyze(rows, {"connections": 50}, _UPPER_MAP)
        assert rec.force_manual and rec.recommended is None
        assert (rec.p95, rec.sample_count) == (None, 0)
        assert named in rec.guardrail_reason and named in rec.reason
        assert rec.queried and rec.promql
        assert report.recommended_changes == 0

    def test_a_row_without_finite_samples_is_no_series(self):
        """只有 NaN 的列不算一條序列：剩下那一列照單一序列處理。"""
        nan_row = {"metric": {"tenant": "t-x"},
                   "values": [[_START + i, "NaN"] for i in range(5)]}
        _, rec = _analyze([_row(None, _WEST), nan_row], {"connections": 50}, _UPPER_MAP)
        assert type(rec) is tr.KeyRecommendation
        assert (rec.recommended, rec.reason) == (90, "relaxed to P99 (increase 80.0%)")


class TestOneRowIsWhatItWas:
    @pytest.mark.parametrize("cluster", [None, "", "west"])
    def test_one_row_is_a_plain_recommendation(self, cluster):
        """一列（有沒有 da_cluster 都一樣）＝把同一批樣本直接交給引擎。"""
        _, rec = _analyze([_row(cluster, _WEST)], {"connections": 50}, _UPPER_MAP)
        with patch("threshold_recommend.query_prometheus_range_ts", return_value=list(_WEST)):
            plain = tr.analyze_tenant("t-x", {"connections": 50},
                                      prometheus_url="http://prom:9090",
                                      observed_map=_UPPER_MAP).keys[0]
        assert type(rec) is tr.KeyRecommendation
        assert asdict(rec) == asdict(plain)
        assert rec.reason == "relaxed to P99 (increase 80.0%)"

    def test_json_has_no_cluster_field_for_one_row(self):
        report, _ = _analyze([_row("west", _WEST)], {"connections": 50}, _UPPER_MAP)
        key = tr.json_document([report])["tenants"][0]["keys"][0]
        assert "clusters" not in key and "conservative_cluster" not in key

    def test_the_samples_are_still_one_list(self):
        """`query_prometheus_range_ts` 仍回所有列的 (ts, value)，另帶逐列的樣本。"""
        rows = [_row("east", [(1.0, 1), (2.0, 2)]), _row("west", [(1.0, 3)])]
        with patch("threshold_recommend.prometheus_instant", return_value=rows):
            got = tr.query_prometheus_range_ts("http://prom:9090", "q")
        assert got == [(1.0, 1.0), (2.0, 2.0), (1.0, 3.0)]
        assert [(m[CLUSTER_IDENTITY_LABEL], pairs) for m, pairs in got.rows] == [
            ("east", [(1.0, 1.0), (2.0, 2.0)]), ("west", [(1.0, 3.0)])]


class TestReports:
    def _report(self):
        return _analyze([_row("east", _EAST), _row("west", _WEST)],
                        {"connections": 50}, _UPPER_MAP)[0]

    _EAST_LINE = 'da_cluster="east": P95 10.0, recommend 25, delta -50.0%, HIGH, 1000 samples'
    _WEST_LINE = 'da_cluster="west": P95 90.0, recommend 90, delta +80.0%, HIGH, 1000 samples'

    def test_text_lists_each_cluster(self):
        text = tr.format_text_report([self._report()])
        assert f"    └─ {self._EAST_LINE}\n    └─ {self._WEST_LINE}" in text
        assert '[da_cluster="west" of 2 clusters: highest value]' in text

    def test_markdown_lists_each_cluster_in_the_row(self):
        md = tr.format_markdown_report([self._report()])
        row = next(line for line in md.splitlines() if line.startswith("| connections"))
        assert row.endswith(f"<br>per cluster: {self._EAST_LINE}; {self._WEST_LINE} |")

    def test_json_carries_clusters(self):
        key = tr.json_document([self._report()])["tenants"][0]["keys"][0]
        assert key["conservative_cluster"] == "west" and key["recommended"] == 90
        assert [c["da_cluster"] for c in key["clusters"]] == ["east", "west"]

    def test_export_patch_writes_the_conservative_value(self):
        patch_text = tr.format_export_patch([self._report()])
        assert '    connections: "90"   # was 50' in patch_text
        assert '[da_cluster="west" of 2 clusters: highest value]' in patch_text
