#!/usr/bin/env python3
"""test_config_diff.py — config-diff：比較兩側的渲染結果（ADR-037）。

分三層：
* 純函式（分類、差異、custom alert 欄位、遮罩、JSON 分組）不需要 da-guard。
* 報告排版用合成的 report（`_report`）餵 `render_markdown`。
* 行為一律經真的入口、真的 da-guard 與路由產生器跑（`_run_cli`）：結束碼、
  #1405 的形狀錯誤、#2120 的平台 `_profile`、只改註解、ADR 的兩個範例。
  每個「會響」的情境都配一個不該響的對照組。

served-values 的讀法本身（閾值怎麼讀成 tighter／looser、設定與 custom alert
怎麼重建）在 test_config_diff_served.py；渲染與比對在 test_render_diff.py。
"""

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

import config_diff as cd  # noqa: E402
import _render_diff as rd  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
SAMPLE = REPO_ROOT / "components" / "threshold-exporter" / "config" / "conf.d"
AT = "2026-10-10T00:00:00Z"

# Every run renders through da-guard (ADR-037); the CLI finds it via $DA_GUARD_BINARY.
pytestmark = pytest.mark.usefixtures("da_guard_env")


def _run_cli(old_dir, new_dir, *extra, at=AT):
    """Invoke the tool the way a pipeline does — as a process — and read its rc."""
    at_args = ("--at", at) if at else ()
    return subprocess.run(
        [sys.executable, "-X", "utf8", cd.__file__,
         "--old-dir", str(old_dir), "--new-dir", str(new_dir), *at_args, *extra],
        capture_output=True, encoding="utf-8", errors="replace", timeout=300,
    )


def _json(p):
    doc = json.loads(p.stdout)
    assert doc["schema"] == cd.JSON_SCHEMA
    return doc


def _write_tenant(directory, tenant_cfg, name="db-a"):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.yaml").write_text(
        yaml.safe_dump({"tenants": {name: tenant_cfg}}), encoding="utf-8", newline="\n")


def _pair(tmp_path, files_old, files_new):
    old, new = tmp_path / "old", tmp_path / "new"
    for root, files in ((old, files_old), (new, files_new)):
        root.mkdir()
        for rel, text in files.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(text, encoding="utf-8", newline="\n")
    return old, new


def _sample_pair(tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    shutil.copytree(SAMPLE, old)
    shutil.copytree(SAMPLE, new)
    return old, new


def _edit(conf_d, old, new, rel="_defaults.yaml"):
    p = Path(conf_d) / rel
    text = p.read_text(encoding="utf-8")
    assert text.count(old) == 1, f"{old!r} occurs {text.count(old)} times in {rel}"
    p.write_text(text.replace(old, new), encoding="utf-8")


_OK = rd.ViewResult("computed", "")


def _v2_doc(files):
    """A tree-mode parse-tree document holding *files* (rel → content), each a
    config file both readers read."""
    def entry(rel):
        name = rel.rsplit("/", 1)[-1]
        cls = ("defaults" if name.lower() in ("_defaults.yaml", "_defaults.yml")
               else "platform" if name.startswith("_") else "tenant")
        return {"path": rel, "depth": rel.count("/"), "class": cls,
                "tenant_named": name.rsplit(".", 1)[0] if cls == "tenant" else None,
                "defaults_document": cls == "defaults", "readers": ["exporter", "generator"],
                "not_read": None, "role": "config", "problem": None}
    return {"schema": "da-guard.parse-tree/v2", "da_guard_version": "t", "mode": "tree",
            "defaults_carriers": {},
            "entries": [entry(r) for r in sorted(files)], "files": files, "rendered_by": {}}


def _row(file, key, status=None, note="not a setting either render reads", change="modified"):
    """A platform-settings row as `platform_settings` returns it."""
    return {"file": file, "key": key, "change": change,
            "status": status or cd.SETTING_NOT_COMPARED, "note": note, "views": []}


def _report(**kw):
    """A report as `build_report` returns it, every section empty unless given."""
    r = {"old_dir": "old", "new_dir": "new", "at": AT,
         "views": {rd.VIEW_SERVED: _OK, rd.VIEW_ROUTES: _OK, cd.VIEW_PLATFORM: _OK},
         "metric": {}, "setting": {}, "custom_alert": {}, "disable": {}, "tenants_added": [],
         "tenants_removed": [], "dropped": {}, "other_served": {}, "routes": [],
         "receivers": [], "inhibits": [], "uncovered_files": [], "platform_settings": []}
    r.update(kw)
    return r


# ── 1. 分類與差異（純函式） ──────────────────────────────────────────


class TestClassifyChange:

    @pytest.mark.parametrize(
        "old, new, off, expected",
        [
            (None, 50, (False, False), "added"),
            (50, None, (False, False), "removed"),
            (80, 50, (False, False), "tighter"),
            (50, 80, (False, False), "looser"),
            (50, "disable", (False, True), "toggled"),
            ("disable", 50, (True, False), "toggled"),
            ({"default": 50, "schedule": []}, {"default": 70, "schedule": []}, (False, False), "modified"),
            (50, 50, (False, False), "unchanged"),
        ],
        ids=["added", "removed", "tighter", "looser", "toggled_disable", "toggled_enable",
             "modified_dict", "same_value_unchanged"],
    )
    def test_classify_change(self, old, new, off, expected):
        assert cd.classify_change(old, new, *off) == expected

    def test_switched_off_is_the_callers_answer_not_the_values(self):
        """#3140 PR2：停用與否是 served-values 的 `keys[].reason` "disabled"（由呼叫端
        傳入），不是這裡讀值：同樣寫 `disable`、沒說停用就不是 toggled。"""
        assert cd.classify_change(50, "disable") == "modified"
        assert cd.classify_change(50, "OFF", False, True) == "toggled"


class TestComputeDiff:

    def test_basic_diff(self):
        diffs = cd.compute_diff({"db-a": {"mysql_connections": 80, "redis_memory": 1024}},
                                {"db-a": {"mysql_connections": 50, "redis_memory": 1024}})
        assert [c["change"] for c in diffs["db-a"]] == ["tighter"]

    def test_new_and_removed_tenant(self):
        assert cd.compute_diff({}, {"db-c": {"pg_cache": 0.9}})["db-c"][0]["change"] == "added"
        assert cd.compute_diff({"db-x": {"m": 50}}, {})["db-x"][0]["change"] == "removed"

    def test_no_changes_and_untouched_tenant(self):
        assert cd.compute_diff({"db-a": {"m": 50}}, {"db-a": {"m": 50}}) == {}
        diffs = cd.compute_diff({"a": {"m": 50}, "b": {"r": 1}}, {"a": {"m": 70}, "b": {"r": 1}})
        assert set(diffs) == {"a"}


class TestEstimateAffectedAlerts:
    """欄位內容取自 rule pack，不是 key 拼法的 CamelCase（舊版寫 *MysqlConnections*）。"""

    def test_names_come_from_the_rule_packs(self):
        assert cd.estimate_affected_alerts("mysql_connections") == \
            "MariaDBHighConnections, MariaDBSystemBottleneck"

    def test_a_key_no_alert_reads(self):
        assert cd.estimate_affected_alerts("cpu") == "—"

    def test_unknown_when_the_rule_packs_are_missing(self, monkeypatch):
        monkeypatch.setattr(cd, "alerts_for_key", lambda key: None)
        assert cd.estimate_affected_alerts("mysql_connections") == "unknown"


def test_format_value():
    """#3140 PR2：顯示 "disabled" 依 exporter 的答案（`disabled` 參數），不讀值。"""
    assert cd._format_value("OFF", True) == "disabled"
    assert cd._format_value("disable") == "disable"
    assert cd._format_value(None) == "—"
    assert cd._format_value(50) == "50"
    assert cd._format_value({"schedule": []}) == "(scheduled)"


# ── 2. custom alert 欄位與排版 ───────────────────────────────────────


def _recipe(name, threshold="150:warning", mode="page", **extra):
    r = {"recipe": "threshold", "name": name, "metric": "mysql_global_status_threads_connected",
         "op": ">", "window": "5m", "threshold": threshold, "mode": mode}
    r.update(extra)
    return r


def _ca_modified(field, old, new):
    return {"t": [{"name": "x", "change": "modified", "old": {}, "new": {},
                   "field_changes": [{"field": field, "old": old, "new": new}]}]}


class TestCustomAlertDiff:
    """Custom alert recipe diffing (ADR-024 Capability B, #741)."""

    def test_added_removed_modified(self):
        old = {"db-b": {"x": _recipe("x"), "gone": _recipe("gone")}}
        new = {"db-b": {"x": _recipe("x", threshold="200:critical"), "new": _recipe("new")}}
        by_name = {c["name"]: c for c in cd.compute_custom_alert_diff(old, new)["db-b"]}
        assert {n: c["change"] for n, c in by_name.items()} == \
            {"gone": "removed", "new": "added", "x": "modified"}
        assert by_name["x"]["field_changes"] == [
            {"field": "threshold", "old": "150:warning", "new": "200:critical"}]

    def test_no_change_when_identical(self):
        same = {"db-b": {"x": _recipe("x")}}
        assert cd.compute_custom_alert_diff(same, same) == {}

    def test_a_switched_off_recipe_is_highlighted_by_its_note(self):
        """Reef 1：停用的 recipe 是消音，不是參數微調。exporter 停用的 recipe 不送 row，
        所以是一筆 removed，由 served-values 的 `custom_alerts[].status` 寫出的 note
        標出（#3140 PR2；`_removed_note`）。"""
        removed = {"t": [{"name": "pager", "change": "removed", "old": {"metric": "m"},
                          "new": None, "field_changes": [], "note": cd.SERVED_DISABLED_NOTE}]}
        md = cd.render_markdown(_report(custom_alert=removed))
        assert "DISABLED (alert silenced)" in md

    def test_a_threshold_field_change_is_not_read_for_a_disable(self):
        """served 視圖的 threshold 是 exporter 送的 `<value>:<severity>`，不會是停用字；
        欄位變更不再自己判停用（#3140 PR2 拿掉 `_threshold_disabled`）。"""
        assert cd._field_change_annotation("threshold", "100:critical", "disable") == ""

    def test_disable_to_enable_not_flagged_as_silencing(self):
        md = cd.render_markdown(_report(custom_alert=_ca_modified("threshold", "disable", "100:critical")))
        assert "DISABLED (alert silenced)" not in md

    def test_mode_page_to_silent_is_highlighted(self):
        md = cd.render_markdown(_report(custom_alert=_ca_modified("mode", "page", "silent")))
        assert "paging suppressed" in md

    def test_format_recipe_value_neutralizes_backticks_and_newlines(self):
        """F5 / Reef 4: a backtick is the only code-span break-out; a newline shatters the list."""
        assert "`" not in cd._format_recipe_value("evil`backtick")
        assert cd._code_span("a`b`c").count("`") == 2
        assert "\n" not in cd._format_recipe_value("line1\nline2")
        assert "\n" not in cd._format_recipe_value("a\r\nb")

    def test_render_neutralizes_injection_in_recipe_value(self):
        evil = {"label": "v\"</details><script>alert(1)</script>`backtick`"}
        md = cd.render_markdown(_report(custom_alert=_ca_modified("selectors", None, evil)))
        assert md.count("`") % 2 == 0, "unbalanced backticks → code-span break-out"
        assert "`backtick`" not in md
        for line in md.splitlines():
            assert line.count("`") % 2 == 0, line

    def test_removed_recipe_carries_the_served_note(self):
        """#1517：served row 消失時，note 說明能不能分辨刪除、disable、被拒收；分不出時
        （SERVED_REMOVED_NOTE）不判定消音。#3140 PR2：分得出時由 served-values 說。"""
        removed = {"t": [{"name": "pager", "change": "removed", "old": {"metric": "m", "threshold": "1:critical"},
                          "new": None, "field_changes": [], "note": cd.SERVED_REMOVED_NOTE}]}
        lines = cd.render_markdown(_report(custom_alert=removed)).splitlines()
        section = lines.index("## Custom Alert Changes")
        limits = next(i for i, ln in enumerate(lines) if "Read from the rows the exporter serves" in ln)
        item = next(i for i, ln in enumerate(lines) if ln.startswith("- `pager` (removed)"))
        assert section < limits < item
        assert lines[item + 1] == f"  - {cd.SERVED_REMOVED_NOTE}"
        for claim in ("an `op` / `window` / `for` change shows as a `recipe_id` change",
                      "the note under it says which when the exporter's reading"):
            assert claim in lines[limits], (claim, lines[limits])
        assert lines[limits].startswith("> ") and lines[limits - 1] == ">"

    def test_render_markdown_truncates_oversized_output(self):
        """Reef 2: never exceed GitHub's comment limit."""
        big = {f"tenant-{i:05d}": [{"name": f"recipe_{i}", "change": "added", "old": None,
                                     "new": {"metric": "m", "threshold": "1:warning", "mode": "page"},
                                     "field_changes": []}] for i in range(2000)}
        md = cd.render_markdown(_report(custom_alert=big))
        assert len(md) <= cd.COMMENT_SAFETY_LIMIT < cd.GITHUB_COMMENT_HARD_LIMIT
        assert "truncated" in md


# ── 3. 報告排版（合成 report） ───────────────────────────────────────


class TestRenderMarkdown:

    def test_no_changes(self):
        md = cd.render_markdown(_report())
        assert md.splitlines()[-1] == "No changes detected."
        assert f"Rendered at: `{AT}`" in md

    def test_threshold_table_and_summary(self):
        md = cd.render_markdown(_report(metric={
            "db-a": [{"key": "mysql_connections", "old": 80, "new": 50, "change": "tighter"}]}))
        assert "## Threshold Changes" in md
        assert "| mysql_connections | 80 | 50 | tighter |" in md and md.count("| tenant `db-a` |") == 1
        assert md.splitlines()[-1].startswith("Summary: 1 tenant(s) changed, 1 metric change(s)")

    def test_summary_counts_each_tenant_once_across_sections(self):
        """CodeRabbit #773：只有 custom alert 或路由變更的租戶也要算進 tenant(s) changed，且同一租戶只算一次。"""
        added = [{"name": "x", "change": "added", "old": None, "new": {}, "field_changes": []}]
        route = rd.RouteChange("db-c", ('tenant="db-c"',), "changed", {"group_wait": ("30s", "1m")})
        platform = rd.RouteChange(rd.PLATFORM, ('severity="critical"',), "added", {"receiver": (None, "noc")})
        md = cd.render_markdown(_report(
            metric={"db-b": [{"key": "m", "old": 80, "new": 50, "change": "tighter"}]},
            custom_alert={"db-b": added}, routes=[route, platform]))
        summary = md.splitlines()[-1]
        assert summary.startswith("Summary: 2 tenant(s) changed"), summary
        assert "2 routing change(s) (some platform-wide)" in summary

    # ── P4：同一個變更只列一次，帶它影響的租戶數與前幾個名字（ADR-037） ──

    def test_a_platform_change_is_one_entry_in_every_section(self):
        """1000 個租戶吃到同一個平台變更：每一段只列一筆，寫租戶數與前 5 個名字。"""
        tenants = [f"t{i:04d}" for i in range(1000)]
        same = lambda c: {t: [dict(c)] for t in tenants}  # noqa: E731
        ca = {"name": "x", "change": "modified", "old": {}, "new": {},
              "field_changes": [{"field": "threshold", "old": "1:warning", "new": "2:warning"}]}
        md = cd.render_markdown(_report(
            metric=same({"key": "container_cpu", "old": 80, "new": 90, "change": "looser"}),
            setting=same({"key": "_silent_mode", "old": None, "new": "warning", "change": "added"}),
            custom_alert=same(ca),
            dropped=same({"key": "_x", "old": None, "new": "bad", "change": "added"}),
            other_served=same({"field": "time_boxes", "key": "k", "old": 1, "new": 2}),
            routes=[rd.RouteChange(t, (f'tenant="{t}"',), "changed", {"group_wait": ("30s", "1m")})
                    for t in tenants],
            receivers=[rd.ReceiverChange(f"tenant-{t}", (t,), "changed", {"send_resolved": (True, False)})
                       for t in tenants],
            inhibits=[rd.InhibitChange(t, "added", {"equal": ["alertname"]}) for t in tenants]))
        who = "1000 tenants: `t0000`, `t0001`, `t0002`, `t0003`, `t0004` (+995 more)"
        assert md.count(who) == 8, md
        assert "`t0005`" not in md
        assert "| container_cpu | 80 | 90 | looser |" in md and md.count("| container_cpu |") == 1
        assert f"- `_silent_mode` (added) — `warning` — {who}" in md
        assert f"- `x` (modified) — {who}" in md
        assert f"- {who} — 1000 routes (changed), e.g. `tenant=\"t0000\"`" in md
        assert f"- 1000 receivers (changed): `tenant-t0000`, " in md
        assert f"- {who} — 1000 rules (added): " in md
        assert len(md) < cd.COMMENT_SAFETY_LIMIT and "truncated" not in md
        # 摘要照舊以租戶計
        assert md.splitlines()[-1].startswith("Summary: 1000 tenant(s) changed, 1000 metric change(s)")

    def test_different_changes_stay_apart(self):
        """對照組：值不同就是不同的變更，各列一筆，各自的租戶。"""
        md = cd.render_markdown(_report(metric={
            "a": [{"key": "m", "old": 1, "new": 2, "change": "looser"}],
            "b": [{"key": "m", "old": 1, "new": 2, "change": "looser"}],
            "c": [{"key": "m", "old": 1, "new": 3, "change": "looser"}]}))
        rows = [ln for ln in md.splitlines() if ln.startswith("| m |")]
        assert len(rows) == 2, rows
        assert rows[0].startswith("| m | 1 | 2 | looser |") and rows[0].endswith("| 2 tenants: `a`, `b` |")
        assert rows[1].startswith("| m | 1 | 3 | looser |") and rows[1].endswith("| tenant `c` |")

    def test_different_routing_changes_stay_apart(self):
        """對照組（路由三段）：值不同的 route／receiver／inhibit 變更各列一筆，不因 kind 相同就合併。"""
        routes = [rd.RouteChange("a", ('tenant="a"',), "changed", {"group_wait": ("30s", "1m")}),
                  rd.RouteChange("b", ('tenant="b"',), "changed", {"group_wait": ("30s", "2m")})]
        recv = [rd.ReceiverChange("ra", ("a",), "changed", {"send_resolved": (True, False)}),
                rd.ReceiverChange("rb", ("b",), "changed", {"send_resolved": (False, True)})]
        inhibit = [rd.InhibitChange("a", "added", {"equal": ["x"]}),
                   rd.InhibitChange("b", "added", {"equal": ["y"]})]
        md = cd.render_markdown(_report(routes=routes, receivers=recv, inhibits=inhibit))
        assert "- tenant `a` — route `tenant=\"a\"` (changed)\n  - `group_wait`: `30s` → `1m`" in md
        assert "- tenant `b` — route `tenant=\"b\"` (changed)\n  - `group_wait`: `30s` → `2m`" in md
        assert "- `ra` (changed) — tenant `a`" in md and "- `rb` (changed) — tenant `b`" in md
        assert "- tenant `a` (added): " in md and "- tenant `b` (added): " in md
        assert "2 tenants" not in md

    def test_routes_differing_beyond_the_tenant_are_listed_apart(self):
        """matcher 除了租戶還有別的（alertname、component）時，那就是 route 抓什麼告警：
        同一個租戶新增兩條 matcher 不同的 route，各列一筆；只差在租戶的才合併。"""
        same = {"receiver": (None, "x")}
        routes = [rd.RouteChange("t1", ('component="custom"', 'tenant="t1"', 'alertname="A"'), "added", same),
                  rd.RouteChange("t1", ('tenant="t1"', 'alertname="A"'), "added", same),
                  rd.RouteChange("t2", ('tenant="t2"', 'alertname="A"'), "added", same)]
        md = cd.render_markdown(_report(routes=routes))
        assert '- tenant `t1` — route `component="custom" / tenant="t1" / alertname="A"` (added)' in md
        assert '- 2 tenants: `t1`, `t2` — 2 routes (added), e.g. `tenant="t1" / alertname="A"`' in md

    def test_a_repeated_sibling_keeps_its_place_in_the_shape(self):
        """同層 matcher 相同的第二個兄弟節點（`#2`）通常被前一個遮住：不得與一般節點併成一筆。"""
        assert cd._path_shape(('tenant="t1" #2',)) == ["tenant=* #2"]
        assert cd._path_shape(('tenant="t1"',)) == ["tenant=*"]
        assert cd._path_shape(('severity="critical", tenant="t1" #2',)) == ['severity="critical", tenant=* #2']

    def test_a_comma_inside_a_quoted_value_is_not_a_matcher_boundary(self):
        """CodeRabbit：值裡的 `, tenant=x` 不是租戶 matcher；兩條只差在那個值的 route 形狀不同。"""
        blue = cd._path_shape(('component="api, tenant=blue", tenant="t1"',))
        green = cd._path_shape(('component="api, tenant=green", tenant="t1"',))
        assert blue == ['component="api, tenant=blue", tenant=*'] and blue != green
        assert cd._split_matchers('a="x\\", b", c="y"') == ['a="x\\", b"', 'c="y"']
        assert cd._split_matchers('tenant="t1" #2') == ['tenant="t1" #2']

    def test_one_tenant_beside_the_platform_is_singular(self):
        recv = [rd.ReceiverChange("noc", (rd.PLATFORM, "a"), "changed", {"send_resolved": (True, False)})]
        assert "- `noc` (changed) — platform and 1 tenant: `a`" in cd.render_markdown(_report(receivers=recv))

    def test_tenants_cell_keeps_a_backslash_single(self):
        """Tenants 欄的名字在 code span 裡，反斜線是字面：不得像 `_cell` 那樣變成兩個；`|` 仍要跳脫。"""
        c = {"key": "m", "old": 1, "new": 2, "change": "looser"}
        md = cd.render_markdown(_report(metric={"x\\y": [dict(c)], "p|q": [dict(c)]}))
        row = next(ln for ln in md.splitlines() if ln.startswith("| m |"))
        assert row.endswith("| 2 tenants: `p\\|q`, `x\\y` |"), row

    def test_platform_is_named_beside_tenants(self):
        """平台與租戶都用到同一個 receiver：寫 platform and N tenants，platform 在前。"""
        recv = [rd.ReceiverChange("noc", ("b", rd.PLATFORM, "a"), "changed", {"send_resolved": (True, False)})]
        md = cd.render_markdown(_report(receivers=recv))
        assert "- `noc` (changed) — platform and 2 tenants: `a`, `b`" in md

    def test_routing_section(self):
        routes = [rd.RouteChange("db-a", ('tenant="db-a"',), "changed", {"group_wait": ("30s", "99s")}),
                  rd.RouteChange(rd.PLATFORM, ('severity="critical"',), "added", {"receiver": (None, "noc")})]
        recv = [rd.ReceiverChange("tenant-db-a", ("db-a",), "changed",
                                  {"webhook_configs.0.url": (rd.MASKED, rd.MASKED)})]
        inhibit = [rd.InhibitChange("db-a", "added", {"source_matchers": ['tenant="db-a"']})]
        md = cd.render_markdown(_report(routes=routes, receivers=recv, inhibits=inhibit))
        assert "- tenant `db-a` — route `tenant=\"db-a\"` (changed)\n  - `group_wait`: `30s` → `99s`" in md
        assert "- platform — route `severity=\"critical\"` (added)" in md
        assert (f"- `tenant-db-a` (changed) — tenant `db-a`\n"
                f"  - `webhook_configs.0.url`: `{rd.MASKED}` → `{rd.MASKED}`") in md
        assert "### Inhibit Rules\n\n- tenant `db-a` (added): " in md

    @pytest.mark.parametrize("status, mark", [("not_computed", ":x:"), ("not_compared", ":warning:")])
    def test_a_view_without_a_result_is_never_no_changes(self, status, mark):
        """沒有結果不是沒有變更（ADR-037 決定 4）：報告要寫出原因，最後一行不得是 No changes detected。"""
        views = {rd.VIEW_SERVED: rd.ViewResult(status, "PR side: x.yaml does not decode"),
                 rd.VIEW_ROUTES: _OK}
        md = cd.render_markdown(_report(views=views))
        assert f"- {mark} **{cd.VIEW_TITLES[rd.VIEW_SERVED]}: {status.replace('_', ' ')}.**" in md
        assert "`PR side: x.yaml does not decode`" in md
        assert "No changes detected." not in md

    def test_setting_names_cannot_break_out_of_their_code_spans(self):
        """檔名與鍵名來自 PR 的樹：反引號不得關掉 code span，控制字元不得原樣印出。"""
        rows = [_row("_x`y\x1b[2J.yaml", "k`\x1b[1m")]
        md = cd.render_markdown(_report(platform_settings=rows, uncovered_files=[rows[0]["file"]]))
        assert "\x1b" not in md
        assert "- `_x'y\\x1b[2J.yaml` `k'\\x1b[1m` (modified)" in md

    def test_long_settings_list_is_capped_in_markdown(self):
        n = cd.UNCOVERED_LIST_LIMIT + 3
        rows = [_row(f"_f{i:03d}.yaml", "defaults") for i in range(n)]
        md = cd.render_markdown(_report(platform_settings=rows,
                                        uncovered_files=[r["file"] for r in rows]))
        assert "- ... (+3 more; the JSON report lists them all)" in md
        assert sum(line.startswith("- `_f") for line in md.splitlines()) == cd.UNCOVERED_LIST_LIMIT
        assert f"**:warning: Not compared** ({n})" in md

    def test_long_names_are_shortened_so_the_list_fits_a_comment(self):
        """名稱來自 PR 的樹：一個 7 萬字元的鍵不得把整則留言擠掉（JSON 保留全名）。"""
        rows = [_row(f"{i:02d}/_" + "c" * 2000 + ".yaml", "k" * 70_000)
                for i in range(cd.UNCOVERED_LIST_LIMIT)]
        md = cd.render_markdown(_report(platform_settings=rows,
                                        uncovered_files=[r["file"] for r in rows]))
        assert len(md) <= cd.COMMENT_SAFETY_LIMIT
        assert "truncated to fit GitHub's comment limit" not in md
        assert "k" * (cd.SETTING_NAME_LIMIT + 1) not in md and "k" * cd.SETTING_NAME_LIMIT + "…" in md


class TestBuildReport:
    """build_report 把 RenderDiff 拆成區段；served 的每一筆變更都要落在某個區段裡。"""

    def _diff(self, *changes, report=None):
        served = rd.ViewResult("computed", "", tuple(changes), report or {})
        return rd.RenderDiff(AT, served, _OK)

    def test_tenants_added_removed_and_dropped_keys(self, tmp_path):
        diff = self._diff(rd.ValueChange("n", "tenant", "n", None, True),
                          rd.ValueChange("g", "tenant", "g", True, None),
                          rd.ValueChange("t", "dropped", "bad_key", None, ["not a number"]))
        r = cd.build_report(diff, tmp_path, tmp_path)
        assert (r["tenants_added"], r["tenants_removed"]) == (["n"], ["g"])
        assert r["dropped"] == {"t": [{"key": "bad_key", "old": None, "new": ["not a number"],
                                       "change": "added"}]}
        assert r["other_served"] == {}

    def test_a_served_change_no_section_shows_is_still_printed(self, tmp_path):
        """served-values 日後多一個欄位、或某個區段漏讀：exit code 會是 1，報告卻說沒有變更。
        這種變更一律列在 Other Served Changes，不得無聲。"""
        diff = self._diff(rd.ValueChange("t", "some_new_field", "k", 1, 2))
        r = cd.build_report(diff, tmp_path, tmp_path)
        assert r["other_served"] == {"t": [{"field": "some_new_field", "key": "k", "old": 1, "new": 2}]}
        assert cd.report_exit_code(diff, r) == 1
        md = cd.render_markdown(r)
        assert "## Other Served Changes" in md and "No changes detected." not in md
        # 租戶已在別的區段出現，也不能把它在新欄位的變更藏起來（CodeRabbit 指出的情況）
        mixed = self._diff(rd.ValueChange("t", "values", "m", 1, 2),
                           rd.ValueChange("t", "some_new_field", "k", 1, 2),
                           report={"metric": {"t": [{"key": "m", "old": 1, "new": 2, "change": "looser"}]}})
        assert cd.build_report(mixed, tmp_path, tmp_path)["other_served"] == {
            "t": [{"field": "some_new_field", "key": "k", "old": 1, "new": 2}]}
        # 對照：同一筆變更若有區段報了它的租戶，就不重複列
        shown = self._diff(rd.ValueChange("t", "values", "m", 1, 2),
                           report={"metric": {"t": [{"key": "m", "old": 1, "new": 2, "change": "looser"}]}})
        assert cd.build_report(shown, tmp_path, tmp_path)["other_served"] == {}

    def test_a_reason_cannot_write_terminal_escapes(self, tmp_path, capsys, monkeypatch):
        """原因裡的檔名來自 PR 的樹：報告與 stderr 都不得原樣印出控制字元。"""
        bad = rd.ViewResult("not_computed", "PR side: x\x1b[2J.yaml does not decode")
        md = cd.render_markdown(_report(views={rd.VIEW_SERVED: bad, rd.VIEW_ROUTES: _OK}))
        assert "\x1b" not in md and "x\\x1b[2J.yaml" in md
        monkeypatch.setattr(cd.rd, "compare", lambda *a, **k: rd.RenderDiff(AT, bad, _OK))
        monkeypatch.setattr(sys, "argv", ["config_diff.py", "--old-dir", str(tmp_path),
                                          "--new-dir", str(tmp_path), "--at", AT])
        with pytest.raises(SystemExit) as exc:
            cd.main()
        assert exc.value.code == 2
        err = capsys.readouterr().err
        assert "\x1b" not in err and "cannot compare configs" in err


# ── 4. JSON：一筆變更一個項目，帶它影響的租戶 ─────────────────────────


class TestJsonReport:

    def test_one_entry_per_distinct_change_with_its_tenants(self):
        """同一個平台變更影響多個租戶時是一筆，不是每個租戶一筆（#1516 拍板）。"""
        same = {"key": "container_memory", "old": 85, "new": 999, "change": "looser"}
        other = {"key": "container_memory", "old": 90, "new": 999, "change": "looser"}
        routes = [rd.RouteChange(t, (f'tenant="{t}"',), "changed", {"group_wait": ("30s", "99s")})
                  for t in ("es-prod", "db-a")]
        recv = [rd.ReceiverChange(f"tenant-{t}", (t,), "changed", {"x.url": (rd.MASKED, rd.MASKED)})
                for t in ("db-a", "es-prod")]
        doc = cd.report_json(_report(metric={"db-a": [same], "es-prod": [same], "db-b": [other]},
                                     routes=routes, receivers=recv), 1)
        assert doc["threshold_changes"] == [dict(same, tenants=["db-a", "es-prod"]),
                                            dict(other, tenants=["db-b"])]
        assert doc["route_changes"] == [{
            "change": "changed", "fields": {"group_wait": {"old": "30s", "new": "99s"}},
            "tenants": ["db-a", "es-prod"], "paths": [['tenant="es-prod"'], ['tenant="db-a"']]}]
        assert doc["receiver_changes"] == [{
            "change": "changed", "fields": {"x.url": {"old": rd.MASKED, "new": rd.MASKED}},
            "receivers": ["tenant-db-a", "tenant-es-prod"], "tenants": ["db-a", "es-prod"]}]

    def test_top_level_carries_schema_at_views_and_exit_code(self):
        views = {rd.VIEW_SERVED: _OK, rd.VIEW_ROUTES: rd.ViewResult("not_compared", "base side: r")}
        doc = cd.report_json(_report(views=views), 1)
        assert doc["schema"] == "da-tools.config-diff/v1"
        assert (doc["at"], doc["exit_code"]) == (AT, 1)
        assert doc["views"] == {"served": {"status": "computed", "reason": ""},
                                "routes": {"status": "not_compared", "reason": "base side: r"}}


# ── 5. CLI：結束碼、渲染失敗、ADR 驗收 ──────────────────────────────


class TestCLI:

    def test_flags(self):
        args = cd.build_parser().parse_args(["--old-dir", "/a", "--new-dir", "/b", "--json-output"])
        assert (args.old_dir, args.new_dir, args.json_output, args.at) == ("/a", "/b", True, None)
        with pytest.raises(SystemExit):
            cd.build_parser().parse_args([])

    @pytest.mark.parametrize("at", ["2026-10-10", "2026-10-10 00:00:00Z", "2026-10-10T00:00:00", "now",
                                    "2026-13-45T25:00:00Z"])
    def test_at_that_is_not_rfc3339_is_a_caller_error(self, tmp_path, at):
        _write_tenant(tmp_path, {"mysql_connections": "80"})
        p = _run_cli(tmp_path, tmp_path, at=at)
        assert p.returncode == 2 and "--at" in p.stderr, (p.returncode, p.stderr)
        # Refused as an argument, before anything renders: no report at all.
        assert p.stdout == "" and "is not RFC3339" in p.stderr, (p.stdout, p.stderr)

    def test_at_is_printed_and_defaults_to_now(self, tmp_path):
        _write_tenant(tmp_path, {"mysql_connections": "80"})
        assert _json(_run_cli(tmp_path, tmp_path, "--format", "json"))["at"] == AT
        p = _run_cli(tmp_path, tmp_path, "--format", "json", at=None)
        assert p.returncode == 0, p.stderr
        assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", _json(p)["at"])
        assert cd._RFC3339.match(cd.now_at())

    def test_at_is_one_instant_for_both_sides(self, tmp_path):
        """--at 是兩側共用的時間點：同一份設定在靜音到期前後讀出不同的值，但兩側讀同一刻，
        所以都沒有差異；對照組是真的改了到期時間。"""
        silence = {"mysql_connections": "80",
                   "_silent_mode": {"target": "all", "expires": "2026-10-10T12:00:00Z"}}
        _write_tenant(tmp_path / "a", silence)
        for at in ("2026-10-10T00:00:00Z", "2026-10-11T00:00:00Z"):
            assert _run_cli(tmp_path / "a", tmp_path / "a", at=at).returncode == 0, at
        _write_tenant(tmp_path / "b", {**silence, "_silent_mode": {"target": "all",
                                                                   "expires": "2026-10-10T13:00:00Z"}})
        assert _run_cli(tmp_path / "a", tmp_path / "b").returncode == 1

    def test_exit_0_no_changes(self, tmp_path):
        _write_tenant(tmp_path, {"mysql_connections": "80"})
        p = _run_cli(tmp_path, tmp_path)
        assert p.returncode == 0, p.stderr
        assert p.stdout.rstrip().endswith("No changes detected.")

    def test_exit_1_changes_detected(self, tmp_path):
        # The base must be declared for the value to be served: without a
        # `_defaults.yaml` both sides read "(not served)" (#3022).
        for side in ("old", "new"):
            (tmp_path / side).mkdir()
            (tmp_path / side / "_defaults.yaml").write_text(
                "defaults:\n  mysql_connections: 80\n", encoding="utf-8", newline="\n")
        _write_tenant(tmp_path / "old", {"mysql_connections": "80"})
        _write_tenant(tmp_path / "new", {"mysql_connections": "50"})
        p = _run_cli(tmp_path / "old", tmp_path / "new")
        assert p.returncode == 1, p.stderr
        assert "| mysql_connections | 80 | 50 | tighter |" in p.stdout

    def test_a_change_to_a_key_served_on_neither_side_says_so(self, tmp_path):
        """No `_defaults.yaml`: /metrics serves the key on neither side, and
        the table says so on both values (#3022)."""
        _write_tenant(tmp_path / "old", {"mysql_connections": "80"})
        _write_tenant(tmp_path / "new", {"mysql_connections": "50"})
        p = _run_cli(tmp_path / "old", tmp_path / "new")
        assert p.returncode == 1, p.stderr
        assert ("| mysql_connections | 80 (not served) | 50 (not served) | tighter |"
                in p.stdout), p.stdout

    def test_old_dir_missing_is_a_first_import(self, tmp_path):
        """base 側沒有 conf.d（第一次匯入）：所有租戶算新增，rc 1，不是呼叫錯誤（ADR-037 決定 4）。"""
        _write_tenant(tmp_path / "new", {"mysql_connections": "80"})
        p = _run_cli(tmp_path / "nope", tmp_path / "new", "--format", "json")
        assert p.returncode == 1, p.stderr
        assert _json(p)["tenants_added"] == ["db-a"]

    @pytest.mark.parametrize("side", ["old", "new"])
    def test_a_file_given_as_a_directory_is_a_caller_error(self, tmp_path, side):
        _write_tenant(tmp_path / "d", {"mysql_connections": "80"})
        f = tmp_path / "file.yaml"
        f.write_text("a: 1\n", encoding="utf-8")
        dirs = {"old": tmp_path / "d", "new": tmp_path / "d", side: f}
        p = _run_cli(dirs["old"], dirs["new"])
        assert p.returncode == 2 and f"--{side}-dir is not a directory" in p.stderr, p.stderr

    def test_new_dir_missing_is_not_computed(self, tmp_path):
        _write_tenant(tmp_path / "old", {"mysql_connections": "80"})
        p = _run_cli(tmp_path / "old", tmp_path / "nope")
        assert p.returncode == 2
        assert "not computed" in p.stdout and "has no config file" in p.stderr

    @pytest.mark.parametrize("label, write", [
        ("malformed_yaml", lambda f: f.write_text("tenants:\n  db-a: [unclosed\n   x: :\n", encoding="utf-8")),
        ("non_utf8", lambda f: f.write_bytes(b"tenants:\n  db-a:\n    note: caf\xe9\n")),
        ("scalar", lambda f: f.write_text("just a string\n", encoding="utf-8")),
        ("tenants_list", lambda f: f.write_text("tenants:\n  - db-a\n", encoding="utf-8")),
        ("directory", lambda f: f.mkdir()),
    ])
    def test_pr_side_file_that_does_not_read_is_rc_2_and_named(self, tmp_path, label, write):
        """#1405：PR 側的檔讀不出租戶，不得讀成「閾值被移除」（rc 1）：rc 2，並具名列出那個檔。"""
        for side in ("old", "new"):
            _write_tenant(tmp_path / side, {"mysql_connections": "80"}, name="db-b")
        _write_tenant(tmp_path / "old", {"mysql_connections": "80"})
        new = tmp_path / "new"
        write(new / "db-a.yaml")
        p = _run_cli(tmp_path / "old", new)
        assert p.returncode == 2, (label, p.returncode, p.stdout[-300:], p.stderr[-300:])
        assert "db-a.yaml" in p.stderr, (label, p.stderr)
        assert "not computed" in p.stdout, (label, p.stdout)
        # The exporter skips a directory without a word, so its view of the
        # tree computes (db-a is gone from it); every other shape stops both
        # renderers, and nothing may then read as "removed" (ADR-037 decision 4:
        # the view that fails says so, the others compare as usual).
        if label != "directory":
            assert "removed" not in p.stdout, (label, p.stdout)

    @pytest.mark.parametrize("text", ["", "# nothing here\n"], ids=["empty", "comment-only"])
    def test_an_empty_tenant_file_is_legal_and_reads_as_removed(self, tmp_path, text):
        """#1405 的對照組：空檔與只有註解的檔是合法的，租戶的閾值確實沒了。"""
        _write_tenant(tmp_path / "old", {"mysql_connections": "80"})
        (tmp_path / "new").mkdir()
        (tmp_path / "new" / "db-a.yaml").write_text(text, encoding="utf-8")
        p = _run_cli(tmp_path / "old", tmp_path / "new", "--format", "json")
        assert p.returncode == 1, p.stderr
        assert _json(p)["tenants_removed"] == ["db-a"]

    @pytest.mark.parametrize("side", ["old", "new"])
    def test_threshold_table_cell_cannot_break_out(self, tmp_path, side):
        """表格欄位的值來自 PR 的檔案：`|` 會結束欄位、換行會結束表格，後面的字就成了
        PR 留言裡自己的 Markdown（標題、連結）。exporter 不送出、照原文列出的值也一樣。"""
        evil = "70\n\n## Approved | [x](https://e.example)\n"
        _write_tenant(tmp_path / "old", {"mysql_connections": evil if side == "old" else "70"})
        _write_tenant(tmp_path / "new", {"mysql_connections": evil if side == "new" else "70"})
        p = _run_cli(tmp_path / "old", tmp_path / "new")
        assert p.returncode == 1, p.stderr
        assert not any(ln.startswith("## Approved") for ln in p.stdout.splitlines()), p.stdout
        row = next(ln for ln in p.stdout.splitlines() if ln.startswith("| mysql_connections |"))
        # 6 欄（含 Tenants），5 個欄位分隔
        assert "\\|" in row and row.count(" | ") == 5, row

    def test_da_guards_own_reason_reaches_the_report(self, tmp_path):
        """da-guard 回 2（同一租戶宣告兩次）時，報告要帶它自己說的原因，不是只有「exited 2」。"""
        _write_tenant(tmp_path / "old", {"mysql_connections": "80"})
        _write_tenant(tmp_path / "new", {"mysql_connections": "80"})
        (tmp_path / "new" / "db-a-copy.yaml").write_text(
            "tenants:\n  db-a:\n    mysql_connections: '90'\n", encoding="utf-8")
        p = _run_cli(tmp_path / "old", tmp_path / "new")
        assert p.returncode == 2, p.stderr
        line = next(ln for ln in p.stderr.splitlines() if "Exporter values" in ln)
        assert "duplicate" in line and "db-a" in line, line

    def test_base_side_that_does_not_read_is_not_compared_and_does_not_block(self, tmp_path):
        """main 上已經壞了、這個 PR 修好它：rc 1，報告寫出原因，不擋 PR。"""
        old, new = tmp_path / "old", tmp_path / "new"
        _write_tenant(old, {"mysql_connections": "80"})
        (old / "zz.yaml").write_text("tenants:\n  - x\n", encoding="utf-8")
        _write_tenant(new, {"mysql_connections": "80"})
        p = _run_cli(old, new)
        assert p.returncode == 1, (p.stdout, p.stderr)
        assert "not compared" in p.stdout and "zz.yaml" in p.stderr
        for text in (p.stdout, p.stderr):
            assert text.count("does not render") <= 2 and "does not render: base side does not render" \
                not in text and "base side does not render: `base side" not in text, text

    def test_an_unexpected_crash_is_rc_2_not_1(self, tmp_path, monkeypatch, capsys):
        """未預期的例外 exit 1 會被讀成「有變更」：一律 rc 2，並寫出兩側目錄與例外型別。"""
        def boom(*a, **k):
            raise RuntimeError("boom")
        monkeypatch.setattr(cd.rd, "compare", boom)
        monkeypatch.setattr(sys, "argv", ["config_diff.py", "--old-dir", str(tmp_path / "o"),
                                          "--new-dir", str(tmp_path / "n")])
        with pytest.raises(SystemExit) as exc:
            cd.main()
        assert exc.value.code == cd.EXIT_CALLER_ERROR
        err = capsys.readouterr().err
        assert "RuntimeError: boom" in err and str(tmp_path / "o") in err and str(tmp_path / "n") in err


class TestAcceptance:
    """ADR-037 與 #1516 P2c 的驗收，在出貨的範例 conf.d 上跑。"""

    def test_comment_only_change_is_rc_0(self, tmp_path):
        old, new = _sample_pair(tmp_path)
        _edit(new, '    mysql_connections: "70"', '    mysql_connections: "70"  # reviewed', rel="db-a.yaml")
        p = _run_cli(old, new)
        assert p.returncode == 0, (p.stdout, p.stderr)
        # 對照：同一個檔真的改值時要響
        _edit(new, '"70"  # reviewed', '"71"', rel="db-a.yaml")
        assert _run_cli(old, new).returncode == 1

    def test_adr_example_group_wait(self, tmp_path):
        old, new = _sample_pair(tmp_path)
        _edit(new, 'group_wait: "30s"', 'group_wait: "99s"')
        doc = _json(_run_cli(old, new, "--format", "json"))
        assert doc["exit_code"] == 1
        assert doc["route_changes"] == [{
            "change": "changed", "fields": {"group_wait": {"old": "30s", "new": "99s"}},
            "tenants": ["db-a", "es-prod"], "paths": [['tenant="db-a"'], ['tenant="es-prod"']]}]
        assert doc["threshold_changes"] == [] and doc["uncovered_files"] == []
        assert [(r["file"], r["key"], r["status"]) for r in doc["platform_settings"]] == [
            ("_defaults.yaml", "_routing_defaults", cd.SETTING_RENDERED)]

    def test_adr_example_container_memory(self, tmp_path):
        old, new = _sample_pair(tmp_path)
        _edit(new, "container_memory: 85", "container_memory: 999")
        doc = _json(_run_cli(old, new, "--format", "json"))
        assert doc["threshold_changes"] == [{
            "key": "container_memory", "old": 85, "new": 999, "change": "looser",
            "tenants": ["db-a", "es-prod", "mongo-prod", "redis-prod"]}]
        assert doc["route_changes"] == []

    def test_platform_receiver_change_reaches_the_inheriting_tenants(self, tmp_path):
        """#1516 的原始症狀：平台預設的 receiver 改了，沒有自己 `_routing` 的租戶要出現在報告裡。"""
        old, new = _sample_pair(tmp_path)
        _edit(new, 'to: ["dba-oncall@example.com"]', 'to: ["noc@example.com"]')
        doc = _json(_run_cli(old, new, "--format", "json"))
        assert doc["exit_code"] == 1
        tenants = {t for c in doc["receiver_changes"] for t in c["tenants"]}
        assert tenants == {"db-a", "es-prod"}, doc["receiver_changes"]

    def test_platform_file_profile_binding_is_reported_for_its_tenant(self, tmp_path):
        """#2120：根目錄 `_platform.yaml` 裡改了租戶的 `_profile`，生效門檻變了的租戶要報出來。"""
        profiles = "profiles:\n  std:\n    mysql_connections: 70\n  strict:\n    mysql_connections: 40\n"
        tenants = {"tx.yaml": "tenants:\n  tx:\n    container_cpu: '80'\n",
                   "ty.yaml": "tenants:\n  ty:\n    container_cpu: '80'\n",
                   "_defaults.yaml": "defaults:\n  mysql_connections: 80\n  container_cpu: 80\n"}
        old, new = _pair(tmp_path,
                         {**tenants, "_profiles.yaml": profiles,
                          "_platform.yaml": "tenants:\n  tx:\n    _profile: std\n"},
                         {**tenants, "_profiles.yaml": profiles,
                          "_platform.yaml": "tenants:\n  tx:\n    _profile: strict\n"})
        p = _run_cli(old, new, "--format", "json")
        assert p.returncode == 1, p.stderr
        doc = _json(p)
        assert doc["threshold_changes"] == [{"key": "mysql_connections", "old": 70, "new": 40,
                                             "change": "tighter", "tenants": ["tx"]}]
        assert doc["uncovered_files"] == []
        assert [(r["file"], r["key"], r["status"]) for r in doc["platform_settings"]] == [
            ("_platform.yaml", "tenants", cd.SETTING_RENDERED)]


# ── 6. 設定與憑證（經 CLI） ──────────────────────────────────────────


class TestTenantSettingChanges:
    """`_` 開頭的租戶設定改了，要報出來、rc=1（以 exporter 讀到的值比）。"""

    BASE = {
        "mysql_connections": "80",
        "_silent_mode": "warning",
        "_state_container_crashloop": "disable",
        "_severity_dedup": "disable",
        "_metadata": {"db_type": "mariadb", "owner": "team-a"},
        "_namespaces": ["ns1"],
        # A route of its own: with `_severity_dedup: disable` and no route the
        # generator renders nothing and refuses the tree (exit 1).
        "_routing": {"receiver": {"type": "webhook", "url": "https://hooks.example/a"}},
    }

    @pytest.mark.parametrize("key", [k for k in BASE if k.startswith("_") and k != "_routing"])
    def test_removing_a_setting_is_reported(self, tmp_path, key):
        _write_tenant(tmp_path / "old", self.BASE)
        _write_tenant(tmp_path / "new", {k: v for k, v in self.BASE.items() if k != key})
        p = _run_cli(tmp_path / "old", tmp_path / "new")
        assert p.returncode == 1, p.stdout + p.stderr
        assert "## Tenant Setting Changes" in p.stdout
        assert re.search(rf"^- `{re.escape(key)}[.`]", p.stdout, re.M), p.stdout

    def test_unchanged_settings_are_not_reported(self, tmp_path):
        _write_tenant(tmp_path, self.BASE)
        p = _run_cli(tmp_path, tmp_path)
        assert p.returncode == 0
        assert "Tenant Setting Changes" not in p.stdout

    def test_json_carries_setting_changes(self, tmp_path):
        _write_tenant(tmp_path / "old", self.BASE)
        _write_tenant(tmp_path / "new", {**self.BASE, "_metadata": {"db_type": "mariadb", "owner": "team-b"}})
        doc = _json(_run_cli(tmp_path / "old", tmp_path / "new", "--format", "json"))
        assert doc["setting_changes"] == [{"key": "_metadata.owner", "old": "team-a", "new": "team-b",
                                           "change": "modified", "tenants": ["db-a"]}]

    def test_a_backtick_in_a_value_cannot_break_the_code_span(self, tmp_path):
        """值來自 PR 的檔案，屬不可信輸入（F5）。"""
        _write_tenant(tmp_path / "old", self.BASE)
        _write_tenant(tmp_path / "new", {**self.BASE, "_metadata": {"db_type": "mariadb",
                                                                    "owner": "x` **bold** `y"}})
        p = _run_cli(tmp_path / "old", tmp_path / "new")
        line = next(ln for ln in p.stdout.splitlines() if ln.startswith("- `_metadata.owner`"))
        # 鍵、前、後各一對，加上租戶名一對
        assert line.count("`") == 8 and line.endswith(" — tenant `db-a`"), line


# A Slack incoming-webhook URL is the credential itself.
_SECRET_OLD = "https://hooks.slack.com/services/T0/B0/oldSECRETvalue"
_SECRET_NEW = "https://hooks.slack.com/services/T0/B0/newSECRETvalue"


class TestCredentialsAreNotPrinted:
    """receiver 的憑證欄位改了要報「有變」，但新舊值都不印——報告會貼成 PR comment。"""

    BASE = {"mysql_connections": "80",
            "_routing": {"receiver": {"type": "slack", "api_url": _SECRET_OLD}, "group_wait": "30s"}}

    def _run(self, tmp_path, old_cfg, new_cfg, *extra):
        _write_tenant(tmp_path / "old", old_cfg)
        _write_tenant(tmp_path / "new", new_cfg)
        return _run_cli(tmp_path / "old", tmp_path / "new", *extra)

    def _rotated(self):
        return {**self.BASE, "_routing": {"receiver": {"type": "slack", "api_url": _SECRET_NEW},
                                          "group_wait": "30s"}}

    def test_a_rotated_credential_is_reported_without_its_values(self, tmp_path):
        p = self._run(tmp_path, self.BASE, self._rotated())
        assert p.returncode == 1, p.stdout + p.stderr
        assert "- `_routing.receiver.api_url`: `<redacted>` → `<redacted>`" in p.stdout
        assert "### Receivers" in p.stdout
        assert "SECRET" not in p.stdout + p.stderr

    def test_json_redacts_too(self, tmp_path):
        p = self._run(tmp_path, self.BASE, self._rotated(), "--format", "json")
        assert p.returncode == 1
        assert "SECRET" not in p.stdout
        doc = _json(p)
        assert {"key": "_routing.receiver.api_url", "old": "<redacted>", "new": "<redacted>",
                "change": "modified", "tenants": ["db-a"]} in doc["setting_changes"]
        assert doc["receiver_changes"], doc

    @pytest.mark.parametrize("direction", ["added", "removed"])
    def test_a_whole_routing_block_is_redacted_inside(self, tmp_path, direction):
        without = {"mysql_connections": "80"}
        old, new = (without, self.BASE) if direction == "added" else (self.BASE, without)
        p = self._run(tmp_path, old, new)
        assert p.returncode == 1
        assert "SECRET" not in p.stdout
        assert "<redacted>" in p.stdout

    def test_a_non_credential_change_still_prints_values(self, tmp_path):
        changed = {**self.BASE, "_routing": {"receiver": {"type": "slack", "api_url": _SECRET_OLD},
                                             "group_wait": "1m"}}
        p = self._run(tmp_path, self.BASE, changed)
        assert "- `_routing.group_wait`: `30s` → `1m`" in p.stdout
        assert "  - `group_wait`: `30s` → `1m`" in p.stdout   # the route node too
        assert "SECRET" not in p.stdout

    def test_credentials_inside_a_list_are_redacted(self):
        diff = cd.compute_setting_diff(
            {"t": {"_routing": {"overrides": []}}},
            {"t": {"_routing": {"overrides": [
                {"receiver": {"type": "pagerduty", "routing_key": "pdSECRET"}}]}}})
        assert "SECRET" not in json.dumps(diff)
        assert diff["t"][0]["new"] == [
            {"receiver": {"type": "pagerduty", "routing_key": "<redacted>"}}]

    def test_the_credential_list_covers_the_schema(self):
        """schema 裡長得像憑證的欄位，每一個都得歸類：要嘛遮、要嘛明列不是。"""
        schema = json.loads((REPO_ROOT / "docs" / "schemas"
                             / "tenant-config.schema.json").read_text(encoding="utf-8"))
        # http_config 整個值當憑證遮（容器整個遮，見 _lib_redact），所以只有
        # http_config 會用到的定義（httpConfig 與它引用的 httpOAuth2、
        # httpTLSConfig…，#2984）裡的欄位已經歸類，不再逐一要求。
        assert "http_config" in cd._CREDENTIAL_KEYS
        defs = schema["definitions"]

        prefix = "#/definitions/"

        def ref_sites(node, prop=None):
            """(definition, enclosing property name or None) for every `$ref`
            in node, read from the JSON structure: a `$ref` nested in
            anyOf / oneOf / allOf / items / not … belongs to the property
            whose value contains it."""
            if isinstance(node, dict):
                ref = node.get("$ref")
                if isinstance(ref, str) and ref.startswith(prefix):
                    yield ref[len(prefix):], prop
                for k, v in node.items():
                    if k in ("properties", "patternProperties", "definitions") and isinstance(v, dict):
                        for name, sub in v.items():
                            yield from ref_sites(sub, None if k == "definitions" else name)
                    else:
                        yield from ref_sites(v, prop)
            elif isinstance(node, list):
                for x in node:
                    yield from ref_sites(x, prop)

        def closure(start):
            seen, stack = set(), list(start)
            while stack:
                name = stack.pop()
                if name not in seen:
                    seen.add(name)
                    stack.extend(d for d, _ in ref_sites(defs[name]))
            return seen

        roots = {"httpConfigOrNull", "httpConfig"}
        inside = closure(roots)
        outside = {k: v for k, v in schema.items() if k != "definitions"}
        outside["definitions"] = {k: v for k, v in defs.items() if k not in inside}
        sites = list(ref_sites(outside))
        # The roots are entered only as the value of an `http_config` key —
        # however that value wraps the $ref (anyOf, oneOf, allOf …).
        entries = [(d, p) for d, p in sites if d in roots]
        assert entries and {p for _, p in entries} == {"http_config"}, entries
        # A definition under http_config that is also used elsewhere is walked,
        # and so is everything it references.
        inside -= closure({d for d, _ in sites if d in inside - roots})
        schema = {k: v for k, v in schema.items() if k != "definitions"}
        schema["definitions"] = {k: v for k, v in defs.items() if k not in inside}
        # secretRef（#1560）的子欄位 `secret` / `key` 是 Secret 的座標（名稱與
        # data key），不是憑證值本身：持有 secretRef 的欄位名本來就在遮罩名單、
        # 整包遮。把 `secret` / `key` 加進名單會讓 credmask 去遮任何叫 key 的
        # 欄位，所以這裡以定義名明列略過。先斷言它還在：改名後豁免不會空轉。
        not_credential_defs = {"secretRef"}
        assert not_credential_defs <= set(schema["definitions"]), \
            not_credential_defs - set(schema["definitions"])
        for name in not_credential_defs:
            del schema["definitions"][name]
        names = set()

        def walk(node):
            if isinstance(node, dict):
                for k, v in node.items():
                    if k == "properties" and isinstance(v, dict):
                        names.update(v)
                    walk(v)
            elif isinstance(node, list):
                for x in node:
                    walk(x)
        walk(schema)
        looks_secret = {n for n in names if re.search(r"pass|key|token|secret|url", n, re.I)}
        not_credentials = {"runbook_url", "icon_url", "client_url"}
        # 名單與 tenant-api 的 credmask 同一份，兩者一致由 tests/ops/test_lib_redact.py 守
        assert looks_secret - not_credentials <= set(cd._CREDENTIAL_KEYS), \
            looks_secret - not_credentials - set(cd._CREDENTIAL_KEYS)


# ── 7. 平台設定：`_` 檔的頂層鍵（ADR-037 決定 3，第一階段） ─────────────


def _settings(doc):
    return [(r["file"], r["key"], r["status"]) for r in doc["platform_settings"]]


class TestPlatformSettings:
    """`_` 開頭檔的每個頂層鍵（載體）以 parse-tree 讀出的值比較（#1516 P3b）。

    第一階段（owner 2026-10-13 拍板）：值變了而涵蓋它的渲染沒有差異，一律 rc 1——
    鍵寫在讀取端不讀的位置與真的沒效果分不開，要等 #2962。註解、排版、同值換引號
    不是變更（取代逐位元組比對，#1420）。"""

    TENANT = "tenants:\n  db-a:\n    mysql_connections: '70'\n"
    DEFAULTS = ("defaults:\n  cpu: 80\n  mem: 90\n"
                "state_filters:\n  crash:\n    reasons: []\n    severity: \"critical\"\n")

    @pytest.mark.parametrize("edited", [
        DEFAULTS + "# note\n",                                                      # 註解
        "defaults: {cpu: 80, mem: 90}\n"
        "state_filters: {crash: {reasons: [], severity: 'critical'}}\n",            # 排版＋換引號
        "state_filters:\n    crash:\n        severity: critical\n        reasons: []\n"
        "defaults:\n    mem: 90\n    cpu: 0x50\n",                                 # 縮排、順序、同值的另一種寫法
    ], ids=["comment", "flow-and-quotes", "reorder-and-hex"])
    def test_same_values_written_differently_are_no_change(self, tmp_path, edited):
        old, new = _pair(tmp_path, {"db-a.yaml": self.TENANT, "_defaults.yaml": self.DEFAULTS},
                         {"db-a.yaml": self.TENANT, "_defaults.yaml": edited})
        p = _run_cli(old, new, "--format", "json")
        assert p.returncode == 0, p.stdout + p.stderr
        assert _json(p)["platform_settings"] == []

    def test_a_value_change_is_listed(self, tmp_path):
        """對照組：同一個檔真的改值要列出來。"""
        old, new = _pair(tmp_path, {"db-a.yaml": self.TENANT, "_defaults.yaml": self.DEFAULTS},
                         {"db-a.yaml": self.TENANT,
                          "_defaults.yaml": self.DEFAULTS.replace("mem: 90", "mem: 91")})
        doc = _json(_run_cli(old, new, "--format", "json"))
        assert [(f, k) for f, k, _ in _settings(doc)] == [("_defaults.yaml", "defaults")]

    def test_defaults_outside_a_carrier_is_not_read_here(self, tmp_path):
        """`defaults` 寫在非預設載體的 `_platform.yaml`：exporter 不讀，渲染沒有差異。
        #2962：parse-tree 的 rendered_by 說這裡沒人讀 ⇒「這裡不會被讀」、rc 1，
        不會被當成「完整涵蓋、沒效果」放過。"""
        old, new = _pair(tmp_path, {"db-a.yaml": self.TENANT, "_defaults.yaml": self.DEFAULTS},
                         {"db-a.yaml": self.TENANT, "_defaults.yaml": self.DEFAULTS,
                          "_platform.yaml": "defaults:\n  cpu: 1\n"})
        p = _run_cli(old, new)
        assert p.returncode == 1, p.stdout + p.stderr
        assert "**:warning: Not read where it is written** (1)" in p.stdout
        assert "- `_platform.yaml` `defaults` (added)" in p.stdout

    @pytest.mark.parametrize("rel,text", [
        ("_platform.yaml", "tenants: {}\n"),
        ("_profiles.yaml", "profiles:\n  unused:\n    cpu: 1\n"),
    ], ids=["empty-tenants", "unbound-profile"])
    def test_full_coverage_without_a_difference_is_a_note(self, tmp_path, rel, text):
        """#2962 (ADR-037 決定 3 第二步)：完整涵蓋、讀取端就在這裡讀、渲染沒有差異
        ⇒「沒有效果」，只附註、不影響結束碼。"""
        base = {"db-a.yaml": self.TENANT, "_defaults.yaml": self.DEFAULTS}
        old, new = _pair(tmp_path, base, {**base, rel: text})
        p = _run_cli(old, new, "--format", "json")
        assert p.returncode == 0, p.stdout + p.stderr
        doc = _json(p)
        assert [s[2] for s in _settings(doc)] == [cd.SETTING_NO_EFFECT]
        assert doc["uncovered_files"] == []
        md = _run_cli(old, new)
        assert "**Changed, no effect on the renders that read it** (1)" in md.stdout

    def test_enforced_match_written_as_a_mapping_shows_in_the_render(self, tmp_path):
        """#2989 (was #2962 review): `_routing_enforced.match` is read only as a list. A
        mapping used to be dropped without a word, which is why the carrier was partial;
        now the block is refused and renders no NOC route, so rewriting the list as the
        `routes[i].match` mapping is a rendered change (the route goes away), and the
        carrier is fully covered."""
        assert cd.RENDERED_CARRIERS["_routing_enforced"][1] == "full"
        enf = ("_routing_enforced:\n  enabled: true\n  match: {match}\n  receiver:\n"
               "    type: webhook\n    url: https://noc.example.com/h\n")
        old, new = _pair(tmp_path, {"db-a.yaml": self.TENANT, "_defaults.yaml": self.DEFAULTS,
                                    "_noc.yaml": enf.format(match="['severity=\"critical\"']")},
                         {"db-a.yaml": self.TENANT, "_defaults.yaml": self.DEFAULTS,
                          "_noc.yaml": enf.format(match="{severity: critical}")})
        p = _run_cli(old, new, "--format", "json")
        assert p.returncode == 1, p.stdout + p.stderr
        assert [s[2] for s in _settings(_json(p))] == [cd.SETTING_RENDERED]

    def test_no_effect_only_says_so_in_the_last_line(self, tmp_path):
        """#2962 review: not "No changes detected." under a list of changed settings."""
        base = {"db-a.yaml": self.TENANT, "_defaults.yaml": self.DEFAULTS}
        old, new = _pair(tmp_path, base, {**base, "_platform.yaml": "tenants: {}\n"})
        p = _run_cli(old, new)
        assert p.returncode == 0, p.stdout + p.stderr
        last = p.stdout.rstrip().splitlines()[-1]
        assert last.startswith("No change in what the two renders show; 1 platform setting(s) "
                               "changed with no effect"), last

    def test_partial_coverage_without_a_difference_still_exits_1(self, tmp_path):
        """`state_filters` 只部分涵蓋：`reasons` 沒有渲染讀，改它渲染沒有差異，仍是 rc 1。"""
        old, new = _pair(tmp_path, {"db-a.yaml": self.TENANT, "_defaults.yaml": self.DEFAULTS},
                         {"db-a.yaml": self.TENANT,
                          "_defaults.yaml": self.DEFAULTS.replace("reasons: []", "reasons: [OOMKilled]")})
        p = _run_cli(old, new, "--format", "json")
        assert p.returncode == 1, p.stdout + p.stderr
        assert [s[2] for s in _settings(_json(p))] == [cd.SETTING_NO_RENDER_CHANGE]

    def test_a_da_guard_without_rendered_by_is_refused(self, tmp_path, monkeypatch):
        """#2962：較舊的 da-guard 沒有 rendered_by ⇒ 不退回檔案層判斷，parse-tree 失敗、rc 2。"""
        import _lib_parse_tree
        real = _lib_parse_tree.run
        def older(*a, **k):
            doc = real(*a, **k)
            doc.pop("rendered_by")
            return doc
        monkeypatch.setattr(_lib_parse_tree, "run", older)
        base = {"db-a.yaml": self.TENANT, "_defaults.yaml": self.DEFAULTS}
        old, new = _pair(tmp_path, base, base)
        with pytest.raises(_lib_parse_tree.ParseTreeError, match="older than this tool"):
            cd._read_tree(new, 60)

    @pytest.mark.parametrize("rel,text,status", [
        ("_defaults.yaml", "defaults:\n  cpu: 80\n_custom_alerts: []\n", cd.SETTING_NOT_COMPARED),
        ("_defaults.yaml", "defaults:\n  cpu: 80\nmax_metrics_per_tenant: 5\n", cd.SETTING_NOT_COMPARED),
        ("_defaults.yaml", "defaults:\n  cpu: 80\nnobody_reads_this: 1\n", cd.SETTING_NOT_COMPARED),
        ("_tenant_orgs.yaml", "tenant_orgs:\n  db-a: 1\n", cd.SETTING_NOT_COMPARED),
        ("_rbac.yaml", "groups: {}\n", cd.SETTING_NOT_COMPARED),
        ("team/_defaults.yaml", "_policies: []\n", cd.SETTING_NOT_READ_HERE),
        ("_platform.yaml", "domain_policies: {}\n", cd.SETTING_NOT_READ_HERE),
    ], ids=["deferred-recipe", "deferred-cap", "unknown-key", "tenant-orgs", "rbac",
            "policies-nested", "domain-policies-misplaced"])
    def test_settings_no_render_reads_exit_1(self, tmp_path, rel, text, status):
        base = {"db-a.yaml": self.TENANT, "_defaults.yaml": "defaults:\n  cpu: 80\n"}
        old, new = _pair(tmp_path, base, {**base, rel: text})
        p = _run_cli(old, new, "--format", "json")
        assert p.returncode == 1, p.stdout + p.stderr
        doc = _json(p)
        rows = [r for r in doc["platform_settings"] if r["file"] == rel]
        assert [r["status"] for r in rows] == [status], doc["platform_settings"]
        assert rel in doc["uncovered_files"]

    def test_other_service_note_says_only_what_config_diff_knows(self, tmp_path):
        base = {"db-a.yaml": self.TENANT, "_defaults.yaml": self.DEFAULTS}
        old, new = _pair(tmp_path, base, {**base, "_rbac.yaml": "groups: {}\n"})
        (row,) = _json(_run_cli(old, new, "--format", "json"))["platform_settings"]
        assert row["note"].startswith("neither the exporter nor the route generator reads this key")
        assert "--rbac" in row["note"]

    def test_a_key_a_render_reads_in_a_tenant_api_file_is_rendered(self, tmp_path):
        """根目錄的 `_rbac.yaml` exporter 也會讀（它讀每個根目錄 `_` 檔的 `tenants:`）：
        不得說「渲染程式都不讀這個檔」。"""
        base = {"db-a.yaml": self.TENANT, "_defaults.yaml": self.DEFAULTS, "_rbac.yaml": "groups: {}\n"}
        old, new = _pair(tmp_path, base, {**base, "_rbac.yaml": "groups: {}\ntenants:\n"
                                          "  db-a:\n    _silent_mode: \"warning\"\n"})
        p = _run_cli(old, new, "--format", "json")
        assert p.returncode == 1, p.stdout + p.stderr
        assert _settings(_json(p)) == [("_rbac.yaml", "tenants", cd.SETTING_RENDERED)]

    def test_an_unselected_defaults_carrier_is_listed(self, tmp_path):
        """`_defaults.yml` 放在 `_defaults.yaml` 旁邊：parse-tree 列為沒有讀取端讀
        （not_read），內容不在文件裡；仍要列出、rc 1，不能消失。"""
        base = {"db-a.yaml": self.TENANT, "_defaults.yaml": self.DEFAULTS}
        old, new = _pair(tmp_path, base, {**base, "_defaults.yml": "defaults:\n  cpu: 1\n"})
        p = _run_cli(old, new, "--format", "json")
        assert p.returncode == 1, p.stdout + p.stderr
        rows = _json(p)["platform_settings"]
        assert [(r["file"], r["change"], r["status"]) for r in rows] == [
            ("_defaults.yml", "added", cd.SETTING_NOT_READ_HERE)]
        assert "unselected_defaults_carrier" in rows[0]["note"]

    def test_a_key_no_render_reads_is_compared_by_source_text(self, tmp_path):
        """yaml.v3 把 `007` 與 `7` 讀成同一個 int，但 tenant-api 以字串讀 org 清單，是兩個 org。
        渲染讀不到的鍵沒有渲染可以作證，所以非字串純量比原文。"""
        base = {"db-a.yaml": self.TENANT, "_defaults.yaml": self.DEFAULTS}
        old, new = _pair(tmp_path, {**base, "_tenant_orgs.yaml": "tenant_orgs:\n  db-a: [007]\n"},
                         {**base, "_tenant_orgs.yaml": "tenant_orgs:\n  db-a: [7]\n"})
        p = _run_cli(old, new, "--format", "json")
        assert p.returncode == 1, p.stdout + p.stderr
        assert _settings(_json(p)) == [("_tenant_orgs.yaml", "tenant_orgs", cd.SETTING_NOT_COMPARED)]

    def test_a_rendered_key_in_a_file_its_reader_does_not_read_is_not_read_here(self, tmp_path):
        """`defaults` 寫在子目錄的非預設載體 `_` 檔：exporter 不讀那個檔（parse-tree 的
        selected 只有 generator）。同一個 PR 另有真的閾值變更時，也不能被算成 rendered。"""
        base = {"db-a.yaml": self.TENANT, "_defaults.yaml": self.DEFAULTS,
                "team/db-b.yaml": "tenants:\n  db-b:\n    cpu: '1'\n"}
        old, new = _pair(tmp_path, base,
                         {**base, "_defaults.yaml": self.DEFAULTS.replace("cpu: 80", "cpu: 81"),
                          "team/_x.yaml": "defaults:\n  cpu: 5\n"})
        p = _run_cli(old, new, "--format", "json")
        assert p.returncode == 1, p.stdout + p.stderr
        doc = _json(p)
        assert dict(((f, k), st) for f, k, st in _settings(doc)) == {
            ("_defaults.yaml", "defaults"): cd.SETTING_RENDERED,
            ("team/_x.yaml", "defaults"): cd.SETTING_NOT_READ_HERE}
        assert doc["uncovered_files"] == ["team/_x.yaml"]

    @pytest.mark.parametrize("rel,text", [
        ("_defaults.yaml", "defaults:\n  cpu: 80\n_policies: []\n"),
        ("_domain_policy.yaml", "domain_policies: {}\n"),
    ], ids=["policies-root-carrier", "domain-policy-file"])
    def test_checked_settings_where_the_check_reads_them_do_not_raise_the_exit_code(
            self, tmp_path, rel, text):
        base = {"db-a.yaml": self.TENANT, "_defaults.yaml": "defaults:\n  cpu: 80\n"}
        old, new = _pair(tmp_path, base, {**base, rel: text})
        p = _run_cli(old, new, "--format", "json")
        assert p.returncode == 0, p.stdout + p.stderr
        doc = _json(p)
        assert [r["status"] for r in doc["platform_settings"]] == [cd.SETTING_CHECKED]
        assert doc["uncovered_files"] == []

    @pytest.mark.parametrize("rel,status,rc", [
        ("_platform.yaml", cd.SETTING_NO_EFFECT, 0),
        ("team-x/_defaults.yaml", cd.SETTING_NOT_READ_HERE, 1),   # nested `tenants:` is ignored
        ("_profiles.yaml", cd.SETTING_NO_EFFECT, 0),
    ])
    def test_added_platform_file_is_named(self, tmp_path, rel, status, rc):
        old, new = _pair(tmp_path, {"db-a.yaml": self.TENANT},
                         {"db-a.yaml": self.TENANT, rel: "tenants: {}\n"})
        p = _run_cli(old, new, "--format", "json")
        assert p.returncode == rc, p.stdout + p.stderr
        doc = _json(p)
        assert _settings(doc) == [(rel, "tenants", status)]
        assert doc["uncovered_files"] == ([rel] if rc else [])

    def test_tenant_files_are_compared_not_listed(self, tmp_path):
        """租戶檔（子目錄裡的也是）由渲染比較報出，不列在平台設定；`.` 開頭的檔 exporter 不讀。"""
        old, new = _pair(
            tmp_path,
            {"db-a.yaml": self.TENANT, "team/db-b.yaml": "tenants:\n  db-b:\n    cpu: '1'\n",
             ".scratch.yaml": "a: 1\n"},
            {"db-a.yaml": self.TENANT.replace("70", "90"),
             "team/db-b.yaml": "tenants:\n  db-b:\n    cpu: '2'\n", ".scratch.yaml": "a: 2\n"})
        doc = _json(_run_cli(old, new, "--format", "json"))
        assert doc["platform_settings"] == [] and doc["uncovered_files"] == []
        assert {t for c in doc["threshold_changes"] for t in c["tenants"]} == {"db-a", "db-b"}

    def test_rows_are_sorted_and_cover_both_sides(self):
        def doc(files):
            return _v2_doc(files)
        x = {"text": "1", "tag": "!!int", "value": "1"}
        old = doc({"_z.yaml": {"k": x}, "a/_q.yaml": {"k": x}, "_a.yaml": {"k": x}, "b/t.yaml": {"k": x}})
        rows = cd.platform_settings(old, doc({}), set(), {}, "old", "new")
        assert [(r["file"], r["key"], r["change"]) for r in rows] == [
            ("_a.yaml", "k", "removed"), ("_z.yaml", "k", "removed"), ("a/_q.yaml", "k", "removed")]

    def test_an_unreadable_base_file_is_listed_not_compared(self, tmp_path):
        """base 側的 `_` 檔讀不了：列出、rc 1（不擋修好它的 PR，ADR-037 決定 4）。"""
        old, new = _pair(tmp_path,
                         {"db-a.yaml": self.TENANT, "_defaults.yaml": "defaults: [\n"},
                         {"db-a.yaml": self.TENANT, "_defaults.yaml": self.DEFAULTS})
        p = _run_cli(old, new, "--format", "json")
        assert p.returncode == 1, p.stdout + p.stderr
        rows = _json(p)["platform_settings"]
        assert [(r["file"], r["change"], r["status"]) for r in rows] == [
            ("_defaults.yaml", "unreadable", cd.SETTING_NOT_COMPARED)]
        assert "base side" in rows[0]["note"]

    def test_a_parse_tree_failure_is_no_result(self, tmp_path, monkeypatch, capsys):
        """parse-tree 本身失敗是工具失敗：rc 2，不是「沒有變更」。"""
        files = {"db-a.yaml": self.TENANT, "_defaults.yaml": self.DEFAULTS}
        old, new = _pair(tmp_path, files, files)

        def fail(*_a, **_k):
            raise cd._lib_parse_tree.ParseTreeError("da-guard parse-tree exited 3")

        monkeypatch.setattr(cd._lib_parse_tree, "run", fail)
        monkeypatch.setattr(sys, "argv", ["config_diff.py", "--old-dir", str(old),
                                          "--new-dir", str(new), "--at", AT])
        with pytest.raises(SystemExit) as exc:
            cd.main()
        assert exc.value.code == cd.EXIT_CALLER_ERROR
        out, err = capsys.readouterr()
        assert f"{cd.VIEW_TITLES[cd.VIEW_PLATFORM]}: not computed: PR side: da-guard parse-tree exited 3" in err
        assert "No changes detected." not in out

    def test_an_unread_file_with_the_same_bytes_is_not_listed(self, tmp_path):
        """沒有讀取端讀的檔兩側位元組相同：不是變更（對照 test_an_unselected_defaults_carrier_is_listed）。"""
        files = {"db-a.yaml": self.TENANT, "_defaults.yaml": self.DEFAULTS,
                 "_defaults.yml": "defaults:\n  cpu: 1\n"}
        old, new = _pair(tmp_path, files, files)
        p = _run_cli(old, new, "--format", "json")
        assert p.returncode == 0, p.stdout + p.stderr
        assert _json(p)["platform_settings"] == []

    def test_a_rendered_key_whose_view_was_not_computed_is_not_compared(self):
        """渲染讀的鍵，但那個面向沒有結果：不能說成 rendered 或「沒有差異」。"""
        x = {"text": "1", "tag": "!!int", "value": "1"}
        doc = _v2_doc({"_defaults.yaml": {"defaults": x}})
        rows = cd.platform_settings({**doc, "files": {"_defaults.yaml": {}}}, doc, set(),
                                    {rd.VIEW_SERVED: "not_computed", rd.VIEW_ROUTES: "computed"},
                                    "old", "new")
        assert [(r["key"], r["status"]) for r in rows] == [("defaults", cd.SETTING_NOT_COMPARED)]

    def test_a_base_side_parse_tree_failure_is_no_result(self, tmp_path, monkeypatch):
        files = {"db-a.yaml": self.TENANT, "_defaults.yaml": self.DEFAULTS}
        old, new = _pair(tmp_path, files, files)
        real = cd._lib_parse_tree.run

        def fail_on_base(root, *a, **k):
            if Path(root) == old:
                raise cd._lib_parse_tree.ParseTreeError("da-guard parse-tree exited 3")
            return real(root, *a, **k)

        monkeypatch.setattr(cd._lib_parse_tree, "run", fail_on_base)
        view, *_ = cd._platform_view(str(old), str(new), 60)
        assert (view.status, view.reason) == ("not_computed", "base side: da-guard parse-tree exited 3")

    def test_the_list_stops_at_the_limit_across_groups(self):
        """上限跨分組累計：第一組已滿時，後面的組只印標題，不再列項目。"""
        n = cd.UNCOVERED_LIST_LIMIT
        rows = ([_row(f"_a{i:03d}.yaml", "k", note="first") for i in range(n)]
                + [_row("_b.yaml", "k", note="second")])
        md = cd.render_markdown(_report(platform_settings=rows,
                                        uncovered_files=[r["file"] for r in rows]))
        assert "- `_b.yaml`" not in md and "- ... (+1 more; the JSON report lists them all)" in md

    def test_canonical_form_does_not_recurse(self):
        deep = {"text": "x", "tag": "!!str", "value": "x"}
        for _ in range(5000):
            deep = [deep]
        assert cd._canonical(deep).count("[") == 5001


def test_a_platform_default_reaches_many_tenants_as_one_entry(tmp_path):
    """端到端：平台預設（閾值與 `_routing_defaults.group_wait`）改到 20 個租戶，
    真正的兩個渲染器報出的變更在 Markdown 各只列一筆，寫租戶數（P4）。"""
    def tree(d, cpu, gw):
        d.mkdir()
        (d / "_defaults.yaml").write_text(
            f"defaults:\n  container_cpu: {cpu}\n_routing_defaults:\n  receiver:\n"
            f"    type: webhook\n    url: https://h/platform\n  group_wait: {gw}\n", encoding="utf-8")
        for i in range(20):
            (d / f"t{i:02d}.yaml").write_text(f"tenants:\n  t{i:02d}:\n    container_memory: 85\n",
                                              encoding="utf-8")
        return d
    p = _run_cli(tree(tmp_path / "old", 80, "30s"), tree(tmp_path / "new", 90, "1m"))
    assert p.returncode == 1, p.stderr
    who = "20 tenants: `t00`, `t01`, `t02`, `t03`, `t04` (+15 more)"
    assert f"| container_cpu | 80 | 90 | looser |" in p.stdout and p.stdout.count("| container_cpu |") == 1
    assert f"| {who} |" in p.stdout
    assert f"- {who} — 20 routes (changed), e.g. " in p.stdout
    assert p.stdout.count("`group_wait`: `30s` → `1m`") == 1


# ── ADR-038 決定 8：這次變更新引入的 `disable` 沒作用／只關到一部分 ─────────


class TestDisableWarnings:
    """PR 新引入的 noop／partial `disable` 列在報告裡；base 已經有的同一件事不重複列。"""

    _DEFAULTS = "defaults:\n  mysql_connections: 80\n  mysql_slave_lag: 30\n  redis_memory: 70\n"

    def test_new_noop_and_partial_are_listed(self, tmp_path):
        old, new = _pair(tmp_path, {
            "_defaults.yaml": self._DEFAULTS,
            "db-a.yaml": "tenants:\n  db-a:\n    mysql_connections: disable\n",
        }, {
            "_defaults.yaml": self._DEFAULTS,
            "db-a.yaml": "tenants:\n  db-a:\n    mysql_connections: disable\n"
                         "    mysql_slave_lag: disable\n"  # 新加、有作用：不列
                         "    mysql_slave_lagg: disable\n"  # 拼錯：沒關到任何東西
                         "    redis_memory: disable\n    redis_memory_critical: \"90\"\n",
        })
        p = _run_cli(old, new, "--format", "json")
        assert p.returncode == 1, p.stderr
        got = {w["key"]: w for w in _json(p)["disable_warnings"]}
        assert got["mysql_slave_lagg"]["disable"] == "noop"
        assert got["mysql_slave_lagg"]["tenants"] == ["db-a"]
        assert got["redis_memory"]["disable"] == "partial"
        assert got["redis_memory"]["served_family"] == ["redis_memory_critical"]
        # 有作用的 disable 不列，新加的也一樣；base 就有的也不列。
        assert "mysql_slave_lag" not in got and "mysql_connections" not in got

        md = _run_cli(old, new).stdout
        assert "## `disable` With No or Partial Effect" in md
        assert "- `mysql_slave_lagg`: noop — " in md
        assert "- `redis_memory`: partial — still served: `redis_memory_critical`" in md

    def test_a_noop_already_on_the_base_side_is_not_listed(self, tmp_path):
        tenant = "tenants:\n  db-a:\n    mysql_slave_lagg: disable\n"
        old, new = _pair(tmp_path, {"_defaults.yaml": self._DEFAULTS, "db-a.yaml": tenant},
                         {"_defaults.yaml": self._DEFAULTS.replace("80", "81"), "db-a.yaml": tenant})
        p = _run_cli(old, new, "--format", "json")
        assert p.returncode == 1, p.stderr  # 對照：門檻改了，確實有變更
        assert _json(p)["disable_warnings"] == []

    def test_a_platform_change_that_makes_a_disable_noop_is_listed(self, tmp_path):
        """平台拿掉 defaults 裡的 key，租戶原本有作用的 disable 變成 noop：也是這次引入的。"""
        tenant = "tenants:\n  db-a:\n    redis_memory: disable\n"
        old, new = _pair(tmp_path, {"_defaults.yaml": self._DEFAULTS, "db-a.yaml": tenant},
                         {"_defaults.yaml": self._DEFAULTS.replace("  redis_memory: 70\n", ""),
                          "db-a.yaml": tenant})
        p = _run_cli(old, new, "--format", "json")
        got = {w["key"]: w for w in _json(p)["disable_warnings"]}
        assert got["redis_memory"]["disable"] == "noop"
        # 租戶本來就關掉它，送出的值沒變：只有提醒，結束碼 0，報告不說「沒有變更」就了事。
        assert p.returncode == 0, p.stderr
        md = _run_cli(old, new).stdout
        assert "See the `disable` warnings above." in md
