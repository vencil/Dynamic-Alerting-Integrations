"""migrate 產物的 annotation 與 validate 的比對組（issue 1818）。

兩件事在 PR 2203 之後仍成立：

1. 遷移後的查詢是 `<agg> by(tenant) (...)`，告警的 $labels 只剩 tenant。
   原規則 annotation 裡的 `{{ $labels.instance }}`、`{{ $labels.queue }}`
   照抄會渲染成空字串（promtool 實測 `summary="Redis queue  is too long"`）。
   owner 裁決保留 by(tenant)：被等值 matcher 定死的 label 代入字面值；其他
   改讀 `$labels.tenant` 並警告。
2. `validate --mapping` 的新查詢固定組成 `tenant:<key>:max`，rate 類產物是
   `:sum`，查不到；舊查詢是原始指標的原值，沒套 rate()，量綱不同。

另外，產物把 annotation 直接包一層雙引號寫出：常見的
`{{ $value | printf "%.2f" }}` 會讓整份 platform-alert-rules.yaml parse 失敗。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

_OPS = Path(__file__).resolve().parents[2] / "scripts" / "tools" / "ops"
sys.path.insert(0, str(_OPS))

import migrate_rule as mr  # noqa: E402
import validate_migration as vm  # noqa: E402

_REPO = Path(__file__).resolve().parents[2]
_MULTIDB = _REPO / "tests" / "fixtures" / "legacy-multidb.yml"


# ---------------------------------------------------------------------------
# 哪些 label 被定死
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("lhs,expected", [
    ('redis_queue_length{queue="order-processing"}', {"queue": "order-processing"}),
    ('rate(x_total{a="1", b="2"}[5m])', {"a": "1", "b": "2"}),
    ('x{a!="1"}', {}),
    ('x{a=~"1|2"}', {}),
    ('x', {}),
    # 兩個指標：另一邊的 series 可能帶別的值
    ('x{a="1"} / y', {}),
    # 同一指標兩個 selector 釘成不同值
    ('x{a="1"} - x{a="2"}', {}),
    ('x{a="1"} - x{a="1", b="3"}', {"a": "1"}),
])
def test_static_label_values(lhs, expected):
    assert mr.static_label_values(lhs) == expected


# ---------------------------------------------------------------------------
# 改寫
# ---------------------------------------------------------------------------

def test_pinned_label_becomes_its_literal_value():
    out, dropped = mr.rewrite_dropped_label_refs(
        {"summary": "Redis queue {{ $labels.queue }} is too long"},
        'redis_queue_length{queue="order-processing"}')
    assert out == {"summary": "Redis queue order-processing is too long"}
    assert dropped == []


def test_unpinned_label_reads_tenant_and_says_so():
    out, dropped = mr.rewrite_dropped_label_refs(
        {"summary": "Too many connections on {{ $labels.instance }}"},
        "mysql_global_status_threads_connected")
    assert out["summary"] == ("Too many connections on {{ $labels.tenant }}"
                              "（原為 instance，已依租戶聚合）")
    assert dropped == ["instance"]


def test_label_inside_a_larger_action_is_rewritten_in_place():
    out, dropped = mr.rewrite_dropped_label_refs(
        {"a": '{{ $labels.pod | toUpper }} / {{ printf "%s" $labels.queue }}'},
        'x{queue="q1"}')
    assert out["a"] == '{{ $labels.tenant | toUpper }} / {{ printf "%s" "q1" }}'
    assert dropped == ["pod"]


def test_tenant_alertname_and_rule_labels_are_left_alone():
    templates = {"s": "{{ $labels.tenant }} {{ $labels.alertname }} {{ $labels.team }}"}
    out, dropped = mr.rewrite_dropped_label_refs(templates, "x", keep={"team"})
    assert out == templates and dropped == []


def test_label_values_get_no_explanation():
    out, _ = mr.rewrite_dropped_label_refs(
        {"host": "{{ $labels.instance }}"}, "x", explain=False)
    assert out == {"host": "{{ $labels.tenant }}"}


def test_process_rule_reports_the_rewrite():
    rule = {"alert": "HighConn", "expr": "mysql_global_status_threads_connected > 100",
            "labels": {"severity": "warning"},
            "annotations": {"summary": "on {{ $labels.instance }}"}}
    r = mr.process_rule(rule, prefix="custom_", dictionary={}, use_ast=True)
    assert r.dropped_label_refs == ["instance"]
    assert any("instance" in n and "$labels.tenant" in n for n in r.notes)
    out = mr.render_alert_rules([r], "custom_")
    assert "# ⚠️ 原 annotation／label 引用的 instance" in out


# ---------------------------------------------------------------------------
# 產物是合法 YAML，而且值與模板逐字相同
# ---------------------------------------------------------------------------

def test_alert_rules_with_quotes_and_templated_labels_parse_back():
    rule = {"alert": "HighConn", "expr": "mysql_global_status_threads_connected > 100",
            "labels": {"severity": "warning", "team": "db", "page": "true",
                       "who": "{{ $labels.tenant }}"},
            "annotations": {"summary": 'Conn {{ $value | printf "%.1f" }} \\ ok',
                            "zh": "連線數過高"}}
    r = mr.process_rule(rule, prefix="custom_", dictionary={}, use_ast=True)
    doc = yaml.safe_load(mr.render_alert_rules([r], "custom_"))
    got = doc["groups"][0]["rules"][0]
    assert got["annotations"] == rule["annotations"]
    assert got["labels"]["who"] == "{{ $labels.tenant }}"
    # "true" 不加引號會被讀成布林，Prometheus 拒收非字串的 label 值
    assert got["labels"]["page"] == "true"
    assert got["labels"]["team"] == "db"


# ---------------------------------------------------------------------------
# validate 的比對組
# ---------------------------------------------------------------------------

def _migrate(tmp_path, fixture=_MULTIDB):
    out = tmp_path / "out"
    proc = subprocess.run(
        [sys.executable, str(_OPS / "migrate_rule.py"), str(fixture), "-o", str(out),
         "--no-dictionary"],
        capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return out


def test_mapping_pairs_point_at_the_real_record_and_keep_rate(tmp_path):
    out = _migrate(tmp_path)
    records = {r["record"] for g in yaml.safe_load(
        (out / "platform-recording-rules.yaml").read_text(encoding="utf-8"))["groups"]
        for r in g["rules"]}
    pairs = {p["label"]: p for p in vm.load_mapping_pairs(str(out / "prefix-mapping.yaml"))}
    assert pairs, "prefix-mapping 沒有任何比對組"
    for label, p in pairs.items():
        assert p["new_query"] in records, (label, p["new_query"])
    ev = pairs["custom_redis_evicted_keys_total"]
    assert ev["new_query"] == "tenant:custom_redis_evicted_keys_total:sum"
    assert ev["old_query"] == "sum by(tenant) (rate(redis_evicted_keys_total[5m]))"


def test_legacy_mapping_file_falls_back_and_warns(tmp_path, capsys):
    p = tmp_path / "prefix-mapping.yaml"
    p.write_text(yaml.safe_dump({
        "custom_x": {"original_metric": "x", "alert_name": "A"},
        "custom_x_critical": {"original_metric": "x", "alert_name": "B"},
    }), encoding="utf-8")
    pairs = vm.load_mapping_pairs(str(p))
    assert [(q["old_query"], q["new_query"]) for q in pairs] == [
        ("x", "tenant:custom_x:max"), ("x", "tenant:custom_x:max")]
    err = capsys.readouterr().err
    assert "old_query" in err and "custom_x_critical" in err


def test_new_mapping_file_is_silent(tmp_path, capsys):
    p = tmp_path / "prefix-mapping.yaml"
    p.write_text(yaml.safe_dump({"custom_x": {
        "original_metric": "x", "old_query": "sum by(tenant) (x)",
        "new_query": "tenant:custom_x:sum"}}), encoding="utf-8")
    assert [(q["old_query"], q["new_query"]) for q in vm.load_mapping_pairs(str(p))] == [
        ("sum by(tenant) (x)", "tenant:custom_x:sum")]
    assert capsys.readouterr().err == ""


# ---------------------------------------------------------------------------
# promtool 端到端
# ---------------------------------------------------------------------------

@pytest.mark.skipif(shutil.which("promtool") is None, reason="promtool not on PATH")
def test_annotations_and_pairs_under_promtool(tmp_path):
    """annotation 渲染出值；rate 類比對組的新舊兩邊在同一時間點相等。"""
    out = _migrate(tmp_path)
    ev = {p["label"]: p for p in vm.load_mapping_pairs(
        str(out / "prefix-mapping.yaml"))}["custom_redis_evicted_keys_total"]
    test = {
        "rule_files": [str(out / "platform-recording-rules.yaml"),
                       str(out / "platform-alert-rules.yaml")],
        "evaluation_interval": "1m",
        "tests": [{
            "interval": "1m",
            "input_series": [
                {"series": 'redis_queue_length{tenant="t1",queue="order-processing",instance="r1"}',
                 "values": "600x20"},
                {"series": 'redis_memory_used_bytes{tenant="t1",instance="r1"}',
                 "values": "9000000000x20"},
                {"series": 'user_threshold{component="custom",metric="redis_queue_length",'
                           'severity="warning",tenant="t1"}', "values": "500x20"},
                {"series": 'user_threshold{component="custom",metric="redis_memory_used_bytes",'
                           'severity="warning",tenant="t1"}', "values": "8589934592x20"},
                {"series": 'redis_evicted_keys_total{tenant="t1",instance="a"}',
                 "values": "0+60x20"},
                {"series": 'redis_evicted_keys_total{tenant="t1",instance="b"}',
                 "values": "0+120x20"},
            ],
            "alert_rule_test": [
                {"eval_time": "15m", "alertname": "CustomRedisQueueTooLong",
                 "exp_alerts": [{"exp_labels": {"tenant": "t1", "severity": "critical",
                                                "source": "legacy",
                                                "migration_status": "shadow"},
                                 "exp_annotations": {
                                     "summary": "Redis queue order-processing is too long"}}]},
                {"eval_time": "15m", "alertname": "CustomRedisHighMemory",
                 "exp_alerts": [{"exp_labels": {"tenant": "t1", "severity": "warning",
                                                "source": "legacy",
                                                "migration_status": "shadow"},
                                 "exp_annotations": {
                                     "summary": "Redis memory usage too high on t1"
                                                "（原為 instance，已依租戶聚合）"}}]},
            ],
            "promql_expr_test": [
                {"expr": ev["old_query"], "eval_time": "15m",
                 "exp_samples": [{"labels": '{tenant="t1"}', "value": 3}]},
                {"expr": ev["new_query"], "eval_time": "15m",
                 "exp_samples": [{"labels": f'{ev["new_query"]}{{tenant="t1"}}',
                                  "value": 3}]},
            ],
        }],
    }
    test_file = tmp_path / "t.yaml"
    test_file.write_text(yaml.safe_dump(test, allow_unicode=True), encoding="utf-8")
    proc = subprocess.run(["promtool", "test", "rules", str(test_file)],
                          capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_golden_entries_get_no_pair(tmp_path, capsys):
    """改用黃金標準的規則沒有 recording rule；舊檔對它們組出的 record 不存在。"""
    out = tmp_path / "out"
    proc = subprocess.run(
        [sys.executable, str(_OPS / "migrate_rule.py"),
         str(_REPO / "tests" / "fixtures" / "legacy-dummy.yml"), "-o", str(out)],
        capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    mapping = yaml.safe_load((out / "prefix-mapping.yaml").read_text(encoding="utf-8"))
    golden = [k for k, v in mapping.items() if v.get("golden_rule")]
    assert golden, "fixture 應有字典判定改用黃金標準的項"
    assert all("new_query" not in mapping[k] for k in golden)
    labels = {p["label"] for p in vm.load_mapping_pairs(str(out / "prefix-mapping.yaml"))}
    assert not labels & set(golden)
    err = capsys.readouterr().err
    assert "黃金標準" in err and "old_query" not in err


def test_non_mapping_entries_are_skipped(tmp_path):
    """手改壞的項（值不是 mapping）跳過，不讓整個檔載入失敗。"""
    p = tmp_path / "prefix-mapping.yaml"
    p.write_text(yaml.safe_dump({
        "custom_bad": "not-a-mapping",
        "custom_x": {"original_metric": "x", "old_query": "max by(tenant) (x)",
                     "new_query": "tenant:custom_x:max"}}), encoding="utf-8")
    assert [q["label"] for q in vm.load_mapping_pairs(str(p))] == ["custom_x"]
