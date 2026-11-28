"""validate --mapping：比對 legacy 與 migrated 規則的告警集合（#4227）。

遷移對照由 migrate-rule 寫（#4226 E′：規則原樣保留、每個 selector 注入租戶）；
validate 每輪做 count 預檢與告警集合比對。這裡量：

* 對照怎麼讀——prefix-mapping 具名拒收、不是對照的地方全部點名、零比對組拒收、
  migrate-rule 實際寫出的檔讀得回來；
* 比對鍵——（租戶, ``da_cluster``, alertname, 狀態）加其餘 label，去掉 ``tenant``、
  ``da_migrated``、``alertstate``；legacy 側的租戶歸屬（``tenant`` → ``namespace``
  → 不可驗）、其他租戶另列；
* 預檢——legacy 有而 pinned 是 0 → rc 2；兩邊都是 0 → 不可驗、不放行；
* 兩個端點各查各的、窗口取樣、收斂判定與 cutover 讀得回 readiness；
* ALERTS 查詢的語意（promtool test rules，載入真的 legacy 與 migrated 規則）。
"""
from __future__ import annotations

import csv
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
_OPS = REPO / "scripts" / "tools" / "ops"
sys.path.insert(0, str(_OPS))
sys.path.insert(0, str(_OPS.parent))

import validate_migration as vm  # noqa: E402
import _lib_shadow_report as report  # noqa: E402
from _lib_exitcodes import CallerError  # noqa: E402
from _lib_prometheus import QueryError  # noqa: E402
from _lib_shadow_report import split_tenant_cell, tenant_cell  # noqa: E402

TENANT = "tenant-x"
LEGACY = "http://legacy:9090"
MIGRATED = "http://edge:9090"


def _mapping_doc(alerts=("QueueDeep",), selectors=None):
    rules = [{"group": "g", "alert": a,
              "selectors": selectors if selectors is not None else
              [{"legacy": "myapp_queue_depth", "pinned": f'myapp_queue_depth{{tenant="{TENANT}"}}'}]}
             for a in alerts]
    rules.append({"group": "migrated-expiry", "alert": "MigratedRulesExpiring",
                  "added": True, "selectors": []})
    return {"tenant": TENANT, "rules_file": ".migration/migrated/x/rules.yml", "rules": rules}


def _write(tmp_path, doc, name="m.yaml"):
    path = tmp_path / name
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return path


def _args(path, *extra):
    return vm.build_parser().parse_args(["--mapping", str(path), *extra])


def _mapping():
    mapping, problems = report.read_mapping(_mapping_doc(), vm.is_valid_tenant_id)
    assert not problems
    return mapping


def _groups(side, alerts=("QueueDeep",), interval=60, tenant=TENANT):
    """A rules-API answer loading *alerts* the way *side* loads them."""
    labels = {"tenant": tenant, "da_migrated": "true"} if side == "migrated" else {}
    return [{"name": side, "interval": interval, "rules": [
        {"name": a, "type": "alerting", "labels": labels} for a in alerts]}]


# ---------------------------------------------------------------------------
# 讀對照
# ---------------------------------------------------------------------------

class TestLoadMapping:
    def test_the_prefix_mapping_is_refused_by_name(self, tmp_path):
        """owner 2026-11-28：數值比對整個移除，prefix-mapping 不退回、具名 rc 2。"""
        path = _write(tmp_path, {"custom_x": {"old_query": "a", "new_query": "b",
                                              "original_metric": "x"}})
        with pytest.raises(CallerError) as exc:
            vm.load_mapping(_args(path))
        assert exc.value.reason == "mapping_prefix_format"
        assert "migrate-rule" in str(exc.value) and ".migration/mapping/" in str(exc.value)

    def test_an_entry_without_the_numeric_fields_is_not_mistaken_for_a_prefix_mapping(self):
        assert not report.is_prefix_mapping(_mapping_doc())
        assert report.is_prefix_mapping({"k": {"original_metric": "x"}})

    def test_every_unusable_place_is_named_and_nothing_is_compared(self, tmp_path):
        """#4210／#4221：讀不了的項目不靜默略過，全部點名。"""
        doc = _mapping_doc(alerts=("A", "B"))
        doc["rules"][0]["selectors"] = [{"legacy": "x"}]           # no pinned
        doc["rules"][1] = {"group": "g", "alert": "B", "record": "r", "selectors": []}
        path = _write(tmp_path, doc)
        with pytest.raises(CallerError) as exc:
            vm.load_mapping(_args(path))
        assert exc.value.reason == "mapping_entry_unusable"
        text = str(exc.value)
        assert "rules[0].selectors[0].pinned" in text and "rules[1]" in text
        assert text.count("應為") == 2

    def test_a_mapping_with_only_the_added_alert_is_refused(self, tmp_path):
        """零比對組等於什麼都沒驗證（fail-closed）。"""
        path = _write(tmp_path, _mapping_doc(alerts=()))
        with pytest.raises(CallerError) as exc:
            vm.load_mapping(_args(path))
        assert exc.value.reason == "mapping_no_alerts"

    @pytest.mark.parametrize("tenant", [" tenant-x", "Tenant-X", "a_b"])
    def test_a_tenant_that_is_no_tenant_id_is_named(self, tmp_path, tenant):
        """審查 Minor 2：填錯的 tenant 讓 migrated 側查空、legacy 側全歸「其他租戶」，
        判成乾淨。"""
        path = _write(tmp_path, {**_mapping_doc(), "tenant": tenant})
        with pytest.raises(CallerError, match="tenant（應為合法的租戶 id"):
            vm.load_mapping(_args(path))

    def test_an_alert_named_twice_is_named(self, tmp_path):
        doc = _mapping_doc(alerts=("A", "A"))
        with pytest.raises(CallerError, match=r"rules\[1\]\.alert（應為不重複的 alert"):
            vm.load_mapping(_args(_write(tmp_path, doc)))

    def test_added_on_another_alert_is_named(self, tmp_path):
        """審查 Minor 3：在一條會 mismatch 的告警上標 added，它就悄悄不比了。"""
        doc = _mapping_doc(alerts=("A", "B"))
        doc["rules"][1]["added"] = True
        with pytest.raises(CallerError, match=r"rules\[1\]\.added（應為只有 MigratedRulesExpiring"):
            vm.load_mapping(_args(_write(tmp_path, doc)))

    def test_the_added_alert_is_not_compared(self):
        mapping = _mapping()
        assert mapping.alerts == ("QueueDeep",)
        assert mapping.added == ("MigratedRulesExpiring",)

    def test_the_mapping_migrate_rule_writes_reads_back(self, tmp_path, da_read_env,
                                                         da_guard_binary, monkeypatch):
        """寫入端與讀取端共用 `_lib_shadow_report` 的欄位：migrate-rule 實際寫出的檔
        validate 讀得回來，selector 原樣。"""
        monkeypatch.setenv("DA_GUARD_BINARY", da_guard_binary)
        rules = tmp_path / "rules.yml"
        rules.write_text(yaml.safe_dump({"groups": [{"name": "g", "rules": [
            {"alert": "QueueDeep", "expr": "sum by (queue) (myapp_queue_depth) > 100",
             "for": "5m"}]}]}), encoding="utf-8")
        out = tmp_path / "out"
        proc = subprocess.run(
            [sys.executable, str(_OPS / "migrate_rule.py"), str(rules), "--tenant", TENANT,
             "--owner", "team", "--expiry", "2099-06-30", "-o", str(out)],
            capture_output=True, text=True, encoding="utf-8", timeout=120, check=False)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        import migrate_rule
        mapping = vm.load_mapping(_args(out / migrate_rule.mapping_rel(TENANT)))
        assert mapping.tenant == TENANT
        assert mapping.alerts == ("QueueDeep",)
        assert mapping.added == (migrate_rule.EXPIRY_ALERT,)
        assert [(p.legacy, p.pinned) for p in mapping.selectors] == [
            ("myapp_queue_depth", f'myapp_queue_depth{{tenant="{TENANT}"}}')]


class TestFlagsRemoved:
    @pytest.mark.parametrize("flag", [["--old", "a"], ["--new", "b"], ["--tolerance", "0.1"]])
    def test_the_numeric_comparison_flags_are_gone(self, tmp_path, flag):
        path = _write(tmp_path, _mapping_doc())
        with pytest.raises(SystemExit) as exc:
            vm.build_parser().parse_args(["--mapping", str(path), *flag])
        assert exc.value.code == 2

    def test_mapping_is_required(self):
        with pytest.raises(SystemExit) as exc:
            vm.build_parser().parse_args([])
        assert exc.value.code == 2

    def test_an_interval_of_zero_leaves_no_window(self, tmp_path):
        path = _write(tmp_path, _mapping_doc())
        with pytest.raises(SystemExit):
            _args(path, "--interval", "0")


# ---------------------------------------------------------------------------
# 查詢與比對鍵
# ---------------------------------------------------------------------------

class TestAlertsQuery:
    def test_the_sides_are_told_apart_by_the_migrated_label(self):
        legacy = vm.alerts_query("QueueDeep", 60, "legacy", TENANT)
        migrated = vm.alerts_query("QueueDeep", 60, "migrated", TENANT)
        assert legacy == 'max_over_time(ALERTS{alertname="QueueDeep", da_migrated=""}[60000ms])'
        assert migrated == ('max_over_time(ALERTS{alertname="QueueDeep", da_migrated="true", '
                            f'tenant="{TENANT}"}}[60000ms])')

    def test_an_alertname_is_quoted(self):
        assert 'alertname="a\\"b\\\\c"' in vm.alerts_query('a"b\\c', 1.5, "legacy", TENANT)
        assert vm.window_text(1.5) == "1500ms"


def _row(alert="QueueDeep", **labels):
    return {"metric": {"__name__": "ALERTS", "alertname": alert, **labels}, "value": [0, "1"]}


def _keys(side, *metrics):
    return {vm.alert_key("QueueDeep", m["metric"], side) for m in metrics}


class TestCompare:
    def _statuses(self, legacy, migrated):
        rows = vm.compare_alerts("QueueDeep", TENANT, _keys("legacy", *legacy),
                                 _keys("migrated", *migrated))
        return [(r["status"], r["legacy"], r["migrated"], r["state"]) for r in rows]

    def test_the_twin_matches_whatever_tenant_and_migration_label_say(self):
        """legacy 的 tenant 來自序列、沒有 da_migrated；migrated 是靜態 tenant 加
        da_migrated="true"——兩者都不在鍵裡。"""
        legacy = [_row(tenant=TENANT, queue="q1", severity="warning", alertstate="firing")]
        migrated = [_row(tenant=TENANT, queue="q1", severity="warning", alertstate="firing",
                         da_migrated="true")]
        assert self._statuses(legacy, migrated) == [("match", 1, 1, "firing")]

    def test_firing_and_pending_are_compared_apart(self):
        legacy = [_row(tenant=TENANT, alertstate="firing")]
        migrated = [_row(tenant=TENANT, alertstate="pending", da_migrated="true")]
        assert sorted(self._statuses(legacy, migrated)) == [
            ("mismatch", 0, 1, "pending"), ("mismatch", 1, 0, "firing")]

    def test_each_instance_is_its_own_key(self):
        """legacy 規則逐 instance 產生多個序列：鍵少了這些 label 會互相覆蓋（#4305）。"""
        legacy = [_row(tenant=TENANT, instance="a", alertstate="firing"),
                  _row(tenant=TENANT, instance="b", alertstate="firing")]
        migrated = [_row(tenant=TENANT, instance="a", alertstate="firing", da_migrated="true")]
        assert self._statuses(legacy, migrated) == [
            ("match", 1, 1, "firing"), ("mismatch", 1, 0, "firing")]

    def test_a_legacy_alert_without_tenant_is_attributed_by_namespace(self):
        legacy = [_row(namespace=TENANT, alertstate="firing")]
        migrated = [_row(namespace=TENANT, tenant=TENANT, alertstate="firing",
                         da_migrated="true")]
        assert self._statuses(legacy, migrated) == [("match", 1, 1, "firing")]

    def test_another_tenants_legacy_alert_is_listed_apart_not_a_mismatch(self):
        legacy = [_row(tenant="other", alertstate="firing")]
        assert self._statuses(legacy, []) == [("other_tenant", 1, 0, "firing")]

    def test_a_legacy_alert_with_neither_tenant_nor_namespace_is_unverifiable(self):
        """owner 2026-11-28：聚合後 tenant、namespace 都不剩 → 不可驗、不放行。"""
        legacy = [_row(alertstate="firing")]
        assert self._statuses(legacy, []) == [("unverifiable", 1, 0, "firing")]
        result = {"rows": vm.compare_alerts("QueueDeep", TENANT, _keys("legacy", *legacy),
                                            set()), "query_errors": 0}
        assert vm.round_exit_code(result) == 1 and not vm.is_clean(result)

    def test_a_cluster_on_one_side_only_is_a_mismatch_naming_the_cluster(self, capsys):
        legacy = [_row(tenant=TENANT, da_cluster="east", alertstate="firing"),
                  _row(tenant=TENANT, da_cluster="west", alertstate="firing")]
        migrated = [_row(tenant=TENANT, da_cluster="east", alertstate="firing",
                         da_migrated="true")]
        rows = vm.compare_alerts("QueueDeep", TENANT, _keys("legacy", *legacy),
                                 _keys("migrated", *migrated))
        assert [(r["status"], r["da_cluster"]) for r in rows] == [
            ("match", "east"), ("mismatch", "west")]
        vm.print_summary({"rows": rows, "query_errors": 0})
        assert f"只在 legacy 端 firing: {TENANT} da_cluster=west" in capsys.readouterr().out

    def test_a_row_without_a_cluster_is_its_own_group(self):
        legacy = [_row(tenant=TENANT, alertstate="firing")]
        migrated = [_row(tenant=TENANT, da_cluster="east", alertstate="firing",
                         da_migrated="true")]
        assert sorted(s for s, *_ in self._statuses(legacy, migrated)) == [
            "mismatch", "mismatch"]


# ---------------------------------------------------------------------------
# 預檢與一輪
# ---------------------------------------------------------------------------

class FakeProm:
    """The query seams: answers by (endpoint, query); records every call.
    Rules: by default every alert is loaded on both endpoints, each the way
    its side loads it (one endpoint, or two, answer the same)."""

    def __init__(self, counts=None, alerts=None, fail=(), rules=None):
        self.counts = counts or {}   # (prom, selector) -> n
        self.alerts = alerts or {}   # (prom, side) -> [rows]
        self.fail = set(fail)        # proms that do not answer
        self.rules = rules           # prom -> groups; None: both sides loaded everywhere
        self.calls = []

    def rules_api(self, prom, rule_type=None):
        assert rule_type == "alert"
        self.calls.append((prom, "rules"))
        if prom in self.fail:
            raise QueryError("connection refused")
        if self.rules is not None:
            return self.rules.get(prom, [])
        return _groups("legacy") + _groups("migrated")

    def __call__(self, prom, query):
        self.calls.append((prom, query))
        if prom in self.fail:
            raise QueryError("connection refused")
        if query.startswith("count("):
            n = self.counts.get((prom, query[len("count("):-1]), 0)
            return [{"metric": {}, "value": [0, str(n)]}] if n else []
        side = "migrated" if 'da_migrated="true"' in query else "legacy"
        return self.alerts.get((prom, side), [])


_PINNED = f'myapp_queue_depth{{tenant="{TENANT}"}}'


@pytest.fixture
def fake(monkeypatch):
    def install(**kw):
        prom = FakeProm(**kw)
        monkeypatch.setattr(vm, "query_prometheus", prom)
        monkeypatch.setattr(vm, "query_rules", prom.rules_api)
        return prom
    return install


class TestRound:
    def test_the_two_endpoints_each_answer_their_side(self, fake):
        prom = fake(counts={(LEGACY, "myapp_queue_depth"): 3, (MIGRATED, _PINNED): 1})
        result = vm.run_round(_mapping(), LEGACY, MIGRATED, 60)
        assert (LEGACY, "count(myapp_queue_depth)") in prom.calls
        assert (MIGRATED, f"count({_PINNED})") in prom.calls
        assert {p for p, q in prom.calls if "ALERTS" in q and 'da_migrated=""' in q} == {LEGACY}
        assert {p for p, q in prom.calls if 'da_migrated="true"' in q} == {MIGRATED}
        assert {p for p, q in prom.calls if q == "rules"} == {LEGACY, MIGRATED}
        assert vm.round_exit_code(result) == 0 and vm.is_clean(result)

    def test_a_pin_selecting_nothing_is_named_and_exits_2(self, fake, capsys):
        fake(counts={(LEGACY, "myapp_queue_depth"): 3})
        result = vm.run_round(_mapping(), LEGACY, MIGRATED, 60)
        (row,) = [r for r in result["rows"] if r["kind"] == "selector"]
        assert row["status"] == "pin_selects_nothing"
        assert vm.round_exit_code(result) == 2
        vm.print_summary(result)
        assert f"注入後選不到資料: myapp_queue_depth → {_PINNED}" in capsys.readouterr().out

    def test_no_series_on_either_side_is_unverifiable_not_a_pass(self, fake):
        fake()
        result = vm.run_round(_mapping(), LEGACY, MIGRATED, 60)
        (row,) = [r for r in result["rows"] if r["kind"] == "selector"]
        assert row["status"] == "unverifiable"
        assert vm.round_exit_code(result) == 1 and not vm.is_clean(result)

    def test_a_query_not_answered_exits_2(self, fake):
        fake(counts={(MIGRATED, _PINNED): 1}, fail={LEGACY})
        result = vm.run_round(_mapping(), LEGACY, MIGRATED, 60)
        assert result["query_errors"] == 3   # the rules, the selector and the alert
        assert vm.round_exit_code(result) == 2

    @pytest.mark.parametrize("missing_on", ["legacy", "migrated"])
    def test_a_rule_not_loaded_is_not_two_empty_sets_that_agree(self, fake, missing_on):
        """審查 Major 1：migrated 規則沒部署（或 --legacy-prometheus 指錯台）而這段期間
        沒有告警響時，兩邊都是空集合——那不是一致，是 rc 2。"""
        rules = {LEGACY: _groups("legacy"), MIGRATED: _groups("migrated")}
        rules[LEGACY if missing_on == "legacy" else MIGRATED] = []
        fake(counts={(LEGACY, "myapp_queue_depth"): 1, (MIGRATED, _PINNED): 1}, rules=rules)
        result = vm.run_round(_mapping(), LEGACY, MIGRATED, 60)
        (row,) = [r for r in result["rows"] if r["kind"] == "rule"]
        assert (row["status"], row[missing_on]) == ("rule_not_loaded", 1)
        assert vm.round_exit_code(result) == 2 and not vm.is_clean(result)

    def test_a_migrated_rule_of_another_tenant_is_not_this_ones(self, fake):
        rules = {LEGACY: _groups("legacy"), MIGRATED: _groups("migrated", tenant="other")}
        fake(counts={(LEGACY, "myapp_queue_depth"): 1, (MIGRATED, _PINNED): 1}, rules=rules)
        result = vm.run_round(_mapping(), LEGACY, MIGRATED, 60)
        assert [r["status"] for r in result["rows"] if r["kind"] == "rule"] == [
            "rule_not_loaded"]

    def test_a_group_evaluated_less_often_than_the_window_is_2(self, fake):
        """審查 Major 2：組的評估間隔 600s、窗口 60s，十輪裡九輪看不到任何 ALERTS
        樣本，會被當成乾淨輪。"""
        rules = {LEGACY: _groups("legacy", interval=600), MIGRATED: _groups("migrated")}
        fake(counts={(LEGACY, "myapp_queue_depth"): 1, (MIGRATED, _PINNED): 1}, rules=rules)
        result = vm.run_round(_mapping(), LEGACY, MIGRATED, 60)
        (row,) = [r for r in result["rows"] if r["kind"] == "rule"]
        assert row["status"] == "window_too_short"
        assert vm.round_exit_code(result) == 2
        assert vm.is_clean(vm.run_round(_mapping(), LEGACY, MIGRATED, 600))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _main(monkeypatch, argv):
    monkeypatch.setattr(sys, "argv", ["validate_migration.py", *argv])
    return vm.main()


class TestMain:
    def test_a_single_run_writes_the_report_by_its_one_definition(self, tmp_path, fake,
                                                                   monkeypatch):
        fake(counts={(LEGACY, "myapp_queue_depth"): 2, (MIGRATED, _PINNED): 2},
             alerts={(LEGACY, "legacy"): [_row(tenant=TENANT, alertstate="firing")],
                     (MIGRATED, "migrated"): [_row(tenant=TENANT, alertstate="firing",
                                                   da_migrated="true", da_cluster="e")]})
        path = _write(tmp_path, _mapping_doc())
        rc = _main(monkeypatch, ["--mapping", str(path), "--prometheus", MIGRATED,
                                 "--legacy-prometheus", LEGACY, "-o", str(tmp_path / "o")])
        assert rc == 1
        with open(tmp_path / "o" / "validation-report.csv", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            assert tuple(reader.fieldnames) == report.REPORT_COLUMNS
            rows = list(reader)
        assert [(r["Kind"], r["Status"]) for r in rows if r["Kind"] != "rule"] == [
            ("selector", "ok"), ("alert", "mismatch"), ("alert", "mismatch")]
        assert split_tenant_cell(rows[1]["Tenant"]) == (TENANT, None)
        assert split_tenant_cell(rows[2]["Tenant"]) == (TENANT, "e")

    def test_the_legacy_endpoint_defaults_to_the_migrated_one(self, tmp_path, fake,
                                                              monkeypatch):
        prom = fake(counts={(MIGRATED, "myapp_queue_depth"): 1, (MIGRATED, _PINNED): 1})
        path = _write(tmp_path, _mapping_doc())
        assert _main(monkeypatch, ["--mapping", str(path), "--prometheus", MIGRATED,
                                   "-o", str(tmp_path / "o")]) == 0
        assert {p for p, _ in prom.calls} == {MIGRATED}

    def test_convergence_needs_the_window_of_clean_rounds_and_cutover_reads_it(
            self, tmp_path, monkeypatch):
        """連續 N 輪乾淨才 ready；中間一輪髒就重算。readiness 欄位是 cutover 要的那一份。"""
        dirty = {"n": 0}

        def seam(prom, query):
            if query.startswith("count("):
                return [{"metric": {}, "value": [0, "1"]}]
            dirty["n"] += 1
            # the migrated side's 2nd query (round 2) fires alone: one dirty round
            if 'da_migrated="true"' in query and dirty["n"] == 4:
                return [_row(tenant=TENANT, alertstate="firing", da_migrated="true")]
            return []
        monkeypatch.setattr(vm, "query_prometheus", seam)
        monkeypatch.setattr(vm, "query_rules",
                            lambda prom, rule_type: _groups("legacy", interval=1)
                            + _groups("migrated", interval=1))
        monkeypatch.setattr(vm.time, "sleep", lambda s: None)
        path = _write(tmp_path, _mapping_doc())
        out = tmp_path / "ready.json"
        rc = _main(monkeypatch, ["--mapping", str(path), "--prometheus", MIGRATED,
                                 "-o", str(tmp_path / "o"), "--watch", "--interval", "1",
                                 "--rounds", "10", "--auto-detect-convergence",
                                 "--stability-window", "3", "--convergence-output", str(out)])
        assert rc == 0
        data = json.loads(out.read_text(encoding="utf-8"))
        assert tuple(data) == report.READINESS_FIELDS
        assert (data["ready"], data["round_count"], data["clean_rounds"]) == (True, 5, 3)
        import cutover_tenant
        assert cutover_tenant.load_cutover_readiness(data, TENANT) is data

    def test_a_pin_selecting_nothing_stops_the_watch_with_2(self, tmp_path, fake,
                                                            monkeypatch):
        fake(counts={(MIGRATED, "myapp_queue_depth"): 5})
        monkeypatch.setattr(vm.time, "sleep", lambda s: None)
        path = _write(tmp_path, _mapping_doc())
        out = tmp_path / "ready.json"
        rc = _main(monkeypatch, ["--mapping", str(path), "--prometheus", MIGRATED,
                                 "-o", str(tmp_path / "o"), "--watch", "--rounds", "5",
                                 "--auto-detect-convergence", "--convergence-output", str(out)])
        assert rc == 2
        data = json.loads(out.read_text(encoding="utf-8"))
        assert (data["ready"], data["round_count"]) == (False, 1)
        assert data["last_round"]["pin_selects_nothing"] == 1

    def test_unconverged_is_1_even_when_the_last_round_is_clean(self, tmp_path, fake,
                                                                 monkeypatch):
        fake(counts={(MIGRATED, "myapp_queue_depth"): 1, (MIGRATED, _PINNED): 1})
        monkeypatch.setattr(vm.time, "sleep", lambda s: None)
        path = _write(tmp_path, _mapping_doc())
        rc = _main(monkeypatch, ["--mapping", str(path), "--prometheus", MIGRATED,
                                 "-o", str(tmp_path / "o"), "--watch", "--rounds", "2",
                                 "--auto-detect-convergence", "--stability-window", "3"])
        assert rc == 1

    def test_cutover_refuses_another_tenants_readiness(self):
        """審查 Major 3：A 租戶的 readiness 拿去切 B 租戶。"""
        import cutover_tenant
        data = {f: None for f in report.READINESS_FIELDS} | {"ready": True, "tenant": TENANT}
        with pytest.raises(ValueError, match="not 'other'"):
            cutover_tenant.load_cutover_readiness(data, "other")
        with pytest.raises(ValueError, match="'ready' is not a boolean"):
            cutover_tenant.load_cutover_readiness(data | {"ready": "false"}, TENANT)

    def test_a_readiness_of_the_numeric_comparison_is_refused_by_cutover(self):
        import cutover_tenant
        old = {"ready": True, "timestamp": "t", "convergence_percentage": 100.0,
               "converged_count": 1, "total_pairs": 1}
        with pytest.raises(ValueError, match="Missing required fields"):
            cutover_tenant.load_cutover_readiness(old)


# ---------------------------------------------------------------------------
# Tenant 欄（ADR-006，#1200）
# ---------------------------------------------------------------------------

_HOSTILE_CLUSTERS = ["east", "a) (da_cluster=b", "x)", "(", ")", "da_cluster=",
                     " (da_cluster=x)", "a\nb", "a\r\nb", "\\", "\\)", "x\\n", "a(b)c\\\\",
                     "東", "a,b\"c"]


@pytest.mark.parametrize("cluster", _HOSTILE_CLUSTERS, ids=repr)
@pytest.mark.parametrize("tenant", ["t1", "t (da_cluster=x)", "a) (da_cluster=b", "t\\"],
                         ids=repr)
def test_the_tenant_cell_reads_back_whatever_the_cluster_is_called(tenant, cluster):
    cell = tenant_cell(tenant, cluster)
    assert split_tenant_cell(cell) == (tenant, cluster)
    assert "\n" not in cell and "\r" not in cell


def test_a_cell_without_a_cluster_is_the_tenant_itself():
    assert tenant_cell("t1") == tenant_cell("t1", None) == tenant_cell("t1", "") == "t1"
    assert split_tenant_cell("t1") == ("t1", None)


def test_a_hostile_cluster_name_survives_the_csv(tmp_path):
    cluster = "a) (da_cluster=b\n|=("
    rows = vm.compare_alerts("QueueDeep", TENANT,
                             _keys("legacy", _row(tenant=TENANT, da_cluster=cluster)),
                             _keys("migrated", _row(tenant=TENANT, da_cluster=cluster,
                                                    da_migrated="true")))
    path = vm.write_csv_report({"rows": rows, "query_errors": 0}, str(tmp_path))
    with open(path, encoding="utf-8-sig", newline="") as fh:
        (row,) = list(csv.DictReader(fh))
    assert split_tenant_cell(row["Tenant"]) == (TENANT, cluster)


# ---------------------------------------------------------------------------
# ALERTS 查詢的語意：promtool 載入真的 legacy 與 migrated 規則
# ---------------------------------------------------------------------------

def _promtool():
    path = shutil.which("promtool")
    if path is None and os.environ.get("CLAUDE_CODE_REMOTE") == "true":
        pytest.fail("promtool is not on PATH (session-start installs it)")
    if path is None:
        pytest.skip("promtool is not on PATH")
    return path


def test_the_alerts_queries_select_each_side_in_a_real_prometheus(tmp_path):
    """同一台 Prometheus 載入 legacy 規則與 migrated 規則：legacy 側的查詢只選到
    legacy 告警（含其他租戶），migrated 側只選到本租戶的 migrated 告警；窗口內
    響過就算：1m 是 pending、2m 起 firing，3m 時 180s 窗口（左開，(0, 3m]）兩個
    狀態都看得到。max_over_time 丟掉 __name__（比對鍵本來就不含它）。"""
    rules = tmp_path / "rules.yml"
    rules.write_text(yaml.safe_dump({"groups": [
        {"name": "legacy", "rules": [
            {"alert": "QueueDeep", "expr": "myapp_queue_depth > 100", "for": "1m"}]},
        {"name": "migrated", "rules": [
            {"alert": "QueueDeep", "expr": f"{_PINNED} > 100", "for": "1m",
             "labels": {"tenant": TENANT, "da_migrated": "true"}}]},
    ]}), encoding="utf-8")
    window = 180

    def sample(side):
        return {"expr": vm.alerts_query("QueueDeep", window, side, TENANT), "eval_time": "3m",
                "exp_samples": []}

    legacy, migrated = sample("legacy"), sample("migrated")
    legacy["exp_samples"] = [
        {"labels": f'{{alertname="QueueDeep", alertstate="{s}", tenant="{t}"}}', "value": 1}
        for t in (TENANT, "other") for s in ("firing", "pending")]
    migrated["exp_samples"] = [
        {"labels": f'{{alertname="QueueDeep", alertstate="{s}", da_migrated="true", '
                   f'tenant="{TENANT}"}}', "value": 1}
        for s in ("firing", "pending")]
    test = tmp_path / "test.yml"
    test.write_text(yaml.safe_dump({
        "rule_files": [str(rules)], "evaluation_interval": "1m",
        "tests": [{"interval": "1m", "input_series": [
            {"series": f'myapp_queue_depth{{tenant="{TENANT}"}}', "values": "0 200x5"},
            {"series": 'myapp_queue_depth{tenant="other"}', "values": "0 200x5"}],
            "promql_expr_test": [legacy, migrated]}]}), encoding="utf-8")
    proc = subprocess.run([_promtool(), "test", "rules", str(test)], capture_output=True,
                          text=True, encoding="utf-8", timeout=60, check=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr


# ---------------------------------------------------------------------------
# 結束碼與輸出位置（沿用的契約）
# ---------------------------------------------------------------------------

def test_the_process_exit_code_is_the_answer(tmp_path):
    """entrypoint 以 exec_module(__main__) 執行：結束碼只能靠 SystemExit 傳出。
    prefix-mapping 在任何查詢之前就 rc 2、點名原因。"""
    path = _write(tmp_path, {"k": {"old_query": "a", "new_query": "b"}})
    proc = subprocess.run([sys.executable, str(_OPS / "validate_migration.py"), "--mapping",
                           str(path), "--prometheus", "http://127.0.0.1:1",
                           "-o", str(tmp_path / "o")],
                          capture_output=True, text=True, encoding="utf-8", timeout=60,
                          check=False)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "prefix-mapping" in proc.stderr
    assert not (tmp_path / "o").exists()


def test_an_unreachable_prometheus_is_2_not_a_finding(tmp_path):
    path = _write(tmp_path, _mapping_doc())
    proc = subprocess.run([sys.executable, str(_OPS / "validate_migration.py"), "--mapping",
                           str(path), "--prometheus", "http://127.0.0.1:1",
                           "-o", str(tmp_path / "o")],
                          capture_output=True, text=True, encoding="utf-8", timeout=120,
                          check=False)
    assert proc.returncode == 2, proc.stdout + proc.stderr


def test_the_partial_readiness_lands_under_the_output_dir(tmp_path, fake, monkeypatch):
    fake()
    monkeypatch.setattr(vm.time, "sleep", lambda s: None)
    path = _write(tmp_path, _mapping_doc())
    rc = _main(monkeypatch, ["--mapping", str(path), "--prometheus", MIGRATED,
                             "-o", str(tmp_path / "o"), "--watch", "--rounds", "2",
                             "--auto-detect-convergence"])
    assert rc == 1
    data = json.loads((tmp_path / "o" / "cutover-readiness.json").read_text(encoding="utf-8"))
    assert (data["ready"], data["clean_rounds"]) == (False, 0)
    assert data["last_round"]["unverifiable"] == 1
