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
    assert cd.compute_diff({"inherits": {}}, {"inherits": {}}) == {}


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
    assert (got["_state_filters.crashloop"]["old"], got["_state_filters.crashloop"]["new"]) == ("critical", "warning")


def test_routing_credential_is_redacted(tmp_path, da_guard):
    def body(url):
        return {"t": {"mysql_connections": "70", "_routing": {"receiver": {"type": "webhook", "url": url}}}}
    b, p = _served(tmp_path, da_guard, body("https://h/SEKRET-1"), body("https://h/SEKRET-2"))
    diff = cd.served_setting_diff(b, p)
    assert "SEKRET" not in repr(diff)
    url = [e for e in diff["t"] if e["key"].endswith(".url")]
    assert url and (url[0]["old"], url[0]["new"]) == (cd._REDACTED, cd._REDACTED)


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
