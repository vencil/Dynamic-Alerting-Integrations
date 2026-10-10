"""config_diff 的三個報告區段改從 served-values 讀（ADR-037）。

現有 test_config_diff 的每種情境，在這裡用真的 da-guard 各跑一次：同樣的變更，
從 served-values 讀出來的區段要報出同一件事。刻意改變的三處（#1516 拍板）各有
一條測試釘住：繼承到的閾值變更會列出、新增／移除的租戶不列設定、custom alert
以 served row 呈現（被 disable 的 recipe 報成移除並附說明）。
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import config_diff as cd
import _lib_tenant_values as tv

AT = "2026-10-10T03:00:00Z"
_DEFAULTS = ("defaults:\n  mysql_connections: 80\n  container_memory: 85\n"
             "state_filters:\n  crashloop:\n    reasons: [\"CrashLoopBackOff\"]\n    severity: critical\n")


@pytest.fixture(scope="module")
def da_guard(da_guard_binary) -> str:
    return da_guard_binary


def _tree(root: Path, tenants: dict, defaults: str = _DEFAULTS) -> Path:
    root.mkdir(parents=True)
    (root / "_defaults.yaml").write_text(defaults, encoding="utf-8")
    for name, body in tenants.items():
        (root / f"{name}.yaml").write_text(yaml.safe_dump({"tenants": {name: body}}), encoding="utf-8")
    return root


def _served(tmp_path, da_guard, base: dict, pr: dict, base_defaults=_DEFAULTS, pr_defaults=_DEFAULTS):
    b = tv.load_served_values(_tree(tmp_path / "base", base, base_defaults), at=AT, binary=da_guard, schedules=True)
    p = tv.load_served_values(_tree(tmp_path / "pr", pr, pr_defaults), at=AT, binary=da_guard, schedules=True)
    return b, p


def _metric(b, p):
    return cd.compute_diff(cd.served_metric_configs(b), cd.served_metric_configs(p))


def _by_key(entries):
    return {e["key"]: e for e in entries}


# ── 閾值區段 ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("old, new, change", [
    ("100", "80", "tighter"), ("80", "120", "looser"),
    ("100", "disable", "toggled"), ("disable", "100", "toggled"),
    ("70", "70:critical", "modified"),
])
def test_tenant_own_threshold_change_is_classified_as_before(tmp_path, da_guard, old, new, change):
    b, p = _served(tmp_path, da_guard, {"t": {"mysql_connections": old}}, {"t": {"mysql_connections": new}})
    got = _by_key(_metric(b, p)["t"])["mysql_connections"]
    assert got["change"] == change
    # 與 YAML 讀法同一個分類（對照：舊路徑對同一組原文值給同一個答案）
    assert cd.classify_change(old, new) == change


def test_severity_only_change_reads_as_written(tmp_path, da_guard):
    b, p = _served(tmp_path, da_guard, {"t": {"mysql_connections": "70"}}, {"t": {"mysql_connections": "70:critical"}})
    got = _by_key(_metric(b, p)["t"])["mysql_connections"]
    assert (got["old"], got["new"]) == (70, "70:critical")


def test_inherited_threshold_change_is_reported_for_tenants_that_do_not_override(tmp_path, da_guard):
    """刻意的改變：`defaults:` 的值改了，沒覆寫它的租戶照樣列出；覆寫的租戶不列。"""
    tenants = {"inherits": {"_silent_mode": "disable"}, "overrides": {"container_memory": "90"}}
    b, p = _served(tmp_path, da_guard, tenants, tenants,
                   pr_defaults=_DEFAULTS.replace("container_memory: 85", "container_memory: 95"))
    diff = _metric(b, p)
    assert _by_key(diff["inherits"])["container_memory"]["change"] == "looser"
    assert "overrides" not in diff
    # 對照：YAML 讀法看不到這個變更（它只讀租戶檔自己寫的值）
    assert cd.compute_diff(cd.load_configs_from_dir(tmp_path / "base"),
                           cd.load_configs_from_dir(tmp_path / "pr")) == {}


def test_added_and_removed_tenant_and_key(tmp_path, da_guard):
    b, p = _served(tmp_path, da_guard, {"gone": {"mysql_connections": "70"}, "t": {"kafka": "1"}},
                   {"new": {"mysql_connections": "70"}, "t": {}})
    diff = _metric(b, p)
    assert {e["change"] for e in diff["gone"]} == {"removed"}
    assert {e["change"] for e in diff["new"]} == {"added"}


def test_scheduled_threshold_shows_as_scheduled(tmp_path, da_guard):
    sched = {"default": "70", "overrides": [{"window": "01:00-09:00", "value": "90"}]}
    b, p = _served(tmp_path, da_guard, {"t": {"mysql_connections": "70"}}, {"t": {"mysql_connections": sched}})
    got = _by_key(_metric(b, p)["t"])["mysql_connections"]
    assert got["change"] == "modified" and isinstance(got["new"], dict)
    assert cd._format_value(got["new"]) == "(scheduled)"


def test_critical_key_keeps_its_own_severity(tmp_path, da_guard):
    """`_critical` 鍵本身就是 critical：數字變大仍是 looser，不會因為 severity 被當成 modified。"""
    b, p = _served(tmp_path, da_guard, {"t": {"mysql_connections_critical": "90"}},
                   {"t": {"mysql_connections_critical": "95"}})
    got = _by_key(_metric(b, p)["t"])["mysql_connections_critical"]
    assert (got["old"], got["new"], got["change"]) == (90, 95, "looser")


@pytest.mark.parametrize("old, new", [
    ({"default": "70", "expires": "2027-01-01T00:00:00Z"}, {"default": "70", "expires": "2028-01-01T00:00:00Z"}),
    ({"default": "70", "expires": "2027-01-01T00:00:00Z"}, "70"),
], ids=["extended", "made-permanent"])
def test_threshold_time_box_change_is_reported(tmp_path, da_guard, old, new):
    """`expires:` 改了（延長或改成永久）送出的值當下不變，仍要列出。"""
    b, p = _served(tmp_path, da_guard, {"t": {"mysql_connections": old}}, {"t": {"mysql_connections": new}})
    assert _by_key(_metric(b, p)["t"])["mysql_connections"]["change"] == "modified"


def test_comment_only_change_is_no_change(tmp_path, da_guard):
    body = {"t": {"mysql_connections": "70", "_silent_mode": "warning"}}
    b, p = _served(tmp_path, da_guard, body, body, pr_defaults=_DEFAULTS + "# note\n")
    assert _metric(b, p) == {}
    assert cd.served_setting_diff(b, p) == {}
    assert cd.served_custom_alert_diff(b, p) == {}


# ── 設定區段 ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("old, new, key", [
    ({"_silent_mode": "warning"}, {"_silent_mode": "all"}, "_silent_mode"),
    ({"_state_crashloop": "disable"}, {}, "_state_crashloop"),
    ({"_severity_dedup": "disable"}, {}, "_severity_dedup"),
])
def test_setting_change_is_reported(tmp_path, da_guard, old, new, key):
    b, p = _served(tmp_path, da_guard, {"t": {"mysql_connections": "70", **old}},
                   {"t": {"mysql_connections": "70", **new}})
    keys = {e["key"] for e in cd.served_setting_diff(b, p)["t"]}
    assert key in keys, keys


def test_writing_the_exporters_default_is_no_change(tmp_path, da_guard):
    """刻意的改變：比的是 exporter 讀到的值。`_severity_dedup` 的預設就是 enable，
    寫上或拿掉 `enable` 不改變送出的結果，所以不報；YAML 讀法會報。"""
    b, p = _served(tmp_path, da_guard, {"t": {"mysql_connections": "70", "_severity_dedup": "enable"}},
                   {"t": {"mysql_connections": "70"}})
    assert cd.served_setting_diff(b, p) == {}
    assert cd.compute_setting_diff({"t": {"_severity_dedup": "enable"}}, {"t": {}})  # 對照：舊讀法會報


def test_state_filter_severity_change_is_a_setting_change(tmp_path, da_guard):
    body = {"t": {"mysql_connections": "70"}}
    b, p = _served(tmp_path, da_guard, body, body,
                   pr_defaults=_DEFAULTS.replace("severity: critical", "severity: warning"))
    got = _by_key(cd.served_setting_diff(b, p)["t"])
    key = f"{cd.SERVED_STATE_FILTERS_KEY}.crashloop"
    assert (got[key]["old"], got[key]["new"]) == ("critical", "warning")


def test_a_tenant_key_named_like_the_state_filters_entry_is_still_compared(tmp_path, da_guard):
    b, p = _served(tmp_path, da_guard, {"t": {"_state_filters": "enable"}}, {"t": {"_state_filters": "disable"}})
    assert "_state_filters" in {e["key"] for e in cd.served_setting_diff(b, p)["t"]}


def test_routing_credential_is_redacted(tmp_path, da_guard):
    def body(url):
        return {"t": {"mysql_connections": "70", "_routing": {"receiver": {"type": "webhook", "url": url}}}}
    b, p = _served(tmp_path, da_guard, body("https://h/SEKRET-1"), body("https://h/SEKRET-2"))
    diff = cd.served_setting_diff(b, p)
    assert "SEKRET" not in repr(diff)
    url = [e for e in diff["t"] if e["key"].endswith(".url")]
    assert url and (url[0]["old"], url[0]["new"]) == (cd._REDACTED, cd._REDACTED)


@pytest.mark.parametrize("receiver", [{"type": "bogus", "url": "https://h/SEKRET"}, {"url": "https://h/SEKRET"}],
                         ids=["unknown-type", "type-left-out"])
def test_rejected_routing_text_is_redacted_whole(tmp_path, da_guard, receiver):
    """exporter 拒收的 `_routing` 以原文字串進 unserved：整段遮罩，變更照報。"""
    def body(suffix):
        return {"t": {"_routing": {"receiver": {**receiver, "url": receiver["url"] + suffix}}}}
    b, p = _served(tmp_path, da_guard, body("-1"), body("-2"))
    diff = cd.served_setting_diff(b, p)
    assert "SEKRET" not in repr(diff)
    assert any(e["key"] == "_routing" for e in diff["t"]), diff


@pytest.mark.parametrize("key", ["_routng", "_Routing", "_receiver"])
def test_a_misspelled_routing_key_served_as_text_is_redacted(tmp_path, da_guard, key):
    """拼錯或大小寫不同的路由鍵，exporter 不讀，以原文字串進 unserved：不論鍵名都整段遮。"""
    def body(suffix):
        return {"t": {key: {"receiver": {"type": "webhook", "url": f"https://h/SEKRET{suffix}"}}}}
    b, p = _served(tmp_path, da_guard, body("1"), body("2"))
    diff = cd.served_setting_diff(b, p)
    assert diff and "SEKRET" not in repr(diff), diff
    import _render_diff as rd
    served = rd.diff_served(b, p)
    assert served and "SEKRET" not in repr(served), served


def test_a_routing_timing_change_still_prints_its_values(tmp_path, da_guard):
    """對照：只有整段原文才遮；`_routing.group_wait` 這類一般欄位照常印出前後值。"""
    def body(gw):
        return {"t": {"_routing": {"receiver": {"type": "webhook", "url": "https://h/x"}, "group_wait": gw}}}
    b, p = _served(tmp_path, da_guard, body("30s"), body("1m"))
    got = _by_key(cd.served_setting_diff(b, p)["t"])
    assert (got["_routing.group_wait"]["old"], got["_routing.group_wait"]["new"]) == ("30s", "1m")


def test_added_or_removed_tenant_lists_no_settings(tmp_path, da_guard):
    """刻意的改變：新增／移除的租戶不在設定區段（閾值區段已列出它）。"""
    b, p = _served(tmp_path, da_guard, {"gone": {"_silent_mode": "warning"}},
                   {"new": {"_silent_mode": "warning"}})
    assert cd.served_setting_diff(b, p) == {}
    # 對照：同一個租戶兩側都在、設定有變時照樣列出
    b, p = _served(tmp_path / "x", da_guard, {"t": {"_silent_mode": "warning"}}, {"t": {"_silent_mode": "all"}})
    assert cd.served_setting_diff(b, p)


# ── custom alert 區段 ───────────────────────────────────────────────────


def _recipe(name="x", **extra):
    r = {"recipe": "threshold", "name": name, "metric": "mysql_global_status_threads_connected",
         "op": ">", "window": "5m", "threshold": "150:warning", "mode": "page"}
    r.update(extra)
    return r


def _ca(tmp_path, da_guard, old: list, new: list):
    b, p = _served(tmp_path, da_guard, {"t": {"mysql_connections": "70", "_custom_alerts": old}},
                   {"t": {"mysql_connections": "70", "_custom_alerts": new}})
    return cd.served_custom_alert_diff(b, p)


def test_custom_alert_threshold_change_is_a_threshold_field_change(tmp_path, da_guard):
    diff = _ca(tmp_path, da_guard, [_recipe(threshold="150:warning")], [_recipe(threshold="200:critical")])
    fields = {f["field"]: f for f in diff["t"][0]["field_changes"]}
    assert (fields["threshold"]["old"], fields["threshold"]["new"]) == ("150:warning", "200:critical")


def test_custom_alert_mode_toggle_is_a_mode_field_change(tmp_path, da_guard):
    diff = _ca(tmp_path, da_guard, [_recipe(mode="page")], [_recipe(mode="silent")])
    fields = {f["field"]: f for f in diff["t"][0]["field_changes"]}
    assert (fields["mode"]["old"], fields["mode"]["new"]) == ("page", "silent")


@pytest.mark.parametrize("change", [{"op": "<"}, {"window": "10m"}, {"for": "5m"}])
def test_custom_alert_op_window_for_change_shows_as_recipe_id(tmp_path, da_guard, change):
    """刻意的改變：op／window／for 的變更以 recipe_id 不同呈現。"""
    diff = _ca(tmp_path, da_guard, [_recipe()], [_recipe(**change)])
    fields = {f["field"] for f in diff["t"][0]["field_changes"]}
    assert fields == {"recipe_id"}


@pytest.mark.parametrize("new", [[], [_recipe(threshold="disable")], [_recipe(**{"for": "2m"})]],
                         ids=["deleted", "disabled", "rejected"])
def test_custom_alert_no_longer_served_is_removed_with_the_note(tmp_path, da_guard, new):
    """刻意的改變：刪掉與 disable 都讓 served row 消失，報成移除並寫明兩種可能。"""
    diff = _ca(tmp_path, da_guard, [_recipe()], new)
    (entry,) = diff["t"]
    assert entry["change"] == "removed" and entry["note"] == cd.SERVED_REMOVED_NOTE


def _two_rows(tmp_path, da_guard, old, new):
    return _ca(tmp_path, da_guard, old, new)


def test_custom_alert_change_in_a_second_row_of_the_same_name_is_seen(tmp_path, da_guard):
    """同名的兩條 recipe：改第二條的 mode／metric 也要報（不是只看第一列）。"""
    a, b2 = _recipe(), _recipe(metric="m2")
    for field, new in (("mode", [a, dict(b2, mode="silent")]), ("metric", [a, dict(b2, metric="m3")])):
        diff = _ca(tmp_path / field, da_guard, [a, b2], new)
        assert diff and diff["t"][0]["change"] == "modified", new
        assert field in {f["field"] for f in diff["t"][0]["field_changes"]}, diff


def test_custom_alert_rows_are_read_in_a_stable_order(tmp_path, da_guard):
    """多列（多個 severity）的 recipe：兩側列序不同不算變更，threshold 依排序後的值比。"""
    tenants = {"t": {"_custom_alerts": [_recipe()]}}
    b, _ = _served(tmp_path, da_guard, tenants, tenants)
    rows = b["t"].values["_custom_alerts"]
    many = [dict(rows[0], value=float(v), severity=sev)
            for v, sev in ((900, "critical"), (150, "warning"), (700, "info"), (300, "critical"),
                           (500, "warning"), (100, "info"), (800, "warning"), (200, "critical"))]
    fwd = b["t"]._replace(values={**b["t"].values, "_custom_alerts": many})
    rev = b["t"]._replace(values={**b["t"].values, "_custom_alerts": many[::-1]})
    assert cd.served_custom_alerts({"t": fwd}) == cd.served_custom_alerts({"t": rev})
    got = cd.served_custom_alerts({"t": fwd})["t"]["x"]["threshold"].split(", ")
    assert got == sorted(got) and len(got) == 8


def test_custom_alert_added_and_identical(tmp_path, da_guard):
    diff = _ca(tmp_path, da_guard, [], [_recipe()])
    assert [e["change"] for e in diff["t"]] == ["added"]
    assert _ca(tmp_path / "same", da_guard, [_recipe()], [_recipe()]) == {}


def test_custom_alerts_stay_out_of_the_settings_section(tmp_path, da_guard):
    b, p = _served(tmp_path, da_guard, {"t": {"_custom_alerts": [_recipe()]}},
                   {"t": {"_custom_alerts": [_recipe(threshold="200:critical")]}})
    assert "t" not in cd.served_setting_diff(b, p)
    assert cd.served_custom_alert_diff(b, p)


def test_rendered_report_reads_the_served_sections(tmp_path, da_guard):
    """三個區段都能直接交給既有的 render_markdown。"""
    b, p = _served(tmp_path, da_guard,
                   {"t": {"mysql_connections": "100", "_silent_mode": "warning", "_custom_alerts": [_recipe()]}},
                   {"t": {"mysql_connections": "80", "_silent_mode": "all",
                          "_custom_alerts": [_recipe(threshold="200:critical")]}})
    md = cd.render_markdown(_metric(b, p), "base", "pr",
                            custom_alert_diffs=cd.served_custom_alert_diff(b, p),
                            setting_diffs=cd.served_setting_diff(b, p))
    assert "mysql_connections" in md and "_silent_mode" in md and "200:critical" in md


def test_every_row_of_a_recipe_counts_not_only_the_first():
    """固定列序：同名的第二列改了 mode，view 也要跟著變（不能只看第一列）。"""
    def row(metric, mode):
        return {"metric": metric, "value": 150.0, "severity": "warning",
                "labels": {"name": "x", "mode": mode, "recipe_id": f"threshold__{metric}"}}

    def tenants(second_mode):
        return {"t": tv.TenantValues("t", {"_custom_alerts": [row("m1", "page"), row("m2", second_mode)]},
                                     {}, {}, {}, None, {}, {})}
    old, new = cd.served_custom_alerts(tenants("page")), cd.served_custom_alerts(tenants("silent"))
    assert old["t"]["x"]["mode"] == "page" and new["t"]["x"]["mode"] == "page, silent"
