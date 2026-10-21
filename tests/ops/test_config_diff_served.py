"""config_diff 的三個報告區段改從 served-values 讀（ADR-037）。

現有 test_config_diff 的每種情境，在這裡用真的 da-guard 各跑一次：同樣的變更，
從 served-values 讀出來的區段要報出同一件事。刻意改變的三處（#1516 拍板）各有
一條測試釘住：繼承到的閾值變更會列出、新增／移除的租戶不列設定、custom alert
以 served row 呈現（被 disable 的 recipe 報成移除並附說明）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

import config_diff as cd
import _lib_tenant_values as tv

AT = "2026-10-10T03:00:00Z"
CONFIG_DIFF = Path(__file__).resolve().parents[2] / "scripts" / "tools" / "ops" / "config_diff.py"
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
    got = _by_key(_served_metric(b, p)["t"])["mysql_connections"]
    assert got["change"] == change


@pytest.mark.parametrize("word", ["OFF", "dİsable", "false"])
def test_a_switched_off_side_is_served_values_answer(tmp_path, da_guard, word):
    """#3140 PR2：停用與否讀 served-values 的 `keys[].reason` "disabled"，不讀值：exporter
    停用的任何寫法都是 toggled，顯示 "disabled"、不標 not served。"""
    b, p = _served(tmp_path, da_guard, {"t": {"mysql_connections": "100"}}, {"t": {"mysql_connections": word}})
    got = _by_key(_served_metric(b, p)["t"])["mysql_connections"]
    assert (got["new"], got["change"], got["disabled"]) == (word, "toggled", ["new"])
    assert "not_served" not in got
    assert cd._format_side(got, "new") == "disabled"


@pytest.mark.parametrize("word", ["disable:critical", "disable\x1c"])
def test_a_value_the_exporter_does_not_switch_off_is_not_toggled(tmp_path, da_guard, word):
    """#3141：閾值列的 `disable:<sev>`、帶 Go 不修剪的空白的 `disable` 不是停用——exporter
    改送預設值 80，served 視圖是 100 → 80（looser），不是 toggled。"""
    b, p = _served(tmp_path, da_guard, {"t": {"mysql_connections": "100"}}, {"t": {"mysql_connections": word}})
    got = _by_key(_served_metric(b, p)["t"])["mysql_connections"]
    assert (got["new"], got["change"], got.get("disabled")) == (80, "tighter", None)


def test_severity_only_change_reads_as_written(tmp_path, da_guard):
    b, p = _served(tmp_path, da_guard, {"t": {"mysql_connections": "70"}}, {"t": {"mysql_connections": "70:critical"}})
    got = _by_key(_metric(b, p)["t"])["mysql_connections"]
    assert (got["old"], got["new"]) == (70, "70:critical")


def test_empty_severity_is_a_change_not_a_failed_side(tmp_path, da_guard):
    """#3060：`"70:"`（severity 為空字串）原本讓 served-values 的排程段少了 `severity`，
    讀取端拒收、整支 rc 2；現在兩側都讀得到，`70` → `70:` 列為變更、rc 1。
    #3071：空後綴讓整個值讀不懂，exporter 改送平台預設值 80，所以變更是 70 → 80（looser）。"""
    base = _tree(tmp_path / "base", {"tx": {"mysql_connections": "70"}})
    pr = _tree(tmp_path / "pr", {"tx": {"mysql_connections": "70:"}})
    p = subprocess.run(
        [sys.executable, str(CONFIG_DIFF), "--old-dir", str(base), "--new-dir", str(pr),
         "--at", AT, "--format", "json"],
        env={**os.environ, "DA_GUARD_BINARY": da_guard},
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
    assert p.returncode == 1, (p.returncode, p.stderr)
    doc = json.loads(p.stdout)
    assert doc["views"]["served"]["status"] == "computed", doc["views"]
    assert doc["threshold_changes"] == [
        {"key": "mysql_connections", "old": 70, "new": 80, "change": "looser", "tenants": ["tx"]}]


def test_unreadable_severity_on_both_sides_is_no_change(tmp_path, da_guard):
    """#3071：兩邊都是讀不懂的 severity（`70:high` → `75:High`）時，兩邊 exporter 都送
    預設值 80，served 視圖沒有差異，rc 0（#3071 之前是 modified、rc 1）。這種樹由
    da-guard 的 value_not_served 擋，不是 config-diff 的事。"""
    base = _tree(tmp_path / "base", {"tx": {"mysql_connections": "70:high"}})
    pr = _tree(tmp_path / "pr", {"tx": {"mysql_connections": "75:High"}})
    p = subprocess.run(
        [sys.executable, str(CONFIG_DIFF), "--old-dir", str(base), "--new-dir", str(pr),
         "--at", AT, "--format", "json"],
        env={**os.environ, "DA_GUARD_BINARY": da_guard},
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
    assert p.returncode == 0, (p.returncode, p.stderr)
    assert json.loads(p.stdout)["threshold_changes"] == []


def test_inherited_threshold_change_is_reported_for_tenants_that_do_not_override(tmp_path, da_guard):
    """刻意的改變：`defaults:` 的值改了，沒覆寫它的租戶照樣列出；覆寫的租戶不列。"""
    tenants = {"inherits": {"_silent_mode": "disable"}, "overrides": {"container_memory": "90"}}
    b, p = _served(tmp_path, da_guard, tenants, tenants,
                   pr_defaults=_DEFAULTS.replace("container_memory: 85", "container_memory: 95"))
    diff = _metric(b, p)
    assert _by_key(diff["inherits"])["container_memory"]["change"] == "looser"
    assert "overrides" not in diff


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


@pytest.mark.parametrize("old, new, key", [
    ({"_silent_mode": {"target": "all", "expires": "2027-01-01T00:00:00Z"}},
     {"_silent_mode": {"target": "all", "expires": "2028-01-01T00:00:00Z"}}, "_silent_mode"),
    ({"_silent_mode": {"target": "all", "expires": "2027-01-01T00:00:00Z"}}, {"_silent_mode": "all"}, "_silent_mode"),
    ({"_state_maintenance": {"target": "enable", "expires": "2027-01-01T00:00:00Z"}},
     {"_state_maintenance": {"target": "enable", "expires": "2028-01-01T00:00:00Z"}}, "_state_maintenance"),
], ids=["silence-extended", "silence-made-open-ended", "maintenance-extended"])
def test_time_box_change_is_a_setting_change(tmp_path, da_guard, old, new, key):
    """延長靜音／維護，或把靜音改成無期限：當下的讀數不變，時效變了也要報。"""
    maint = _DEFAULTS + "  maintenance:\n    reasons: []\n    default_state: disable\n"
    b, p = _served(tmp_path, da_guard, {"t": {"mysql_connections": "70", **old}},
                   {"t": {"mysql_connections": "70", **new}}, base_defaults=maint, pr_defaults=maint)
    keys = {e["key"] for e in cd.served_setting_diff(b, p)["t"]}
    assert f"{cd.SERVED_TIME_BOXES_KEY}.{key}" in keys, keys


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


@pytest.mark.parametrize("new,note", [
    ([], cd.SERVED_DELETED_NOTE),
    ([_recipe(threshold="disable")], cd.SERVED_DISABLED_NOTE),
    ([_recipe(threshold="disable:critical")], cd.SERVED_DISABLED_NOTE),
    ([_recipe(threshold="NaN")], cd.SERVED_REFUSED_NOTE.format(refused="non_finite")),
    ([_recipe(threshold="abc")], cd.SERVED_REFUSED_NOTE.format(refused="unparsed")),
    ([_recipe(**{"for": "2m"})], cd.SERVED_REMOVED_NOTE),
], ids=["deleted", "disabled", "disabled-critical", "refused-non-finite", "refused-unparsed", "rejected"])
def test_custom_alert_no_longer_served_is_removed_with_the_note(tmp_path, da_guard, new, note):
    """served row 消失時報成移除；為什麼消失讀 served-values 的 `custom_alerts[].status`／
    `refused`（#3140 PR2）：沒有條目是刪除、`disabled` 是消音、因門檻被拒收另有一句；
    其他拒收（如 `for`）仍是不分辨的那句。"""
    diff = _ca(tmp_path, da_guard, [_recipe()], new)
    (entry,) = diff["t"]
    assert entry["change"] == "removed" and entry["note"] == note


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


def test_served_sections_are_what_the_report_prints(tmp_path, da_guard):
    """`served_sections` 交給報告的區段，就是各自測過的那幾個讀法（`disable` 一節在
    test_config_diff 的 TestDisableWarnings）。"""
    b, p = _served(tmp_path, da_guard,
                   {"t": {"mysql_connections": "100", "_silent_mode": "warning", "_custom_alerts": [_recipe()]}},
                   {"t": {"mysql_connections": "80", "_silent_mode": "all",
                          "_custom_alerts": [_recipe(threshold="200:critical")]}})
    sections = cd.served_sections(b, p)
    assert sections == {"metric": _metric(b, p), "setting": cd.served_setting_diff(b, p),
                        "custom_alert": cd.served_custom_alert_diff(b, p),
                        "disable": cd.served_disable_warnings(b, p)}
    assert sections["metric"] and sections["setting"] and sections["custom_alert"]


def test_every_row_of_a_recipe_counts_not_only_the_first():
    """固定列序：同名的第二列改了 mode，view 也要跟著變（不能只看第一列）。"""
    def row(metric, mode):
        return {"metric": metric, "value": 150.0, "severity": "warning",
                "labels": {"name": "x", "mode": mode, "recipe_id": f"threshold__{metric}"}}

    def tenants(second_mode):
        return {"t": tv.TenantValues("t", {"_custom_alerts": [row("m1", "page"), row("m2", second_mode)]},
                                     {}, {}, {}, None, {}, {}, {})}
    old, new = cd.served_custom_alerts(tenants("page")), cd.served_custom_alerts(tenants("silent"))
    assert old["t"]["x"]["mode"] == "page" and new["t"]["x"]["mode"] == "page, silent"


# ── unserved ↔ 出列（#3022）───────────────────────────────────────────────


_NO_PG = _DEFAULTS
_WITH_PG = _DEFAULTS.replace("  container_memory: 85\n", "  container_memory: 85\n  pg_connections: 80\n")


def _served_metric(b, p):
    return cd.served_metric_diff(b, p)


@pytest.mark.parametrize("base_defaults, pr_defaults, change, not_served", [
    (_NO_PG, _WITH_PG, "added", ["old"]),     # 補上 base：開始出列
    (_WITH_PG, _NO_PG, "removed", ["new"]),   # 拿掉 base：不再出列
])
def test_a_key_crossing_unserved_and_served_is_reported_with_the_same_number(
        tmp_path, da_guard, base_defaults, pr_defaults, change, not_served):
    """寫入的數字沒變，但 /metrics 有沒有這列變了：告警因此開始或停止生效。
    舊行為把 `'85'`（unserved 原文）與 `85` 比成相同，整列不報。"""
    tenants = {"t": {"pg_connections": "85"}}
    b, p = _served(tmp_path, da_guard, tenants, tenants,
                   base_defaults=base_defaults, pr_defaults=pr_defaults)
    got = _by_key(_served_metric(b, p)["t"])["pg_connections"]
    assert (got["change"], got["not_served"]) == (change, not_served)
    # 讀數本身維持 served-values 給的樣子（遮罩與格式化照舊作用在它上面）
    assert {float(got["old"]), float(got["new"])} == {85.0}


def test_a_crossing_with_a_new_number_is_still_added(tmp_path, da_guard):
    b, p = _served(tmp_path, da_guard, {"t": {"pg_connections": "85"}},
                   {"t": {"pg_connections": "90"}}, base_defaults=_NO_PG, pr_defaults=_WITH_PG)
    got = _by_key(_served_metric(b, p)["t"])["pg_connections"]
    assert (got["change"], got["not_served"]) == ("added", ["old"])


def test_an_unserved_key_that_stays_unserved_is_not_a_change(tmp_path, da_guard):
    tenants = {"t": {"pg_connections": "85"}}
    b, p = _served(tmp_path, da_guard, tenants, tenants)
    assert "t" not in _served_metric(b, p)


def test_a_change_on_a_key_served_on_neither_side_is_marked_on_both(tmp_path, da_guard):
    b, p = _served(tmp_path, da_guard, {"t": {"pg_connections": "85"}},
                   {"t": {"pg_connections": "90"}})
    got = _by_key(_served_metric(b, p)["t"])["pg_connections"]
    assert got["not_served"] == ["old", "new"]
    assert cd._format_side(got, "old") == "85 (not served)"


@pytest.mark.parametrize("root", [".nan", ".inf", "-.inf"])
def test_a_row_lost_to_a_non_finite_root_default_is_marked_not_served(tmp_path, da_guard, root):
    """#3140：根 `defaults:` 改成非有限數、租戶沒寫這個 key：沒有預設列。
    served-values 標 `root_default_non_finite`，所以標 not served，不是單純的移除。"""
    tenants = {"t": {"container_memory": "70"}}
    b, p = _served(tmp_path, da_guard, tenants, tenants,
                   pr_defaults=_DEFAULTS.replace("mysql_connections: 80", f"mysql_connections: {root}"))
    got = _by_key(_served_metric(b, p)["t"])["mysql_connections"]
    assert (got["change"], got.get("not_served")) == ("removed", ["new"]), got
    assert cd._format_side(got, "new").endswith("(not served)")


def test_a_disabled_key_still_reads_as_written(tmp_path, da_guard):
    """值為 disable 的維度鍵不標 not served，toggled 的判斷不變。"""
    b, p = _served(tmp_path, da_guard,
                   {"t": {'mysql_connections{schema="x"}': "disable"}},
                   {"t": {'mysql_connections{schema="x"}': "70"}})
    got = _by_key(_served_metric(b, p)["t"])['mysql_connections{schema="x"}']
    assert (got["old"], got["change"]) == ("disable", "toggled")
    assert "not_served" not in got


def test_the_marker_is_added_after_masking_so_a_short_credential_stays_masked(tmp_path, da_guard):
    """遮罩比對整個值：標記若寫進值裡，`Ab3x9Q (not served)` 就不再等於秘密本身。"""
    secret = "Ab3x9Q"
    report = {"metric": {"t": [{"key": "webhook_url", "old": secret, "new": "Zq7w2K",
                                 "change": "modified", "not_served": ["old", "new"]}]}}
    masked, count = cd.mask_values(report, {secret, "Zq7w2K"})
    c = masked["metric"]["t"][0]
    assert count == 2 and cd._format_side(c, "old") == f"{cd.REDACTED} (not served)"


def test_a_scheduled_value_not_served_still_reads_as_scheduled():
    c = {"key": "pg_d", "old": "85", "new": {"default": "85", "overrides": []},
         "change": "modified", "not_served": ["new"]}
    assert cd._format_side(c, "new") == "(scheduled) (not served)"
    assert cd._format_side(c, "old") == "85"


# ── #3045：沒登記的 `_` 鍵不再送出 ──────────────────────────────────────

_PLAIN = "defaults:\n  mysql_connections: 80\n"


def _diff_json(tmp_path, da_guard, base: dict, pr: dict, base_defaults: str, pr_defaults: str) -> dict:
    b = _tree(tmp_path / "base", base, base_defaults)
    p = _tree(tmp_path / "pr", pr, pr_defaults)
    r = subprocess.run(
        [sys.executable, str(CONFIG_DIFF), "--old-dir", str(b), "--new-dir", str(p),
         "--at", AT, "--format", "json"],
        env={**os.environ, "DA_GUARD_BINARY": da_guard},
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
    assert r.returncode == 1, (r.returncode, r.stderr)
    doc = json.loads(r.stdout)
    assert doc["views"]["served"]["status"] == "computed", doc["views"]
    return doc


def _changes(entries):
    return sorted((e["key"], e["change"], tuple(e["tenants"])) for e in entries)


def test_adding_a_leading_underscore_reports_the_rows_that_disappear(tmp_path, da_guard):
    """#3045：把 `myth` 改名成沒登記的 `_myth`（根 defaults 與租戶檔都改），它在
    任何一層都不送出：閾值區段列出兩個租戶的那一列被移除，`_myth` 只出現在設定
    區段——不會被當成一列新的閾值。遷移（反方向）則是那兩列出現。"""
    tenants_plain = {"ta": {"myth": "7"}, "tb": {"mysql_connections": "60"}}
    tenants_under = {"ta": {"_myth": "7"}, "tb": {"mysql_connections": "60"}}
    doc = _diff_json(tmp_path, da_guard, tenants_plain, tenants_under,
                     _PLAIN + "  myth: 5\n", _PLAIN + "  _myth: 5\n")
    assert _changes(doc["threshold_changes"]) == [("myth", "removed", ("ta",)),
                                                  ("myth", "removed", ("tb",))]
    assert _changes(doc["setting_changes"]) == [("_myth", "added", ("ta",)),
                                                ("_myth", "added", ("tb",))]
    old = {tuple(e["tenants"]): e["old"] for e in doc["threshold_changes"]}
    assert old == {("ta",): 7, ("tb",): 5}

    back = _diff_json(tmp_path / "back", da_guard, tenants_under, tenants_plain,
                      _PLAIN + "  _myth: 5\n", _PLAIN + "  myth: 5\n")
    assert _changes(back["threshold_changes"]) == [("myth", "added", ("ta",)),
                                                   ("myth", "added", ("tb",))]


def test_a_root_only_unregistered_key_is_reported_for_every_tenant(tmp_path, da_guard):
    """#3045：根 defaults 新增沒登記的 `_myth`、沒有租戶寫它：served-values 把它列進
    每個租戶的 unserved，所以 config-diff 對每個租戶都報，不會只報「平台檔改了」。"""
    tenants = {"ta": {"mysql_connections": "70"}, "tb": {}}
    doc = _diff_json(tmp_path, da_guard, tenants, tenants, _PLAIN, _PLAIN + "  _myth: 5\n")
    assert doc["threshold_changes"] == []
    # 同一個值（根的 5）：一筆，兩個租戶都列。
    assert _changes(doc["setting_changes"]) == [("_myth", "added", ("ta", "tb"))]
