"""validate／shadow-verify 以（租戶、叢集）對值（#1200，ADR-006「多叢集同租戶」）。

同一個租戶的兩列不互相覆蓋；逐叢集比對；只在一邊出現的叢集是點名叢集的
mismatch；沒有叢集 label 的輸入，輸出與先前逐位元組相同（既有測試不動，見
test_validate_migration.py）。

migrate-rule 那一半（原式子的分組沒有 da_cluster 時標記、不改寫）在
test_migrate_rule.py；#4226 之前它產生逐叢集聚合的 recording rule，那條路徑已移除。
"""
from __future__ import annotations

import csv
import io
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

_TOOLS = Path(__file__).resolve().parents[2] / "scripts" / "tools"
sys.path.insert(0, str(_TOOLS / "ops"))
sys.path.insert(0, str(_TOOLS / "lint"))
sys.path.insert(0, str(_TOOLS))

import shadow_verify as sv  # noqa: E402
import validate_migration as vm  # noqa: E402
from _lib_shadow_report import REPORT_COLUMNS, split_tenant_cell, tenant_cell  # noqa: E402


# ---------------------------------------------------------------------------
# validate：以（租戶、叢集）對值
# ---------------------------------------------------------------------------

def _row(value, tenant="t1", **labels):
    return {"metric": {"tenant": tenant, **labels}, "value": [0, str(value)]}


def test_two_clusters_of_one_tenant_do_not_overwrite_each_other():
    got = vm.extract_value_map([_row(60, da_cluster="east"), _row(150, da_cluster="west"),
                                _row(7, tenant="t2")])
    assert got == {vm.ClusterKey("t1", "east"): 60.0, vm.ClusterKey("t1", "west"): 150.0,
                   "t2": 7.0}


def test_an_unlabelled_row_is_its_own_group():
    got = vm.extract_value_map([_row(1), _row(2, da_cluster="east"), _row(3, da_cluster="")])
    # 空字串的 label 就是沒有這個 label
    assert got == {"t1": 3.0, vm.ClusterKey("t1", "east"): 2.0}


def test_values_are_compared_per_cluster():
    old = vm.extract_value_map([_row(60, da_cluster="east"), _row(150, da_cluster="west")])
    new = vm.extract_value_map([_row(60, da_cluster="east"), _row(151, da_cluster="west")])
    assert vm.compare_vectors(old, new) == [
        {"tenant": "t1", "da_cluster": "east", "old_value": 60.0, "new_value": 60.0,
         "status": "match", "delta": 0.0},
        {"tenant": "t1", "da_cluster": "west", "old_value": 150.0, "new_value": 151.0,
         "status": "mismatch", "delta": 1.0},
    ]


def test_a_row_without_a_cluster_compares_as_it_always_did():
    """沒有叢集 label 的輸入：差異的每一欄與先前相同，沒有多出來的欄。"""
    diffs = vm.compare_vectors(vm.extract_value_map([_row(5)]), vm.extract_value_map([_row(5)]))
    assert diffs == [{"tenant": "t1", "old_value": 5.0, "new_value": 5.0,
                      "status": "match", "delta": 0.0}]


@pytest.mark.parametrize("old_rows,new_rows,missing_side", [
    ([_row(60, da_cluster="east"), _row(60, da_cluster="west")],
     [_row(60, da_cluster="east")], "new"),
    ([_row(60, da_cluster="east")],
     [_row(60, da_cluster="east"), _row(60, da_cluster="west")], "old"),
])
def test_a_cluster_on_one_side_only_is_a_mismatch_naming_the_cluster(
        old_rows, new_rows, missing_side, capsys, tmp_path):
    diffs = vm.compare_vectors(vm.extract_value_map(old_rows), vm.extract_value_map(new_rows))
    assert [(d["tenant"], d["da_cluster"], d["status"]) for d in diffs] == [
        ("t1", "east", "match"), ("t1", "west", "mismatch")]
    west = diffs[1]
    assert west["delta"] is None
    assert west[f"{missing_side}_value"] is None
    result = {"label": "custom_x", "old_query": "o", "new_query": "n",
              "old_count": len(old_rows), "new_count": len(new_rows), "diffs": diffs}
    assert vm.classify_results([result]) == (True, False)
    vm.print_summary([result])
    out = capsys.readouterr().out
    assert "數值差異: 1" in out and "安全切換" not in out
    assert "[custom_x] tenant=t1 da_cluster=west:" in out
    assert "這個叢集只在一邊出現" in out
    rows = _csv_rows(tmp_path, [result])
    assert [(r["Tenant"], r["Status"]) for r in rows] == [
        ("t1 (da_cluster=east)", "match"), ("t1 (da_cluster=west)", "mismatch")]


def test_a_rule_still_grouped_by_tenant_against_a_per_cluster_one_is_reported():
    """舊版 migrate 產的 `by(tenant)` 規則對上新的逐叢集規則：三列都不是 match。"""
    old = vm.extract_value_map([_row(120)])
    new = vm.extract_value_map([_row(60, da_cluster="east"), _row(60, da_cluster="west")])
    assert [(d["tenant"], d.get("da_cluster"), d["status"])
            for d in vm.compare_vectors(old, new)] == [
        ("t1", None, "new_missing"), ("t1", "east", "mismatch"), ("t1", "west", "mismatch")]


def test_a_value_mismatch_in_one_cluster_names_it(capsys):
    old = vm.extract_value_map([_row(60, da_cluster="east"), _row(150, da_cluster="west")])
    new = vm.extract_value_map([_row(60, da_cluster="east"), _row(120, da_cluster="west")])
    vm.print_summary([{"label": "custom_x", "old_query": "o", "new_query": "n",
                       "diffs": vm.compare_vectors(old, new)}])
    assert ("  • [custom_x] tenant=t1 da_cluster=west: 舊=150.0 新=120.0 (差異=-30.0000)\n"
            in capsys.readouterr().out)


def test_run_single_comparison_pairs_per_cluster():
    answers = {"old": [_row(60, da_cluster="east"), _row(60, da_cluster="west")],
               "new": [_row(60, da_cluster="east"), _row(60, da_cluster="west")]}
    with mock.patch.object(vm, "query_prometheus", side_effect=lambda url, q: answers[q]):
        result = vm.run_single_comparison("http://p", "old", "new", "custom_x")
    assert [(d["da_cluster"], d["status"]) for d in result["diffs"]] == [
        ("east", "match"), ("west", "match")]


def _csv_rows(tmp_path, results):
    path = vm.write_csv_report(results, tmp_path / "report")
    text = Path(path).read_text(encoding="utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    assert tuple(reader.fieldnames) == REPORT_COLUMNS
    return list(reader)


def test_the_report_of_a_run_without_clusters_holds_the_tenant_alone(tmp_path):
    diffs = vm.compare_vectors({"t1": 1.0}, {"t1": 1.0})
    (row,) = _csv_rows(tmp_path, [{"label": "k", "old_query": "o", "new_query": "n",
                                   "diffs": diffs}])
    assert row["Tenant"] == "t1"


_HOSTILE_CLUSTERS = ["east", "dc/eu:1", ")", "(", "=", "|", "a) (da_cluster=b",
                     " (da_cluster=x)", "a\nb", "a\r\nb", "\\", "\\)", "x\\n", "a(b)c\\\\",
                     "東", "a,b\"c"]


@pytest.mark.parametrize("cluster", _HOSTILE_CLUSTERS, ids=repr)
@pytest.mark.parametrize("tenant", ["t1", "__no_label__", "t (da_cluster=x)",
                                    "a) (da_cluster=b", "t\\"], ids=repr)
def test_the_tenant_cell_reads_back_whatever_the_cluster_is_called(tenant, cluster):
    cell = tenant_cell(tenant, cluster)
    assert split_tenant_cell(cell) == (tenant, cluster)
    assert "\n" not in cell and "\r" not in cell   # 一列仍是一行


def test_a_cell_without_a_cluster_is_the_tenant_itself():
    assert tenant_cell("t1") == tenant_cell("t1", None) == tenant_cell("t1", "") == "t1"
    assert split_tenant_cell("t1") == ("t1", None)
    assert tenant_cell("t1", "east") == "t1 (da_cluster=east)"
    assert tenant_cell("t1", "a) (da_cluster=b") == "t1 (da_cluster=a\\) \\(da_cluster=b)"


def test_a_hostile_cluster_name_survives_the_csv(tmp_path):
    cluster = "a) (da_cluster=b\n|=("
    diffs = vm.compare_vectors({vm.ClusterKey("t1", cluster): 1.0},
                               {vm.ClusterKey("t1", cluster): 1.0})
    (row,) = _csv_rows(tmp_path, [{"label": "k", "old_query": "o", "new_query": "n",
                                   "diffs": diffs}])
    assert split_tenant_cell(row["Tenant"]) == ("t1", cluster)


def test_shadow_verify_counts_a_tenant_once_however_many_clusters(tmp_path):
    ts = "2026-03-15 10:00:00"
    records = [
        {**dict.fromkeys(REPORT_COLUMNS, ""), "Tenant": tenant_cell("t1", "east"),
         "Status": "match", "Timestamp": ts},
        {**dict.fromkeys(REPORT_COLUMNS, ""), "Tenant": tenant_cell("t1", "west"),
         "Status": "mismatch", "Timestamp": ts},
        {**dict.fromkeys(REPORT_COLUMNS, ""), "Tenant": "t2", "Status": "match",
         "Timestamp": ts},
    ]
    args = SimpleNamespace(prometheus="http://localhost:9090")
    with mock.patch.object(sv, "query_prometheus", return_value=([], None)):
        checks = {c["check"]: c for c in sv.check_runtime(args, {"report_csv": records})}
    assert checks["csv_tenant_coverage"]["detail"] == "2 tenants in report: t1, t2"
    assert checks["csv_mismatch_ratio"]["status"] == "fail"
    assert checks["csv_mismatch_ratio"]["detail"].startswith("1/3 mismatches")
