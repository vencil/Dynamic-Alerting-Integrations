"""validate-config `no_alert`（ADR-038 決定 10、#3591）：送出了、但出貨的告警規則都不比較的閾值鍵。

送出的鍵取自共用判決（exporter 讀、/metrics 有送的鍵，帶值來自哪個檔）；有沒有告警比較它，
問 `metric_observed_map.yaml`（`_observed_map_lib.lookup`，由
`check_threshold_observed_map.py` 守住與規則包一致），不另建一份鍵→告警對照。
永遠只是建議：最多 WARN，不影響 exit code。租戶 id 是 fixture 名（CLAUDE.md #9）。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import validate_config as vc
from _registry_lib import DEPRECATED_KEY_ALIASES

pytestmark = pytest.mark.usefixtures("da_guard_env")

_REPO = Path(__file__).resolve().parents[2]
_DEFAULTS = ("defaults:\n  mysql_connection_usage_percent: 80\n  mysql_connectoins: 5\n  redis_evicted_keys_rate: 70\n"
             "  container_cpu: 50\ndimensions:\n  container_cpu: [version]\n")


def _tree(root: Path, files: dict[str, str]) -> str:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    return str(root)


def test_a_served_key_no_alert_compares_is_one_line_on_its_file(tmp_path):
    """根 `defaults:` 的錯字送給每個租戶：一行、掛在 `_defaults.yaml`、列出租戶。
    沒有 critical 層的鍵（#3658 刻意不補的吞吐量鍵）上的 `<key>_critical` 一樣列出；有比較的、
    observed-map 延後判的（KNOWN_DEFERRED 的 `container_cpu`，含維度鍵）不列。"""
    r = vc.check_no_alert(_tree(tmp_path, {
        "_defaults.yaml": _DEFAULTS,
        "ta.yaml": "tenants:\n  ta:\n    mysql_connection_usage_percent: 70\n    mysql_connection_usage_percent_critical: 90\n",
        "team/tb.yaml": "tenants:\n  tb:\n    redis_evicted_keys_rate_critical: 90\n"
                        "    container_cpu{version=\"v2\"}: \"60\"\n"}))
    assert r["status"] == vc.WARN, r
    assert r["details"] == [
        "no_alert `mysql_connectoins` — served, but no shipped alert compares it "
        "— file(s) _defaults.yaml — tenant(s) ta, tb",
        "no_alert `redis_evicted_keys_rate_critical` — served, but no shipped alert "
        "compares it — file(s) team/tb.yaml — tenant(s) tb",
    ]


def test_one_line_per_key_whatever_the_files_and_dimensions(tmp_path):
    """判的是鍵：同一個鍵寫在多個檔、帶不同維度 label，仍是一行（正規 base），
    檔與租戶各自截到 WARN_LIMIT。行數的上界是平台宣告的鍵數，不隨租戶的 label 值增長。"""
    files = {"_defaults.yaml": "defaults:\n  redis_evicted_keys_rate: 70\n"
                               "dimensions:\n  redis_evicted_keys_rate: [db]\n"}
    for i in range(7):
        files[f"t{i}.yaml"] = (f"tenants:\n  t{i}:\n    redis_evicted_keys_rate_critical: 90\n"
                               f"    redis_evicted_keys_rate{{db=\"d{i}\"}}: \"80:critical\"\n")
    r = vc.check_no_alert(_tree(tmp_path, files))
    assert r["details"] == [
        "no_alert `redis_evicted_keys_rate` (served as critical) — served, but no shipped alert "
        "compares it — file(s) t0.yaml, t1.yaml, t2.yaml, t3.yaml, t4.yaml (+2 more) — "
        "tenant(s) t0, t1, t2, t3, t4 (+2 more)",
        "no_alert `redis_evicted_keys_rate_critical` — served, but no shipped alert compares it "
        "— file(s) t0.yaml, t1.yaml, t2.yaml, t3.yaml, t4.yaml (+2 more) — "
        "tenant(s) t0, t1, t2, t3, t4 (+2 more)",
    ], r


def test_every_key_compared_passes(tmp_path):
    r = vc.check_no_alert(_tree(tmp_path, {
        "_defaults.yaml": "defaults:\n  mysql_connection_usage_percent: 80\n",
        "ta.yaml": "tenants:\n  ta:\n    mysql_connection_usage_percent: 70\n"}))
    assert r["status"] == vc.PASS, r


def test_a_retired_spelling_is_looked_up_in_the_canonical_one(tmp_path, monkeypatch):
    """舊拼法的鍵照 exporter 的正規拼法查（問 da-guard key-spellings，Python 不留換名規則）：
    正規拼法有告警比較，就不列。#3883：送出的鍵與等級改讀 `--schedules-only`，它以正規拼法
    記鍵；這支斷言舊拼法的鍵確實算進送出的鍵、以正規拼法（含 critical 層）去查，而不是因為
    鍵被丟掉才通過。"""
    import _observed_map_lib
    old, new = next(iter(DEPRECATED_KEY_ALIASES.items()))
    asked: list[str] = []
    real = _observed_map_lib.lookup

    def spy(observed, key):
        asked.append(key)
        return real(observed, key)
    monkeypatch.setattr(_observed_map_lib, "lookup", spy)
    r = vc.check_no_alert(_tree(tmp_path, {
        "_defaults.yaml": f"defaults:\n  {new}: 80\n",
        "ta.yaml": f"tenants:\n  ta:\n    {old}: \"70:critical\"\n"}))
    assert r["status"] == vc.PASS, r
    assert r["details"][0].startswith("1 served key row(s)"), r
    assert asked == [f"{new}_critical"], asked


def test_a_retired_spelling_whose_canonical_key_no_alert_compares_is_listed(tmp_path, monkeypatch):
    """#3883 審查：正規拼法沒有出貨告警比較時，舊拼法寫的鍵要報出來，行上是正規拼法。"""
    import _observed_map_lib
    old, new = next(iter(DEPRECATED_KEY_ALIASES.items()))
    real = _observed_map_lib.load_observed_map
    monkeypatch.setattr(_observed_map_lib, "load_observed_map",
                        lambda *a, **kw: {k: e for k, e in real(*a, **kw).items()
                                          if k not in (new, f"{new}_critical")})
    r = vc.check_no_alert(_tree(tmp_path, {
        "_defaults.yaml": f"defaults:\n  {new}: 80\n",
        "ta.yaml": f"tenants:\n  ta:\n    {old}: 70\n"}))
    assert r["status"] == vc.WARN, r
    assert r["details"] == [f"no_alert `{new}` — served, but no shipped alert compares it "
                            "— file(s) ta.yaml — tenant(s) ta"], r


def test_a_value_served_at_critical_is_looked_up_as_its_critical_tier(tmp_path):
    """`"95:critical"` 送出的是 critical 列：查 `<key>_critical`。`clickhouse_queries_rate`
    刻意沒有 critical 層（#3658 不補吞吐量鍵），所以列出並標註；mariadb 有，`mysql_connection_usage_percent: "90:critical"` 不列。"""
    r = vc.check_no_alert(_tree(tmp_path, {
        "_defaults.yaml": "defaults:\n  clickhouse_queries_rate: 80\n  mysql_connection_usage_percent: 80\n",
        "ta.yaml": "tenants:\n  ta:\n    clickhouse_queries_rate: \"95:critical\"\n"
                   "    mysql_connection_usage_percent: \"90:critical\"\n"}))
    assert r["details"] == [
        "no_alert `clickhouse_queries_rate` (served as critical) — served, but no shipped "
        "alert compares it — file(s) ta.yaml — tenant(s) ta"], r


def test_a_deferred_base_is_not_listed_at_critical_either(tmp_path):
    """observed-map 延後判的 base（version-aware 的 `container_cpu`）寫成 `"N:critical"`：
    它的 critical 層同樣是延後判，不列。"""
    r = vc.check_no_alert(_tree(tmp_path, {
        "_defaults.yaml": "defaults:\n  container_cpu: 50\n",
        "ta.yaml": "tenants:\n  ta:\n    container_cpu: \"60:critical\"\n"}))
    assert r["status"] == vc.PASS, r


def test_a_tree_the_verdict_cannot_judge_is_not_checked_not_a_pass(tmp_path):
    """沒有根平台檔：閾值鍵判不了，這一列不能說「每個都有告警比較」。"""
    r = vc.check_no_alert(_tree(tmp_path, {"ta.yaml": "tenants:\n  ta:\n    mysql_connectoins: \"5\"\n"}))
    assert r["status"] == vc.WARN and r["details"][0].startswith("not checked:"), r
    assert r["hint"] == vc._NO_ALERT_NOT_CHECKED_HINT, r


def test_no_verdict_from_the_routes_row_is_not_judged_again(tmp_path, monkeypatch):
    """`main` 在 `routes` 列判過卻沒給出判決時傳 judge=False：不再跑一次判決（原因那一列
    已報），這一列寫 not checked。"""
    import _lib_key_verdict

    def must_not_run(*_a, **_k):
        raise AssertionError("judged again")
    monkeypatch.setattr(_lib_key_verdict, "judge_tree", must_not_run)
    r = vc.check_no_alert(str(tmp_path), verdict=None, judge=False)
    assert r["status"] == vc.WARN and r["details"][0].startswith("not checked:"), r


def test_a_defect_of_this_tool_is_not_swallowed(tmp_path, monkeypatch):
    """只有讀不到（da-guard、served-values、observed-map）才是「not checked」；
    這支工具自己的錯照樣拋出（由 `_run_check` 報成 exit 2），不藏成 WARN。"""
    import _observed_map_lib

    def bug(*_a, **_k):
        raise AttributeError("a field the map no longer has")
    monkeypatch.setattr(_observed_map_lib, "load_observed_map", bug)
    with pytest.raises(AttributeError):
        vc.check_no_alert(_tree(tmp_path, {
            "_defaults.yaml": "defaults:\n  mysql_connection_usage_percent: 80\n",
            "ta.yaml": "tenants:\n  ta:\n    mysql_connection_usage_percent: \"70\"\n"}))


def test_a_map_that_cannot_be_read_is_not_checked_never_a_fail(tmp_path, monkeypatch):
    import _observed_map_lib

    def broken(*_a, **_k):
        raise _observed_map_lib.ObservedMapError("no map", "missing")
    monkeypatch.setattr(_observed_map_lib, "load_observed_map", broken)
    r = vc.check_no_alert(_tree(tmp_path, {
        "_defaults.yaml": "defaults:\n  mysql_connection_usage_percent: 80\n",
        "ta.yaml": "tenants:\n  ta:\n    mysql_connection_usage_percent: 70\n"}))
    assert r["status"] == vc.WARN and r["details"][0].startswith("not checked:"), r


def test_it_never_changes_the_exit_code(tmp_path):
    """只有 no_alert 有發現的樹：exit 0（WARN），不是 1。"""
    conf_d = _tree(tmp_path, {
        "_defaults.yaml": "defaults:\n  mysql_connection_usage_percent: 80\n  mysql_connectoins: 5\n",
        "ta.yaml": "tenants:\n  ta:\n    mysql_connection_usage_percent: \"70\"\n"})
    p = subprocess.run([sys.executable, str(_REPO / "scripts/tools/ops/validate_config.py"),
                        "--config-dir", conf_d, "--json"],
                       capture_output=True, text=True, encoding="utf-8", timeout=300)
    rows = {r["check"]: r for r in json.loads(p.stdout)}
    assert rows["no_alert"]["status"] == vc.WARN, rows["no_alert"]
    assert p.returncode == 0, (p.returncode, [r for r in rows.values() if r["status"] == vc.FAIL])


# The shipped example tree's no_alert keys. Shrink-only — a key that appears
# here is new and fails; a listed key that gets fixed fails too until it is
# removed. #3658 shipped the mongodb / redis critical tiers that held its last
# three entries, so it is empty: the example tree has no known gaps.
_SHIPPED_KNOWN: set[str] = set()


def test_the_shipped_example_tree_has_only_the_known_gaps():
    r = vc.check_no_alert(str(_REPO / "components/threshold-exporter/config/conf.d"))
    got = set()
    for line in r["details"]:
        if line.startswith("no_alert `"):
            got.add(line[len("no_alert `"):].partition("`")[0])
    if r["status"] == vc.PASS:
        got = set()
    assert not any(d.startswith("not checked:") for d in r["details"]), r
    assert got == _SHIPPED_KNOWN, (
        f"new: {sorted(got - _SHIPPED_KNOWN)}; fixed, remove from _SHIPPED_KNOWN: "
        f"{sorted(_SHIPPED_KNOWN - got)}")
