"""validate-config `custom_alert_cap`（#3042 決定 1）：租戶自己的 `_custom_alerts`
送出的列數上限，在 CI 擋。

計數是 da-guard served-values 的 `custom_alert_rows`，與 da-guard
`custom_alert_over_cap` 判的是同一個數（`pkg/config.CustomAlertRowCount`）：
清單要 exporter 送出的列，在每租戶指標上限（`max_metrics_per_tenant`）截斷之前
數。停用的不送、不計；slo_burn_rate 送兩列、計 2；被當成重複丟掉的不計
（tenant-api 寫入檢查則直接拒收重複）。只有租戶檔與根平台檔 `tenants:` 的清單算
租戶自己的；profile 給的是它的租戶共用的形狀，不計。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import validate_config as vc
from _lib_constants import MAX_CUSTOM_RECIPES

pytestmark = pytest.mark.usefixtures("da_guard_env")

_REPO = Path(__file__).resolve().parents[2]
_ROOT = "defaults:\n  mysql_connections: 80\n"


def _thr(i: int, threshold: str = "10:warning") -> str:
    return (f"      - {{recipe: threshold, name: r{i}, metric: m{i}, op: '>', window: 5m, "
            f"threshold: '{threshold}'}}\n")


def _slo(i: int) -> str:
    return (f"      - {{recipe: slo_burn_rate, name: s{i}, metric: err{i}, "
            f"denominator_metric: req{i}, objective: '99.9'}}\n")


def _list(recipes: list[str]) -> str:
    return "    _custom_alerts:\n" + "".join(recipes)


def _tree(root: Path, files: dict[str, str]) -> str:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    return str(root)


def test_the_cap_is_the_write_checks():
    """Python 與 Go 的上限是同一個數字：兩邊都取 recipe 契約檔的 custom_alert_cap。"""
    path = _REPO / "components/threshold-exporter/app/pkg/config/recipe_contract.json"
    contract = json.loads(path.read_text(encoding="utf-8"))
    assert contract["custom_alert_cap"] == MAX_CUSTOM_RECIPES


@pytest.mark.parametrize("recipes, status, rows", [
    ([_thr(i) for i in range(MAX_CUSTOM_RECIPES)], vc.PASS, None),
    ([_thr(i) for i in range(MAX_CUSTOM_RECIPES + 1)], vc.FAIL, MAX_CUSTOM_RECIPES + 1),
    # slo_burn_rate 送兩列（critical、warning），計 2。
    ([_slo(i) for i in range(MAX_CUSTOM_RECIPES // 2)], vc.PASS, None),
    ([_slo(i) for i in range(MAX_CUSTOM_RECIPES // 2)] + [_thr(99)], vc.FAIL, MAX_CUSTOM_RECIPES + 1),
    # 停用的不送列，不計。
    ([_thr(i) for i in range(MAX_CUSTOM_RECIPES)] + [_thr(99, "disable")], vc.PASS, None),
    # 同形狀、同嚴重度的第二條被 exporter 丟掉（#3112），不計；da-guard 另報
    # custom_alert_duplicate_series。
    ([_thr(i) for i in range(MAX_CUSTOM_RECIPES)]
     + [_thr(0).replace("name: r0", "name: r0-again")], vc.PASS, None),
], ids=["at-cap", "over", "slo-at-cap", "slo-over", "disabled-not-counted", "duplicate-not-counted"])
def test_a_tenants_own_rows_are_counted(tmp_path, recipes, status, rows):
    r = vc.check_custom_alert_cap(_tree(tmp_path, {
        "_defaults.yaml": _ROOT, "t1.yaml": "tenants:\n  t1:\n" + _list(recipes)}))
    assert r["status"] == status, r
    if status == vc.FAIL:
        assert r["details"] == [f"t1.yaml: tenant t1 asks the exporter to serve {rows} custom-alert rows from its "
                                f"own `_custom_alerts`, over the per-tenant cap of {MAX_CUSTOM_RECIPES}"]


def test_a_root_platform_files_entry_is_the_tenants_own(tmp_path):
    r = vc.check_custom_alert_cap(_tree(tmp_path, {
        "_defaults.yaml": _ROOT,
        "_platform.yaml": "tenants:\n  t1:\n" + _list([_thr(i) for i in range(MAX_CUSTOM_RECIPES + 1)]),
        "t1.yaml": "tenants:\n  t1:\n    mysql_connections: 70\n"}))
    assert r["status"] == vc.FAIL
    assert r["details"][0].startswith("_platform.yaml: tenant t1 asks the exporter to serve 21 custom-alert rows")


def test_a_profiles_recipes_are_not_counted(tmp_path):
    r = vc.check_custom_alert_cap(_tree(tmp_path, {
        "_defaults.yaml": _ROOT,
        "_profiles.yaml": "profiles:\n  p1:\n" + _list([_thr(i) for i in range(MAX_CUSTOM_RECIPES + 1)]),
        "t1.yaml": "tenants:\n  t1:\n    _profile: p1\n"}))
    assert r["status"] == vc.PASS, r
    assert r["details"] == [f"0 tenant(s) writing their own `_custom_alerts`, each within the cap of "
                            f"{MAX_CUSTOM_RECIPES} rows"]


def test_da_guard_failing_is_not_counted_never_a_fail(tmp_path, monkeypatch):
    monkeypatch.setenv("DA_GUARD_BINARY", str(tmp_path / "no-such-da-guard"))
    r = vc.check_custom_alert_cap(_tree(tmp_path, {"_defaults.yaml": _ROOT, "t1.yaml": "tenants:\n  t1: {}\n"}))
    assert r["status"] == vc.WARN
    assert r["details"][0].startswith("not counted: the custom alerts the exporter serves were not read")


def test_rows_are_counted_before_the_per_tenant_metric_limit_truncates_them(tmp_path):
    """`max_metrics_per_tenant` 把送出的列截到 10 條，served-values 的
    `_custom_alerts` 只剩 10 條；上限判的是截斷前的 21 條，與 da-guard 一致。"""
    d = _tree(tmp_path, {
        "_defaults.yaml": _ROOT + "max_metrics_per_tenant: 10\n",
        "t1.yaml": "tenants:\n  t1:\n" + _list([_thr(i) for i in range(MAX_CUSTOM_RECIPES + 1)])})
    assert len(vc.load_served_tree(d).tenants["t1"].values["_custom_alerts"]) == 10
    r = vc.check_custom_alert_cap(d)
    assert r["status"] == vc.FAIL, r
    assert r["details"] == [f"t1.yaml: tenant t1 asks the exporter to serve {MAX_CUSTOM_RECIPES + 1} custom-alert rows from "
                            f"its own `_custom_alerts`, over the per-tenant cap of {MAX_CUSTOM_RECIPES}"]


def test_a_da_guard_without_the_count_is_not_counted(tmp_path, monkeypatch):
    real = vc.load_served_tree

    def stale(*a, **k):
        tree = real(*a, **k)
        return tree._replace(tenants={t: v._replace(custom_alert_rows=None) for t, v in tree.tenants.items()})
    monkeypatch.setattr(vc, "load_served_tree", stale)
    r = vc.check_custom_alert_cap(_tree(tmp_path, {
        "_defaults.yaml": _ROOT, "t1.yaml": "tenants:\n  t1:\n" + _list([_thr(0)])}))
    assert r["status"] == vc.WARN
    assert r["details"] == ["not counted: da-guard served-values has no `custom_alert_rows` — this "
                            "da-guard is older than this tool: upgrade or rebuild it"]
