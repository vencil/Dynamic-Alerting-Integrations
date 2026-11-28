"""migrate-rule（#4226）：一個租戶的傳統規則原樣遷移成 migrated 規則。

釘住的契約（#4226 驗收）：

1. 兩份 legacy fixture 的每條告警，要嘛遷移（輸出過 lint），要嘛字典命中、在
   報告裡點名；沒有一條悄悄消失。
2. 依 ``_lib_metric_sources.pin_form`` 的三類結果各有注入測試：帶 tenant 的
   ``tenant="X"``；kube-state-metrics 有 namespace 的物件 ``namespace="X",
   tenant=""``；其餘（叢集層級物件、平台自己的序列、每個抓取目標都有的序列、
   告警的序列）rc 2 點名。
3. 每種 rc 2 形狀各一個測試，且什麼都不寫。
4. recording rule：同檔被讀到的一起遷移（遞移），讀到檔裡沒有的 rc 2。
5. expr 以外原樣保留；labels 加 tenant／component=migrated，annotations 加
   owner／expiry；到期伴隨告警；mapping 檔的 selector 注入前後。
6. 輸出過 ``lint_custom_rules --ci``（租戶規則檔佈局），promtool 量得到
   隔離：別的租戶的序列不讓 migrated 告警響。
"""
from __future__ import annotations

import datetime
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

_REPO = Path(__file__).resolve().parents[2]
_TOOLS = _REPO / "scripts" / "tools"
_SCRIPT = _TOOLS / "ops" / "migrate_rule.py"
sys.path.insert(0, str(_TOOLS / "ops"))
sys.path.insert(0, str(_TOOLS))

import _lib_metric_sources  # noqa: E402
import migrate_rule as mr  # noqa: E402

pytestmark = pytest.mark.usefixtures("da_read_env", "da_guard_env")

TENANT = "db-a"
OWNER = "team-db"
EXPIRY = "2099-12-31"


def run(tmp_path, rules, *extra, tenant=TENANT, owner=OWNER, expiry=EXPIRY, out=None):
    """Run the CLI on *rules* (a YAML text or a fixture path)."""
    if isinstance(rules, Path):
        src = rules
    else:
        src = tmp_path / "rules.yml"
        src.write_text(rules, encoding="utf-8")
    out = out or tmp_path / "out"
    argv = [sys.executable, str(_SCRIPT), str(src), "--tenant", tenant, "--owner", owner,
            "--expiry", expiry, "-o", str(out), *extra]
    proc = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                          timeout=300, env=os.environ.copy())
    return proc, out


def rules_of(out, tenant=TENANT):
    return yaml.safe_load((out / mr.rules_rel(tenant)).read_text(encoding="utf-8"))


def by_name(doc):
    return {r.get("alert") or r.get("record"): r for g in doc["groups"] for r in g["rules"]}


def one(alert, expr, **rule):
    return yaml.safe_dump({"groups": [{"name": "g", "rules": [
        {"alert": alert, "expr": expr, **rule}]}]}, sort_keys=False)


def nothing_written(out):
    return not out.exists() or not any(out.rglob("*"))


# ---------------------------------------------------------------------------
# 1. 兩份 legacy fixture
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("fixture", ["legacy-dummy.yml", "legacy-multidb.yml"])
def test_every_fixture_alert_is_migrated_or_named_golden(tmp_path, fixture):
    src = _REPO / "tests" / "fixtures" / fixture
    proc, out = run(tmp_path, src)
    assert proc.returncode == 0, proc.stderr
    source = yaml.safe_load(src.read_text(encoding="utf-8"))
    alerts = {r["alert"] for g in source["groups"] for r in g["rules"]}
    migrated = set(by_name(rules_of(out))) - {mr.EXPIRY_ALERT}
    report = (out / mr.report_rel(TENANT)).read_text(encoding="utf-8")
    golden = {a for a in alerts if f"  - {a}（" in report.split("改用黃金標準")[-1]}
    assert migrated | golden == alerts
    assert not migrated & golden
    assert golden, "the dictionary names no golden rule for this fixture: the test proves nothing"


def test_without_the_dictionary_every_alert_is_migrated(tmp_path):
    src = _REPO / "tests" / "fixtures" / "legacy-dummy.yml"
    proc, out = run(tmp_path, src, "--no-dictionary")
    assert proc.returncode == 0, proc.stderr
    source = yaml.safe_load(src.read_text(encoding="utf-8"))
    assert set(by_name(rules_of(out))) - {mr.EXPIRY_ALERT} == {
        r["alert"] for g in source["groups"] for r in g["rules"]}


@pytest.mark.parametrize("fixture", ["legacy-dummy.yml", "legacy-multidb.yml"])
def test_the_output_passes_the_lint_as_the_tenants_rule_file(tmp_path, fixture, da_crdecode_env):
    proc, out = run(tmp_path, _REPO / "tests" / "fixtures" / fixture, "--no-dictionary")
    assert proc.returncode == 0, proc.stderr
    lint = subprocess.run(
        [sys.executable, str(_TOOLS / "ops" / "lint_custom_rules.py"),
         str(out / mr.DRAFT_ROOT / "migrated"), "--ci"],
        capture_output=True, text=True, encoding="utf-8", timeout=300)
    assert lint.returncode == 0, lint.stdout + lint.stderr
    assert "Errors: 0" in lint.stdout


# ---------------------------------------------------------------------------
# 2. 注入：pin_form 的三類
# ---------------------------------------------------------------------------

def test_a_metric_whose_series_carry_tenant_gets_tenant(tmp_path):
    proc, out = run(tmp_path, one("A", 'rate(app_errors_total{job="app"}[5m] offset 1m) > 1'))
    assert proc.returncode == 0, proc.stderr
    assert by_name(rules_of(out))["A"]["expr"] == (
        'rate(app_errors_total{job="app",tenant="db-a"}[5m] offset 1m) > 1')


def test_a_namespaced_kube_state_metrics_series_gets_namespace_and_an_empty_tenant(tmp_path):
    proc, out = run(tmp_path, one("A", 'kube_pod_container_status_restarts_total > 3'))
    assert proc.returncode == 0, proc.stderr
    assert by_name(rules_of(out))["A"]["expr"] == (
        'kube_pod_container_status_restarts_total{namespace="db-a",tenant=""} > 3')


@pytest.mark.parametrize("metric", [
    "container_fs_writes_total",            # cAdvisor
    "kubelet_volume_stats_used_bytes",      # the kubelet's volume stats
])
def test_an_infrastructure_series_gets_namespace_and_an_empty_tenant(tmp_path, metric):
    # #4178: the infrastructure families name the tenant only as namespace,
    # on every install; a tenant pin would select nothing.
    proc, out = run(tmp_path, one("A", f"{metric} > 0"))
    assert proc.returncode == 0, proc.stderr
    assert by_name(rules_of(out))["A"]["expr"] == (
        f'{metric}{{namespace="db-a",tenant=""}} > 0')


@pytest.mark.parametrize("metric", [
    "kube_node_status_condition",       # cluster-scoped object
    "kube_persistentvolume_status_phase",
    "prometheus_tsdb_head_series",      # the platform's own series
    "up",                               # every scrape target's
    "kube_state_metrics_list_total",    # kube-state-metrics' own (review of #4341)
    "ALERTS",                           # an alert's
])
def test_a_series_no_pin_selects_one_tenant_of_is_refused(tmp_path, metric):
    proc, out = run(tmp_path, one("A", f"{metric} > 0"))
    assert proc.returncode == 2
    assert f"g/A: selector {metric}:" in proc.stderr
    assert "platform-level rule" in proc.stderr
    assert nothing_written(out)


#: kube-state-metrics' cluster-scoped resources (its docs/metrics), written out
#: here so dropping one from the table is red, not a smaller loop.
_CLUSTER_SCOPED = ("kube_node_", "kube_persistentvolume_", "kube_storageclass_",
                   "kube_clusterrole_", "kube_clusterrolebinding_",
                   "kube_certificatesigningrequest_", "kube_mutatingwebhookconfiguration_",
                   "kube_validatingwebhookconfiguration_", "kube_ingressclass_",
                   "kube_volumeattachment_")


def test_the_pin_form_table_is_kube_state_metrics_own():
    assert set(_lib_metric_sources.KSM_CLUSTER_SCOPED_PREFIXES) == set(_CLUSTER_SCOPED)
    # every cluster-scoped prefix is a kube-state-metrics family, or it pins nothing
    for prefix in _CLUSTER_SCOPED:
        assert _lib_metric_sources.pin_form(prefix + "info")[0] is None
        assert _lib_metric_sources.resolve_family(prefix + "info")[1:] == (
            "service", ("monitoring", "kube-state-metrics"))
    # the claim is namespaced though its name starts like the volume's
    assert _lib_metric_sources.pin_form("kube_persistentvolumeclaim_info") == ("namespace", None)


# ---------------------------------------------------------------------------
# 3. rc 2：每種形狀，且什麼都不寫
# ---------------------------------------------------------------------------

REFUSED = {
    "another-tenant": (one("A", 'x{tenant="db-b"} > 0'), 'tenant="db-b"'),
    "tenant-regex": (one("A", 'x{tenant=~"db-.*"} > 0'), 'tenant=~"db-.*"'),
    "tenant-negation": (one("A", 'x{tenant!="db-b"} > 0'), 'tenant!="db-b"'),
    "another-namespace": (one("A", 'x{namespace="db-b"} > 0'), 'namespace="db-b"'),
    "ksm-another-namespace": (one("A", 'kube_pod_info{namespace="db-b"} > 0'),
                              'namespace="db-b"'),
    "no-metric-name": (one("A", '{job="x"} > 0'), "names no metric"),
    "regex-metric-name": (one("A", '{__name__=~"x.*"} > 0'), "names no metric"),
    "missing-record": (one("A", "job:x:rate5m > 0"), "does not define"),
    "tenant-label": (one("A", "x > 0", labels={"tenant": "db-b"}), "label tenant"),
    "component-label": (one("A", "x > 0", labels={"component": "mysql"}), "label component"),
    "owner-label": (one("A", "x > 0", labels={"owner": "someone"}), "owner is a label"),
    "other-owner": (one("A", "x > 0", annotations={"owner": "someone"}), "annotation owner"),
    "other-expiry": (one("A", "x > 0", annotations={"expiry": "2030-01-01"}),
                     "annotation expiry"),
    "expiry-alert-name": (one(mr.EXPIRY_ALERT, "x > 0"), "expiry alert"),
    "lint-denied-function": (one("A", "predict_linear(x[1h], 3600) < 0"),
                             "lint of the migrated rules"),
    "lint-range": (one("A", "rate(x[7d]) > 0"), "lint of the migrated rules"),
    "keeps-component-by": (one("A", "sum by (component) (rate(x_total[5m])) > 1"),
                           "keeps component (by(component))"),
    "keeps-component-matcher": (one("A", 'x{component="db"} > 1'), "a component matcher"),
    "keeps-component-on": (one("A", "x * on(component) group_left y > 1"), "on/group_*"),
    "writes-component": (one("A", 'label_replace(x, "component", "$1", "job", "(.*)") > 1'),
                         'label_replace(…, "component", …)'),
    # review of #4341: the migrated record carries tenant, sum(y) does not
    "record-matches-differently": (yaml.safe_dump({"groups": [{"name": "g", "rules": [
        {"record": "job:x:sum", "expr": "sum(app_x)"},
        {"alert": "A", "expr": "job:x:sum / sum(app_y) > 1"}]}]}),
        "job:x:sum / sum(app_y): the migrated record carries tenant"),
    "record-name-taken": (yaml.safe_dump({"groups": [{"name": "g", "rules": [
        {"record": "job:x:sum", "expr": "sum by (tenant) (app_x)"},
        {"record": "migrated:db_a:job:x:sum", "expr": "sum by (tenant) (app_y)"},
        {"alert": "A", "expr": "job:x:sum > 1"}]}]}),
        "its migrated name migrated:db_a:job:x:sum is a record this file defines too"),
}


@pytest.mark.parametrize("expr", [
    "job:x:sum / job:y:sum > 1",            # both sides migrated records: both carry it
    "job:x:sum / on() sum(app_y) > 1",      # on() without tenant does not look at it
    "job:x:sum > 1 unless on(job) sum(app_y) > 0",
])
def test_a_match_the_added_tenant_does_not_change_is_migrated(tmp_path, expr):
    proc, _ = run(tmp_path, yaml.safe_dump({"groups": [{"name": "g", "rules": [
        {"record": "job:x:sum", "expr": "sum(app_x)"},
        {"record": "job:y:sum", "expr": "sum(app_y)"},
        {"alert": "A", "expr": expr}]}]}), "--no-dictionary")
    assert proc.returncode == 0, proc.stderr


def test_a_golden_hit_beside_a_selector_naming_no_metric_is_not_golden(tmp_path):
    # review of #4341: `{job="x"}` is not what the golden rule covers
    proc, out = run(tmp_path, one(
        "A", 'mysql_global_status_threads_connected > 100 or {job="mysql"} > 100'))
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert 'selector {job="mysql"}: it names no metric' in proc.stderr
    assert nothing_written(out)


@pytest.mark.parametrize("case", sorted(REFUSED))
def test_a_rule_that_cannot_be_migrated_as_written_is_refused_and_nothing_is_written(
        tmp_path, case, da_crdecode_env):
    text, named = REFUSED[case]
    proc, out = run(tmp_path, text, "--no-dictionary")
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert named in proc.stderr
    assert "nothing was written" in proc.stderr
    assert nothing_written(out)


def test_a_group_named_as_the_expiry_group_is_refused(tmp_path):
    text = yaml.safe_dump({"groups": [{"name": mr.EXPIRY_GROUP, "rules": [
        {"alert": "A", "expr": "x > 0"}]}]})
    proc, out = run(tmp_path, text)
    assert proc.returncode == 2 and f"group {mr.EXPIRY_GROUP}" in proc.stderr
    assert nothing_written(out)


def test_every_refused_rule_is_listed_not_only_the_first(tmp_path):
    text = yaml.safe_dump({"groups": [{"name": "g", "rules": [
        {"alert": "A", "expr": 'x{tenant="db-b"} > 0'},
        {"alert": "B", "expr": "up == 0"},
        {"alert": "C", "expr": "y > 0"}]}]})
    proc, _ = run(tmp_path, text)
    assert proc.returncode == 2
    assert "g/A:" in proc.stderr and "g/B:" in proc.stderr and "g/C:" not in proc.stderr
    assert "2 problem(s)" in proc.stderr


@pytest.mark.parametrize("flag, value, named", [
    ("tenant", "DB_A", "not a valid tenant id"),
    ("owner", "{{ $labels.team }}", "annotation is a template"),
    ("owner", "  ", "--owner is empty"),
    ("expiry", "2020-01-01", "after today"),
    ("expiry", "2099-02-30", "calendar date"),
    ("expiry", "31/12/2099", "YYYY-MM-DD"),
])
def test_a_bad_flag_is_refused_and_nothing_is_written(tmp_path, flag, value, named):
    proc, out = run(tmp_path, one("A", "x > 0"), **{flag: value})
    assert proc.returncode == 2
    assert named in proc.stderr
    assert nothing_written(out)


def test_an_output_path_that_is_a_file_is_refused_before_anything_is_written(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / mr.DRAFT_ROOT).write_text("not a directory", encoding="utf-8")
    proc, _ = run(tmp_path, one("A", "x > 0"), out=out)
    # the shared one line (#4250: every target judged before the first write)
    assert proc.returncode == 2 and "ERROR: cannot write " in proc.stderr
    assert mr.rules_rel(TENANT) in proc.stderr
    assert sorted(p.name for p in out.iterdir()) == [mr.DRAFT_ROOT]
    assert (out / mr.DRAFT_ROOT).read_text(encoding="utf-8") == "not a directory"


def test_a_later_target_that_cannot_be_written_stops_the_run_before_the_first(tmp_path):
    out = tmp_path / "out"
    (out / mr.report_rel(TENANT)).mkdir(parents=True)  # the last of the three files
    proc, _ = run(tmp_path, one("A", "x > 0"), out=out)
    assert proc.returncode == 2 and mr.report_rel(TENANT) in proc.stderr
    assert not (out / mr.rules_rel(TENANT)).exists()
    assert not (out / mr.mapping_rel(TENANT)).exists()


_SERVED_TREE = {
    "_defaults.yaml": "defaults:\n  mysql_connection_usage_percent: 80\n",
    "tenant-a.yaml": 'tenants:\n  tenant-a:\n    mysql_connection_usage_percent: "70"\n',
}


def _served(root):
    sv = subprocess.run([os.environ["DA_GUARD_BINARY"], "served-values", "--config-dir",
                         str(root), "--at", "2026-11-13T12:00:00Z"], capture_output=True,
                        text=True, encoding="utf-8", timeout=120)
    assert sv.returncode == 0, sv.stdout + sv.stderr
    doc = __import__("json").loads(sv.stdout)
    doc.pop("da_guard_version", None)
    return doc


@pytest.mark.parametrize("where", ["o_is_the_tree", "o_is_a_new_subdirectory"])
def test_a_run_into_a_served_tree_changes_nothing_it_serves(tmp_path, where):
    # #4014: the rule YAML lands under the dot-prefixed draft root, which every
    # conf.d reader prunes whole — so -o may lie inside a served conf.d.
    live = tmp_path / "live"
    for rel, body in _SERVED_TREE.items():
        (live / rel).parent.mkdir(parents=True, exist_ok=True)
        (live / rel).write_text(body, encoding="utf-8")
    before = _served(live)
    assert sorted(before["tenants"]) == ["tenant-a"]
    out = live if where == "o_is_the_tree" else live / "newsub"
    proc, _ = run(tmp_path, one("A", "x > 0"), out=out)
    assert proc.returncode == 0, proc.stderr
    assert (out / mr.rules_rel(TENANT)).is_file()  # the control: it did write
    assert _served(live) == before


def test_two_tenants_into_one_o_keep_each_others_report(tmp_path):
    out = tmp_path / "out"
    for tenant in ("db-a", "db-b"):
        proc, _ = run(tmp_path, one("A", "x > 0"), tenant=tenant, out=out)
        assert proc.returncode == 0, proc.stderr
    for tenant in ("db-a", "db-b"):
        assert (out / mr.report_rel(tenant)).read_text(encoding="utf-8").startswith(
            f"migrate-rule 遷移報告：租戶 {tenant}")
        assert (out / mr.rules_rel(tenant)).is_file()


def test_a_run_that_migrates_nothing_names_an_earlier_runs_output(tmp_path):
    out = tmp_path / "out"
    proc, _ = run(tmp_path, one("A", "x > 0"), out=out)
    assert proc.returncode == 0, proc.stderr
    golden = _REPO / "tests" / "fixtures" / "legacy-dummy.yml"
    text = golden.read_text(encoding="utf-8")
    only_golden = yaml.safe_dump({"groups": [{"name": "g", "rules": [
        r for g in yaml.safe_load(text)["groups"] for r in g["rules"]
        if r["alert"] == "MySQLTooManyConnections"]}]})
    proc, _ = run(tmp_path, only_golden, out=out)
    assert proc.returncode == 0, proc.stderr
    assert "[WARN]" in proc.stderr and mr.rules_rel(TENANT) in proc.stderr


# ---------------------------------------------------------------------------
# 4. recording rule
# ---------------------------------------------------------------------------

RECORDS = yaml.safe_dump({"groups": [{"name": "g", "interval": "30s", "rules": [
    {"record": "job:x:sum", "expr": "sum by (pod) (rate(x_total[5m]))"},
    {"record": "job:x:max", "expr": "max(job:x:sum)"},
    {"record": "unused:y", "expr": "y"},
    {"alert": "A", "expr": "job:x:max > 1", "for": "5m"},
]}]}, sort_keys=False)


def test_the_records_an_alert_reads_are_migrated_with_it_and_carry_the_tenant(tmp_path):
    proc, out = run(tmp_path, RECORDS)
    assert proc.returncode == 0, proc.stderr
    rules = by_name(rules_of(out))
    # renamed, and every reference with it: the legacy record of the same name
    # is loaded beside it (review of #4226)
    assert "job:x:sum" not in rules and "job:x:max" not in rules
    s, m = "migrated:db_a:job:x:sum", "migrated:db_a:job:x:max"
    assert rules[s]["expr"] == 'sum by (pod) (rate(x_total{tenant="db-a"}[5m]))'
    assert rules[s]["labels"] == {"tenant": TENANT}
    # the record read only through another record is migrated too (transitive)
    assert rules[m]["expr"] == f'max({s}{{tenant="db-a"}})'
    assert rules["A"]["expr"] == f'{m}{{tenant="db-a"}} > 1'
    assert "unused:y" not in rules and "migrated:db_a:unused:y" not in rules
    report = (out / mr.report_rel(TENANT)).read_text(encoding="utf-8")
    assert "  - unused:y" in report.split("未遷移的 recording rule")[1]
    assert rules_of(out)["groups"][0]["interval"] == "30s"


def test_a_record_of_another_file_is_refused(tmp_path):
    proc, out = run(tmp_path, one("A", "rate5m:x:sum > 1"))
    assert proc.returncode == 2
    assert "reads recording rule rate5m:x:sum, which this file does not define" in proc.stderr
    assert nothing_written(out)


# ---------------------------------------------------------------------------
# 5. 原樣保留、標記、到期伴隨告警、mapping
# ---------------------------------------------------------------------------

def test_everything_but_the_expression_is_kept_and_the_marks_are_added(tmp_path):
    rule = {"alert": "Slow", "expr": "app_latency_seconds > 2", "for": "10m",
            "keep_firing_for": "5m",
            "labels": {"severity": "critical", "team": "db"},
            "annotations": {"summary": "slow {{ $labels.instance }}", "runbook": "r"}}
    text = yaml.safe_dump({"groups": [{"name": "g", "rules": [rule]}]})
    proc, out = run(tmp_path, text)
    assert proc.returncode == 0, proc.stderr
    got = by_name(rules_of(out))["Slow"]
    assert got == {**rule, "expr": 'app_latency_seconds{tenant="db-a"} > 2',
                   "labels": {**rule["labels"], "tenant": TENANT, "component": "migrated"},
                   "annotations": {**rule["annotations"], "owner": OWNER, "expiry": EXPIRY}}


def test_a_tenant_or_owner_already_written_as_this_run_writes_it_is_kept(tmp_path):
    text = one("A", 'x{tenant="db-a"} > 0', labels={"tenant": TENANT},
               annotations={"owner": OWNER})
    proc, out = run(tmp_path, text)
    assert proc.returncode == 0, proc.stderr
    assert by_name(rules_of(out))["A"]["expr"] == 'x{tenant="db-a"} > 0'


def test_the_marks_are_the_routes_label(tmp_path):
    import _grar_routes
    proc, out = run(tmp_path, one("A", "x > 0"))
    assert proc.returncode == 0, proc.stderr
    labels = by_name(rules_of(out))["A"]["labels"]
    assert labels[_grar_routes.MIGRATED_LABEL] == _grar_routes.MIGRATED_VALUE
    route = _grar_routes._build_migrated_alert_routes()[0][0]
    assert f'{_grar_routes.MIGRATED_LABEL}="{_grar_routes.MIGRATED_VALUE}"' in route["matchers"]


def test_the_expiry_alert_fires_from_two_weeks_before_the_expiry(tmp_path):
    proc, out = run(tmp_path, one("A", "x > 0"))
    assert proc.returncode == 0, proc.stderr
    doc = rules_of(out)
    assert doc["groups"][-1]["name"] == mr.EXPIRY_GROUP
    alert = by_name(doc)[mr.EXPIRY_ALERT]
    warn_at = datetime.datetime(2099, 12, 17, tzinfo=datetime.timezone.utc)
    assert alert["expr"] == f"vector(time()) > {int(warn_at.timestamp())}"
    assert alert["labels"] == {"severity": "warning", "tenant": TENANT, "component": "migrated"}
    assert alert["annotations"]["owner"] == OWNER
    assert alert["annotations"]["expiry"] == EXPIRY


def test_the_mapping_names_each_selector_before_and_after_the_pin(tmp_path):
    text = yaml.safe_dump({"groups": [{"name": "g", "rules": [
        {"alert": "A", "expr": 'rate(x_total{job="a"}[5m]) / on(pod) kube_pod_info > 1'}]}]})
    proc, out = run(tmp_path, text)
    assert proc.returncode == 0, proc.stderr
    mapping = yaml.safe_load((out / mr.mapping_rel(TENANT)).read_text(encoding="utf-8"))
    assert mapping == {"tenant": TENANT, "rules_file": mr.rules_rel(TENANT), "rules": [
        {"group": "g", "alert": "A", "selectors": [
            {"legacy": 'x_total{job="a"}', "pinned": 'x_total{job="a",tenant="db-a"}'},
            {"legacy": "kube_pod_info",
             "pinned": 'kube_pod_info{namespace="db-a",tenant=""}'}]},
        # only on the migrated side: the alert-set comparison expects it
        {"group": mr.EXPIRY_GROUP, "alert": mr.EXPIRY_ALERT, "added": True,
         "selectors": []}]}


def test_the_mapping_names_a_records_migrated_name(tmp_path):
    proc, out = run(tmp_path, RECORDS)
    assert proc.returncode == 0, proc.stderr
    mapping = yaml.safe_load((out / mr.mapping_rel(TENANT)).read_text(encoding="utf-8"))
    rec = next(r for r in mapping["rules"] if r.get("record") == "job:x:max")
    assert rec["migrated_record"] == "migrated:db_a:job:x:max"
    assert rec["selectors"] == [{"legacy": "job:x:sum",
                                 "pinned": 'migrated:db_a:job:x:sum{tenant="db-a"}'}]


def test_a_dry_run_prints_the_report_and_writes_nothing(tmp_path):
    proc, out = run(tmp_path, one("A", "x > 0"), "--dry-run")
    assert proc.returncode == 0, proc.stderr
    assert "已遷移的告警（1）" in proc.stdout
    assert nothing_written(out)


def test_the_report_names_the_load_point(tmp_path):
    proc, out = run(tmp_path, one("A", "x > 0"))
    assert proc.returncode == 0, proc.stderr
    assert "federation 架構下的 edge" in (out / mr.report_rel(TENANT)).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 6. 多叢集：原式子的分組只標記、不改寫（#1200，#4305 帶過來）
# ---------------------------------------------------------------------------

GROUPED = [
    ("app_hit_ratio / on(instance) app_all_ratio > 0.8", ["on(instance)"]),
    ("app_errs_count / ON(instance) group_left app_reqs_count != 0", ["on(instance)"]),
    ("app_disk_usage / on(tenant) group_left app_quota_usage > 0.9", ["on(tenant)"]),
    ("sum by(instance) (rate(app_reqs_total[5m])) > 5", ["by(instance)"]),
    ('max by("instance") (app_gc_delay) > 5', ["by(instance)"]),
    ("max(app_pool_usage) by (pod, namespace) > 0.9", ["by(pod, namespace)"]),
    ("sum by(pod) (app_a_count) / on(pod) sum by(pod) (app_b_count) > 2",
     ["on(pod)", "by(pod)"]),
    ("sum without(da_cluster) (app_open_files) > 1", ["without(da_cluster)"]),
]
PLAIN = [
    "app_connections > 100",
    "sum without(instance) (app_open_files) > 1000",
    "app_share_ratio / on() group_left app_fleet_ratio > 0.5",
    "max by(instance, da_cluster) (app_heap_usage) > 0.9",
    'app_notes_count{note="sum by(instance) on(pod)"} > 3',
]


@pytest.mark.parametrize("expr, groupings", GROUPED)
def test_a_grouping_without_the_cluster_is_noted_and_not_rewritten(tmp_path, expr, groupings):
    proc, out = run(tmp_path, one("A", expr))
    assert proc.returncode == 0, proc.stderr
    report = (out / mr.report_rel(TENANT)).read_text(encoding="utf-8")
    assert f"  - A：{mr.cluster_grouping_note(groupings)}" in report
    assert "在持有多個叢集資料的 Prometheus 上" in report
    assert "da_cluster" not in by_name(rules_of(out))["A"]["expr"].replace(
        "without (da_cluster)", "")


@pytest.mark.parametrize("expr", PLAIN)
def test_other_expressions_get_no_note(tmp_path, expr):
    proc, out = run(tmp_path, one("A", expr))
    assert proc.returncode == 0, proc.stderr
    assert "需人工確認（多叢集）" not in (out / mr.report_rel(TENANT)).read_text(encoding="utf-8")


def test_the_grouping_helpers_read_the_tree():
    (tree,) = mr.read_trees(["sum WITHOUT (instance, da_cluster) (x)"])
    assert mr.groupings_without_cluster(tree) == ["without(instance, da_cluster)"]
    (tree,) = mr.read_trees(['sum by(pod, "a\\nb") (x)'])
    found = mr.groupings_without_cluster(tree)
    assert found == ['by(pod, "a\\nb")'] and "\n" not in mr.cluster_grouping_note(found)


def test_the_identity_labels_are_the_lints():
    sys.path.insert(0, str(_TOOLS / "lint"))
    import check_cluster_identity as cid
    assert mr._IDENTITY_LABELS == cid.IDENTITY_LABELS


# ---------------------------------------------------------------------------
# 7. promtool：別的租戶的序列不讓 migrated 告警響
# ---------------------------------------------------------------------------

def test_another_tenants_series_does_not_fire_the_migrated_alert(tmp_path):
    exe = shutil.which("promtool")
    if exe is None:
        pytest.skip("promtool is not on PATH (scripts/ops/install-promtool.sh installs it)")
    proc, out = run(tmp_path, one("High", "app_queue_length > 10", **{"for": "1m"}))
    assert proc.returncode == 0, proc.stderr
    test = {"rule_files": [str(out / mr.rules_rel(TENANT))], "evaluation_interval": "1m",
            "tests": [{"interval": "1m", "input_series": [
                {"series": 'app_queue_length{tenant="db-a",instance="i1"}', "values": "5x5"},
                {"series": 'app_queue_length{tenant="db-b",instance="i2"}', "values": "50x5"},
                {"series": 'app_queue_length{tenant="db-a",instance="i3"}', "values": "50x5"}],
                "alert_rule_test": [{"eval_time": "3m", "alertname": "High", "exp_alerts": [
                    {"exp_labels": {"tenant": "db-a", "instance": "i3",
                                    "component": "migrated"},
                     "exp_annotations": {"owner": OWNER, "expiry": EXPIRY}}]}]}]}
    path = tmp_path / "t.yml"
    path.write_text(yaml.safe_dump(test), encoding="utf-8")
    res = subprocess.run([exe, "test", "rules", str(path)], capture_output=True, text=True,
                         encoding="utf-8", timeout=120)
    assert res.returncode == 0, res.stdout + res.stderr


def _promtool(tmp_path, rule_files, input_series, alert_tests):
    exe = shutil.which("promtool")
    if exe is None:
        pytest.skip("promtool is not on PATH (scripts/ops/install-promtool.sh installs it)")
    test = {"rule_files": [str(f) for f in rule_files], "evaluation_interval": "1m",
            "tests": [{"interval": "1m", "input_series": input_series,
                       "alert_rule_test": alert_tests}]}
    path = tmp_path / "promtool-test.yml"
    path.write_text(yaml.safe_dump(test), encoding="utf-8")
    res = subprocess.run([exe, "test", "rules", str(path)], capture_output=True, text=True,
                         encoding="utf-8", timeout=120)
    assert res.returncode == 0, res.stdout + res.stderr


_MARKS = {"tenant": TENANT, "component": "migrated"}
_ANN = {"owner": OWNER, "expiry": EXPIRY}


def test_another_namespaces_kube_state_metrics_series_does_not_fire_it(tmp_path):
    proc, out = run(tmp_path, one("Restarts", "kube_pod_container_status_restarts_total > 3"))
    assert proc.returncode == 0, proc.stderr
    _promtool(tmp_path, [out / mr.rules_rel(TENANT)], [
        {"series": 'kube_pod_container_status_restarts_total{namespace="db-b",pod="p1"}',
         "values": "9x3"},
        {"series": 'kube_pod_container_status_restarts_total{namespace="db-a",pod="p2"}',
         "values": "9x3"}],
        [{"eval_time": "2m", "alertname": "Restarts", "exp_alerts": [
            {"exp_labels": {**_MARKS, "namespace": "db-a", "pod": "p2"},
             "exp_annotations": _ANN}]}])


def test_a_ratio_of_two_migrated_records_alerts_as_the_legacy_one(tmp_path):
    # review of #4341: both records carry the tenant, so they still pair
    legacy = tmp_path / "legacy.yml"
    legacy.write_text(yaml.safe_dump({"groups": [{"name": "g", "rules": [
        {"record": "job:x:sum", "expr": "sum(app_x)"},
        {"record": "job:y:sum", "expr": "sum(app_y)"},
        {"alert": "Ratio", "expr": "job:x:sum / job:y:sum > 1"}]}]}), encoding="utf-8")
    proc, out = run(tmp_path, legacy, "--no-dictionary")
    assert proc.returncode == 0, proc.stderr
    _promtool(tmp_path, [legacy, out / mr.rules_rel(TENANT)], [
        {"series": 'app_x{tenant="db-a"}', "values": "20x3"},
        {"series": 'app_y{tenant="db-a"}', "values": "10x3"}],
        [{"eval_time": "2m", "alertname": "Ratio", "exp_alerts": [
            {"exp_labels": {}},
            {"exp_labels": _MARKS, "exp_annotations": _ANN}]}])


def test_the_legacy_rules_loaded_beside_the_migrated_ones_alert_as_before(tmp_path):
    # review of #4226: with the record's name kept, the legacy alert read the
    # migrated record's series too and fired one more alert, not shadow-routed
    legacy = tmp_path / "legacy.yml"
    legacy.write_text(yaml.safe_dump({"groups": [{"name": "g", "rules": [
        {"record": "job:conn:sum", "expr": "sum by (job) (app_conn)"},
        {"alert": "TooMany", "expr": "job:conn:sum > 100"}]}]}), encoding="utf-8")
    proc, out = run(tmp_path, legacy)
    assert proc.returncode == 0, proc.stderr
    _promtool(tmp_path, [legacy, out / mr.rules_rel(TENANT)], [
        {"series": 'app_conn{job="mysql",tenant="db-a",instance="i1"}', "values": "150x3"},
        {"series": 'app_conn{job="mysql",tenant="db-b",instance="i2"}', "values": "150x3"}],
        [{"eval_time": "2m", "alertname": "TooMany", "exp_alerts": [
            {"exp_labels": {"job": "mysql"}},                       # legacy, as before
            {"exp_labels": {**_MARKS, "job": "mysql"}, "exp_annotations": _ANN}]}])
