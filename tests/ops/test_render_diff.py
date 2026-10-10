"""_render_diff：兩側各渲染一次、比較渲染結果（ADR-037）。

用真的 da-guard（conftest 的 `da_guard_binary`，從本 repo 原始碼 build）與真的路由產生器，
在 repo 出貨的範例 conf.d 副本上每次只改一處。每個「會響」的情境都配一個「只改註解」或
「改到別處」的對照組，證明比較方法不會被格式變動誤導、也不是什麼都報。
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

import _render_diff as rd
import _lib_tenant_values as tv

REPO = Path(__file__).resolve().parents[2]
SAMPLE = REPO / "components" / "threshold-exporter" / "config" / "conf.d"
AT = "2026-10-10T00:00:00Z"

_ENFORCED = """
_routing_enforced:
  enabled: true
  receiver:
    type: "webhook"
    url: "https://noc.example.com/alerts"
  match:
    - 'severity="critical"'
  group_by: ["alertname", "tenant"]
  group_wait: "15s"
  repeat_interval: "1h"
"""


@pytest.fixture(scope="module")
def da_guard(da_guard_binary) -> str:
    return da_guard_binary


def _copy(tmp_path: Path, name: str) -> Path:
    dst = tmp_path / name
    shutil.copytree(SAMPLE, dst)
    return dst


def _edit(conf_d: Path, old: str, new: str, rel: str = "_defaults.yaml") -> None:
    p = conf_d / rel
    text = p.read_text(encoding="utf-8")
    assert text.count(old) == 1, f"{old!r} occurs {text.count(old)} times in {rel}"
    p.write_text(text.replace(old, new), encoding="utf-8")


def _diff(tmp_path: Path, da_guard: str, edit) -> rd.RenderDiff:
    base, pr = _copy(tmp_path, "base"), _copy(tmp_path, "pr")
    edit(pr)
    return rd.compare(base, pr, AT, binary=da_guard)


def _routes(d: rd.RenderDiff, cls=rd.RouteChange) -> list:
    return [c for c in d.routes.changes if isinstance(c, cls)]


# ── ADR-037 的兩個範例，逐字重現 ──────────────────────────────────────


def test_adr_example_group_wait_reaches_the_two_inheriting_tenants(tmp_path, da_guard):
    d = _diff(tmp_path, da_guard, lambda c: _edit(c, '  group_wait: "30s"', '  group_wait: "99s"'))
    got = {c.tenant: c.changes for c in _routes(d)}
    assert got == {"db-a": {"group_wait": ("30s", "99s")}, "es-prod": {"group_wait": ("30s", "99s")}}
    assert all(c.kind == "changed" for c in _routes(d))
    assert d.served.status == "computed" and d.served.changes == ()  # exporter 不讀 _routing_defaults
    assert d.exit_code == 1


def test_adr_example_container_memory_reaches_the_four_tenants_not_overriding_it(tmp_path, da_guard):
    d = _diff(tmp_path, da_guard, lambda c: _edit(c, "  container_memory: 85", "  container_memory: 999"))
    got = {c.tenant: (c.base, c.pr) for c in d.served.changes if c.field == "values"}
    assert got == {"db-a": (85, 999), "es-prod": (85, 999), "mongo-prod": (85, 999), "redis-prod": (85, 999)}
    assert d.routes.status == "computed" and d.routes.changes == ()
    assert d.exit_code == 1


def test_control_comment_only_change_is_no_change(tmp_path, da_guard):
    """對照組：只改註解，兩個面向都沒有差異、結束碼 0。"""
    d = _diff(tmp_path, da_guard, lambda c: _edit(c, "  container_memory: 85", "  container_memory: 85  # note"))
    assert (d.served.status, d.routes.status) == ("computed", "computed")
    assert d.served.changes == () and d.routes.changes == ()
    assert d.exit_code == 0


# ── 平台面的載體 ──────────────────────────────────────────────────────


def test_routing_enforced_adds_a_platform_route_and_receiver(tmp_path, da_guard):
    d = _diff(tmp_path, da_guard, lambda c: (c / "_defaults.yaml").write_text(
        (c / "_defaults.yaml").read_text(encoding="utf-8") + _ENFORCED, encoding="utf-8"))
    added = [c for c in _routes(d) if c.kind == "added"]
    assert added and all(c.tenant == rd.PLATFORM for c in added), added
    receivers = _routes(d, rd.ReceiverChange)
    assert [r.kind for r in receivers] == ["added"]
    # 憑證（webhook URL）不出現在差異裡
    assert "noc.example.com" not in repr(d.routes.changes)


def test_state_filter_severity_change_is_seen_through_served_values(tmp_path, da_guard):
    d = _diff(tmp_path, da_guard, lambda c: _edit(c, '    severity: "critical"', '    severity: "warning"'))
    sf = [c for c in d.served.changes if c.field == "state_filters"]
    assert sf and {c.key for c in sf} == {"container_crashloop"}
    assert all((c.base, c.pr) == ("critical", "warning") for c in sf)
    assert not [c for c in d.served.changes if c.field == "values"]  # `_state_*` 的 true/false 不變


def test_receiver_credential_change_is_reported_masked(tmp_path, da_guard):
    """db-b 自己的 webhook URL 改了：歸給 db-b，前後值都遮罩。"""
    text = (SAMPLE / "db-b.yaml").read_text(encoding="utf-8")
    url = next(ln.split("url:", 1)[1].strip().strip('"\'') for ln in text.splitlines() if "url:" in ln)
    d = _diff(tmp_path, da_guard, lambda c: _edit(c, url, url + "-rotated", rel="db-b.yaml"))
    rc = _routes(d, rd.ReceiverChange)
    assert [(r.tenants, r.kind) for r in rc] == [(("db-b",), "changed")]
    assert all(v == (rd.MASKED, rd.MASKED) for v in rc[0].changes.values())
    assert url not in repr(d.routes.changes)


# ── 渲染失敗的處理表（ADR-037 決定 4） ────────────────────────────────

_BROKEN = "tenants:\n  broken:\n    x: [1\n"


def test_pr_side_broken_config_is_not_computed(tmp_path, da_guard):
    d = _diff(tmp_path, da_guard, lambda c: (c / "zz-broken.yaml").write_text(_BROKEN, encoding="utf-8"))
    assert d.served.status == "not_computed" and d.routes.status == "not_computed"
    assert "PR side" in d.served.reason and "zz-broken.yaml" in d.served.reason
    assert d.exit_code == 2


def test_base_side_broken_config_is_not_compared_and_does_not_block(tmp_path, da_guard):
    """main 上的設定已壞、這個 PR 修好它：不擋（rc 1），兩個面向都列為沒有比對。"""
    base, pr = _copy(tmp_path, "base"), _copy(tmp_path, "pr")
    (base / "zz-broken.yaml").write_text(_BROKEN, encoding="utf-8")
    d = rd.compare(base, pr, AT, binary=da_guard)
    assert (d.served.status, d.routes.status) == ("not_compared", "not_compared")
    assert "base side" in d.served.reason and "zz-broken.yaml" in d.served.reason
    assert d.exit_code == 1


def test_base_side_duplicate_tenant_is_the_configs_fault(tmp_path, da_guard):
    """da-guard 認得參數卻回 2（同一租戶宣告兩次）：算設定壞了，不是工具壞了。"""
    base, pr = _copy(tmp_path, "base"), _copy(tmp_path, "pr")
    (base / "zz-dup.yaml").write_text((SAMPLE / "db-a.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    d = rd.compare(base, pr, AT, binary=da_guard)
    assert d.served.status == "not_compared", d.served
    assert d.exit_code == 1


def test_first_import_counts_every_tenant_as_added(tmp_path, da_guard):
    pr = _copy(tmp_path, "pr")
    for base in (tmp_path / "missing", tmp_path / "empty"):
        if base.name == "empty":
            base.mkdir()
        d = rd.compare(base, pr, AT, binary=da_guard)
        added = {c.tenant for c in d.served.changes if c.field == "tenant"}
        assert added == {"db-a", "db-b", "es-prod", "mongo-prod", "redis-prod"}, base
        assert all(c.base is None and c.pr is True for c in d.served.changes if c.field == "tenant")
        # 產生器對沒有租戶的樹什麼都不寫，所以平台自己的 route 也算新增
        route_tenants = {c.tenant for c in _routes(d) if c.kind == "added"}
        assert route_tenants == added | {rd.PLATFORM}
        assert d.exit_code == 1


def test_pr_side_without_config_is_not_computed(tmp_path, da_guard):
    base = _copy(tmp_path, "base")
    (tmp_path / "empty").mkdir()
    for pr in (tmp_path / "missing", tmp_path / "empty"):
        d = rd.compare(base, pr, AT, binary=da_guard)
        assert (d.served.status, d.routes.status) == ("not_computed", "not_computed")
        assert d.exit_code == 2


def test_tool_failure_on_the_base_side_still_blocks(tmp_path, da_guard, monkeypatch):
    """工具本身失敗不論哪一側都是 rc 2：base 側找不到 da-guard 不能降級成「沒有比對」。"""
    base, pr = _copy(tmp_path, "base"), _copy(tmp_path, "pr")
    real = rd.render_served

    def flaky(conf_d, at, *, binary=None, timeout=rd.DEFAULT_TIMEOUT):
        if Path(conf_d) == base:
            return real(conf_d, at, binary=str(tmp_path / "no-such-da-guard"), timeout=timeout)
        return real(conf_d, at, binary=binary, timeout=timeout)

    monkeypatch.setattr(rd, "render_served", flaky)
    d = rd.compare(base, pr, AT, binary=da_guard)
    assert d.served.status == "not_computed" and "base side" in d.served.reason
    assert d.routes.status == "computed"
    assert d.exit_code == 2


def test_both_sides_broken_follows_the_pr_side(tmp_path, da_guard):
    base, pr = _copy(tmp_path, "base"), _copy(tmp_path, "pr")
    for side in (base, pr):
        (side / "zz-broken.yaml").write_text(_BROKEN, encoding="utf-8")
    d = rd.compare(base, pr, AT, binary=da_guard)
    assert d.served.status == "not_computed" and "PR side" in d.served.reason
    assert d.exit_code == 2


# ── 分類與比對的單元行為（不需 da-guard） ────────────────────────────


@pytest.mark.parametrize("err, kind", [
    (tv.ParseFailedError("t.yaml", ValueError("x"), []), "config"),
    (tv.ServedValuesError("exited 2", 2, "duplicate tenant"), "config"),
    (tv.ServedValuesError("exited 2", 2, "", stale=True), "tool"),
    (tv.ServedValuesError("bad json", 0, "", broken=True), "tool"),
    (tv.ServedValuesError("did not finish", None, ""), "tool"),
    (tv.ServedValuesError("killed", -9, ""), "tool"),
    (tv.DaGuardNotFoundError("no da-guard"), "tool"),
], ids=["parse-failed", "tree-rejected", "stale", "broken", "timeout", "signal", "missing"])
def test_served_failure_classification(monkeypatch, err, kind):
    def boom(*a, **k):
        raise err
    monkeypatch.setattr(tv, "load_served_tree", boom)
    with pytest.raises(rd.RenderError) as ei:
        rd.render_served("x", AT)
    assert ei.value.failure.kind == kind


@pytest.mark.parametrize("rc, stderr, kind", [
    (1, "t.yaml: failed to parse", "config"),
    (1, "Traceback (most recent call last):\n  File ...\nKeyError: 'x'", "tool"),
    (2, "ERROR: config directory not found", "tool"),
])
def test_generator_failure_classification(monkeypatch, tmp_path, rc, stderr, kind):
    class P:
        returncode, stdout = rc, ""
    P.stderr = stderr
    monkeypatch.setattr(rd.subprocess, "run", lambda *a, **k: P)
    with pytest.raises(rd.RenderError) as ei:
        rd.render_routes(tmp_path)
    assert ei.value.failure.kind == kind


def test_exit_code_table():
    ok, chg = rd.ViewResult("computed", ""), rd.ViewResult("computed", "", ("x",))
    nc, ncmp = rd.ViewResult("not_computed", "r"), rd.ViewResult("not_compared", "r")
    assert rd.RenderDiff(AT, ok, ok).exit_code == 0
    assert rd.RenderDiff(AT, chg, ok).exit_code == 1
    assert rd.RenderDiff(AT, ok, ncmp).exit_code == 1
    assert rd.RenderDiff(AT, ncmp, nc).exit_code == 2
    assert rd.RenderDiff(AT, chg, nc).exit_code == 2


def test_route_identity_is_the_matcher_path_not_the_receiver_name():
    """產生器替 receiver 改名不是「路由搬家」：同一 matcher 路徑只報 receiver 欄位變更。"""
    def cfg(recv):
        return {"route": {"receiver": "default", "routes": [
            {"matchers": ['tenant="a"'], "receiver": recv, "routes": [{"matchers": ['severity="critical"'], "receiver": recv}]},
            {"matchers": ['tenant="a"'], "receiver": "second"},
        ]}, "receivers": [{"name": "default"}, {"name": recv}, {"name": "second"}]}
    out = [c for c in rd.diff_routes(cfg("old"), cfg("new")) if isinstance(c, rd.RouteChange)]
    assert {(c.path, c.kind) for c in out} == {
        (('tenant="a"',), "changed"), (('tenant="a"', 'severity="critical"'), "changed")}
    assert all(c.tenant == "a" and set(c.changes) == {"receiver"} for c in out)
    # 第二個同 matcher 的兄弟節點以 #2 區分，沒有變更就不報
    assert ('tenant="a" #2',) in rd.flatten_routes(cfg("x"))


def test_inhibit_rules_compare_as_a_multiset_and_belong_to_their_tenant():
    rule = {"source_matchers": ['severity="critical"', 'tenant="a"'], "target_matchers": ['severity="warning"', 'tenant="a"'], "equal": ["alertname"]}
    plat = {"source_matchers": ['severity="critical"'], "target_matchers": ['severity="warning"'], "equal": ["alertname"]}
    base = {"route": {}, "inhibit_rules": [rule, plat]}
    pr = {"route": {}, "inhibit_rules": [plat, rule, rule]}
    out = [c for c in rd.diff_routes(base, pr) if isinstance(c, rd.InhibitChange)]
    assert [(c.tenant, c.kind) for c in out] == [("a", "added")]
    out = [c for c in rd.diff_routes(pr, {"route": {}, "inhibit_rules": []}) if isinstance(c, rd.InhibitChange)]
    assert sorted((c.tenant, c.kind) for c in out) == [(rd.PLATFORM, "removed"), ("a", "removed"), ("a", "removed")]


@pytest.mark.parametrize("matcher, tenant", [
    ('tenant="db-a"', "db-a"), ("tenant=db-a", "db-a"), ('tenant =  "x"', "x"),
    ('tenant=~"db-.*"', None), ('tenant!="db-a"', None), ('tenants="db-a"', None),
])
def test_tenant_matcher(matcher, tenant):
    assert rd._tenant_of_matcher(matcher) == tenant


def test_generator_renders_nothing_for_a_tree_without_tenants(tmp_path):
    """產生器對沒有租戶的樹回 0 且不寫檔：讀成空的設定，不是工具失敗。"""
    (tmp_path / "_defaults.yaml").write_text("defaults:\n  x: 1\n", encoding="utf-8")
    assert rd.render_routes(tmp_path) == rd.EMPTY_ROUTES


def test_generator_is_found_beside_this_module():
    """repo 與 da-tools 映像（扁平）都把產生器放在本模組旁邊。"""
    assert rd.GENERATOR.is_file() and rd.GENERATOR.parent == Path(rd.__file__).resolve().parent
    assert sys.executable
