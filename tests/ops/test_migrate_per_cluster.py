"""migrate_rule 產生的規則逐叢集聚合；validate 以（租戶、叢集）對值（#1200）。

ADR-006「多叢集同租戶」：同一個租戶跑在兩個叢集時，`sum by(tenant)` 把兩個
叢集加在一起、`max by(tenant)` 只留較大的那個，告警也說不出是哪個叢集。
migrate_rule 產生的 metric 側 recording rule 因此改成
`<agg> by(tenant, da_cluster) (…)`（owner 2026-11-24 裁決）。這與 issue 1818
的裁決不衝突：那項裁決是不把實例層級的 label 帶回聚合；粒度仍是租戶，只是
逐叢集各算一次。

釘住的契約
----------
1. metric 側分組是 `by(tenant, da_cluster)`，label 只有一種拼法
   （`_lib_cluster_identity.CLUSTER_IDENTITY_LABEL`）；閾值側、告警的
   `on(tenant) group_left`、維護的 `unless on(tenant)` 不變。
2. 原式子自己寫的 `by(...)`／`on(...)` 照抄、不改寫（與 main 逐位元組相同）：
   要不要補叢集 label 取決於配對的兩邊是否都帶它，工具看不出來。點名身分
   label 卻沒有叢集 label 的，從 Prometheus 的語法樹（`da-read promql-shape`）
   偵測，並在既有的註記管道標出來請人確認。
3. `{{ $labels.da_cluster }}` 照留；`{{ $labels.instance }}` 照舊改寫。
4. `check_cluster_identity` 自己的判定：沒有原式子自帶分組的形狀是零
   finding（把分組改回 `by(tenant)` 就有 finding：不是空集合上的綠燈）；
   lint 會報的規則恰好就是工具標了註記的規則，沒有一條是悄悄產出的。
5. promtool 跑產生出來的規則：兩個叢集各自比對閾值，告警帶 `da_cluster`
   與該叢集自己的值；序列沒有 `da_cluster` 時，告警、label、值與
   `by(tenant)` 的產物相同。
6. validate：同一個租戶的兩列不互相覆蓋；逐叢集比對；只在一邊出現的叢集
   是點名叢集的 mismatch；沒有叢集 label 的輸入，輸出與先前逐位元組相同
   （既有測試不動，見 test_validate_migration.py）。
"""
from __future__ import annotations

import csv
import io
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest
import yaml

_TOOLS = Path(__file__).resolve().parents[2] / "scripts" / "tools"
sys.path.insert(0, str(_TOOLS / "ops"))
sys.path.insert(0, str(_TOOLS / "lint"))
sys.path.insert(0, str(_TOOLS))

import migrate_rule as mr  # noqa: E402
import shadow_verify as sv  # noqa: E402
import validate_migration as vm  # noqa: E402
from _lib_cluster_identity import CLUSTER_IDENTITY_LABEL  # noqa: E402
from _lib_shadow_report import REPORT_COLUMNS, split_tenant_cell, tenant_cell  # noqa: E402

_PROMTOOL = shutil.which("promtool")
needs_promtool = pytest.mark.skipif(_PROMTOOL is None, reason="promtool not on PATH")


# ---------------------------------------------------------------------------
# 產生：與 main() 相同的步驟，純函式、不寫檔
# ---------------------------------------------------------------------------

def generate(rules, *, prefix="custom_", mod=mr):
    """(results, recording rules 文字, alert rules 文字)：*rules* 是傳統規則的
    list，經 process_rule → 配對 → critical 改讀 base → render，如同 main()。
    *mod* 是 migrate_rule 模組（另一個版本的產生器也能餵同一組輸入）。"""
    trees = mod.read_trees([rule["expr"] for rule in rules])
    results = [r for r in (mod.process_rule(rule, prefix=prefix, dictionary={}, tree=tree)
                           for rule, tree in zip(rules, trees)) if r]
    mod.apply_auto_suppression(results)
    mod.route_unpaired_critical_to_base(results)
    return (results, mod.render_recording_rules(results, prefix),
            mod.render_alert_rules(results, prefix))


def _rule(alert, expr, severity="warning", **annotations):
    rule = {"alert": alert, "expr": expr, "labels": {"severity": severity}}
    if annotations:
        rule["annotations"] = annotations
    return rule


#: 每一種 migrate_rule 會產出規則的輸入形狀（沒有自帶身分分組的）：六個比較
#: 運算子、兩種聚合模式（sum：rate／_total；max：其餘）、算術、不點名身分
#: label 的分組與配對、warning＋critical 配對、只有 critical（改讀 base 閾值列）。
PLAIN_RULES = [
    _rule("Gt", "app_connections > 100"),
    _rule("Ge", "app_threads >= 10"),
    _rule("Lt", "app_free_ratio < 0.1"),
    _rule("Le", "app_idle_workers <= 2"),
    _rule("Eq", "app_state == 2"),
    _rule("Ne", "app_mode != 1"),
    _rule("SumTotal", "app_queue_jobs_total > 100"),
    _rule("SumRate", "rate(app_errors_total[5m]) > 1"),
    _rule("Arithmetic", "app_used_bytes / app_limit_bytes * 100 > 80"),
    _rule("Quantile",
          "histogram_quantile(0.99, sum by(le) (rate(app_latency_bucket[5m]))) > 2"),
    _rule("OnNothing", "app_share_ratio / on() group_left app_fleet_ratio > 0.5"),
    _rule("Without", "sum without(instance) (app_open_files) > 1000"),
    _rule("Ignoring", "app_busy_workers / ignoring(instance) app_max_workers > 0.9"),
    _rule("AlreadyPerCluster",
          "max by(instance, da_cluster) (app_heap_usage) "
          "/ on(instance, da_cluster) app_heap_limit_usage > 0.9"),
    _rule("StringMatcher", 'app_notes_count{note="sum by(instance) on(pod)"} > 3'),
    _rule("PairWarn", "app_replica_lag > 10"),
    _rule("PairCrit", "app_replica_lag > 60", severity="critical"),
    _rule("OnlyCrit", "app_backlog_queue > 500", severity="critical"),
]

#: 原式子自己寫了點名身分 label、卻沒有叢集 label 的 `on(...)`／`by(...)`：
#: (規則, groupings_without_cluster 的結果)。
GROUPED_RULES = [
    (_rule("PairOnInstance", "app_hit_ratio / on(instance) app_all_ratio > 0.8"),
     ["on(instance)"]),
    (_rule("PairGroupLeft",
           "app_role_lag * on(instance) group_left(version) app_build_info > 30"),
     ["on(instance)"]),
    (_rule("PairUpper", "app_errs_count / ON(instance) group_left app_reqs_count != 0"),
     ["on(instance)"]),
    (_rule("PairGroupLeftCluster",
           "app_lat_latency * on(instance) group_left(da_cluster) app_info_latency > 5"),
     ["on(instance)"]),
    (_rule("PairOnTenant", "app_disk_usage / on(tenant) group_left app_quota_usage > 0.9"),
     ["on(tenant)"]),
    (_rule("InnerBy", "sum by(instance) (rate(app_reqs_total[5m])) > 5"), ["by(instance)"]),
    (_rule("InnerByUpper", "sum BY(instance) (rate(app_jobs_total[5m])) > 5"),
     ["by(instance)"]),
    (_rule("InnerByQuoted", 'max by("instance") (app_gc_delay) > 5'), ["by(instance)"]),
    (_rule("InnerByPostfix", "max(app_pool_usage) by (pod, namespace) > 0.9"),
     ["by(pod, namespace)"]),
    (_rule("InnerByTenant", "sum by(tenant, node) (app_node_errors) > 3"),
     ["by(tenant, node)"]),
    (_rule("Both", "sum by(pod) (app_a_count) / on(pod) sum by(pod) (app_b_count) > 2"),
     ["on(pod)", "by(pod)"]),
]
EMITTING_RULES = PLAIN_RULES + [rule for rule, _groupings in GROUPED_RULES]

#: migrate_rule 不產出規則、只給 LLM prompt 的輸入：absent() 類、沒有數字
#: 閾值的向量比較、bool 修飾、整條包在括號裡、predict_linear。
MANUAL_RULES = [
    # 根節點是 and／or／unless，不是「比較、右邊是數字」（#4215 起不再被切成
    # `a > 5 and b` 對 3）
    _rule("CompoundAnd", "app_and_lag > 5 and app_y_lag > 3"),
    _rule("CompoundOr", "app_or_lag > 5 or app_z_lag > 3"),
    _rule("CompoundUnless", "app_unless_lag > 5 unless app_w_lag > 3"),
    _rule("Absent", "absent(app_up) == 1"),
    _rule("AbsentBare", "absent(app_up)"),
    _rule("VectorCompare", "app_x > app_y"),
    _rule("Bool", "app_x > bool 5"),
    _rule("Paren", "(app_x > 5)"),
    _rule("Predict", "predict_linear(app_free_bytes[1h], 3600) < 0"),
]


# ---------------------------------------------------------------------------
# 1. 產生的形狀
# ---------------------------------------------------------------------------

def test_the_grouping_spells_the_cluster_label_once():
    assert mr.METRIC_GROUPING == f"by(tenant, {CLUSTER_IDENTITY_LABEL})"
    assert mr.METRIC_GROUPING == "by(tenant, da_cluster)"


@pytest.mark.parametrize("prefix", ["custom_", ""], ids=["prefix", "no-prefix"])
def test_every_metric_side_rule_is_per_cluster_and_the_rest_is_per_tenant(prefix):
    results, _rec, _alerts = generate(EMITTING_RULES, prefix=prefix)
    assert len(results) == len(EMITTING_RULES)
    for r in results:
        metric_rule, threshold_rule = r.recording_rules
        assert r.agg_mode in ("sum", "max")
        assert metric_rule["expr"].startswith(f"{r.agg_mode} by(tenant, da_cluster) ("), metric_rule
        # 閾值是租戶一份（ADR-006 契約 3）
        assert threshold_rule["expr"].startswith("max by(tenant) (user_threshold{"), threshold_rule
        assert "da_cluster" not in threshold_rule["expr"]
        (alert,) = r.alert_rules
        assert f"{r.op} on(tenant) group_left\n" in alert["expr"]
        assert alert["expr"].endswith(mr.MAINTENANCE_TAIL)
        assert "da_cluster" not in alert["expr"]
    assert {r.agg_mode for r in results} == {"sum", "max"}
    assert {r.op for r in results} == {">", ">=", "<", "<=", "==", "!="}


def test_rules_left_for_manual_handling_emit_nothing_and_the_prompt_says_the_shape():
    results, rec, alerts = generate(MANUAL_RULES)
    assert [r.status for r in results] == ["unparseable"] * len(MANUAL_RULES)
    assert all(not r.recording_rules and not r.alert_rules for r in results)
    assert "record:" not in rec and "alert:" not in alerts
    for r in results:
        assert "sum/max by(tenant, da_cluster) 的 Recording Rule" in r.llm_prompt
        assert "on(tenant) group_left" in r.llm_prompt
        assert "by(tenant) 的 Recording Rule" not in r.llm_prompt


def test_old_query_is_grouped_as_the_recording_rule_is(tmp_path, monkeypatch):
    results, _rec, _alerts = generate(EMITTING_RULES)
    written = {}
    monkeypatch.setattr(mr, "write_operator_output_or_die",
                        lambda path, text, flag: written.update(text=text))
    mr.write_prefix_mapping(results, tmp_path, "custom_")
    mapping = yaml.safe_load(written["text"])
    assert mapping["custom_app_queue_jobs_total"]["old_query"] == (
        "sum by(tenant, da_cluster) (app_queue_jobs_total)")
    # 原式子照抄：只有外層分組是新的
    assert mapping["custom_app_hit_ratio"]["old_query"] == (
        "max by(tenant, da_cluster) (app_hit_ratio / on(instance) app_all_ratio)")
    queries = [e["old_query"] for e in mapping.values()]
    assert len(queries) >= 15
    assert all(" by(tenant, da_cluster) (" in q for q in queries)


# ---------------------------------------------------------------------------
# 2. 原式子自己的配對與分組
# ---------------------------------------------------------------------------

#: 原式子帶 `on()`／`by()` 的規則，產生的 metric 側 recording rule——與
#: origin/main 的產物只差外層的 `, da_cluster`（main 的產生器另外跑過一次，
#: 見 commit 訊息）。
_AS_WRITTEN = {
    "PairOnInstance": 'max by(tenant, da_cluster) (app_hit_ratio{tenant=~".+"} '
                      '/ on(instance) app_all_ratio{tenant=~".+"})',
    "PairUpper": 'sum by(tenant, da_cluster) (app_errs_count{tenant=~".+"} '
                 '/ ON(instance) group_left app_reqs_count{tenant=~".+"})',
    "PairGroupLeftCluster": 'max by(tenant, da_cluster) (app_lat_latency{tenant=~".+"} '
                            '* on(instance) group_left(da_cluster) '
                            'app_info_latency{tenant=~".+"})',
    "PairOnTenant": 'max by(tenant, da_cluster) (app_disk_usage{tenant=~".+"} '
                    '/ on(tenant) group_left app_quota_usage{tenant=~".+"})',
    "InnerBy": 'sum by(tenant, da_cluster) (sum by(instance) '
               '(rate(app_reqs_total{tenant=~".+"}[5m])))',
    "InnerByUpper": 'sum by(tenant, da_cluster) (sum BY(instance) '
                    '(rate(app_jobs_total{tenant=~".+"}[5m])))',
    "InnerByQuoted": 'max by(tenant, da_cluster) (max by("instance") '
                     '(app_gc_delay{tenant=~".+"}))',
    "InnerByPostfix": 'max by(tenant, da_cluster) (max(app_pool_usage{tenant=~".+"}) '
                      'by (pod, namespace))',
}


def test_the_original_expression_goes_in_as_written():
    results, _rec, _alerts = generate([rule for rule, _g in GROUPED_RULES])
    got = {r.alert_name: r.recording_rules[0]["expr"] for r in results}
    assert {name: got[name] for name in _AS_WRITTEN} == _AS_WRITTEN
    for r in results:
        # 舊查詢也是原式子本身
        assert r.recording_lhs_original == r.original_expr.rsplit(" ", 2)[0]
    assert not hasattr(mr, "keep_cluster_in_groupings")


@pytest.mark.parametrize("rule,groupings", GROUPED_RULES, ids=lambda v: v.get("alert")
                         if isinstance(v, dict) else None)
def test_a_grouping_that_names_an_identity_without_the_cluster_gets_the_note(rule, groupings):
    r = mr.process_rule(rule, prefix="custom_", dictionary={})
    assert r.cluster_groupings == groupings
    note = mr.cluster_grouping_note(groupings)
    assert "\n" not in note and "、".join(groupings) in note and "da_cluster" in note
    assert r.notes.count(note) == 1
    # 既有的「要人看」狀態：帶括號的式子本來就是 complex／review，不另設狀態
    assert (r.status, r.triage_action) == ("complex", "review")


@pytest.mark.parametrize("rule", PLAIN_RULES, ids=lambda r: r["alert"])
def test_other_expressions_are_checked_and_get_no_note(rule):
    r = mr.process_rule(rule, prefix="custom_", dictionary={})
    assert r.cluster_groupings == []
    assert not any("da_cluster" in n for n in r.notes)


def test_the_note_reaches_the_report_the_rule_file_the_tenant_file_notes_and_the_console(capsys):
    rules = [GROUPED_RULES[0][0], PLAIN_RULES[0]]
    results, recording, _alerts = generate(rules)
    note = mr.cluster_grouping_note(["on(instance)"])
    assert recording.count(f"      # ⚠️ {note}\n      - record: tenant:custom_app_hit_ratio:max\n") == 1
    assert recording.count(note) == 1   # 不掛在閾值那一條、也不掛在別的規則上
    report = mr.render_report(results)
    assert report.count(f"    ⚠️ {note}\n") == 1
    mr.print_dry_run(results)
    assert capsys.readouterr().out.count(f"     ⚠️ {note}\n") == 1
    assert results[0].notes == [note]   # tenant_config_entry 把 notes 寫成 📖 註解


def _lhs_tree(expr):
    return mr.read_trees([expr])[0]


@pytest.mark.parametrize("expr, found", [
    # naming the cluster label in `without`／`ignoring` drops it just the same
    ("sum without(da_cluster) (app_open_files)", ["without(da_cluster)"]),
    ("sum WITHOUT (instance, da_cluster) (app_open_files)", ["without(instance, da_cluster)"]),
    ("app_a_ratio / ignoring(da_cluster) app_b_ratio", ["ignoring(da_cluster)"]),
    # not naming it keeps it: nothing to say
    ("sum without(instance) (app_open_files)", []),
    ("app_a_ratio / ignoring(instance) app_b_ratio", []),
])
def test_without_and_ignoring_that_name_the_cluster_are_flagged(expr, found):
    assert mr.groupings_without_cluster(_lhs_tree(expr)) == found


def test_a_quoted_label_name_stays_on_one_line_of_the_note():
    found = mr.groupings_without_cluster(_lhs_tree('sum by(pod, "a\\nb") (x)'))
    assert found == ['by(pod, "a\\nb")']
    assert "\n" not in mr.cluster_grouping_note(found)


def test_there_is_no_unchecked_path_left():
    """語法樹一律由 Prometheus 的 parser 讀（#4215）：產出規則的每一條都有樹，
    沒有「沒檢查」這種狀態；`--no-ast` 已移除。"""
    assert not hasattr(mr, "CLUSTER_GROUPING_NOT_CHECKED")
    assert not hasattr(mr.MigrationResult("A", "perfect"), "cluster_grouping_checked")


def test_the_note_does_not_change_the_exit_code(tmp_path):
    rules = tmp_path / "rules.yml"
    rules.write_text(yaml.safe_dump({"groups": [{"name": "g", "rules": [
        rule for rule, _g in GROUPED_RULES[:3]]}]}), encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(_TOOLS / "ops" / "migrate_rule.py"), str(rules), "--dry-run",
         "--no-dictionary"], capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.count("⚠️ 原式子的 on(instance) 不保留 da_cluster") == 3
    assert "未檢查" not in proc.stdout + proc.stderr


def test_the_identity_labels_are_the_lints():
    import check_cluster_identity as cid
    assert mr._IDENTITY_LABELS == cid.IDENTITY_LABELS
    assert CLUSTER_IDENTITY_LABEL == cid.CLUSTER_LABEL


# ---------------------------------------------------------------------------
# 3. annotation：da_cluster 照留，實例層級的 label 照舊改寫
# ---------------------------------------------------------------------------

def test_the_cluster_label_survives_and_instance_is_still_rewritten():
    out, dropped = mr.rewrite_dropped_label_refs(
        {"summary": "{{ $labels.instance }} in {{ $labels.da_cluster }}",
         "detail": '{{ printf "%s/%s" $labels.da_cluster $labels.pod }}'},
        "app_connections")
    assert out == {
        "summary": "{{ $labels.tenant }}（原為 instance，已依租戶聚合） in {{ $labels.da_cluster }}",
        "detail": '{{ printf "%s/%s" $labels.da_cluster $labels.tenant }}'}
    assert dropped == ["instance", "pod"]


def test_a_rule_that_only_reads_the_cluster_label_is_not_reported_as_rewritten():
    rule = _rule("Conn", "app_connections > 100", where="{{ $labels.da_cluster }}")
    r = mr.process_rule(rule, prefix="custom_", dictionary={})
    assert r.dropped_label_refs == []
    assert r.alert_rules[0]["annotations"] == {"where": "{{ $labels.da_cluster }}"}
    assert "⚠️ 原 annotation" not in mr.render_alert_rules([r], "custom_")


# ---------------------------------------------------------------------------
# 4. check_cluster_identity 自己的判定
# ---------------------------------------------------------------------------

def _lint_tree(root: Path, recording: str, alerts: str) -> Path:
    packs = root / "rule-packs"
    packs.mkdir(parents=True)
    (packs / "rule-pack-migrated-recording.yaml").write_text(recording, encoding="utf-8")
    (packs / "rule-pack-migrated-alerts.yaml").write_text(alerts, encoding="utf-8")
    return root


@pytest.mark.usefixtures("da_read_env")
@pytest.mark.parametrize("prefix", ["custom_", ""], ids=["prefix", "no-prefix"])
def test_the_lint_finds_nothing_in_rules_without_a_grouping_of_their_own(tmp_path, prefix):
    import check_cluster_identity as cid
    results, recording, alerts = generate(PLAIN_RULES + MANUAL_RULES,
                                          prefix=prefix)
    root = _lint_tree(tmp_path / "now", recording, alerts)
    judged = cid._judged(root)
    emitted = [r for r in results if r.alert_rules]
    assert len(emitted) == len(PLAIN_RULES)
    # 每條產生的規則都被判到：去重後的 recording rule，加上每條 alert
    assert len(judged) == len(mr._dedup_recording_rules(results)) + len(emitted)
    assert cid.findings(root) == {}
    assert cid.stale_declarations(root) == []

    # 判定是真的在看這棵樹：分組改回 by(tenant)，metric 側規則就有 finding
    merged = _lint_tree(tmp_path / "merged",
                        recording.replace(mr.METRIC_GROUPING, "by(tenant)"), alerts)
    found = cid.findings(merged)
    assert set(found) == {"rule-pack-migrated-recording.yaml"}
    assert all("by-drops-cluster (tenant)" in f
               for f in found["rule-pack-migrated-recording.yaml"])
    flagged = {f.split(" / ", 1)[1].split(": ", 1)[0]
               for f in found["rule-pack-migrated-recording.yaml"]}
    # by(le) 那條的內層已經不帶叢集，外層沒有東西可丟；其餘每一條都報
    quantile = next(r.record_name for r in results if r.alert_name == "Quantile")
    assert flagged == {r.record_name for r in emitted} - {quantile}
    assert len(flagged) >= 15


@pytest.mark.usefixtures("da_read_env")
def test_the_rules_the_lint_flags_are_the_rules_that_carry_the_note(tmp_path):
    """產生器不會悄悄產出一條 lint 會報的規則：lint 報的 record 集合，等於
    帶註記的規則的 record 集合。"""
    import check_cluster_identity as cid
    results, recording, alerts = generate(EMITTING_RULES + MANUAL_RULES)
    found = cid.findings(_lint_tree(tmp_path, recording, alerts))
    assert set(found) == {"rule-pack-migrated-recording.yaml"}
    flagged = {f.split(" / ", 1)[1].split(": ", 1)[0]
               for f in found["rule-pack-migrated-recording.yaml"]}
    noted = {r.record_name for r in results if r.cluster_groupings}
    assert len(noted) == len(GROUPED_RULES)
    assert flagged == noted
    kinds = {f.rsplit(": ", 1)[1].split(" (")[0]
             for f in found["rule-pack-migrated-recording.yaml"]}
    assert kinds == {"by-drops-cluster", "on-drops-cluster"}


# ---------------------------------------------------------------------------
# 5. promtool：產生出來的規則在兩個叢集上
# ---------------------------------------------------------------------------

_ALERT_LABELS = {"severity": "warning", "source": "legacy",
                 "migration_status": "shadow", "maintenance": "mute"}


def threshold(metric, value, **labels):
    """threshold-exporter 發的那一列（`custom_<metric>` 的 warning 層）。"""
    extra = "".join(f',{k}="{v}"' for k, v in labels.items())
    return {"series": f'user_threshold{{component="custom",metric="{metric}",'
                      f'severity="warning",tenant="t1"{extra}}}', "values": f"{value}x10"}


def series(name, value, **labels):
    body = ",".join(f'{k}="{v}"' for k, v in {"tenant": "t1", **labels}.items())
    return {"series": f"{name}{{{body}}}", "values": f"{value}x10"}


def alert(value, cluster=None, **annotations):
    labels = {"tenant": "t1", **_ALERT_LABELS}
    if cluster is not None:
        labels[CLUSTER_IDENTITY_LABEL] = cluster
    return {"exp_labels": labels, "exp_annotations": {"value": str(value), **annotations}}


def run_promtool(tmp_path, recording, alerts, input_series, expected):
    """*expected*：{alertname: [exp_alert, …]}（空 list ＝ 不得有告警）。"""
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "recording.yaml").write_text(recording, encoding="utf-8")
    (tmp_path / "alerts.yaml").write_text(alerts, encoding="utf-8")
    (tmp_path / "test.yaml").write_text(yaml.safe_dump({
        "rule_files": ["recording.yaml", "alerts.yaml"],
        "evaluation_interval": "1m",
        "tests": [{
            "interval": "1m",
            "input_series": input_series,
            "alert_rule_test": [
                {"eval_time": "5m", "alertname": name, "exp_alerts": exp}
                for name, exp in expected.items()],
        }],
    }, allow_unicode=True), encoding="utf-8")
    return subprocess.run([_PROMTOOL, "test", "rules", "test.yaml"], cwd=tmp_path,
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=120)


def assert_alerts(tmp_path, rules, input_series, expected, *, mod=mr):
    _results, recording, alerts = generate(rules, mod=mod)
    proc = run_promtool(tmp_path, recording, alerts, input_series, expected)
    assert proc.returncode == 0, proc.stdout + proc.stderr


_SUM_RULE = [_rule("QueueJobs", "app_queue_jobs_total > 100", value="{{ $value }}")]
_MAX_RULE = [_rule("Conns", "app_connections > 100", value="{{ $value }}")]


@needs_promtool
def test_sum_two_clusters_under_the_threshold_each_do_not_alert(tmp_path):
    """60 + 60 vs 100：`sum by(tenant)` 會發一則值 120 的告警。"""
    assert_alerts(tmp_path, _SUM_RULE, [
        series("app_queue_jobs_total", 60, da_cluster="east", instance="i1"),
        series("app_queue_jobs_total", 60, da_cluster="west", instance="i1"),
        threshold("app_queue_jobs_total", 100),
    ], {"CustomQueueJobs": []})


@needs_promtool
def test_sum_alerts_for_the_cluster_over_the_threshold_with_its_own_value(tmp_path):
    assert_alerts(tmp_path, _SUM_RULE, [
        series("app_queue_jobs_total", 60, da_cluster="east", instance="i1"),
        series("app_queue_jobs_total", 100, da_cluster="west", instance="i1"),
        series("app_queue_jobs_total", 50, da_cluster="west", instance="i2"),
        threshold("app_queue_jobs_total", 100),
    ], {"CustomQueueJobs": [alert(150, "west")]})


@needs_promtool
def test_max_alerts_for_the_cluster_over_the_threshold_only(tmp_path):
    assert_alerts(tmp_path, _MAX_RULE, [
        series("app_connections", 90, da_cluster="east", instance="i1"),
        series("app_connections", 150, da_cluster="west", instance="i1"),
        series("app_connections", 120, da_cluster="west", instance="i2"),
        threshold("app_connections", 100),
    ], {"CustomConns": [alert(150, "west")]})


@needs_promtool
def test_both_clusters_over_the_threshold_alert_once_each(tmp_path):
    assert_alerts(tmp_path, _MAX_RULE, [
        series("app_connections", 130, da_cluster="east", instance="i1"),
        series("app_connections", 150, da_cluster="west", instance="i1"),
        threshold("app_connections", 100),
    ], {"CustomConns": [alert(130, "east"), alert(150, "west")]})


#: 原式子自己寫的配對：`group_left` 留著左邊的 label（含 tenant）。
_PAIR_EXPR = "app_hit_ratio * on(instance) group_left app_weight > 0.8"

#: 序列沒有 da_cluster（單一 Prometheus）：告警、label、值。同一組輸入與預期
#: 也拿去跑過 origin/main 的產生器（`by(tenant)`），兩邊都過（見 commit 訊息）。
SINGLE_PROMETHEUS_CASES = {
    "sum": (_SUM_RULE, [
        series("app_queue_jobs_total", 60, instance="i1"),
        series("app_queue_jobs_total", 60, instance="i2"),
        threshold("app_queue_jobs_total", 100),
    ], {"CustomQueueJobs": [alert(120)]}),
    "max": (_MAX_RULE, [
        series("app_connections", 90, instance="i1"),
        series("app_connections", 150, instance="i2"),
        threshold("app_connections", 100),
    ], {"CustomConns": [alert(150)]}),
    "under": (_SUM_RULE + _MAX_RULE, [
        series("app_queue_jobs_total", 40, instance="i1"),
        series("app_queue_jobs_total", 40, instance="i2"),
        series("app_connections", 90, instance="i1"),
        threshold("app_queue_jobs_total", 100),
        threshold("app_connections", 100),
    ], {"CustomQueueJobs": [], "CustomConns": []}),
    "pair": ([_rule("Pair", _PAIR_EXPR, value="{{ $value }}")], [
        series("app_hit_ratio", 0.9, instance="i1"), series("app_weight", 1, instance="i1"),
        threshold("app_hit_ratio", 0.8),
    ], {"CustomPair": [alert(0.9)]}),
}


@needs_promtool
@pytest.mark.parametrize("case", sorted(SINGLE_PROMETHEUS_CASES))
def test_without_the_cluster_label_the_alerts_are_what_by_tenant_gave(tmp_path, case):
    rules, input_series, expected = SINGLE_PROMETHEUS_CASES[case]
    assert_alerts(tmp_path, rules, input_series, expected)


@needs_promtool
def test_thresholds_and_maintenance_stay_one_per_tenant(tmp_path):
    """閾值從兩個叢集 federate 上來時身上也帶 da_cluster（契約 3）：`max by(tenant)`
    收成一份、`on(tenant) group_left` 套到每個叢集，不會變成 many-to-many；
    維護狀態 `unless on(tenant)` 一次靜音該租戶的所有叢集。"""
    inputs = [
        series("app_connections", 90, da_cluster="east", instance="i1"),
        series("app_connections", 150, da_cluster="west", instance="i1"),
        threshold("app_connections", 100, da_cluster="east"),
        threshold("app_connections", 100, da_cluster="west"),
    ]
    assert_alerts(tmp_path / "federated", _MAX_RULE, inputs,
                  {"CustomConns": [alert(150, "west")]})
    both = [series("app_connections", 150, da_cluster="east", instance="i1")] + inputs[1:]
    assert_alerts(tmp_path / "maintenance", _MAX_RULE, both + [
        {"series": 'user_state_filter{filter="maintenance",tenant="t1"}', "values": "1x10"},
    ], {"CustomConns": []})


@needs_promtool
def test_a_pairing_with_a_one_per_tenant_series_still_fires(tmp_path):
    """`usage / on(tenant) group_left quota`，右邊是租戶一份、不帶 da_cluster 的
    序列（契約 3 的形狀）。原式子照抄，所以兩個叢集時照樣配得到：超標的叢集
    告警、值是它自己的。把 `on(tenant)` 改寫成 `on(tenant, da_cluster)` 的話
    什麼都配不到，告警悄悄消失——這條規則帶著註記交給人判斷。"""
    rule = _rule("Quota", "app_disk_usage / on(tenant) group_left app_quota_usage > 0.9",
                 value="{{ $value }}")
    results, _rec, _alerts = generate([rule])
    assert results[0].cluster_groupings == ["on(tenant)"]
    assert "on(tenant) group_left app_quota_usage" in results[0].recording_rules[0]["expr"]
    inputs = [
        series("app_disk_usage", 95, da_cluster="east", instance="i1"),
        series("app_disk_usage", 10, da_cluster="west", instance="i1"),
        series("app_quota_usage", 100),
        threshold("app_disk_usage", 0.9),
    ]
    assert_alerts(tmp_path / "two", [rule], inputs, {"CustomQuota": [alert(0.95, "east")]})
    # 單一 Prometheus：與 by(tenant) 的產物相同
    assert_alerts(tmp_path / "one", [rule], [
        series("app_disk_usage", 95, instance="i1"), series("app_quota_usage", 100),
        threshold("app_disk_usage", 0.9),
    ], {"CustomQuota": [alert(0.95)]})


@needs_promtool
def test_an_original_on_instance_fails_on_two_clusters_and_carries_the_note(tmp_path):
    """已知、未在這裡解決：原式子的 `on(instance)` 照抄，兩個叢集位址相同的
    `i1` 配成 many-to-many，規則求值失敗（main 的產物也一樣）。工具做的是
    標出來：這條規則帶註記。"""
    results, recording, alerts = generate([_rule("Pair", _PAIR_EXPR)])
    assert results[0].cluster_groupings == ["on(instance)"]
    assert mr.cluster_grouping_note(["on(instance)"]) in recording
    proc = run_promtool(tmp_path, recording, alerts, [
        series("app_hit_ratio", 0.5, da_cluster="east", instance="i1"),
        series("app_weight", 1, da_cluster="east", instance="i1"),
        series("app_hit_ratio", 0.9, da_cluster="west", instance="i1"),
        series("app_weight", 1, da_cluster="west", instance="i1"),
        threshold("app_hit_ratio", 0.8),
    ], {"CustomPair": []})
    assert proc.returncode != 0
    assert "many-to-many matching not allowed" in proc.stdout + proc.stderr


@needs_promtool
def test_the_alert_names_its_cluster_in_the_annotation(tmp_path):
    rules = [_rule("Conns", "app_connections > 100", value="{{ $value }}",
                   where="{{ $labels.da_cluster }}", host="{{ $labels.instance }}")]
    assert_alerts(tmp_path, rules, [
        series("app_connections", 150, da_cluster="west", instance="i1"),
        threshold("app_connections", 100),
    ], {"CustomConns": [alert(150, "west", where="west",
                              host="t1（原為 instance，已依租戶聚合）")]})


# ---------------------------------------------------------------------------
# 6. validate：以（租戶、叢集）對值
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
