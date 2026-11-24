#!/usr/bin/env python3
"""threshold_recommend：統計逐叢集算，建議取最保守的（#1200，ADR-006）。

門檻一個租戶只有一個值（契約 3），告警逐叢集觸發。先前把各叢集的樣本倒在一
起算百分位數，那個數字不描述任何一個叢集。這裡釘住：

  - 每個叢集各算各的；候選值取自「有建議值」的叢集裡不讓任何叢集多響的那個
    （上界 key 取最高，下界 key 取最低的 floor）
  - 候選值只拿到**引擎沒給建議值的叢集**的樣本上重放（閒置的 DR 備援、在
    margin 內）：這種叢集不會單憑「沒有建議值」擋住整個 key，只有它的樣本
    會——上界 key 抬到蓋住那些樣本，下界 key 不給建議。自己有建議值的叢集
    不重放：取最保守的值已經涵蓋它，引擎自己的容忍度（P5 floor 底下本來就
    有約 5% 的樣本）照舊
  - 有叢集要人工審查時整個 key 跟著它；信心等級取有樣本的叢集裡最弱的
  - 同一個叢集、時間重疊的兩列（沒去重的 HA pair）不相加：算沒量成
    （query_error）；時間不重疊的兩列是同一條序列的前後段，接起來照常量
  - 沒有任何路徑會把多列的樣本當成一條序列
  - 只有一列時結果與先前相同，JSON 沒有新欄位
"""

import random
import re
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import threshold_govern as tg  # noqa: E402
import threshold_recommend as tr  # noqa: E402
from _lib_cluster_identity import CLUSTER_IDENTITY_LABEL  # noqa: E402

_UPPER_MAP = {"connections": {"scope": "tenant", "direction": ">",
                              "observed_series": "tenant:mysql_threads_connected:max"}}
_LOWER_MAP = {"hit_ratio": {"scope": "tenant", "direction": "<",
                            "observed_series": "tenant:db2_bufferpool_hit_ratio:min",
                            "recommendation_mode": "percentile-lower"}}
_START = 1_700_000_000.0
_UNESCAPED_PIPE = re.compile(r"(?<!\\)\|")


def _row(cluster, pairs, **extra):
    """Prometheus 回的一列；`cluster=None` 是沒有 da_cluster label 的列。"""
    metric = {"tenant": "t-x", **extra}
    if cluster is not None:
        metric[CLUSTER_IDENTITY_LABEL] = cluster
    return {"metric": metric, "values": [[ts, str(v)] for ts, v in pairs]}


def _flat(values, step=100.0):
    """1000 點、step 100s ≈ 27.7 小時，過 24 小時的涵蓋門檻。"""
    return [(_START + i * step, float(v)) for i, v in enumerate(values)]


def _floor(floor, days=9, pts_per_day=200, hi=None):
    """每天的 P5 落在 `floor` 的樣本（同 test_threshold_recommend._floor_samples）：
    每天一成的點在 `floor`，其餘在 `hi`（預設 floor + 0.02）。"""
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    hi, step = (min(0.9999, floor + 0.02) if hi is None else hi), 86400 // pts_per_day
    return [((base + timedelta(days=d, seconds=i * step)).timestamp(),
             floor if i < pts_per_day * 0.10 else hi)
            for d in range(days) for i in range(pts_per_day)]


def _continuous(seed, low=0.95, width=0.005, n=1500, step=600.0):
    """連續值的下界序列（審查者的 `held`／`two` 形狀）：1500 點、每 10 分鐘一點、
    均勻落在 [low, low + width)。P5 floor 底下必然有約 5% 的樣本——離散的
    `_floor` 沒有任何樣本低於 floor，測不到這件事。"""
    rng = random.Random(seed)
    return [(_START + i * step, low + rng.random() * width) for i in range(n)]


def _nan(n=500):
    return [(_START + i, "NaN") for i in range(n)]


def _analyze(rows, config, observed_map):
    with patch("threshold_recommend.prometheus_instant", return_value=rows):
        report = tr.analyze_tenant("t-x", config, prometheus_url="http://prom:9090",
                                   observed_map=observed_map)
    return report, report.keys[0]


# 現值 50。east 恆為 10；west 九成 40、一成 90（P95 = 90，高於現值 → 放寬到 P99 = 90）。
_EAST = _flat([10] * 1000)
_WEST = _flat([40] * 900 + [90] * 100)
_IDLE = _flat([0] * 1000)


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
        assert (rec.confidence, rec.held_by_cluster, rec.direction) == ("HIGH", None, ">")
        assert rec.reason == ('relaxed to P99 (increase 80.0%) '
                              '[da_cluster="west" of 2 clusters: highest value]')
        assert rec.clusters == [
            {"da_cluster": "east", "measured": True, "sample_count": 1000,
             "p50": 10.0, "p95": 10.0, "p99": 10.0,
             "recommended": 25, "delta_pct": -50.0, "confidence": "HIGH", "force_manual": False,
             "reason": ("tighten floored at 25 above P95 10 (decrease 50.0%): one round "
                        "tightens at most 50% (half of current, rounded up, is 25)")},
            {"da_cluster": "west", "measured": True, "sample_count": 1000,
             "p50": 40.0, "p95": 90.0, "p99": 90.0,
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


class TestAClusterWithNoRecommendationOfItsOwn:
    """閒置的 DR 備援是租戶同時在兩個叢集最常見的原因。它沒有建議值（P95 ≤ 0、
    或全是 NaN），但不能因此讓忙的那個叢集永遠拿不到建議——只有它的**樣本**
    能擋：候選值在它身上重放，多響一步都不行。"""

    def test_idle_standby_does_not_block_a_relax(self):
        """dr 恆為 0（P95 非正，引擎不給建議），prod 要放寬到 90：90 ≥ 現值 50，
        在任何叢集都不會多響 → 採用 90。"""
        report, rec = _analyze([_row("dr", _IDLE), _row("prod", _WEST)],
                               {"connections": 50}, _UPPER_MAP)
        assert (rec.recommended, rec.delta_pct, rec.conservative_cluster) == (90, 80.0, "prod")
        assert (rec.held_by_cluster, rec.confidence) == (None, "HIGH")
        dr = rec.clusters[0]
        assert (dr["da_cluster"], dr["recommended"], dr["sample_count"]) == ("dr", None, 1000)
        assert dr["reason"] == "P95 is non-positive (max in window 0) — no recommendation"
        assert report.recommended_changes == 1

    def test_idle_standby_does_not_block_a_tighten(self):
        """現值 100，prod 恆為 20：引擎的建議是 50（一輪最多收一半）。dr 恆為 0，
        沒有任何樣本落在 (50, 100] → 採用 50。"""
        report, rec = _analyze([_row("dr", _IDLE), _row("prod", _flat([20] * 1000))],
                               {"connections": 100}, _UPPER_MAP)
        assert (rec.recommended, rec.delta_pct, rec.held_by_cluster) == (50, -50.0, None)
        assert rec.reason.endswith('[da_cluster="prod" of 2 clusters: highest value]')
        assert report.recommended_changes == 1

    def test_a_drill_on_the_standby_raises_the_candidate(self):
        """dr 97% 的時間是 0、演練時到 80（P95 = 0，仍然沒有建議值）；prod 的建議
        是 50。50 會讓 dr 的 30 個點（80 > 50，而 80 ≤ 現值 100）開始響 → 抬到
        dr 在現值以下的最大值向上取整：80，即 −20%。"""
        drill = _flat([0] * 970 + [80] * 30)
        report, rec = _analyze([_row("dr", drill), _row("prod", _flat([20] * 1000))],
                               {"connections": 100}, _UPPER_MAP)
        assert (rec.recommended, rec.delta_pct) == (80, -20.0)
        assert (rec.conservative_cluster, rec.held_by_cluster) == ("prod", "dr")
        assert rec.reason == (
            'tighten floored at 50 above P95 20 (decrease 50.0%): one round tightens at most '
            '50% (half of current, rounded up, is 50) — held at 80 instead of 50: '
            'da_cluster="dr" has values up to 80 at or below current — no new firing '
            '(decrease 20.0%) [da_cluster="prod" of 2 clusters: highest value]')
        assert [c["recommended"] for c in rec.clusters] == [None, 50]   # 各叢集自己的不變
        assert report.recommended_changes == 1

    def test_a_drill_fraction_is_rounded_up_in_currents_precision(self):
        """演練峰值 79.2、現值是整數 100 → 向上取整到 80（不是 79）。"""
        drill = _flat([0] * 970 + [79.2] * 30)
        _, rec = _analyze([_row("dr", drill), _row("prod", _flat([20] * 1000))],
                          {"connections": 100}, _UPPER_MAP)
        assert (rec.recommended, rec.delta_pct, rec.held_by_cluster) == (80, -20.0, "dr")

    def test_a_drill_at_current_means_no_change(self):
        """演練峰值就是現值 100：抬到 100，等於現值 → 不變更，原因點名 dr。"""
        drill = _flat([0] * 970 + [100] * 30)
        report, rec = _analyze([_row("dr", drill), _row("prod", _flat([20] * 1000))],
                               {"connections": 100}, _UPPER_MAP)
        assert (rec.recommended, rec.delta_pct, rec.held_by_cluster) == (100, 0.0, "dr")
        assert rec.reason.startswith('within 5% margin, no change needed — tighten floored at 50 ')
        assert ('— held at 100 instead of 50: da_cluster="dr" has values up to 100 at or below '
                'current — no new firing [') in rec.reason
        assert not tr._exportable(rec) and report.recommended_changes == 0

    def test_a_drill_above_current_does_not_hold_the_tighten(self):
        """演練峰值 150 高於現值：現值下本來就會響，不是「多響」→ 照採用 50。"""
        drill = _flat([0] * 970 + [150] * 30)
        _, rec = _analyze([_row("dr", drill), _row("prod", _flat([20] * 1000))],
                          {"connections": 100}, _UPPER_MAP)
        assert (rec.recommended, rec.held_by_cluster) == (50, None)

    def test_a_cluster_with_only_nan_is_listed_and_does_not_block(self):
        """閒置叢集的比率序列常是 0/0 = NaN：列在 clusters 裡（與 backtest 一致），
        不擋建議、也不把信心拉成 LOW。"""
        report, rec = _analyze([_row("dr", _nan()), _row("prod", _WEST)],
                               {"connections": 50}, _UPPER_MAP)
        assert (rec.recommended, rec.confidence, rec.conservative_cluster) == (90, "HIGH", "prod")
        assert rec.clusters[0] == {
            "da_cluster": "dr", "measured": True, "sample_count": 0,
            "p50": None, "p95": None, "p99": None, "recommended": None, "delta_pct": None,
            "confidence": "LOW", "force_manual": False, "reason": "no data points found"}
        assert report.recommended_changes == 1

    def test_the_candidates_own_reason_survives_being_held(self):
        """候選值本來就因為「資料只涵蓋 1 個小時」被判 LOW（#2941）：被 dr 抬高之後
        那句解釋還在，不會只剩一個沒有原因的 LOW。"""
        short = [(_START + i, 45.0) for i in range(2000)]          # 2000 點擠在 34 分鐘內
        drill = [(_START + i * 900, 0.0) for i in range(1999)] + [(_START + 1999 * 900, 70.0)]
        _, rec = _analyze([_row("a", short), _row("dr", drill)], {"connections": 80}, _UPPER_MAP)
        assert (rec.recommended, rec.confidence, rec.held_by_cluster) == (70, "LOW", "dr")
        assert rec.reason == (
            'recommended at P95 (decrease 43.8%); data in 1 distinct hour(s) < 24 → LOW — held '
            'at 70 instead of 45: da_cluster="dr" has values up to 70 at or below current — no '
            'new firing (decrease 12.5%) [da_cluster="a" of 2 clusters: highest value]')

    def test_upper_bound_guarantees_hold_over_random_clusters(self):
        """隨機的 2–4 個叢集（閒置、尖峰、忙、混合、空）跑 400 輪：最後的建議值
        (1) 在引擎沒給建議值的叢集裡，不會比現值多響任何一步；(2) 不低於任何一個
        叢集自己的建議值；(3) 被擋下的叢集一定是自己沒有建議值的。"""
        rng = random.Random(7)
        checked = held = 0
        for _ in range(400):
            current = rng.choice([80, "80", "80.5", "0.5", "100.0", 7, "12.345"])
            cur = tr.written_threshold_number(current)
            clusters = {}
            for c in range(rng.randint(2, 4)):
                mode, n = rng.choice(["low", "idle", "spike", "high", "mix", "nan"]), rng.choice([150, 400])
                values = {"low": [rng.uniform(0.1, 0.6) * cur for _ in range(n)],
                          "idle": [0.0] * n,
                          "spike": [0.0] * (n - 1) + [rng.uniform(0.5, 1.2) * cur],
                          "high": [rng.uniform(0.9, 2) * cur for _ in range(n)],
                          "mix": [rng.uniform(0, 1.3) * cur for _ in range(n)],
                          "nan": []}[mode]
                clusters[f"c{c}"] = [(_START + i * 900, v) for i, v in enumerate(values)]
            groups = {c: [{"metric": {CLUSTER_IDENTITY_LABEL: c}, "pairs": p, "span": (0, 1)}]
                      for c, p in clusters.items()}
            rec = tr._across_clusters("k", current, ">", groups, 100)
            own = {e["da_cluster"]: e["recommended"] for e in rec.clusters}
            if rec.held_by_cluster is not None:
                held += 1
                assert own[rec.held_by_cluster] is None
            if rec.recommended is None:
                continue
            checked += 1
            assert rec.recommended >= max(v for v in own.values() if v is not None)
            for c, pairs in clusters.items():
                if own[c] is None:
                    assert not tr._new_firing(">", [v for _, v in pairs], cur, rec.recommended), (
                        current, c, rec.recommended, rec.reason)
        assert checked > 200 and held >= 5      # 這個測試確實走到了被擋下的情形

    def test_no_cluster_with_a_value_leaves_the_key_without_one(self):
        report, rec = _analyze([_row("a", _nan()), _row("b", _IDLE)],
                               {"connections": 50}, _UPPER_MAP)
        assert rec.recommended is None and not rec.force_manual and not rec.query_error
        assert rec.conservative_cluster == "b"          # 有樣本的那個叢集的原因
        assert rec.reason == ('P95 is non-positive (max in window 0) — no recommendation '
                              '[da_cluster="b" of 2 clusters: no cluster has a value to recommend]')
        assert report.recommended_changes == 0


class TestLowerBound:
    def test_the_lowest_floor_is_taken(self):
        """現值 0.95：east 的 P5 是 0.97，west 是 0.98。floor 抬到 0.98 會讓
        east 開始響；取最低的 0.97（miss-rate 0.05 → 0.03，−40%）。"""
        report, rec = _analyze([_row("east", _floor(0.97)), _row("west", _floor(0.98))],
                               {"hit_ratio": "0.95"}, _LOWER_MAP)
        assert rec.recommended == pytest.approx(0.97, abs=1e-9)
        assert (rec.delta_pct, rec.conservative_cluster, rec.held_by_cluster) == (-40.0, "east", None)
        assert rec.direction == "<"
        assert [c["recommended"] for c in rec.clusters] == [
            pytest.approx(0.97, abs=1e-9), pytest.approx(0.98, abs=1e-9)]
        assert rec.reason.endswith('[da_cluster="east" of 2 clusters: lowest floor]')
        assert "miss-rate" in rec.reason and report.recommended_changes == 1

    def test_a_floor_is_not_blocked_by_its_own_clusters_tail(self):
        """審查者的 `held`：east 的值連續落在 [0.95, 0.955)，dr 全是 NaN，現值 0.9。
        east 單獨交給引擎是 0.9502（miss-rate −50.2%）——它自己有 55 個樣本低於
        這個 floor（P5 的定義）。那不是「多響」的理由：引擎對單一叢集就是這樣
        容忍的，多一個全 NaN 的叢集不該讓建議消失。"""
        east = _continuous(3)
        alone = tr.recommend_threshold_lower("hit_ratio", "0.9", east, 100)
        assert (alone.recommended, alone.delta_pct) == (0.9502, -50.2)
        assert sum(1 for _, v in east if 0.9 <= v < 0.9502) == 55
        report, rec = _analyze([_row("dr", _nan(1500)), _row("east", east)],
                               {"hit_ratio": "0.9"}, _LOWER_MAP)
        assert (rec.recommended, rec.delta_pct, rec.confidence) == (0.9502, -50.2, "HIGH")
        assert (rec.conservative_cluster, rec.held_by_cluster) == ("east", None)
        assert rec.reason == (alone.reason + ' [da_cluster="east" of 2 clusters: lowest floor]')
        assert report.recommended_changes == 1
        assert len(tg.build_governance_plan([report], 20.0)) == 1

    def test_two_continuous_clusters_take_the_lower_floor(self):
        """審查者的 `two`：兩個叢集各自有建議值（east 0.9502、west 0.9552）→ 取
        較低的 0.9502。east 有 55 個樣本、west 沒有樣本低於它；兩邊都不重放。"""
        east, west = _continuous(3), _continuous(4, low=0.955)
        own = [tr.recommend_threshold_lower("hit_ratio", "0.9", p, 100).recommended
               for p in (east, west)]
        assert own == [0.9502, 0.9552]
        report, rec = _analyze([_row("east", east), _row("west", west)],
                               {"hit_ratio": "0.9"}, _LOWER_MAP)
        assert (rec.recommended, rec.delta_pct) == (0.9502, -50.2)
        assert (rec.conservative_cluster, rec.held_by_cluster) == ("east", None)
        assert [c["recommended"] for c in rec.clusters] == own
        assert report.recommended_changes == 1

    def test_two_identical_continuous_clusters_recommend_what_one_does(self):
        _, rec = _analyze([_row("east", _continuous(3)), _row("west", _continuous(3))],
                          {"hit_ratio": "0.9"}, _LOWER_MAP)
        assert (rec.recommended, rec.held_by_cluster) == (0.9502, None)

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

    def test_samples_between_current_and_the_candidate_mean_no_recommendation(self):
        """現值 0.90，prod 的 P5 是 0.97（候選 floor 0.97）。另一個叢集 `mixed`
        的 P5 是 0.905（在 margin 內，自己沒有建議值），它 1800 個樣本全在
        [0.90, 0.97)：floor 抬到 0.97 它會整段開始響 → 不給建議，點名 mixed。"""
        report, rec = _analyze([_row("mixed", _floor(0.905)), _row("prod", _floor(0.97))],
                               {"hit_ratio": "0.90"}, _LOWER_MAP)
        assert rec.recommended is None and rec.delta_pct is None and not rec.force_manual
        assert (rec.conservative_cluster, rec.held_by_cluster) == ("prod", "mixed")
        assert rec.reason == (
            'lower-bound P5 floor → tighten (raise floor); miss-rate -70.0% — not recommended: '
            'a floor of 0.97 would start firing in da_cluster="mixed" '
            '(1800 of its samples are at or above current and below it) '
            '[da_cluster="prod" of 2 clusters: lowest floor]')
        assert [c["recommended"] for c in rec.clusters] == [None, pytest.approx(0.97, abs=1e-9)]
        assert not tr._exportable(rec) and report.recommended_changes == 0

    def test_a_cluster_with_only_nan_does_not_block_a_floor(self):
        report, rec = _analyze([_row("dr", _nan()), _row("prod", _floor(0.97))],
                               {"hit_ratio": "0.90"}, _LOWER_MAP)
        assert rec.recommended == pytest.approx(0.97, abs=1e-9)
        assert (rec.held_by_cluster, rec.clusters[0]["reason"]) == (None, "no data points found")
        assert report.recommended_changes == 1

    def test_a_standby_reporting_zero_is_manual_review_and_says_so(self):
        """下界序列恆為 0 的備援：引擎的原話是「check units」，那不是真的。
        仍是人工審查（force_manual 優先），但原因寫實情。"""
        _, rec = _analyze([_row("dr", _floor(0.0, hi=0.0)), _row("prod", _floor(0.97))],
                          {"hit_ratio": "0.90"}, _LOWER_MAP)
        assert rec.force_manual and rec.recommended is None
        assert rec.conservative_cluster == "dr"
        assert rec.guardrail_reason == (
            "every sample is 0 (an idle cluster?) — no floor can be learned from it; "
            'manual review [da_cluster="dr" of 2 clusters: needs manual review]')
        assert "check units" not in rec.reason and "check units" not in rec.clusters[0]["reason"]


class TestRowsTheLabelDoesNotTellApart:
    def test_mixed_labelled_and_unlabelled_are_two_groups(self):
        _, rec = _analyze([_row("west", _EAST), _row(None, _WEST)],
                          {"connections": 50}, _UPPER_MAP)
        assert [c["da_cluster"] for c in rec.clusters] == ["", "west"]
        assert (rec.recommended, rec.conservative_cluster) == (90, "")
        assert rec.reason.endswith("[no da_cluster of 2 clusters: highest value]")

    _DEDUP = ("— not one series, and never added up; query a deduplicated endpoint "
              "(one series per cluster)")

    def test_rows_one_after_the_other_are_one_series(self):
        """審查者的 `churn`：同一個叢集的 label 在視窗內換過一次（`prometheus=old-pod`
        → `new-pod`），兩列的時間不重疊——是同一條序列的前後段，不是 HA pair。
        接起來量，結果與把同一批樣本當一列完全相同（main 的結果）。"""
        old, new = _WEST[:400], _WEST[400:]
        _, one = _analyze([_row("east", _WEST)], {"connections": 50}, _UPPER_MAP)
        for rows in ([_row("east", old, prometheus="old-pod"), _row("east", new, prometheus="new-pod")],
                     [_row("east", new, prometheus="new-pod"), _row("east", old, prometheus="old-pod")],
                     [_row(None, old, prometheus="old-pod"), _row(None, new, prometheus="new-pod")]):
            _, rec = _analyze(rows, {"connections": 50}, _UPPER_MAP)
            assert type(rec) is tr.KeyRecommendation
            assert asdict(rec) == asdict(one)

    def test_rows_one_after_the_other_in_one_of_two_clusters(self):
        _, rec = _analyze([_row("east", _EAST[:300], pod="a"), _row("east", _EAST[300:], pod="b"),
                           _row("west", _WEST)], {"connections": 50}, _UPPER_MAP)
        assert not rec.query_error and (rec.recommended, rec.conservative_cluster) == (90, "west")
        assert [(c["da_cluster"], c["sample_count"]) for c in rec.clusters] == [
            ("east", 1000), ("west", 1000)]

    def test_rows_sharing_one_timestamp_overlap(self):
        """前一列的最後一點與後一列的第一點同一個時間：那一刻有兩條序列。"""
        _, rec = _analyze([_row("east", _WEST[:401], pod="a"), _row("east", _WEST[400:], pod="b")],
                          {"connections": 50}, _UPPER_MAP)
        assert 'da_cluster="east": 2 series at the same time that differ only in pod' in (
            rec.query_error)

    @pytest.mark.parametrize("rows,why", [
        ([_row("west", _EAST, replica="a"), _row("west", _WEST, replica="b")],
         'da_cluster="west": 2 series at the same time that differ only in replica '),
        ([_row(None, _EAST, replica="a"), _row(None, _WEST, replica="b")],
         "no da_cluster: 2 series at the same time that differ only in replica "),
    ], ids=["ha-pair", "unlabelled-ha-pair"])
    def test_two_rows_of_one_cluster_are_not_measured(self, rows, why):
        """沒去重的 HA pair：不相加、沒有百分位數；與查詢沒被回答同一類結果
        （`query_error` → 報告 partial、結束碼 2），不是「不需要變更」。"""
        report, rec = _analyze(rows, {"connections": 50}, _UPPER_MAP)
        assert rec.query_error == why + self._DEDUP
        assert rec.reason == "not measured: " + rec.query_error
        assert rec.recommended is None and not rec.force_manual
        assert (rec.p95, rec.sample_count) == (None, 0)
        assert [(c["measured"], c["sample_count"]) for c in rec.clusters] == [(False, 0)]
        assert tr.query_error_count([report]) == 1
        doc = tr.json_document([report])
        assert (doc["status"], doc["reason"]) == ("partial", "prometheus_query_error")
        assert tg.collect_not_measured([report])[0].error == rec.query_error

    def test_a_measurable_cluster_beside_it_is_still_listed(self):
        """prod 量得到、dr 重複：整個 key 沒量成（不從剩下的叢集給建議），prod
        自己的數字照列。"""
        report, rec = _analyze([_row("dr", _IDLE, replica="a"), _row("dr", _IDLE, replica="b"),
                                _row("prod", _WEST)], {"connections": 50}, _UPPER_MAP)
        assert rec.query_error and rec.recommended is None
        dr, prod = rec.clusters
        assert (dr["measured"], dr["reason"]) == (
            False, "not measured: 2 series at the same time that differ only in replica " + self._DEDUP)
        assert (prod["measured"], prod["recommended"], prod["sample_count"]) == (True, 90, 1000)
        assert report.recommended_changes == 0

    def test_a_nan_row_does_not_hide_a_second_row_of_its_cluster(self):
        """dr 一列全是 NaN、一列有值：仍是同一個叢集的兩列，不能只看有值的那列。"""
        _, rec = _analyze([_row("dr", _nan(), replica="a"), _row("dr", _IDLE, replica="b"),
                           _row("prod", _WEST)], {"connections": 50}, _UPPER_MAP)
        assert 'da_cluster="dr": 2 series at the same time that differ only in replica' in rec.query_error

    def test_a_histogram_only_row_is_no_series(self):
        """沒有 float 樣本的列（native histogram）不算一條序列，照舊略過。"""
        histogram = {"metric": {"tenant": "t-x"}, "histograms": [[_START, {"count": "3"}]]}
        _, rec = _analyze([_row(None, _WEST), histogram], {"connections": 50}, _UPPER_MAP)
        assert type(rec) is tr.KeyRecommendation
        assert (rec.recommended, rec.reason) == (90, "relaxed to P99 (increase 80.0%)")


class TestNoPathPoolsRows:
    """沒有「把每一列的樣本當成一條」的後備：查詢函式只回逐列的結果，
    一個扁平的 (ts, value) list 不會被當成一條序列讀進去。"""

    def test_rows_come_back_one_by_one_with_their_labels(self):
        rows = [_row("east", [(1.0, 1), (2.0, 2)]), _row("west", [(1.0, 3)]),
                _row("idle", [(1.0, "NaN")]),
                {"metric": {"tenant": "t-x"}, "histograms": [[1.0, {"count": "3"}]]}]
        with patch("threshold_recommend.prometheus_instant", return_value=rows):
            got = tr.query_prometheus_range_rows("http://prom:9090", "q")
        assert [(m.get(CLUSTER_IDENTITY_LABEL), pairs, span) for m, pairs, span in got] == [
            ("east", [(1.0, 1.0), (2.0, 2.0)], (1.0, 2.0)), ("west", [(1.0, 3.0)], (1.0, 1.0)),
            ("idle", [], (1.0, 1.0))]

    def test_two_rows_given_without_a_span_are_never_one_series_by_accident(self):
        """double 給的 `(labels, samples)` 沒有時間範圍時用樣本自己的：同時間的
        兩列仍是沒量成，不會被接成一條。"""
        answer = [({"replica": "a"}, list(_EAST)), ({"replica": "b"}, list(_WEST))]
        with patch("threshold_recommend.query_prometheus_range_rows", return_value=answer):
            rec = tr.analyze_tenant("t-x", {"connections": 50}, prometheus_url="http://p:9090",
                                    observed_map=_UPPER_MAP).keys[0]
        assert rec.query_error and rec.recommended is None and rec.p95 is None

    @pytest.mark.parametrize("answer", [
        list(_EAST) + list(_WEST),                      # 兩個叢集的樣本倒在一起
        [[(1.0, 1.0)], [(2.0, 2.0)]],                    # 沒有 label 的列
    ], ids=["flat-pairs", "rows-without-labels"])
    def test_a_flat_list_of_samples_is_refused_not_measured_as_one_series(self, answer):
        with patch("threshold_recommend.query_prometheus_range_rows", return_value=answer):
            rec = tr.analyze_tenant("t-x", {"connections": 50}, prometheus_url="http://p:9090",
                                    observed_map=_UPPER_MAP).keys[0]
        assert rec.force_manual and rec.recommended is None
        assert (rec.p95, rec.sample_count) == (None, 0)
        assert "a series must be (labels, samples)" in rec.reason


class TestOneRowIsWhatItWas:
    @pytest.mark.parametrize("cluster", [None, "", "west"])
    def test_one_row_is_a_plain_recommendation(self, cluster):
        """一列（有沒有 da_cluster 都一樣）＝把同一批樣本直接交給引擎。"""
        _, rec = _analyze([_row(cluster, _WEST)], {"connections": 50}, _UPPER_MAP)
        plain = tr.recommend_threshold(
            key="connections", current_value=50,
            pcts=tr.compute_percentiles([v for _, v in _WEST]),
            sample_count=1000, min_samples=100, values=[v for _, v in _WEST])
        plain.promql, plain.queried = rec.promql, True
        assert type(rec) is tr.KeyRecommendation
        assert asdict(rec) == asdict(plain)
        assert rec.reason == "relaxed to P99 (increase 80.0%)"

    def test_json_has_no_cluster_field_for_one_row(self):
        report, _ = _analyze([_row("west", _WEST)], {"connections": 50}, _UPPER_MAP)
        key = tr.json_document([report])["tenants"][0]["keys"][0]
        assert not {"clusters", "conservative_cluster", "held_by_cluster", "direction"} & set(key)

    @pytest.mark.parametrize("rows", [[], [_row("west", _nan())]], ids=["no-row", "only-nan"])
    def test_no_finite_sample_is_no_data(self, rows):
        _, rec = _analyze(rows, {"connections": 50}, _UPPER_MAP)
        assert type(rec) is tr.KeyRecommendation
        assert (rec.reason, rec.sample_count, rec.confidence) == ("no data points found", 0, "LOW")


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
        east, west = (line.replace("da_cluster", "da\\_cluster")
                      for line in (self._EAST_LINE, self._WEST_LINE))
        assert row.endswith(f"<br>per cluster: {east}; {west} |")
        assert ('relaxed to P99 (increase 80.0%) '
                '\\[da\\_cluster="west" of 2 clusters: highest value\\]<br>') in row

    def test_a_lower_bound_line_says_p5_floor_and_miss_rate(self):
        """下界 key 的數字是 P5 floor 與 miss-rate 的變化，不是 P95 與值的變化。"""
        report, _ = _analyze([_row("east", _floor(0.97)), _row("west", _floor(0.98))],
                             {"hit_ratio": "0.95"}, _LOWER_MAP)
        text = tr.format_text_report([report])
        assert ('    └─ da_cluster="east": P5 floor 0.9700, recommend 0.97, delta -40.0% miss, '
                'HIGH, 1800 samples') in text
        assert ('    └─ da_cluster="west": P5 floor 0.9800, recommend 0.98, delta -60.0% miss, '
                'HIGH, 1800 samples') in text
        assert "P95" not in "".join(tr._cluster_lines(report.keys[0]))

    def test_a_cluster_without_a_value_shows_its_reason(self):
        report, _ = _analyze([_row("dr", _nan()), _row("prod", _WEST)],
                             {"connections": 50}, _UPPER_MAP)
        assert ('    └─ da_cluster="dr": P95 —, recommend —, delta —, LOW, 0 samples '
                '(no data points found)') in tr.format_text_report([report])

    def test_json_carries_clusters(self):
        key = tr.json_document([self._report()])["tenants"][0]["keys"][0]
        assert key["conservative_cluster"] == "west" and key["recommended"] == 90
        assert (key["held_by_cluster"], key["direction"]) == (None, ">")
        assert [c["da_cluster"] for c in key["clusters"]] == ["east", "west"]

    def test_export_patch_writes_the_conservative_value(self):
        patch_text = tr.format_export_patch([self._report()])
        assert '    connections: "90"   # was 50' in patch_text
        assert '[da_cluster="west" of 2 clusters: highest value]' in patch_text

    def test_a_cluster_name_cannot_break_any_report(self):
        """叢集名稱來自 Prometheus：`|`、反引號、HTML、換行都不得變成標記——
        Reason 欄的主原因與逐叢集那一段都一樣；patch 的註解仍是一行。"""
        evil = "a|b`c<b>d\n  x: 1"
        report, rec = _analyze([_row(evil, _WEST), _row("west", _EAST)],
                               {"connections": 50}, _UPPER_MAP)
        assert rec.conservative_cluster == evil
        md = tr.format_markdown_report([report])
        row = next(line for line in md.splitlines() if line.startswith("| connections"))
        shown = 'da\\_cluster="a\\|b\\`c\\<b\\>d?  x: 1"'
        assert f"\\[{shown} of 2 clusters: highest value\\]<br>per cluster: {shown}: P95" in row
        assert len(_UNESCAPED_PIPE.findall(row)) == 8          # 仍是七欄的一列
        assert "<b>" not in row and "`" not in row.replace("\\`", "")
        assert len(md.splitlines()) == 7                       # 表格仍只有一列資料
        patch_text = tr.format_export_patch([report])
        assert [line for line in patch_text.splitlines()
                if not line.startswith(("#", " ", "tenants:"))] == []
        assert "\n  x: 1" not in patch_text


class TestGovernanceSeesAHeldKey:
    """被某個叢集擋下的 key 不能從治理迴圈裡消失：export-patch 的註解與
    threshold-govern 的報告都要帶原因。"""

    def _held(self):
        return _analyze([_row("mixed", _floor(0.905)), _row("prod", _floor(0.97))],
                        {"hit_ratio": "0.90"}, _LOWER_MAP)[0]

    _WHY = ('lower-bound P5 floor → tighten (raise floor); miss-rate -70.0% — not recommended: '
            'a floor of 0.97 would start firing in da_cluster="mixed" '
            '(1800 of its samples are at or above current and below it) '
            '[da_cluster="prod" of 2 clusters: lowest floor]')

    def test_export_patch_names_it_as_a_comment(self):
        assert f"# [t-x] (skipped) hit_ratio: {self._WHY}" in tr.format_export_patch([self._held()])

    def test_an_unlabelled_cluster_that_holds_is_listed_too(self):
        """The group with no `da_cluster` is named "" — "no cluster held it" must
        be a value no cluster can have, or this key vanishes from the run."""
        report = _analyze([_row(None, _floor(0.905)), _row("prod", _floor(0.97))],
                          {"hit_ratio": "0.90"}, _LOWER_MAP)[0]
        rec = report.keys[0]
        assert (rec.recommended, rec.held_by_cluster) == (None, "")
        held = tg.collect_cluster_held([report], 20.0)
        assert [(k.key, k.cluster) for k in held] == [("hit_ratio", "")]

    def test_govern_lists_it_with_its_reason(self):
        report = self._held()
        assert tg.build_governance_plan([report], 20.0) == []
        held = tg.collect_cluster_held([report], 20.0)
        assert held == [tg.ClusterHeldKey("t-x", "hit_ratio", "mixed", self._WHY)]
        with patch.object(tg, "_LANG", "en"):
            text = tg.format_text_report([], held_by_cluster=held)
        assert ("⚠ 1 threshold(s) held back by one cluster, not in the plan "
                "(several clusters — #1200; reason per line):\n"
                f"    t-x / hit_ratio: {self._WHY}") in text
        doc = tg.json_document([], held_by_cluster=held)
        assert doc["held_by_cluster"] == [{"tenant": "t-x", "key": "hit_ratio",
                                           "cluster": "mixed", "reason": self._WHY}]
        assert doc["summary"]["held_by_cluster"] == 1

    def test_a_held_value_that_is_still_actionable_is_in_the_plan_only(self):
        """演練把 50 抬到 80（−20%）：仍過 20% 的門檻 → 進計畫（帶原因），不重複列。"""
        report, _ = _analyze([_row("dr", _flat([0] * 970 + [80] * 30)),
                              _row("prod", _flat([20] * 1000))], {"connections": 100}, _UPPER_MAP)
        (plan,) = tg.build_governance_plan([report], 20.0)
        assert (plan.changes[0].recommended, plan.changes[0].delta_pct) == (80, -20.0)
        assert 'held at 80 instead of 50: da_cluster="dr" has values up to 80' in (
            plan.changes[0].reason)
        assert tg.collect_cluster_held([report], 20.0) == []
        # 門檻調到 30%：−20% 進不了計畫 → 列為被擋下，不消失。
        assert tg.build_governance_plan([report], 30.0) == []
        assert [h.cluster for h in tg.collect_cluster_held([report], 30.0)] == ["dr"]

    def test_a_run_with_no_held_key_has_the_document_it_had(self):
        doc = tg.json_document([])
        assert "held_by_cluster" not in doc and "held_by_cluster" not in doc["summary"]

    def test_run_collects_held_keys(self, monkeypatch):
        report = self._held()
        monkeypatch.setattr(tg.recommend, "run_analysis", lambda *a, **k: [report])
        result = tg.run(SimpleNamespace(config_dir="x", prometheus="http://prom:9090",
                                        tenant=None, lookback="7d", min_samples=100,
                                        min_delta_pct=20.0))
        assert [h.key for h in result.held_by_cluster] == ["hit_ratio"]
