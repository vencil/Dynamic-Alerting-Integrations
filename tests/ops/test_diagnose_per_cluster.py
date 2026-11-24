"""diagnose：exporter 健康逐叢集判定（ADR-006 契約 6，#1200）。

先前的查詢是 `min by(tenant) (mysql_up{…})`：租戶宣告 `mariadb@east`、
`mariadb@west`，east 完全沒有序列、west 是 1 時回 `healthy`——同一時間
TenantExporterAbsent 正對 east 告警；east 是 0 時只說 DOWN，不說哪個叢集。

判定規則照告警的（_lib_exporter_presence.absent_expr），不另立一套：

* 宣告了叢集：該叢集沒有帶自己 `da_cluster` 的 `mysql_up` 就是缺席；
  但一條**不帶** `da_cluster` 的序列對每個宣告的叢集都算存在。
* 沒宣告叢集：任何一個叢集有序列就算存在（行為不變）。

本工具的 Pod／exporter 檢查只涵蓋 mariadb 一種，所以鏡像的是告警對 mariadb
（`mysql_up`）的判定。
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

import pytest

_TOOLS = Path(__file__).resolve().parents[2] / "scripts" / "tools"
sys.path.insert(0, str(_TOOLS))
sys.path.insert(0, str(_TOOLS / "ops"))
import _lib_cluster_identity  # noqa: E402
import _lib_exporter_presence  # noqa: E402
import batch_diagnose  # noqa: E402
import diagnose  # noqa: E402

PROM = "http://prom:9090"
DOWN = "Exporter reports DOWN (mysql_up!=1)"


def _declared(*clusters):
    """tenant_expected_exporter 的回應：沒給叢集就是只寫種類的那一條。"""
    if not clusters:
        return [{"metric": {"tenant": "t1", "pack": "mariadb"}, "value": [0, "1"]}]
    return [{"metric": {"tenant": "t1", "pack": "mariadb", "expected_cluster": c},
             "value": [0, "1"]} for c in clusters]


def _up(**by_cluster):
    """健康查詢的回應：`_up(east="1")`；key `none` 是不帶 da_cluster 的那一條。"""
    rows = []
    for cluster, value in by_cluster.items():
        metric = {"tenant": "t1"}
        if cluster != "none":
            metric["da_cluster"] = cluster
        rows.append({"metric": metric, "value": [0, value]})
    return rows


def _run(declared, up, *, pod="Running", maintenance=(), silent=()):
    """跑一次 check()；回 (文件, 送出的查詢)。"""
    asked = []

    def fake(_url, query):
        asked.append(query)
        if query.startswith("tenant_expected_exporter"):
            return declared, None
        if "mysql_up{" in query:
            return up, None
        if query.startswith("user_state_filter"):
            return list(maintenance), None
        if query.startswith("user_silent_mode"):
            return list(silent), None
        raise AssertionError(f"unexpected query: {query}")

    with mock.patch.object(diagnose, "query_prometheus", side_effect=fake), \
            mock.patch.object(diagnose, "pod_phase", return_value=pod), \
            mock.patch.object(diagnose, "mariadb_logs", return_value=""):
        return diagnose.check("t1", PROM), asked


# ── 重現的兩個案例 ────────────────────────────────────────────────


def test_a_declared_cluster_with_no_series_is_absent_not_healthy():
    doc, _ = _run(_declared("east", "west"), _up(west="1"))
    assert doc["status"] == "error", doc
    assert doc["issues"] == ["Exporter absent in east"], doc
    assert diagnose.exit_code(doc) == 1   # 與 exporter DOWN 同級
    assert doc["exporter_clusters"] == [
        {"cluster": "east", "declared": True, "state": "absent"},
        {"cluster": "west", "declared": True, "state": "up"}], doc


def test_a_down_exporter_names_its_cluster():
    doc, _ = _run(_declared("east", "west"), _up(east="0", west="1"))
    assert doc["issues"] == ["Exporter reports DOWN in east (mysql_up!=1)"], doc
    assert diagnose.exit_code(doc) == 1
    assert [e["state"] for e in doc["exporter_clusters"]] == ["down", "up"], doc


def test_several_down_clusters_are_all_named_without_a_declaration():
    """沒宣告也一樣：序列帶 da_cluster，DOWN 就點名。"""
    doc, _ = _run(_declared(), _up(east="0", north="1", west="0"))
    assert doc["issues"] == ["Exporter reports DOWN in east, west (mysql_up!=1)"], doc


# ── 與告警同一條規則 ──────────────────────────────────────────────


def test_an_unlabelled_series_counts_for_every_declared_cluster():
    """自己抓 exporter 的 Prometheus 序列上沒有 da_cluster：`kind@cluster` 在那裡
    讀成只寫種類，不對每個宣告的叢集報缺席（告警的第二個 `unless`）。"""
    doc, _ = _run(_declared("east", "west"), _up(none="1"))
    assert doc["status"] == "healthy", doc
    assert doc["exporter_clusters"] == [
        {"cluster": None, "declared": False, "state": "up"},
        {"cluster": "east", "declared": True, "state": "unlabelled"},
        {"cluster": "west", "declared": True, "state": "unlabelled"}], doc


def test_no_declaration_and_one_cluster_missing_is_unchanged():
    """沒宣告叢集：至少一個叢集有序列就算存在；一個叢集、沒宣告 ⇒ 文件不多欄位。"""
    doc, _ = _run(_declared(), _up(west="1"))
    assert doc == {"status": "healthy", "tenant": "t1"}, doc


def test_declared_clusters_and_nothing_answering_names_each_cluster():
    doc, _ = _run(_declared("east", "west"), [])
    assert doc["issues"] == ["Exporter absent in east", "Exporter absent in west"], doc


def test_federated_copies_of_one_declaration_are_one_cluster():
    """同一條宣告經兩個 edge federate 上來是兩條序列（各帶該 edge 的 da_cluster）；
    宣告的叢集讀 expected_cluster，不讀那個 da_cluster。"""
    rows = [{"metric": {"tenant": "t1", "pack": "mariadb", "expected_cluster": "east",
                        "da_cluster": via}, "value": [0, "1"]} for via in ("east", "west")]
    doc, _ = _run(rows, _up(east="1", west="1"))
    assert doc["status"] == "healthy", doc
    assert doc["exporter_clusters"] == [
        {"cluster": "east", "declared": True, "state": "up"},
        {"cluster": "west", "declared": False, "state": "up"}], doc


def test_another_kinds_clusters_are_not_mariadbs():
    """`redis@north` 不是 mariadb 的宣告：不會多出一個缺席的 north。"""
    rows = _declared() + [{"metric": {"tenant": "t1", "pack": "redis",
                                      "expected_cluster": "north"}, "value": [0, "1"]}]
    doc, _ = _run(rows, _up(none="1"))
    assert doc == {"status": "healthy", "tenant": "t1"}, doc


# ── 兩個叢集都正常 ────────────────────────────────────────────────


@pytest.mark.parametrize("declared", [_declared("east", "west"), _declared()])
def test_two_clusters_up_is_healthy_and_lists_both(declared):
    doc, _ = _run(declared, _up(east="1", west="1"))
    assert doc["status"] == "healthy" and "issues" not in doc, doc
    assert [(e["cluster"], e["state"]) for e in doc["exporter_clusters"]] == [
        ("east", "up"), ("west", "up")], doc


# ── 單一 Prometheus：逐位元組不變 ─────────────────────────────────


@pytest.mark.parametrize("up,want", [
    (_up(none="1"), {"status": "healthy", "tenant": "t1"}),
    (_up(none="0"), {"status": "error", "tenant": "t1", "issues": [DOWN], "recent_logs": []}),
    ([], {"status": "error", "tenant": "t1", "issues": [DOWN], "recent_logs": []}),
    # 舊替身的列沒有 `metric`：同樣是那一條不帶叢集的序列。
    ([{"value": [0, "1"]}], {"status": "healthy", "tenant": "t1"}),
])
def test_a_single_prometheus_reads_as_before(up, want):
    doc, _ = _run(_declared(), up)
    assert doc == want, doc
    assert diagnose.exit_code(doc) == (0 if want["status"] == "healthy" else 1)


def test_the_health_query_is_per_cluster():
    _, asked = _run(_declared(), _up(none="1"))
    assert asked[1] == ('min by(tenant, da_cluster) '
                        '(mysql_up{job="tenant-exporters",tenant="t1"})'), asked


# ── Pod 檢查：只看目前的 kubectl context ──────────────────────────


def test_pod_not_found_is_not_a_tenant_wide_failure_across_clusters():
    """跨叢集的租戶：kubectl 目前的 context 只是其中一個（或根本不是）叢集。
    那裡沒有 Pod 不是「租戶的 Pod 不見了」——文件說明檢查範圍，不列為 issue。"""
    doc, _ = _run(_declared("east", "west"), _up(east="1", west="1"), pod=None)
    assert doc["status"] == "healthy", doc
    assert "Pod not found" not in str(doc), doc
    assert doc["pod_check"] == {"scope": "current kubectl context only", "phase": None,
                                "note": diagnose.POD_NOT_FOUND_ELSEWHERE}, doc


@pytest.mark.parametrize("declared,up", [
    (_declared("east", "west"), _up(east="1", west="1")),
    (_declared(), _up(east="1", west="1")),          # 沒宣告，但序列來自兩個叢集
    (_declared("east"), _up(none="1")),              # 有宣告，序列不帶叢集
])
def test_the_pod_check_states_its_scope_across_clusters(declared, up):
    doc, _ = _run(declared, up)
    assert doc["pod_check"] == {"scope": "current kubectl context only", "phase": "Running"}, doc


def test_a_pod_that_is_there_and_not_running_is_still_an_issue_across_clusters():
    doc, _ = _run(_declared("east", "west"), _up(east="1", west="1"), pod="Pending")
    assert doc["issues"] == ["Pod status is Pending"], doc
    assert doc["pod_check"]["phase"] == "Pending", doc


def test_pod_not_found_in_one_cluster_is_the_issue_it_was():
    doc, _ = _run(_declared(), _up(none="1"), pod=None)
    assert doc == {"status": "error", "tenant": "t1", "issues": ["Pod not found"],
                   "recent_logs": []}, doc


def test_a_failed_health_query_keeps_the_scope_note_for_a_declared_tenant():
    """健康查詢失敗時叢集數不明；有逐叢集宣告就已經知道租戶跨叢集。"""
    def fake(_url, query):
        if query.startswith("tenant_expected_exporter"):
            return _declared("east", "west"), None
        if "mysql_up{" in query:
            return None, "connection refused"
        return [], None
    with mock.patch.object(diagnose, "query_prometheus", side_effect=fake), \
            mock.patch.object(diagnose, "pod_phase", return_value=None), \
            mock.patch.object(diagnose, "mariadb_logs", return_value=""):
        doc = diagnose.check("t1", PROM)
    assert doc["issues"] == [f"Prometheus query failed ({PROM})"], doc
    assert diagnose.exit_code(doc) == 2
    assert doc["pod_check"]["phase"] is None and "exporter_clusters" not in doc, doc


# ── 維護／靜音：federate 上來的多條副本不改變判定 ─────────────────


def _copies(metric):
    return [{"metric": {**metric, "tenant": "t1", "da_cluster": c}, "value": [0, "1"]}
            for c in ("east", "west")]


def test_federated_copies_of_the_maintenance_series_read_as_one():
    one, _ = _run(_declared(), _up(none="1"),
                  maintenance=[{"metric": {"tenant": "t1", "filter": "maintenance"},
                                "value": [0, "1"]}])
    many, _ = _run(_declared(), _up(none="1"), maintenance=_copies({"filter": "maintenance"}))
    assert many == one == {"status": "healthy", "tenant": "t1",
                           "operational_mode": "maintenance"}


@pytest.mark.parametrize("severities,mode", [
    (["warning"], "silent:warning"),
    (["warning", "critical"], "silent:all"),
])
def test_federated_copies_of_the_silent_series_read_as_one(severities, mode):
    single = [{"metric": {"tenant": "t1", "target_severity": s}, "value": [0, "1"]}
              for s in severities]
    copies = [row for s in severities for row in _copies({"target_severity": s})]
    one, _ = _run(_declared(), _up(none="1"), silent=single)
    many, _ = _run(_declared(), _up(none="1"), silent=copies)
    assert many == one == {"status": "healthy", "tenant": "t1", "operational_mode": mode}


# ── 與告警共用的名字；batch-diagnose ──────────────────────────────


def test_the_names_are_the_alerts_own():
    """da-tools 映像不帶 _lib_exporter_presence，所以 diagnose 自己寫這兩個名字；
    這裡把它們釘在告警用的那一份上。"""
    assert diagnose.EXPECTED_CLUSTER_LABEL == _lib_exporter_presence.EXPECTED_CLUSTER_LABEL
    assert _lib_exporter_presence.pack_presence(diagnose.MARIADB_KIND) == (
        diagnose.MARIADB_PRESENCE_METRIC,)
    assert diagnose.CLUSTER_IDENTITY_LABEL is _lib_cluster_identity.CLUSTER_IDENTITY_LABEL
    assert _lib_exporter_presence.CLUSTER_LABEL == diagnose.CLUSTER_IDENTITY_LABEL
    assert diagnose.TENANT_EXPORTER_JOB in _lib_exporter_presence.JOB_SELECTOR


def test_batch_diagnose_counts_an_absent_cluster_as_an_issue():
    absent, _ = _run(_declared("east", "west"), _up(west="1"))
    healthy, _ = _run(_declared("east", "west"), _up(east="1", west="1"), pod=None)
    healthy["tenant"] = "t2"
    report = batch_diagnose.generate_report([absent, healthy], PROM)
    assert (report["issue_count"], report["healthy_count"]) == (1, 1), report
    assert report["tenants"]["t1"]["exporter_clusters"][0]["state"] == "absent"
    assert report["tenants"]["t2"]["pod_check"]["scope"] == "current kubectl context only"
    assert len(report["recommendations"]) == 1 and "Check exporter" in report["recommendations"][0]
    assert batch_diagnose.batch_exit_code([absent, healthy]) == 1
