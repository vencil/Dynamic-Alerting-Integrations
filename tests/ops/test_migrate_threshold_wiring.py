"""migrate 產出的規則與 threshold-exporter 真的接得起來（issue 1818）。

migrate 產出三件套：租戶閾值、recording rule、alert rule。三者要在 runtime
接起來，必須同時成立：

1. recording rule 讀的是客戶**已經有**的指標。先前的產物把來源指標也加上
   `custom_` 前綴（`custom_mysql_global_status_threads_connected`），而沒有
   任何東西會產生這個名字的 series，於是 recording rule 恆為空。
2. 閾值 recording rule 的 selector 要對上 exporter 發射的 label。exporter 以
   key 的第一個 `_` 拆成 `component` 與 `metric`（`pkg/config/parse.go` 的
   `parseMetricKey`），先前的產物卻寫 `user_threshold{metric="<整個 key>"}`。
3. key 要在 `_defaults.yaml` 宣告過。沒宣告時 exporter 只記一行
   `unknown key ... not in defaults`，不發射任何 series。

三者任一不成立，產出的告警就永遠不會響，而且沒有任何錯誤訊息。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

_TOOLS_DIR = os.path.join(os.path.dirname(__file__), '..', '..', 'scripts', 'tools', 'ops')
sys.path.insert(0, _TOOLS_DIR)

import migrate_rule as mr  # noqa: E402

_REPO = Path(__file__).resolve().parents[2]
_FIXTURE = _REPO / "tests" / "fixtures" / "legacy-dummy.yml"

# 與 components/threshold-exporter/app/config_resolve_test.go 的
# TestParseMetricKey 同一組向量，外加 migrate 會產生的 custom_ key 與
# parse.go 註解裡寫明的「開頭就是 _」邊界。
_EXPORTER_SPLIT_VECTORS = [
    ("mysql_connections", "mysql", "connections"),
    ("mysql_threads_running", "mysql", "threads_running"),
    ("container_cpu_percent", "container", "cpu_percent"),
    ("standalone", "default", "standalone"),
    ("_leading", "default", "_leading"),
    ("custom_mysql_global_status_threads_connected",
     "custom", "mysql_global_status_threads_connected"),
]


@pytest.mark.parametrize("key,component,metric", _EXPORTER_SPLIT_VECTORS)
def test_split_threshold_key_matches_the_exporter(key, component, metric):
    assert mr.split_threshold_key(key) == (component, metric)


def _process(expr, severity="warning", prefix="custom_"):
    rule = {"alert": "T", "expr": expr, "labels": {"severity": severity}}
    return mr.process_rule(rule, prefix=prefix, dictionary={}, use_ast=True)


def _threshold_expr(result):
    return next(r["expr"] for r in result.recording_rules
                if r["record"].startswith("tenant:alert_threshold:"))


@pytest.mark.parametrize("severity,suffix", [("warning", ""), ("critical", "_critical")])
def test_threshold_selector_uses_the_labels_the_exporter_emits(severity, suffix):
    result = _process("mysql_global_status_threads_connected > 150", severity)
    assert f"custom_mysql_global_status_threads_connected{suffix}" in result.tenant_config
    assert _threshold_expr(result) == (
        'max by(tenant) (user_threshold{component="custom", '
        'metric="mysql_global_status_threads_connected", '
        f'severity="{severity}"}})'
    )


def test_threshold_selector_without_prefix_splits_the_bare_key():
    result = _process("mysql_global_status_threads_connected > 150", prefix="")
    assert 'component="mysql", metric="global_status_threads_connected"' in _threshold_expr(result)


@pytest.mark.parametrize("expr,metric", [
    ("mysql_global_status_threads_connected > 150", "mysql_global_status_threads_connected"),
    ("rate(mysql_global_status_slow_queries[5m]) > 0.5", "mysql_global_status_slow_queries"),
])
def test_recording_rule_reads_the_original_metric(expr, metric):
    result = _process(expr)
    lhs = result.recording_rules[0]["expr"]
    assert f'{metric}{{tenant=~".+"}}' in lhs
    assert f"custom_{metric}" not in lhs


def _run_migrate(tmp_path):
    # --no-dictionary：fixture 的指標在 metric-dictionary.yaml 裡有黃金標準，
    # 字典讀得到時會改判 use_golden、不產出 custom_ 三件套。這裡測的是三件套
    # 本身接不接得上 exporter，與字典無關。
    out = tmp_path / "out"
    proc = subprocess.run(
        [sys.executable, str(Path(_TOOLS_DIR) / "migrate_rule.py"), str(_FIXTURE),
         "-o", str(out), "--no-dictionary"],
        capture_output=True, text=True, encoding="utf-8", timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    return out, proc.stdout


def test_defaults_snippet_declares_every_key_with_a_number(tmp_path):
    out, stdout = _run_migrate(tmp_path)
    snippet = yaml.safe_load((out / "defaults-snippet.yaml").read_text(encoding="utf-8"))
    # `<base>_critical` 不進 defaults：exporter 會把它當成另一個 base 指標、
    # 發射成 severity="warning"，不是 critical 層。
    assert snippet == {"defaults": {
        "custom_mysql_global_status_threads_connected": 150,
        "custom_mysql_global_status_slow_queries": 0.5,
    }}
    assert "defaults-snippet.yaml" in stdout


def test_defaults_snippet_declares_the_base_key_for_a_critical_only_rule(tmp_path):
    rules = tmp_path / "rules.yml"
    rules.write_text(
        "groups:\n- name: g\n  rules:\n"
        "  - alert: OnlyCritical\n    expr: mysql_global_status_threads_connected > 200\n"
        "    labels: {severity: critical}\n",
        encoding="utf-8")
    out = tmp_path / "out"
    proc = subprocess.run(
        [sys.executable, str(Path(_TOOLS_DIR) / "migrate_rule.py"), str(rules), "-o", str(out),
         "--no-dictionary"],
        capture_output=True, text=True, encoding="utf-8", timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    defaults = yaml.safe_load(
        (out / "defaults-snippet.yaml").read_text(encoding="utf-8"))["defaults"]
    # exporter 只在 base key 存在時才發射 critical 那一列
    # （resolveCriticalRows；沒有 base 時整條 critical 被丟掉）。
    assert defaults == {"custom_mysql_global_status_threads_connected": 200}


def test_defaults_snippet_never_carries_a_critical_or_dimensional_key():
    """check_threshold_reachability 的豁免理由靠這兩點成立（issue 1412 的形狀）。"""
    rules = [
        {"alert": "W", "expr": 'mysql_x{queue="tasks"} > 5', "labels": {"severity": "warning"}},
        {"alert": "C", "expr": 'mysql_x{queue="tasks"} > 9', "labels": {"severity": "critical"}},
    ]
    results = [mr.process_rule(r, prefix="custom_", dictionary={}, use_ast=True) for r in rules]
    assert any(r.dim_hints for r in results)
    defaults = yaml.safe_load(mr.render_defaults_snippet(results))["defaults"]
    assert defaults == {"custom_mysql_x": 5}
    assert not any(k.endswith("_critical") or "{" in k for k in defaults)


def test_prefix_mapping_critical_entry_names_the_real_metric(tmp_path):
    out, _ = _run_migrate(tmp_path)
    mapping = yaml.safe_load((out / "prefix-mapping.yaml").read_text(encoding="utf-8"))
    assert (mapping["custom_mysql_global_status_threads_connected_critical"]["original_metric"]
            == "mysql_global_status_threads_connected")


@pytest.mark.skipif(shutil.which("promtool") is None, reason="promtool not on PATH")
def test_generated_rules_fire_end_to_end_under_promtool(tmp_path):
    """用 promtool 跑 migrate 的真實產物。

    輸入的 user_threshold 是 exporter 對 defaults-snippet 的 key 實際發射的
    形狀（component／metric 依 parseMetricKey 拆開）。舊產物在同一組輸入下
    三條 recording rule 都是空集合。
    """
    out, _ = _run_migrate(tmp_path)
    test_file = tmp_path / "t.yaml"
    test_file.write_text(yaml.safe_dump({
        "rule_files": [str(out / "platform-recording-rules.yaml"),
                       str(out / "platform-alert-rules.yaml")],
        "evaluation_interval": "1m",
        "tests": [{
            "interval": "1m",
            "input_series": [
                {"series": 'mysql_global_status_threads_connected{tenant="t1",instance="i1"}',
                 "values": "210x15"},
                {"series": 'user_threshold{component="custom",'
                           'metric="mysql_global_status_threads_connected",'
                           'severity="warning",tenant="t1"}',
                 "values": "150x15"},
                {"series": 'user_threshold{component="custom",'
                           'metric="mysql_global_status_threads_connected",'
                           'severity="critical",tenant="t1"}',
                 "values": "200x15"},
            ],
            "alert_rule_test": [
                {"eval_time": "10m", "alertname": "CustomMySQLTooManyConnections",
                 "exp_alerts": [{"exp_labels": {
                     "tenant": "t1", "severity": "warning", "source": "legacy",
                     "migration_status": "shadow", "metric_group": "connected"},
                     # 原規則的 {{ $labels.instance }} 在 by(tenant) 聚合後已不存在
                     "exp_annotations": {"summary": "Too many connections on "}}]},
                {"eval_time": "10m", "alertname": "CustomMySQLTooManyConnectionsCritical",
                 "exp_alerts": [{"exp_labels": {
                     "tenant": "t1", "severity": "critical", "source": "legacy",
                     "migration_status": "shadow", "metric_group": "connected"},
                     "exp_annotations": {"summary": "Critical connections on "}}]},
            ],
        }],
    }), encoding="utf-8")
    proc = subprocess.run(["promtool", "test", "rules", str(test_file)],
                          capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr
