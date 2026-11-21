#!/usr/bin/env python3
"""test_lint_custom_rules.py — CI Deny-list Linter pytest 測試套件 (Phase 10)。

驗證 lint_custom_rules.py 的核心功能:
  1. Denied function 偵測
  2. preserved_labels：alert 的結果保留 tenant（#4098，Prometheus 語法樹）
  3. Required label 檢查
  4. Range vector duration 超限
  5. ConfigMap wrapper 解析
  6. Policy 載入與合併
  7. Duration parsing
  8. Expiry / owner label 檢查

用法:
  python3 -m pytest tests/test_lint_custom_rules.py -v
"""

import itertools
import json
import os
import sys
import tempfile

import pytest
import yaml

# Add scripts/tools to path

import lint_custom_rules  # noqa: E402
import _lib_exitcodes  # noqa: E402
from _lib_python import parse_duration_seconds  # noqa: E402


# ── Shared Fixture ────────────────────────────────────────────────

#: The compiled pack's lookback cap: the override in the built-in policy
#: (#4099: written once, by gen_recipe_contract, into the policy file).
FORECAST_MAX_LOOKBACK = lint_custom_rules.builtin_policy()["file_overrides"][0]["policy"]["max_range_duration"]

@pytest.fixture
def policy():
    """提供預設 lint 政策。"""
    return lint_custom_rules.builtin_policy()


# ── 1. Duration parsing ──────────────────────────────────────────

@pytest.mark.parametrize("value, seconds", [
    ("30s", 30), ("5m", 300), ("1h", 3600), ("2d", 172800),
    # #4061: Prometheus' grammar (model.ParseDuration), not single-unit only
    ("1h30m", 5400), ("2w", 1209600), ("1y", 31536000), ("500ms", 0.5),
    # what Prometheus refuses: no unit, a fraction, units out of order, text
    (60, None), ("60", None), ("1.5h", None), ("1m1h", None), ("abc", None), ("", None),
])
def test_durations_are_read_as_prometheus_reads_them(value, seconds):
    assert lint_custom_rules.duration_seconds(value) == seconds


def test_lint_quantile_over_time_detected(policy):
    """測試 quantile_over_time 函數偵測（holt_winters 在 Prometheus 3 已不是函式，
    parser 直接拒收，見 test_override_keeps_other_checks_live）。"""
    results = lint_custom_rules.lint_expr(
        "quantile_over_time(0.9, my_metric[1h])", policy, "test.yaml", "TestRule"
    )
    errors = [r for r in results if "quantile_over_time" in r.message]
    assert len(errors) == 1

def test_lint_predict_linear_detected(policy):
    """測試 predict_linear 函數偵測。"""
    results = lint_custom_rules.lint_expr(
        "predict_linear(disk_free[1h], 3600) < 0", policy, "test.yaml", "TestRule"
    )
    errors = [r for r in results if "predict_linear" in r.message]
    assert len(errors) == 1

def test_lint_safe_function_passes(policy):
    """測試安全函數通過檢查。"""
    results = lint_custom_rules.lint_expr(
        "rate(http_requests_total[5m]) > 100", policy, "test.yaml", "TestRule"
    )
    func_errors = [r for r in results if "denied function" in r.message]
    assert len(func_errors) == 0

def test_lint_function_name_not_substring(policy):
    """測試 metric 名稱中包含函數名稱不被誤判。"""
    results = lint_custom_rules.lint_expr(
        "my_predict_linear_metric > 100", policy, "test.yaml", "TestRule"
    )
    func_errors = [r for r in results if "predict_linear" in r.message]
    assert len(func_errors) == 0


# ── 2. preserved_labels (#4098) ───────────────────────────────────

def _lint_alert(tmp_path, expr, tenant="{{ $labels.tenant }}", records=None, name="r.yaml"):
    """lint_file of one alert *expr* (its `tenant` label *tenant*; None for
    none) beside the recording rules *records* ({name: expr})."""
    rules = [{"record": n, "expr": e} for n, e in (records or {}).items()]
    labels = {"owner": "o", "expiry": "2099-01-01"}
    if tenant is not None:
        labels["tenant"] = tenant
    rules.append({"alert": "A", "expr": expr, "labels": labels})
    f = tmp_path / name
    f.write_text(yaml.safe_dump({"groups": [{"name": "g", "rules": rules}]}), encoding="utf-8")
    results, _ = lint_custom_rules.lint_file(str(f), lint_custom_rules.builtin_policy())
    return [r.message for r in results if r.severity == "ERROR"]


DROPS = "the result drops label 'tenant'"
UNPROVEN = "cannot prove the result keeps label 'tenant'"


@pytest.mark.parametrize("expr", [
    # every spelling the text match walked past (#4098), and the shapes it
    # never named: the alert's `{{ $labels.tenant }}` renders empty for all
    "sum without(tenant)(x)", "sum(x) without(tenant)", "sum without(tenant, b)(x)",
    "sum without(b, tenant)(x)", 'sum without("tenant")(x)', "sum without(tenant,)(x)",
    "sum by (a) (x)", "sum(x)", 'sum(x{tenant="t"}) by (a)', "x * ignoring(tenant) y",
    "x * on(a) y", "vector(1)", "x or vector(0)", 'absent(nope{tenant="t1", tenant!="t2"})',
    'count_values without(tenant) ("v", x)',
])
def test_a_result_that_drops_tenant_is_an_error(tmp_path, expr):
    assert any(DROPS in m for m in _lint_alert(tmp_path, expr)), expr


@pytest.mark.parametrize("expr", [
    "x > 0", "sum by (tenant) (x)", "sum by (tenant, a) (x)", "topk without(tenant) (3, x)",
    "max_over_time(x[5m])", "x * on() group_left y", "x / scalar(sum(y))",
    "x and on() (count(y) > 0)", 'absent(nope{tenant="t1"})',
    # #4098: `=~".*"` selects what the selector without it selects — it was a
    # finding of the text match, not of anything the selector reads
    'x{a=~".*"} > 0', "x{tenant=~'.*'} > 0",
])
def test_a_result_that_keeps_tenant_passes(tmp_path, expr):
    assert _lint_alert(tmp_path, expr) == [], expr


@pytest.mark.parametrize("expr", [
    'label_replace(x, "tenant", "", "tenant", ".*")', 'label_join(x, "tenant", "", "a")',
    'x{tenant!=""} * on() group_left(tenant) y', "tenant:not_in_this_run:sum > 0",
    '{__name__=~"tenant:z:.*"} > 0',
])
def test_a_result_the_parse_tree_cannot_prove_is_an_error(tmp_path, expr):
    assert any(UNPROVEN in m for m in _lint_alert(tmp_path, expr)), expr


def test_a_record_that_dropped_tenant_cannot_hide_it(tmp_path):
    assert any(DROPS in m for m in _lint_alert(
        tmp_path, "job:x:sum > 0", records={"job:x:sum": "sum by (job) (x)"}))
    assert _lint_alert(tmp_path, "tenant:x:sum > 0",
                       records={"tenant:x:sum": "sum by (tenant) (x)"}) == []


@pytest.mark.parametrize("tenant, finding", [
    (None, DROPS),                              # no label: the series' own value
    ("{{ $labels.tenant }}", DROPS),
    ("{{$labels.tenant}}", DROPS),
    ("{{ .Labels.tenant }}", DROPS),
    ("db-a", None),                             # a literal: the alert sets it
    ("", None),                                 # no tenant, written so
    ("{{ $labels.namespace }}", "is a template that does not copy"),
    ("x-{{ $labels.tenant }}", "is a template that does not copy"),
])
def test_what_the_alert_writes_as_tenant_decides_the_check(tmp_path, tenant, finding):
    errors = _lint_alert(tmp_path, "sum(x) > 0", tenant=tenant)
    if finding is None:
        assert not any("tenant'" in m or "template" in m for m in errors), errors
    else:
        assert any(finding in m for m in errors), errors


def test_a_record_of_another_file_of_the_run_is_followed(tmp_path, monkeypatch, capsys):
    (tmp_path / "a.yaml").write_text(yaml.safe_dump({"groups": [{"name": "g", "rules": [
        {"record": "job:x:sum", "expr": "sum by (job) (x)"}]}]}), encoding="utf-8")
    _lint_alert(tmp_path, "job:x:sum > 0", name="b.yaml")
    assert _run_main([str(tmp_path), "--ci"], monkeypatch) == 1
    assert DROPS in capsys.readouterr().out
    # alone, the record is unseen: not proven, never taken for a raw metric
    assert _run_main([str(tmp_path / "b.yaml"), "--ci"], monkeypatch) == 1
    assert UNPROVEN in capsys.readouterr().out


def test_a_record_of_a_shipped_rule_pack_is_followed(tmp_path, monkeypatch):
    """#4098 review: the generated CI lints rule-packs/custom/ alone, and a
    custom rule reads the packs' `tenant:…` records — the shipped packs are
    the records it may read without linting them with it."""
    assert _lint_alert(tmp_path, 'tenant:mysql_connection_usage:percent{tenant="db-a"} > 95') == []
    pack = tmp_path / "pack" / "rule-pack-x.yaml"
    pack.parent.mkdir()
    pack.write_text(yaml.safe_dump({"groups": [{"name": "g", "rules": [
        {"record": "pack:x:sum", "expr": "sum(x)"}]}]}), encoding="utf-8")
    monkeypatch.setattr(lint_custom_rules, "shipped_pack_paths", lambda linted=(): [str(pack)])
    assert any(DROPS in m for m in _lint_alert(tmp_path, "pack:x:sum > 0"))
    monkeypatch.setattr(lint_custom_rules, "shipped_pack_paths", lambda linted=(): [])
    assert any(UNPROVEN in m for m in _lint_alert(tmp_path, "pack:x:sum > 0"))


def test_the_shipped_packs_are_the_tenant_packs_not_in_the_run():
    shipped = lint_custom_rules.shipped_pack_paths()
    names = {os.path.basename(p) for p in shipped}
    assert "rule-pack-mariadb.yaml" in names and "rule-pack-platform.yaml" not in names, names
    mariadb = [p for p in shipped if p.endswith("rule-pack-mariadb.yaml")]
    assert "rule-pack-mariadb.yaml" not in {
        os.path.basename(p) for p in lint_custom_rules.shipped_pack_paths(mariadb)}


def test_group_labels_are_the_alerts_labels_too(tmp_path):
    """#4098 review: Prometheus writes a group's `labels:` on its alerts
    (rules/manager.go), each a template: a group-level tenant decides as a
    rule-level one does, the rule's own winning."""
    def lint(group_labels, rule_tenant=None):
        rule = {"alert": "A", "expr": "sum(x) > 0", "labels": {"owner": "o", "expiry": "2099-01-01"}}
        if rule_tenant is not None:
            rule["labels"]["tenant"] = rule_tenant
        f = tmp_path / "g.yaml"
        f.write_text(yaml.safe_dump({"groups": [{"name": "g", "labels": group_labels,
                                                 "rules": [rule]}]}), encoding="utf-8")
        policy = dict(lint_custom_rules.builtin_policy(), required_labels=[])
        results, _ = lint_custom_rules.lint_file(str(f), policy)
        return [r.message for r in results if r.severity == "ERROR"]
    assert any("is a template that does not copy" in m
               for m in lint({"tenant": "{{ $labels.namespace }}"}))
    assert lint({"tenant": "db-a"}) == []
    assert any(DROPS in m for m in lint({"tenant": "db-a"}, "{{ $labels.tenant }}"))


def test_a_records_group_labels_reach_it(tmp_path):
    """#4098 review: a group's `labels: {tenant: ""}` removes tenant from the
    records it writes; the rule's own labels win."""
    f = tmp_path / "r.yaml"
    f.write_text(yaml.safe_dump({"groups": [{"name": "g", "labels": {"tenant": ""}, "rules": [
        {"record": "tenant:x:max", "expr": "max by (tenant) (x)"},
        {"record": "tenant:y:max", "expr": "max by (tenant) (y)", "labels": {"tenant": "t1", "a": "b"}},
    ]}]}), encoding="utf-8")
    recs = lint_custom_rules.rule_records(lint_custom_rules.read_rule_files([str(f)])[str(f)])
    assert recs["tenant:x:max"][0]["labels"] == {"tenant": ""}
    assert recs["tenant:y:max"][0]["labels"] == {"tenant": "t1", "a": "b"}


# ── #4099: denied_functions names a function a rule can call ─────────────

@pytest.mark.parametrize("name, ok", [
    ("predict_linear", True), ("topk", True), ("count_values", True), ("limitk", False),
    ("predict_lineer", False),                 # a typo denied nothing
    ("holt_winters", False),                   # gone in Prometheus 3
    ("double_exponential_smoothing", False),   # experimental, not enabled
    ("step", False),                           # spelt as `step()` in a range, never a call
    ("SUM", False), ("and", False),
])
def test_a_denied_name_no_rule_can_call_is_refused(name, ok):
    problems = lint_custom_rules.denied_function_problems({"denied_functions": [name]})
    assert (problems == []) is ok, problems
    if not ok:
        assert problems == [f"denied_functions has {name!r}, which no rule can call: "
                            "Prometheus' parser (as it loads rule files, no experimental "
                            "features) has no function or aggregation of that name"]


def test_an_overrides_denied_names_are_judged_too():
    policy = {"denied_functions": ["rate"], "file_overrides": [
        {"path": "a.yaml", "policy": {"denied_functions": ["quantile_over_time", "nope"]}}]}
    assert lint_custom_rules.denied_function_problems(policy) == [
        "file_overrides[0].policy.denied_functions has 'nope', which no rule can call: "
        "Prometheus' parser (as it loads rule files, no experimental features) has no "
        "function or aggregation of that name"]


def test_the_built_in_policy_denies_only_names_a_rule_can_call():
    assert lint_custom_rules.denied_function_problems(lint_custom_rules.load_policy(None)) == []


def test_a_policy_denying_an_uncallable_name_is_rc_2_before_any_file_is_read(
        tmp_path, monkeypatch, capsys):
    pol = tmp_path / "p.yaml"
    pol.write_text("denied_functions: [predict_lineer]\n", encoding="utf-8")
    (tmp_path / "r").mkdir()
    (tmp_path / "r" / "a.yaml").write_text(_OK_RULE, encoding="utf-8")
    read = []
    monkeypatch.setattr(lint_custom_rules, "read_rule_files", lambda files: read.append(files))
    assert _run_main([str(tmp_path / "r"), "--policy", str(pol), "--ci"], monkeypatch) == 2
    assert "predict_lineer" in capsys.readouterr().err
    assert read == []


def test_no_da_read_for_the_names_is_a_caller_error_not_a_crash(tmp_path, monkeypatch, capsys):
    (tmp_path / "a.yaml").write_text(_OK_RULE, encoding="utf-8")
    def gone(names):
        raise lint_custom_rules._lib_consumer_read.ConsumerReadError("da-read was not found")
    monkeypatch.setattr(lint_custom_rules._lib_consumer_read, "promql_names", gone)
    assert _run_main([str(tmp_path), "--ci"], monkeypatch) == 2
    err = capsys.readouterr().err
    assert "denied_functions: da-read was not found" in err and "internal error" not in err


def test_a_run_over_no_file_does_not_ask_for_the_names(tmp_path, monkeypatch):
    monkeypatch.setattr(lint_custom_rules, "denied_function_problems",
                        lambda policy: pytest.fail("asked da-read for a run over nothing"))
    assert _run_main([str(tmp_path)], monkeypatch) == 0


def test_preserved_labels_cannot_be_relaxed_by_an_override():
    with pytest.raises(_lib_exitcodes.CallerError) as exc:
        lint_custom_rules.load_policy({"file_overrides": [{
            "path": "a.yaml", "policy": {"preserved_labels": []}}]}, "p.yaml")
    assert exc.value.reason == "policy_invalid"
    assert "not preserved_labels" in str(exc.value)


def test_the_retired_denied_patterns_is_refused_naming_its_replacement():
    with pytest.raises(_lib_exitcodes.CallerError) as exc:
        lint_custom_rules.load_policy({"denied_patterns": ['=~".*"']}, "p.yaml")
    assert exc.value.reason == "policy_unknown_key"
    assert "denied_patterns was removed (#4098)" in str(exc.value)
    assert "preserved_labels" in str(exc.value)


def test_lint_exceeds_max_range(policy):
    """測試超出最大範圍。"""
    results = lint_custom_rules.lint_expr(
        "rate(my_metric[7d])", policy, "test.yaml", "TestRule"
    )
    range_errors = [r for r in results if "exceeds max allowed" in r.message]
    assert len(range_errors) == 1

def test_lint_within_max_range(policy):
    """測試在最大範圍內。"""
    results = lint_custom_rules.lint_expr(
        "rate(my_metric[30m])", policy, "test.yaml", "TestRule"
    )
    range_errors = [r for r in results if "exceeds max allowed" in r.message]
    assert len(range_errors) == 0

def test_lint_exact_max_range_passes(policy):
    """測試精確最大範圍。"""
    results = lint_custom_rules.lint_expr(
        "rate(my_metric[1h])", policy, "test.yaml", "TestRule"
    )
    range_errors = [r for r in results if "exceeds max allowed" in r.message]
    assert len(range_errors) == 0


def test_lint_missing_tenant_label(policy):
    """測試缺少租戶標籤。"""
    results = lint_custom_rules.lint_labels(
        {"severity": "warning"}, policy, "test.yaml", "TestRule", is_recording=False
    )
    assert len(results) == 1
    assert "tenant" in results[0].message

def test_lint_has_tenant_label(policy):
    """測試有租戶標籤。"""
    results = lint_custom_rules.lint_labels(
        {"severity": "warning", "tenant": "db-a"}, policy, "test.yaml", "TestRule",
        is_recording=False
    )
    assert len(results) == 0

def test_lint_recording_rule_skips_label_check(policy):
    """測試記錄規則跳過標籤檢查。"""
    results = lint_custom_rules.lint_labels(
        {}, policy, "test.yaml", "TestRule", is_recording=True
    )
    assert len(results) == 0


def test_expiry_missing_expiry_warns():
    """測試缺少到期日期警告。"""
    results = lint_custom_rules.check_expiry_label(
        {"owner": "team-a"}, "test.yaml", "TestRule"
    )
    assert len(results) == 1
    assert results[0].severity == "WARN"

def test_expiry_has_expiry_passes():
    """測試有到期日期通過檢查。"""
    results = lint_custom_rules.check_expiry_label(
        {"expiry": "2026-06-30"}, "test.yaml", "TestRule"
    )
    assert len(results) == 0

def test_owner_missing_owner_warns():
    """測試缺少擁有者警告。"""
    results = lint_custom_rules.check_owner_label(
        {"expiry": "2026-06-30"}, "test.yaml", "TestRule"
    )
    assert len(results) == 1
    assert results[0].severity == "WARN"

def test_owner_has_owner_passes():
    """測試有擁有者通過檢查。"""
    results = lint_custom_rules.check_owner_label(
        {"owner": "team-a"}, "test.yaml", "TestRule"
    )
    assert len(results) == 0


def test_lint_file_direct_rule_format():
    """測試直接 Prometheus rule group 格式。"""
    content = {
        "groups": [{
            "name": "test_group",
            "rules": [{
                "alert": "TestAlert",
                "expr": "my_metric > 100",
                "labels": {"severity": "warning", "tenant": "db-a",
                           "owner": "team-a", "expiry": "2026-12-31"},
            }]
        }]
    }
    with tempfile.NamedTemporaryFile(
        mode='w', suffix='.yaml', delete=False, encoding='utf-8'
    ) as f:
        yaml.safe_dump(content, f)
        fpath = f.name
    try:
        results, count = lint_custom_rules.lint_file(
            fpath, lint_custom_rules.builtin_policy()
        )
        assert count == 1
        # Should not have errors (clean rule)
        errors = [r for r in results if r.severity == "ERROR"]
        assert len(errors) == 0
    finally:
        os.unlink(fpath)

def test_lint_file_configmap_wrapper():
    """測試 ConfigMap data wrapper 格式。"""
    inner_yaml = yaml.safe_dump({
        "groups": [{
            "name": "wrapped_group",
            "rules": [{
                "alert": "WrappedAlert",
                "expr": 'predict_linear(my_metric[1h], 3600) > 100',
                "labels": {"severity": "critical"},
            }]
        }]
    })
    content = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "r"},
               "data": {"rules.yaml": inner_yaml}}
    with tempfile.NamedTemporaryFile(
        mode='w', suffix='.yaml', delete=False, encoding='utf-8'
    ) as f:
        yaml.safe_dump(content, f)
        fpath = f.name
    try:
        results, count = lint_custom_rules.lint_file(
            fpath, lint_custom_rules.builtin_policy()
        )
        assert count == 1
        errors = [r for r in results if r.severity == "ERROR"]
        # Should detect: denied function + missing tenant label
        assert len(errors) >= 2
    finally:
        os.unlink(fpath)

def test_lint_file_empty_file():
    """#3426 F14：空檔沒有任何物件也沒有 `groups`——比照 kubectl 的
    「no objects passed to apply」點名為 ERROR，不當成沒有 rule 的檔。"""
    with tempfile.NamedTemporaryFile(
        mode='w', suffix='.yaml', delete=False, encoding='utf-8'
    ) as f:
        f.write("")
        fpath = f.name
    try:
        results, count = lint_custom_rules.lint_file(
            fpath, lint_custom_rules.builtin_policy()
        )
        assert count == 0
        assert [r.severity for r in results] == ["ERROR"], [str(r) for r in results]
        assert "no objects passed to apply" in results[0].message
    finally:
        os.unlink(fpath)

def test_lint_file_nonexistent_file():
    """測試不存在的檔案應回傳 ERROR。"""
    results, count = lint_custom_rules.lint_file(
        "/nonexistent/path.yaml", lint_custom_rules.builtin_policy()
    )
    assert count == 0
    assert len(results) == 1
    assert results[0].severity == "ERROR"


def test_load_policy_no_policy_uses_defaults():
    """測試無政策時使用預設值。"""
    policy = lint_custom_rules.load_policy(None)
    assert policy == lint_custom_rules.builtin_policy()

def _read_policy(path):
    """--policy *path* as the tool reads it (#3445: `_lib_input.document`)."""
    import argparse
    import _lib_input
    parser = argparse.ArgumentParser()
    _lib_input.add_input(parser, "--policy", decode=lint_custom_rules.POLICY_DOCUMENT)
    return _lib_input.document(parser.parse_args(["--policy", str(path)]), "policy", lang="en")


def test_load_policy_custom_policy_overrides():
    """測試自訂政策覆寫。"""
    custom = {"max_range_duration": "2h", "denied_functions": ["my_func"]}
    with tempfile.NamedTemporaryFile(
        mode='w', suffix='.yaml', delete=False, encoding='utf-8'
    ) as f:
        yaml.safe_dump(custom, f)
        fpath = f.name
    try:
        policy = lint_custom_rules.load_policy(_read_policy(fpath))
        assert policy["max_range_duration"] == "2h"
        assert policy["denied_functions"] == ["my_func"]
        # Unspecified keys should retain defaults
        assert policy["required_labels"] == ["tenant"]
    finally:
        os.unlink(fpath)

def test_load_policy_unreadable_policy_is_caller_error(tmp_path, monkeypatch, capsys):
    """讀不到的 --policy 必須 exit 2，不可回退到內建預設。

    ⛔ 這條原本叫 `test_load_policy_invalid_policy_falls_back`，斷言
    `== 內建政策`（#1556）。「回退」的實際後果是：`lint --policy <打錯的路徑>`
    拿內建規則去 lint、印一行沒人在 CI 裡讀的 stderr 警告、然後 exit 0——
    操作者以為自己的政策生效了。dev-rules #13 把「檔案/路徑不存在」歸在
    EXIT_CALLER_ERROR。#3445：讀取改由 `_lib_input.document`（CallerError，
    run() 收成 rc 2）。
    """
    from _lib_exitcodes import CallerError
    (tmp_path / "r.yaml").write_text(_HOLT_RULE, encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["lint_custom_rules", "--policy",
                                      "/nonexistent/policy.yaml", str(tmp_path / "r.yaml")])
    with pytest.raises(CallerError) as exc:
        lint_custom_rules.main()
    assert exc.value.reason == "file_not_found"
    assert str(exc.value).startswith("--policy /nonexistent/policy.yaml: ")


@pytest.mark.parametrize("content, got", [
    ("[]\n", "a list"), ("0\n", "a number"), ('""\n', "a string"), ("false\n", "a boolean"),
])
def test_a_policy_that_is_no_mapping_is_refused_not_the_default(tmp_path, monkeypatch,
                                                                content, got):
    """#3445: `yaml.safe_load(f) or {}` read `[]` (and `0`, `""`, `false`) as
    no override — the run linted against the built-in policy at rc 0, the
    operator's file never applied. A top level that is no mapping is refused,
    named."""
    from _lib_exitcodes import CallerError
    policy = tmp_path / "policy.yaml"
    policy.write_text(content, encoding="utf-8")
    (tmp_path / "r.yaml").write_text(_HOLT_RULE, encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["lint_custom_rules", "--policy", str(policy),
                                      str(tmp_path / "r.yaml")])
    with pytest.raises(CallerError) as exc:
        lint_custom_rules.main()
    assert exc.value.reason == "file_unreadable"
    assert str(exc.value) == f"--policy {policy}: the top level is not a mapping (got {got})"


@pytest.mark.parametrize("content", ["", "~\n", "# only a comment\n"])
def test_an_empty_policy_is_no_override(tmp_path, content):
    """Declared (`empty_is=dict`): an empty document is no override — what the
    old `or {}` meant it for."""
    policy = tmp_path / "policy.yaml"
    policy.write_text(content, encoding="utf-8")
    assert lint_custom_rules.load_policy(_read_policy(policy)) == lint_custom_rules.builtin_policy()


# ── #1618: scan targets that do not exist / files that cannot be decoded ──

_HOLT_RULE = (
    "groups:\n"
    "- name: g\n"
    "  rules:\n"
    "  - alert: Bad\n"
    "    expr: holt_winters(my_metric[1h], 0.3, 0.7) > 1\n"
    "    labels:\n"
    "      severity: critical\n"
)


@pytest.mark.parametrize("extra", [[], ["--ci"]], ids=["plain", "ci"])
def test_main_missing_target_is_caller_error(tmp_path, monkeypatch, capsys, extra):
    """不存在的掃描目標 → exit 2 並指名路徑；有無 --ci 答案相同。

    ⛔ 之前 collect_files 直接略過它，`lint /typo --ci` 印 "No YAML files
    found." 然後 exit 0——一個沒人掃過的路徑通過了治理閘門。
    """
    missing = tmp_path / "missing"
    monkeypatch.setattr(sys, "argv", ["lint_custom_rules", str(missing), *extra])
    assert _main_code() == 2
    err = capsys.readouterr().err
    assert f"ERROR: {missing}: " in err
    assert "Do not drop the path" in err


def test_main_missing_target_names_only_the_missing_one(tmp_path, monkeypatch, capsys):
    """`lint <真目錄> <不存在>` → 2，訊息只指名不存在的那個。"""
    real = tmp_path / "real"
    real.mkdir()
    missing = tmp_path / "missing"
    monkeypatch.setattr(sys, "argv", ["lint_custom_rules", str(real), str(missing)])
    assert _main_code() == 2
    err = capsys.readouterr().err
    assert f"ERROR: {missing}: " in err
    assert str(real) not in err


@pytest.mark.parametrize("extra", [[], ["--ci"]], ids=["plain", "ci"])
def test_main_empty_string_target_is_caller_error(tmp_path, monkeypatch, capsys, extra):
    """`lint ''` → exit 2 and the message shows the empty string as ''.

    Blind-review follow-up to #1618. `Path('')` is `PosixPath('.')`, so
    `exists()` is True and the missing-target guard let it through: measured
    on HEAD (820c7785) from a directory holding rule files, `lint ''` scanned
    the CURRENT DIRECTORY and exited 0 — an unset shell variable turned into
    "lint whatever is here".
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "rules.yaml").write_text(_HOLT_RULE, encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["lint_custom_rules", "", *extra])
    assert _main_code() == 2
    captured = capsys.readouterr()
    assert "''" in captured.err
    assert "Do not drop the path" in captured.err
    assert "Scanned" not in captured.out      # nothing in cwd was linted


def test_main_empty_string_alongside_real_dir_names_only_the_empty_one(
        tmp_path, monkeypatch, capsys):
    """`lint <real dir> ''` → 2, message names '' and not the real dir."""
    real = tmp_path / "real"
    real.mkdir()
    monkeypatch.setattr(sys, "argv", ["lint_custom_rules", str(real), ""])
    assert _main_code() == 2
    err = capsys.readouterr().err
    assert "''" in err
    assert repr(str(real)) not in err


def test_main_existing_empty_dir_is_ok(tmp_path, monkeypatch, capsys):
    """control：不帶 --ci 時，存在但沒有 YAML 的目錄 → exit 0，"No YAML files found" 並指名目標。"""
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setattr(sys, "argv", ["lint_custom_rules", str(empty)])
    with pytest.raises(SystemExit) as exc:
        lint_custom_rules.main()
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "No YAML files found" in out
    assert repr(str(empty)) in out


@pytest.mark.parametrize("make", ["empty", "only-txt", "rule-pack-dir-platform-only"])
def test_main_ci_that_selects_no_file_is_a_caller_error(tmp_path, make):
    """#3462：--ci 下一個檔都沒選到 → rc 2、點名目標，stdout 沒有東西；先前是 rc 0，
    治理閘門在「什麼都沒掃」時綠燈。"""
    import subprocess
    import _lib_consumer_read
    target = tmp_path / "t"
    target.mkdir()
    if make == "only-txt":
        (target / "notes.txt").write_text("x\n", encoding="utf-8")
    if make == "rule-pack-dir-platform-only":
        platform = sorted(_lib_consumer_read.PLATFORM_PACKS)[0]
        (target / _lib_consumer_read.RULE_PACKS.file_name(platform)).write_text(
            "groups: []\n", encoding="utf-8")
        argv = ["--rule-pack-dir", str(target)]
        named = f"--rule-pack-dir {target}: selects nothing"
    else:
        argv = [str(target)]
        named = f"{target}: selects nothing"
    proc = subprocess.run([sys.executable, lint_custom_rules.__file__, *argv, "--ci"],
                          capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert proc.stdout == "", proc.stdout
    assert named in proc.stderr and "Traceback" not in proc.stderr, proc.stderr


_CP950_BYTES = b"groups:\n  - name: \xa4\xa4\n"   # cp950「中」— invalid UTF-8


def test_lint_file_non_utf8_is_one_error_result(tmp_path):
    """非 UTF-8 檔案 → 一筆 ERROR "cannot read file"，不 raise。

    ⛔ UnicodeDecodeError 是 ValueError、不是 OSError；原本的 `except OSError`
    看不到它，整個 run 以 traceback 收場、stdout 0 bytes。
    """
    bad = tmp_path / "aa_bad.yaml"
    bad.write_bytes(_CP950_BYTES)
    results, count = lint_custom_rules.lint_file(str(bad), lint_custom_rules.builtin_policy())
    assert count == 0
    assert len(results) == 1
    assert results[0].severity == "ERROR"
    assert "cannot read file" in results[0].message


def test_main_non_utf8_file_is_quarantined_per_file(tmp_path, monkeypatch, capsys):
    """--ci 下：壞檔一筆 ERROR，其他檔案的 finding 照印，整體 exit 1。"""
    (tmp_path / "aa_bad.yaml").write_bytes(_CP950_BYTES)
    (tmp_path / "zz_ok.yaml").write_text(_HOLT_RULE, encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["lint_custom_rules", str(tmp_path), "--ci"])
    with pytest.raises(SystemExit) as exc:
        lint_custom_rules.main()
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "cannot read file" in out
    assert "aa_bad.yaml" in out
    assert "zz_ok.yaml" in out
    assert "holt_winters" in out


def test_group_interval_exceeds_max():
    """測試超出最大求值時間間隔。"""
    policy = {"max_evaluation_interval": "60s"}
    results = lint_custom_rules.lint_group_interval(
        "120s", policy, "test.yaml", "my_group"
    )
    assert len(results) == 1
    assert results[0].severity == "WARN"

def test_group_interval_within_max():
    """測試在最大求值時間間隔內。"""
    policy = {"max_evaluation_interval": "60s"}
    results = lint_custom_rules.lint_group_interval(
        "30s", policy, "test.yaml", "my_group"
    )
    assert len(results) == 0


def test_collect_yaml_files():
    """測試收集 YAML 檔案。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create test files
        for name in ["a.yaml", "b.yml", "c.txt", "d.yaml"]:
            with open(os.path.join(tmpdir, name), 'w', encoding="utf-8") as f:
                f.write("test")
        files = lint_custom_rules.collect_files([tmpdir])
        assert len(files) == 3  # a.yaml, b.yml, d.yaml
        assert all(f.endswith((".yaml", ".yml")) for f in files)

def test_collect_single_file():
    """測試收集單一檔案。"""
    with tempfile.NamedTemporaryFile(
        suffix='.yaml', delete=False
    ) as f:
        fpath = f.name
    try:
        files = lint_custom_rules.collect_files([fpath])
        assert len(files) == 1
    finally:
        os.unlink(fpath)


# ── Per-file overrides: GENERATED compiled pack (forecast / predict_linear) ──
#
# Regression for the latent CI conflict found while reviewing #819: the policy
# denies predict_linear + ranges >1h for tenant raw rules, but the forecast
# recipe (ADR-024 能力 B) legitimately compiles to predict_linear with a
# platform-derived lookback of up to FORECAST_MAX_LOOKBACK (the compiled-pack override's max_range_duration) inside the GENERATED
# rule-packs/rule-pack-custom-alerts.yaml. The `file_overrides` exemption must
# let the compiled forecast shape through while keeping every other check live.

_REPO_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
_REAL_POLICY = os.path.join(_REPO_ROOT, ".github", "custom-rule-policy.yaml")

_GENERATED_PACK_RELPATH = os.path.join("rule-packs", "rule-pack-custom-alerts.yaml")


def _longest_horizon():
    """The contract's largest forecast horizon (the enum the compiler takes)."""
    with open(os.path.join(_REPO_ROOT, "components", "threshold-exporter", "app", "pkg", "config",
                           "recipe_contract.json"), encoding="utf-8") as f:
        enum = json.load(f)["horizon"]["enum"]
    return max(enum, key=parse_duration_seconds)


def _compiled_forecast_pack_text():
    """Compile a real forecast declaration at the contract's largest horizon
    (the longest lookback a compiled pack reads) through the actual compiler
    + renderer, so the fixture cannot drift from what `make
    custom-alerts-compile` emits."""
    import compile_custom_alerts as cc
    conf = (
        "tenants:\n"
        "  ta:\n"
        "    _custom_alerts:\n"
        '      - {recipe: forecast, name: disk_full, metric: disk_avail,'
        f' capacity_metric: disk_cap, op: "<", horizon: {_longest_horizon()},'
        ' threshold: "0.15:warning"}\n'
    )
    with tempfile.TemporaryDirectory() as confdir:
        with open(os.path.join(confdir, "a.yaml"), "w", encoding="utf-8") as f:
            f.write(conf)
        pack = cc.build_pack(confdir)
    return cc._render(pack.groups)


@pytest.fixture
def compiled_forecast_pack(tmp_path, da_guard_env):  # the compiler reads through da-guard (#3042)
    """A real compiled forecast pack written at the canonical relative path."""
    text = _compiled_forecast_pack_text()
    assert "predict_linear" in text          # fixture sanity: forecast shape present
    # The compiler's longest lookback is the one the policy exempts — the Go
    # compiler and gen_recipe_contract.py derive it from the same contract.
    assert f"[{parse_duration_seconds(FORECAST_MAX_LOOKBACK)}s]" in text
    fpath = tmp_path / _GENERATED_PACK_RELPATH
    fpath.parent.mkdir(parents=True)
    fpath.write_text(text, encoding="utf-8")
    return fpath


def test_forecast_in_generated_pack_passes_real_policy(compiled_forecast_pack):
    """forecast shape 出現在 compiled pack 時，真 policy 檔不誤殺（0 ERROR）。"""
    policy = lint_custom_rules.load_policy(_read_policy(_REAL_POLICY))
    assert os.path.isfile(_REAL_POLICY)      # guard: fallback-to-defaults would hide drift
    results, count = lint_custom_rules.lint_file(str(compiled_forecast_pack), policy)
    errors = [r for r in results if r.severity == "ERROR"]
    assert count > 0
    assert errors == [], [str(r) for r in errors]


def test_forecast_in_generated_pack_passes_default_policy(compiled_forecast_pack):
    """validate_config 無 --policy 時走內建政策（即 repo 的政策檔），行為須一致。"""
    results, _ = lint_custom_rules.lint_file(
        str(compiled_forecast_pack), lint_custom_rules.builtin_policy()
    )
    errors = [r for r in results if r.severity == "ERROR"]
    assert errors == [], [str(r) for r in errors]


def test_generated_pack_without_marker_stays_denied(compiled_forecast_pack):
    """缺 GENERATED 檔頭 → 豁免不生效：predict_linear 照殺 + 標記 ERROR。"""
    text = compiled_forecast_pack.read_text(encoding="utf-8")
    stripped = "\n".join(
        line for line in text.splitlines() if not line.startswith("#")
    )
    compiled_forecast_pack.write_text(stripped, encoding="utf-8")
    results, _ = lint_custom_rules.lint_file(
        str(compiled_forecast_pack), lint_custom_rules.builtin_policy()
    )
    messages = [r.message for r in results if r.severity == "ERROR"]
    assert any("GENERATED" in m for m in messages)
    assert any("predict_linear" in m for m in messages)


def test_override_scoped_to_path_only(compiled_forecast_pack, tmp_path):
    """同內容、不同檔名 → 不豁免（override 綁定 path）。"""
    other = tmp_path / "rule-packs" / "rule-pack-handwritten.yaml"
    other.write_text(
        compiled_forecast_pack.read_text(encoding="utf-8"), encoding="utf-8"
    )
    results, _ = lint_custom_rules.lint_file(
        str(other), lint_custom_rules.builtin_policy()
    )
    messages = [r.message for r in results if r.severity == "ERROR"]
    assert any("predict_linear" in m for m in messages)


def test_override_keeps_other_checks_live(tmp_path):
    """豁免檔內其餘 deny 照跑：超過 forecast lookback 上限的 range 仍是 ERROR；
    holt_winters 在 Prometheus 3 已不是函式，整檔被 rule loader 拒收，同樣是點名的
    ERROR（#3206：檔案以 rulefmt 讀）。"""
    over = f"{parse_duration_seconds(FORECAST_MAX_LOOKBACK) + 1}s"   # 剛好超過豁免上限
    header = ("# GENERATED test fixture — mimics compile_custom_alerts header\n"
              "groups:\n"
              "- name: g\n"
              "  rules:\n")
    fpath = tmp_path / _GENERATED_PACK_RELPATH
    fpath.parent.mkdir(parents=True)

    def errors(rules):
        fpath.write_text(header + rules, encoding="utf-8")
        results, _ = lint_custom_rules.lint_file(
            str(fpath), lint_custom_rules.builtin_policy()
        )
        return [r.message for r in results if r.severity == "ERROR"]

    # Prometheus prints the range as it spells durations (345601s is 4d1s)
    assert any(m.startswith("selector my_metric[")
               and m.endswith(f"exceeds max allowed [{FORECAST_MAX_LOOKBACK}]")
               for m in errors(
        "  - record: custom:metric:y\n"
        f"    expr: avg_over_time(my_metric[{over}])\n"))
    refused = errors("  - record: custom:metric:x\n"
                     "    expr: holt_winters(my_metric[1h], 0.3, 0.7)\n")
    assert any("rule loader refuses" in m and "holt_winters" in m for m in refused), refused


def test_committed_pack_carries_the_generated_marker():
    """The committed compiled pack carries the GENERATED marker its override
    needs."""
    committed = os.path.join(_REPO_ROOT, _GENERATED_PACK_RELPATH)
    with open(committed, encoding="utf-8") as f:
        head = list(itertools.islice(f, lint_custom_rules.GENERATED_MARKER_SCAN_LINES))
    assert any(lint_custom_rules.GENERATED_MARKER in line for line in head)


def test_the_built_in_policy_is_the_repository_policy_file():
    """#4099: one policy, not three copies — the built-in policy is read
    from .github/custom-rule-policy.yaml (the image ships it beside the
    tool), so no copy can keep a name the file dropped."""
    assert os.path.samefile(lint_custom_rules.builtin_policy_path(), _REAL_POLICY)
    assert lint_custom_rules.load_policy(None) == lint_custom_rules.load_policy(
        _read_policy(_REAL_POLICY), _REAL_POLICY)


def test_a_missing_built_in_policy_is_a_caller_error(monkeypatch):
    monkeypatch.setattr(lint_custom_rules, "builtin_policy_path", lambda: None)
    monkeypatch.setattr(lint_custom_rules, "_BUILTIN_POLICY", None)
    with pytest.raises(_lib_exitcodes.CallerError) as exc:
        lint_custom_rules.load_policy(None)
    assert exc.value.reason == "builtin_policy_missing"


def test_a_complete_policy_needs_no_built_in_one(monkeypatch):
    full = lint_custom_rules.builtin_policy()
    monkeypatch.setattr(lint_custom_rules, "builtin_policy_path", lambda: None)
    monkeypatch.setattr(lint_custom_rules, "_BUILTIN_POLICY", None)
    assert lint_custom_rules.load_policy(full, "p.yaml") == full
    with pytest.raises(_lib_exitcodes.CallerError) as exc:
        lint_custom_rules.load_policy({"denied_functions": []}, "p.yaml")
    assert "give a --policy that sets every key" in str(exc.value)


def test_the_image_reads_the_policy_beside_the_tool(tmp_path, monkeypatch):
    """The da-tools image ships the policy file beside the tool (build.sh
    REPO_DATA_FILES): the flat layout is read first."""
    (tmp_path / lint_custom_rules.BUILTIN_POLICY_NAME).write_text(
        open(_REAL_POLICY, encoding="utf-8").read(), encoding="utf-8")
    monkeypatch.setattr(lint_custom_rules, "_THIS_DIR", str(tmp_path))
    assert lint_custom_rules.builtin_policy_path() == tmp_path / lint_custom_rules.BUILTIN_POLICY_NAME


def test_a_built_in_policy_lacking_a_key_is_refused(monkeypatch):
    lacking = lint_custom_rules.builtin_policy()
    del lacking["max_evaluation_interval"]
    monkeypatch.setattr(lint_custom_rules, "_BUILTIN_POLICY", lacking)
    with pytest.raises(_lib_exitcodes.CallerError) as exc:
        lint_custom_rules.load_policy(None)
    assert exc.value.reason == "policy_invalid" and "lacks max_evaluation_interval" in str(exc.value)


def test_a_built_in_policy_with_a_key_lint_does_not_read_is_refused(monkeypatch):
    monkeypatch.setattr(lint_custom_rules, "_BUILTIN_POLICY",
                        {**lint_custom_rules.builtin_policy(), "denied_patterns": []})
    with pytest.raises(_lib_exitcodes.CallerError) as exc:
        lint_custom_rules.load_policy(None)
    assert exc.value.reason == "policy_invalid" and "denied_patterns" in str(exc.value)


def test_nested_path_does_not_get_exemption(compiled_forecast_pack, tmp_path):
    """對抗式 review FINDING 1：租戶把合法檔名塞到 rule-packs/ 樹的 NESTED 路徑
    （suffix 仍命中 override.path）不得取得豁免——否則 predict_linear + 長達 lookback 上限的 range
    被遞迴掃描放行，而 drift gate 只看單一 canonical 路徑、抓不到此檔。"""
    text = compiled_forecast_pack.read_text(encoding="utf-8")  # 帶真 GENERATED 檔頭
    nested = tmp_path / "rule-packs" / "custom" / "rule-packs" / "rule-pack-custom-alerts.yaml"
    nested.parent.mkdir(parents=True)
    nested.write_text(text, encoding="utf-8")
    results, _ = lint_custom_rules.lint_file(
        str(nested), lint_custom_rules.builtin_policy()
    )
    messages = [r.message for r in results if r.severity == "ERROR"]
    assert any("predict_linear" in m for m in messages), \
        "nested path must be fully linted, not exempted"


def test_override_cannot_relax_non_whitelisted_check():
    """對抗式 review FINDING 3：override.policy 設 OVERRIDABLE_POLICY_KEYS 以外的 key
    （如清空 required_labels）不得生效。#3977：載入時就拒收（rc 2），不論有沒有檔案
    匹配；先前只在匹配的檔上回報一個 finding（不帶 --ci 時 rc 0）。"""
    with pytest.raises(_lib_exitcodes.CallerError) as exc:
        lint_custom_rules.load_policy({"file_overrides": [{
            "path": "rule-packs/nothing-matches.yaml",
            "policy": {"required_labels": []},   # 攻擊：想關掉 tenant label 強制
        }]}, "p.yaml")
    assert exc.value.reason == "policy_invalid"
    assert str(exc.value) == ("--policy p.yaml: file_overrides[0].policy may only relax "
                              "denied_functions, max_range_duration, not required_labels")


@pytest.mark.parametrize("overrides, said", [
    # #3977: every one of these was silent (rc 0), a finding only on a
    # matching file, or a crash — now the policy is refused when loaded.
    ([{"path": "a.yaml", "require_generated_markr": False, "bogus": 1}],
     "file_overrides[0] has no key(s) bogus, require_generated_markr "
     "(an entry has path, require_generated_marker, policy)"),
    (["oops-a-string"], "file_overrides[0] must be a mapping, not str"),
    ({"path": "a.yaml"}, "file_overrides must be a list of entries, not dict"),
    (None, "file_overrides must be a list of entries, not NoneType"),
    ([{"policy": {}}], "file_overrides[0].path must be a non-empty string"),
    ([{"path": ""}], "file_overrides[0].path must be a non-empty string"),
    ([{"path": "a.yaml"}, {"path": "a.yaml"}],
     "file_overrides[1].path a.yaml is given twice: the first entry would shadow this one"),
    ([{"path": "a.yaml", "require_generated_marker": "false"}],
     "file_overrides[0].require_generated_marker must be true or false"),
    ([{"path": "a.yaml", "policy": ["denied_functions"]}],
     "file_overrides[0].policy must be a mapping, not list"),
    ([{"path": "a.yaml", "policy": {"denied_functions": "holt_winters"}}],
     "file_overrides[0].policy.denied_functions must be a list of non-empty strings"),
    ([{"path": "a.yaml", "policy": {"max_range_duration": "banana"}}],
     "file_overrides[0].policy.max_range_duration must be a positive Prometheus duration such as 90m or 1h30m"),
    ([{"path": "a.yaml", "policy": {"max_range_duration": "1.5h"}}],
     "file_overrides[0].policy.max_range_duration must be a positive Prometheus duration such as 90m or 1h30m"),
    ([{"path": "a.yaml", "policy": {"max_range_duration": "9" * 400 + "h"}}],
     "file_overrides[0].policy.max_range_duration must be a positive Prometheus duration such as 90m or 1h30m"),
    ([{"path": "a.yaml"}, {"path": "sub/a.yaml"}],
     "file_overrides[1].path sub/a.yaml and a.yaml can match the same file: which entry "
     "applies would depend on their order"),
    ([{"path": "a.yaml", "policy": {"max_range_duration": "0s"}}],
     "file_overrides[0].policy.max_range_duration must be a positive Prometheus duration such as 90m or 1h30m"),
])
def test_a_malformed_file_override_is_refused_when_the_policy_is_loaded(overrides, said):
    """#3977: one shape for an entry, judged in load_policy — matched or not."""
    with pytest.raises(_lib_exitcodes.CallerError) as exc:
        lint_custom_rules.load_policy({"file_overrides": overrides}, "p.yaml")
    assert exc.value.reason == "policy_invalid"
    assert str(exc.value) == f"--policy p.yaml: {said}"


@pytest.mark.parametrize("custom", [
    {"file_overrides": [{"path": "a.yaml", "x\nERROR: forged": 1}]},
    {"file_overrides": [{"path": "a.yaml", "policy": {"x\nERROR: forged": 1}}]},
    {"x\nERROR: forged": 1},
])
def test_a_key_in_a_refusal_cannot_print_a_line_of_its_own(custom):
    """#3977 review: a key is echoed through safe_label, as a path is."""
    with pytest.raises(_lib_exitcodes.CallerError) as exc:
        lint_custom_rules.load_policy(custom, "p.yaml")
    assert "\n" not in str(exc.value), str(exc.value)


@pytest.mark.parametrize("key, value, said", [
    # #4061: each of these was a check turned off (rc 0), a value matched
    # character by character, or a crash — now the policy is refused.
    ("denied_functions", "predict_linear", "must be a list of non-empty strings"),
    ("denied_functions", {"a": 1}, "must be a list of non-empty strings"),
    ("denied_functions", None, "must be a list of non-empty strings"),
    ("preserved_labels", "tenant", "must be a list of non-empty strings"),
    ("preserved_labels", [""], "must be a list of non-empty strings"),
    ("required_labels", "team", "must be a list of non-empty strings"),
    ("required_labels", [5], "must be a list of non-empty strings"),
    ("max_range_duration", "banana", "must be a positive Prometheus duration such as 90m or 1h30m"),
    ("max_range_duration", "0s", "must be a positive Prometheus duration such as 90m or 1h30m"),
    ("max_range_duration", 5, "must be a positive Prometheus duration such as 90m or 1h30m"),
    ("max_range_duration", None, "must be a positive Prometheus duration such as 90m or 1h30m"),
    ("max_range_duration", "9" * 5000 + "h",
     "must be a positive Prometheus duration such as 90m or 1h30m"),
    ("max_evaluation_interval", ["1m"], "must be a positive Prometheus duration such as 90m or 1h30m"),
])
def test_a_malformed_top_level_value_is_refused_when_the_policy_is_loaded(key, value, said):
    with pytest.raises(_lib_exitcodes.CallerError) as exc:
        lint_custom_rules.load_policy({key: value}, "p.yaml")
    assert exc.value.reason == "policy_invalid"
    assert str(exc.value) == f"--policy p.yaml: {key} {said}"


@pytest.mark.parametrize("key, value", [
    ("max_range_duration", "1h30m"), ("max_range_duration", "2w"),
    ("max_evaluation_interval", "90s"), ("denied_functions", []), ("required_labels", []),
    ("preserved_labels", []),
])
def test_a_well_formed_top_level_value_loads(key, value):
    assert lint_custom_rules.load_policy({key: value}, "p.yaml")[key] == value


def test_a_compound_range_max_is_enforced(tmp_path):
    """#4061: `max_range_duration: 1h30m` read as None turned the range
    check off; read as Prometheus does, a [2h] range is over it."""
    policy = lint_custom_rules.load_policy({"max_range_duration": "1h30m"}, "p.yaml")
    found = lint_custom_rules.lint_expr('rate(x{tenant="t"}[2h]) > 1', policy, "f.yaml", "A")
    assert any("exceeds max allowed [1h30m]" in r.message for r in found), found


# ── #4080: ranges and function names as Prometheus reads them ────────────

_RANGE_1H = {"max_range_duration": "1h"}


@pytest.mark.parametrize("expr, window", [
    # spellings the regex `\[(\d+[smhd])\]` did not see
    ("rate(x[1w])", "1w"),
    ("rate(x[1y])", "1y"),
    ("rate(x[1h30m])", "1h30m"),
    ("rate(x[7200000ms])", "2h"),
    ("rate(x[ 2h ])", "2h"),
    ("rate(x[(30d)])", "30d"),
    ("rate(x[2h*100])", "8d8h"),
    # a subquery widens the window of every selector in it
    ("max_over_time(rate(x[5m])[3h:1m])", "3h5m"),
    ("max_over_time(rate(x[5m])[3h:])", "3h5m"),
    ("max_over_time(rate(x[5m])[1h*24:])", "1d5m"),
    ("max_over_time(x[2h:])", "2h"),
    ("max_over_time(rate(x[5m])[1h:1m])", "1h5m"),
    ("max_over_time(max_over_time(rate(x[5m])[50m:1m])[50m:1m])", "1h45m"),
])
def test_a_window_over_the_max_is_an_error_however_it_is_spelt(policy, expr, window):
    found = lint_custom_rules.lint_expr(expr, {**policy, **_RANGE_1H}, "f.yaml", "A")
    msgs = [r.message for r in found if "exceeds max allowed [1h]" in r.message]
    assert len(msgs) == 1 and f"reads [{window}]" in msgs[0], found


@pytest.mark.parametrize("expr", [
    "rate(x[1h])",                               # at the max
    "rate(x[5m] offset 1w)",                     # offset moves the window only
    "rate(x[30m] offset -2h)",
    'rate(x{a="[2h]"}[5m])',                     # a label value is no range
    "max_over_time((x @ end())[30d:1m])",        # a selector at a fixed time
    "max_over_time(max_over_time(x[1h:1m] @ 100)[30d:1m])",  # @ drops the outer subquery
    "max_over_time(rate(x[5m])[55m:1m])",
    "max_over_time(vector(1)[3h:])",             # reads no series
])
def test_a_window_within_the_max_passes(policy, expr):
    found = lint_custom_rules.lint_expr(expr, {**policy, **_RANGE_1H}, "f.yaml", "A")
    assert not [r for r in found if "exceeds max allowed" in r.message], found


def test_a_window_message_names_the_selector_and_its_parts(policy):
    found = lint_custom_rules.lint_expr("max_over_time(rate(x[5m])[3h:1m])",
                                        {**policy, **_RANGE_1H}, "f.yaml", "A")
    assert [r.message for r in found] == [
        "selector x[5m] reads [3h5m] (range 5m + subquery 3h), exceeds max allowed [1h]"]


def test_a_multi_line_selector_is_named_on_one_line(policy):
    """The finding names the selector as Prometheus prints it: one line,
    label values quoted — never the text as typed, which put a label value
    at the start of a line of the report."""
    expr = 'rate(x{\n  tenant="t",\n  note="WARN: [5m]"\n}[2h] offset 1d)'
    found = lint_custom_rules.lint_expr(expr, {**policy, **_RANGE_1H}, "f.yaml", "A")
    assert [r.message for r in found] == [
        'selector x{note="WARN: [5m]",tenant="t"}[2h] offset 1d reads [2h], '
        "exceeds max allowed [1h]"]


def test_a_reader_that_cannot_read_the_expressions_is_named_not_a_crash(
        tmp_path, monkeypatch, capsys):
    """#4080: da-read failing on promql-reads (older than lint, say) is a
    caller error naming the file — not lint's own crash."""
    f = tmp_path / "r.yaml"
    f.write_text("groups:\n- name: g\n  rules:\n  - {record: 'a:x', expr: 'rate(x[5m])'}\n",
                 encoding="utf-8")

    def stale(exprs, *a, **k):
        raise lint_custom_rules._lib_consumer_read.ConsumerReadError(
            "da-read printed JSON without one reading per expression")

    monkeypatch.setattr(lint_custom_rules._lib_consumer_read, "promql_reads", stale)
    assert _run_main([str(f), "--ci"], monkeypatch) == 2
    err = capsys.readouterr().err
    assert "r.yaml: expressions: da-read printed JSON" in err, err
    assert "internal error" not in err and "Traceback" not in err, err


def test_the_window_is_compared_in_whole_milliseconds(policy):
    """1001ms against a 1.001s max: 1.001 * 1000 is 1000.9999999999999 in
    floating point, so a float comparison called it over."""
    at = lint_custom_rules.lint_expr("rate(x[1001ms])", {**policy, "max_range_duration": "1s1ms"},
                                     "f.yaml", "A")
    over = lint_custom_rules.lint_expr("rate(x[1002ms])", {**policy, "max_range_duration": "1s1ms"},
                                       "f.yaml", "A")
    assert not [r for r in at if "exceeds" in r.message], at
    assert [r for r in over if "exceeds" in r.message], over


@pytest.mark.parametrize("expr, name", [
    ("topk(3, x)", "topk"),
    ("topk by (a) (3, x)", "topk"),              # the regex saw no `topk(` here
    ("topk without (a) (3, x)", "topk"),
    ("quantile by (a) (0.9, x)", "quantile"),
    ("predict_linear\n(x[5m], 60)", "predict_linear"),
])
def test_a_denied_function_or_aggregation_is_found_however_it_is_spelt(expr, name):
    policy = {**lint_custom_rules.builtin_policy(), "denied_functions": [name]}
    found = lint_custom_rules.lint_expr(expr, policy, "f.yaml", "A")
    assert [r.message for r in found] == [f"denied function '{name}' in expr"]


@pytest.mark.parametrize("expr", [
    'x{a="predict_linear("}',                    # a label value
    "x # predict_linear(\n",                     # a comment
    "predict_linear_total",                      # a metric name
    "sum by (predict_linear) (x)",               # a grouping label
])
def test_a_function_name_that_is_no_call_is_no_finding(expr):
    policy = {**lint_custom_rules.builtin_policy(), "denied_functions": ["predict_linear"]}
    found = lint_custom_rules.lint_expr(expr, policy, "f.yaml", "A")
    assert not [r for r in found if "denied function" in r.message], found


_FULL_SELECTOR = {"selector": "x[5m]", "start": 5, "end": 10, "range_ms": 300000,
                  "range": "5m", "subqueries_ms": [], "subqueries": [], "window_ms": 300000,
                  "window": "5m"}


@pytest.mark.parametrize("entry", [
    {"selectors": [], "calls": []},                                   # no problem key
    {"selectors": [], "calls": [], "problem": 5},
    {"selectors": [{k: v for k, v in _FULL_SELECTOR.items() if k != "window_ms"}],
     "calls": [], "problem": None},
    {"selectors": [], "calls": [{"kind": "function"}], "problem": None},
], ids=["no-problem", "problem-not-text", "selector-no-window", "call-no-name"])
def test_a_reading_not_of_the_promql_reads_shape_is_a_reader_fault(tmp_path, entry):
    """An incomplete answer (an older or broken da-read) is a ConsumerReadError
    before lint reads a field of it — never a KeyError in lint_expr."""
    fake = tmp_path / "da-read"
    fake.write_text("#!/bin/sh\ncat >/dev/null\nprintf '%s\\n' '"
                    + json.dumps({"documents": [[entry]]}) + "'\n", encoding="utf-8")
    fake.chmod(0o755)
    import _lib_consumer_read
    assert _lib_consumer_read._is_reads({"selectors": [_FULL_SELECTOR], "calls": [],
                                         "problem": None})
    with pytest.raises(_lib_consumer_read.ConsumerReadError):
        _lib_consumer_read.promql_reads(["rate(x[5m])"], binary=str(fake))


def test_an_expression_prometheus_cannot_evaluate_is_an_error(policy):
    """`[step()]` parses, and the rule fails every evaluation."""
    found = lint_custom_rules.lint_expr("rate(x[step()])", policy, "f.yaml", "A")
    assert [r.severity for r in found] == ["ERROR"]
    assert "Prometheus cannot evaluate the expression" in found[0].message
    assert "greater than 0" in found[0].message


def test_a_file_is_read_by_prometheus_in_one_call(tmp_path, monkeypatch):
    """lint_file hands every expression of the file to da-read at once, each
    rule judged on its own reading."""
    calls = []
    real = lint_custom_rules._lib_consumer_read.promql_reads

    def spy(exprs, *a, **k):
        calls.append(list(exprs))
        return real(exprs, *a, **k)

    monkeypatch.setattr(lint_custom_rules._lib_consumer_read, "promql_reads", spy)
    f = tmp_path / "r.yaml"
    f.write_text("groups:\n- name: g\n  rules:\n"
                 "  - {record: 'a:x', expr: 'rate(x[5m])'}\n"
                 "  - {record: 'b:x', expr: 'rate(y[1w])'}\n"
                 "  - {record: 'c:x', expr: 'rate(z[1m])'}\n", encoding="utf-8")
    results, count = lint_custom_rules.lint_file(str(f), lint_custom_rules.builtin_policy())
    assert calls == [["rate(x[5m])", "rate(y[1w])", "rate(z[1m])"]] and count == 3
    over = [r for r in results if "exceeds max allowed" in r.message]
    assert [r.rule_name for r in over] == ["b:x"], results


def test_a_compound_group_interval_is_judged(tmp_path):
    """#4061: a group `interval: 1h30m` was skipped (unreadable); read as
    Prometheus does, it is over a 60s maximum."""
    found = lint_custom_rules.lint_group_interval("1h30m", lint_custom_rules.builtin_policy(),
                                                  "f.yaml", "g")
    assert any("exceeds recommended max" in r.message for r in found), found


def test_every_problem_of_the_overrides_is_named_at_once():
    with pytest.raises(_lib_exitcodes.CallerError) as exc:
        lint_custom_rules.load_policy({"file_overrides": [
            "x", {"path": "a.yaml", "policy": {"max_range_duration": "x"}}]}, "p.yaml")
    assert str(exc.value).count("file_overrides[") == 2, str(exc.value)


def test_the_built_in_overrides_are_of_the_shape_judged():
    """The built-in policy is judged as a --policy is (#3977)."""
    assert lint_custom_rules.file_overrides_problems(
        lint_custom_rules.builtin_policy()["file_overrides"]) == []
    assert lint_custom_rules.load_policy(None) == lint_custom_rules.builtin_policy()


def test_an_entry_a_file_matches_relaxes_only_its_listed_keys(tmp_path):
    """The override applies as before for a policy that loads."""
    policy = lint_custom_rules.load_policy({"file_overrides": [{
        "path": "rule-packs/x.yaml", "require_generated_marker": False,
        "policy": {"denied_functions": []}}]}, "p.yaml")
    effective, results = lint_custom_rules.resolve_file_policy(
        str(tmp_path / "rule-packs" / "x.yaml"), "groups: []\n", policy)
    assert results == []
    assert effective["denied_functions"] == []
    assert effective["required_labels"] == policy["required_labels"]


def test_lint_result_error_format():
    """測試 LintResult 錯誤格式。"""
    r = lint_custom_rules.LintResult("test.yaml", "MyRule", None, "ERROR", "bad thing")
    assert str(r) == "ERROR: test.yaml [MyRule] - bad thing"

def test_lint_result_with_line_hint():
    """測試 LintResult 包含行提示。"""
    r = lint_custom_rules.LintResult("test.yaml", "MyRule", 42, "WARN", "warning")
    assert str(r) == "WARN: test.yaml:42 [MyRule] - warning"

def test_lint_result_no_rule_name():
    """測試 LintResult 無規則名稱。"""
    r = lint_custom_rules.LintResult("test.yaml", None, None, "ERROR", "msg")
    assert str(r) == "ERROR: test.yaml - msg"


# ── Which files are rule files (#3206, #3426) ──────────────────────
# Which files are linted is the caller's declaration, by path: a directory's
# *.yaml / *.yml less `--exclude` (`_lib_consumer_read.files_to_read`). Every
# file chosen is read by `_lib_consumer_read.rule_files_in`: a file that is
# no manifest and no rule file — a recipe, the registry beside the packs, a
# Chart.yaml — is an ERROR naming it with the `--exclude` hint (#3426 round
# 4: no content test told them apart from a broken rule file); a rule file
# the loader refuses is an ERROR naming it.

_RECIPE = "recipe: threshold\nparams:\n  metric: {type: string}\n"
_REGISTRY = "entries:\n- metric: mysql_up\n  default: 1\n"
_OK_RULE = (
    "groups:\n- name: g\n  rules:\n  - alert: Ok\n    expr: up{tenant=\"t\"} == 0\n"
    "    labels: {tenant: t, severity: warning}\n"
)


def _main_code():
    """`main()` 以 `run()` 的方式結束後的 exit code：`sys.exit` 的碼、乾淨 return 的 0，
    或 `CallerError`（#3901：宣告的輸入被拒）——stderr 一行、rc 2。"""
    from _lib_exitcodes import CallerError, exit_on_crash
    try:
        lint_custom_rules.main()
    except SystemExit as exc:
        return exc.code
    except CallerError as exc:
        try:
            exit_on_crash(exc)
        except SystemExit as done:
            return done.code
    return 0


def _run_main(argv, monkeypatch):
    """main() 的 exit code：失敗時 sys.exit，乾淨時正常 return（= 0）。"""
    monkeypatch.setattr(sys, "argv", ["lint_custom_rules", *argv])
    return _main_code()


_CHART = "apiVersion: v2\nname: c\nversion: 0.1.0\n"


def test_yaml_that_is_no_rule_file_is_named_until_excluded(tmp_path, monkeypatch, capsys):
    """(a) #3426：目錄內不是 rule file 也不是 manifest 的 YAML（recipe、registry、
    Chart.yaml）→ 逐檔 ERROR、建議 `--exclude`；以 `--exclude` 排除後通過，
    被排除的檔不讀。"""
    (tmp_path / "recipes").mkdir()
    (tmp_path / "recipes" / "threshold.yaml").write_text(_RECIPE, encoding="utf-8")
    (tmp_path / "threshold-registry.yaml").write_text(_REGISTRY, encoding="utf-8")
    (tmp_path / "helm").mkdir()
    (tmp_path / "helm" / "Chart.yaml").write_text(_CHART, encoding="utf-8")
    (tmp_path / "rules.yaml").write_text(_OK_RULE, encoding="utf-8")
    assert _run_main([str(tmp_path), "--ci"], monkeypatch) == 1
    errors = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("ERROR")]
    assert len(errors) == 3, errors
    for name in ("Chart.yaml", "threshold.yaml", "threshold-registry.yaml"):
        (line,) = [e for e in errors if f"{name} - " in e]
        assert "no reader reads it" in line and "--exclude" in line, line
    seen = []
    real = lint_custom_rules.read_rule_files
    monkeypatch.setattr(lint_custom_rules, "read_rule_files",
                        lambda files: seen.extend(files) or real(files))
    assert _run_main([str(tmp_path), "--ci", "--exclude", "Chart.yaml", "--exclude",
                      "recipes/*", "--exclude", "threshold-registry.yaml"], monkeypatch) == 0
    assert "ERROR" not in capsys.readouterr().out
    # the excluded ones are not read (#4098: the shipped rule packs are, for
    # the records a rule may read — they are not the scan's)
    assert [f for f in seen if str(f).startswith(str(tmp_path))] == [str(tmp_path / "rules.yaml")]


@pytest.mark.parametrize("name,text,why", [
    # a rule pack whose `groups` is misspelt: no rule file, never "no rules"
    ("grups.yaml", "grups:\n- name: g\n  rules: []\n", "it has no `kind`"),
    # a ConfigMap whose data key lost an indent: a top-level unknown field
    ("cm.yaml", "apiVersion: v1\nkind: ConfigMap\nmetadata: {name: c}\ndata:\n"
                "rules.yml: |\n  groups: []\n", 'unknown field "rules.yml"'),
])
def test_a_file_that_reads_as_no_rule_file_is_an_error(tmp_path, monkeypatch, capsys, name,
                                                       text, why):
    (tmp_path / name).write_text(text, encoding="utf-8")
    assert _run_main([str(tmp_path), "--ci"], monkeypatch) == 1
    errors = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("ERROR")]
    assert len(errors) == 1 and name in errors[0] and why in errors[0], errors


def test_a_prometheusrule_is_linted(tmp_path, monkeypatch, capsys):
    """#3426 F11: a PrometheusRule's `spec` is a rule file the Operator
    writes; it used to be passed over (ConfigMaps only)."""
    (tmp_path / "pr.yaml").write_text(
        "apiVersion: monitoring.coreos.com/v1\nkind: PrometheusRule\nmetadata: {name: p}\n"
        "spec:\n  groups:\n  - name: g\n    rules:\n"
        "    - {alert: Forecast, expr: 'predict_linear(x[1h], 3600) < 0',"
        " labels: {tenant: t, severity: warning}}\n", encoding="utf-8")
    assert _run_main([str(tmp_path), "--ci"], monkeypatch) == 1
    out = capsys.readouterr().out
    assert "Scanned 1 file(s), 1 rule(s)." in out, out
    assert "pr.yaml" in out and "predict_linear" in out, out


@pytest.mark.parametrize("text,why", [
    # PromQL the loader does not parse
    ("groups:\n- name: g\n  rules:\n  - {alert: B, expr: 'rate(x[5m]'}\n", "parse error"),
    # a key Prometheus does not know
    ("groups:\n- name: g\n  type: prometheus\n  rules: []\n", "field type not found"),
    # a second document Prometheus would drop
    (_OK_RULE + "---\nfoo: 1\n", "document 2"),
    # neither kubectl's reading nor the loader reads it: never skipped
    ("groups: [unclosed\n", "rule loader"),
    # a tab in the indentation: both refuse it, so it cannot be ruled out
    ("groups:\n  - name: x\n    rules:\n\t- alert: BrokenTabAlert\n      expr: up == 0\n",
     "rule loader"),
])
def test_broken_plain_rule_file_is_an_error(tmp_path, monkeypatch, capsys, text, why):
    """(b) 有 `groups:` 但規則載入器拒收的 plain 檔 → ERROR，點名檔案。"""
    bad = tmp_path / "broken.yaml"
    bad.write_text(text, encoding="utf-8")
    (tmp_path / "recipe.yaml").write_text(_RECIPE, encoding="utf-8")
    assert _run_main([str(tmp_path), "--ci", "--exclude", "recipe.yaml"], monkeypatch) == 1
    out = capsys.readouterr().out
    errors = [line for line in out.splitlines() if line.startswith("ERROR")]
    assert len(errors) == 1, errors
    assert "broken.yaml" in errors[0] and "rule loader refuses" in errors[0], errors
    assert why in out, out


def test_rule_file_kubectl_refuses_but_prometheus_loads_is_linted(tmp_path):
    """kubectl 的讀法（sigs.k8s.io/yaml）拒收、規則載入器會讀的檔：照樣 lint，
    不因 Kubernetes 讀法拒收而略過。`.inf` 對 sigs.k8s.io/yaml 是 +Inf（無
    JSON 表示），對 rulefmt 是字串 ".inf"。"""
    rule = tmp_path / "inf.yaml"
    rule.write_text(
        "groups:\n- name: g\n  rules:\n  - alert: Forecast\n"
        "    expr: predict_linear(disk_free[1h], 3600) < 0\n"
        "    annotations: {x: .inf}\n", encoding="utf-8")
    import _lib_consumer_read
    with pytest.raises(_lib_consumer_read.InputRefused):
        _lib_consumer_read.kubernetes_documents(str(rule))
    results, count = lint_custom_rules.lint_file(str(rule), lint_custom_rules.builtin_policy())
    assert count == 1
    assert any(r.severity == "ERROR" and "predict_linear" in r.message
               for r in results), [str(r) for r in results]


def test_repo_rule_packs_pass_the_ci_policy(monkeypatch, capsys):
    """(c) smoke：repo 真實的 rule-packs/ 以 CI 的 policy、CI 的呼叫方式
    （`--rule-pack-dir`：依 rule_pack_files 的宣告取檔）跑 --ci → rc 0。"""
    code = _run_main(["--rule-pack-dir", os.path.join(_REPO_ROOT, "rule-packs"),
                      "--policy", _REAL_POLICY, "--ci"], monkeypatch)
    out = capsys.readouterr().out
    assert code == 0, [line for line in out.splitlines() if line.startswith("ERROR")]


def test_configmap_good_and_broken_rule_values(tmp_path, monkeypatch, capsys):
    """ConfigMap 一好一壞兩個 data 值 → 壞的被點名、rc 非零。"""
    good = _OK_RULE.replace("\n", "\n    ")
    bad = "groups:\n- name: g\n  rules:\n  - {alert: B, expr: 'rate(x[5m]'}\n".replace(
        "\n", "\n    ")
    (tmp_path / "cm.yaml").write_text(
        "apiVersion: v1\nkind: ConfigMap\nmetadata: {name: rules}\ndata:\n"
        f"  good.yml: |\n    {good}\n  bad.yml: |\n    {bad}\n", encoding="utf-8")
    assert _run_main([str(tmp_path), "--ci"], monkeypatch) == 1
    errors = [line for line in capsys.readouterr().out.splitlines()
              if line.startswith("ERROR")]
    assert len(errors) == 1, errors
    assert "cm.yaml" in errors[0] and "'bad.yml'" in errors[0], errors


def _cm_list(rule_text: str) -> str:
    """A `kind: List` (`kubectl get cm -o yaml`) holding one ConfigMap whose
    `r.yml` is *rule_text*."""
    body = "".join(f"        {ln}\n" for ln in rule_text.splitlines())
    return ("apiVersion: v1\nkind: List\nitems:\n- apiVersion: v1\n  kind: ConfigMap\n"
            "  metadata: {name: listed}\n  data:\n    r.yml: |\n" + body)


def test_a_configmap_inside_a_list_is_linted(tmp_path, monkeypatch, capsys):
    """#3426: the rule files a file holds are `rule_files_in`'s answer, which
    reads a List as its items — a ConfigMap in one used to be passed over
    (`read_rule_files(['list.yaml']) == {'list.yaml': []}`)."""
    lst = tmp_path / "list.yaml"
    lst.write_text(_cm_list("groups:\n- name: g\n  rules:\n  - {alert: B, expr: 'rate(x[5m]'}\n"),
                   encoding="utf-8")
    got = lint_custom_rules.read_rule_files([str(lst)])[str(lst)]
    assert [(rf.kind, rf.key) for rf in got] == [("ConfigMap", "r.yml")]
    assert _run_main([str(tmp_path), "--ci"], monkeypatch) == 1
    out = capsys.readouterr().out
    assert "data key 'r.yml'" in out and "parse error" in out, out


# ── #3426 round 3: shapes the API server refuses are named, not read as empty ──
_PR_TOP_GROUPS = ("apiVersion: monitoring.coreos.com/v1\nkind: PrometheusRule\n"
                  "metadata: {name: p}\ngroups:\n- name: g\n  rules:\n  - {alert: A, expr: up}\n")
_CM_DATA_LIST = ("apiVersion: v1\nkind: ConfigMap\nmetadata: {name: c}\n"
                 "data:\n- groups: []\n")
_APIVERSION_NO_KIND = "apiVersion: v1\nmetadata: {name: c}\ndata: {r.yml: 'groups: []'}\n"


@pytest.mark.parametrize("text,why", [
    (_CM_DATA_LIST, "its data is list, not a mapping"),
    (_APIVERSION_NO_KIND, "it has no `kind`"),
])
def test_a_manifest_the_api_server_refuses_is_an_error(tmp_path, monkeypatch, capsys, text, why):
    """A ConfigMap whose `data` is a list used to be a ConfigMap with no
    rule files; a manifest with an `apiVersion` but no `kind` used to be
    other YAML. Both are named now."""
    (tmp_path / "bad.yaml").write_text(text, encoding="utf-8")
    assert _run_main([str(tmp_path), "--ci"], monkeypatch) == 1
    out = capsys.readouterr().out
    errors = [ln for ln in out.splitlines() if ln.startswith("ERROR")]
    assert len(errors) == 1 and "bad.yaml" in errors[0] and why in errors[0], errors


# ── #3426 round 5: --rule-pack-dir, exclude-all, no object ─────────

def _rule_packs_tree(tmp_path):
    rp = tmp_path / "rule-packs"
    (rp / "recipes").mkdir(parents=True)
    (rp / "rule-pack-x.yaml").write_text(_OK_RULE, encoding="utf-8")
    (rp / "threshold-registry.yaml").write_text(_REGISTRY, encoding="utf-8")
    (rp / "recipes" / "threshold.yaml").write_text(_RECIPE, encoding="utf-8")
    return rp


def test_rule_pack_dir_lints_the_declared_packs_only(tmp_path, monkeypatch, capsys):
    """F13: `--rule-pack-dir` takes the packs from `rule_pack_files` (the one
    declaration) — the registry and recipes/ are not read, with no
    `--exclude` written anywhere."""
    rp = _rule_packs_tree(tmp_path)
    assert _run_main(["--rule-pack-dir", str(rp), "--ci"], monkeypatch) == 0
    out = capsys.readouterr().out
    assert "Scanned 1 file(s)" in out and "ERROR" not in out, out


def test_rule_pack_dir_names_an_undeclared_yaml(tmp_path, monkeypatch, capsys):
    rp = _rule_packs_tree(tmp_path)
    (rp / "extra-pack.YAML").write_text(_OK_RULE, encoding="utf-8")
    assert _run_main(["--rule-pack-dir", str(rp), "--ci"], monkeypatch) == 2
    assert "extra-pack.YAML" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [
    [],                                              # neither
    ["{rp}", "--rule-pack-dir", "{rp}"],            # both
    ["--rule-pack-dir", "{rp}", "--exclude", "x"],  # a pattern beside the declaration
])
def test_rule_pack_dir_is_exactly_one_way_to_choose(tmp_path, monkeypatch, argv):
    rp = _rule_packs_tree(tmp_path)
    assert _run_main([a.replace("{rp}", str(rp)) for a in argv], monkeypatch) == 2


def test_an_exclude_that_leaves_nothing_is_a_caller_error(tmp_path, monkeypatch, capsys):
    """F16: every file found excluded is no clean run."""
    (tmp_path / "a.yaml").write_text(_OK_RULE, encoding="utf-8")
    assert _run_main([str(tmp_path), "--exclude", "*.yaml", "--ci"], monkeypatch) == 2
    assert "every one excluded" in capsys.readouterr().err


def test_a_file_with_no_object_is_an_error(tmp_path, monkeypatch, capsys):
    """F14: an empty file in a scanned directory is named (kubectl: no
    objects passed to apply)."""
    (tmp_path / "rules.yaml").write_text(_OK_RULE, encoding="utf-8")
    (tmp_path / "empty.yml").write_text("# nothing yet\n", encoding="utf-8")
    assert _run_main([str(tmp_path), "--ci"], monkeypatch) == 1
    errors = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("ERROR")]
    assert len(errors) == 1 and "empty.yml" in errors[0] and "no objects" in errors[0], errors


# ── #3797: the lint policy has its own keys and no other ──────────────────

def test_a_key_the_lint_policy_does_not_have_is_refused():
    """A deployment policy key here (or a misspelt one) was silently unused:
    `validate-config --policy` handed the deployment policy to lint, and a
    typo linted by the default. Refused, naming the key and, for a
    deployment key, where it goes."""
    from _lib_exitcodes import CallerError
    with pytest.raises(CallerError) as exc:
        lint_custom_rules.load_policy({"allowed_domains": [], "denied_functions": []}, "p.yaml")
    assert exc.value.reason == "policy_unknown_key"
    assert str(exc.value).startswith("--policy p.yaml: the custom-rule lint policy has no "
                                     "key(s) allowed_domains"), str(exc.value)
    assert "validate-config --lint-policy" in str(exc.value)
    with pytest.raises(CallerError) as exc:
        lint_custom_rules.load_policy({"denied_function": []}, "p.yaml")
    assert "denied_function" in str(exc.value) and "deployment policy" not in str(exc.value)


def test_the_lint_policy_keys_are_the_ones_it_reads():
    """The keys are what this module judges (#4099: not read off the
    built-in file, which is judged by them) — and the built-in policy has
    each of them."""
    assert lint_custom_rules.POLICY_KEYS == (
        frozenset(lint_custom_rules.POLICY_VALUE_PROBLEMS) | {"file_overrides"})
    assert set(lint_custom_rules.builtin_policy()) == lint_custom_rules.POLICY_KEYS
    assert lint_custom_rules.load_policy(lint_custom_rules.builtin_policy(), "p") \
        == lint_custom_rules.builtin_policy()


def test_the_repository_lint_policy_holds_lint_keys_only():
    """#3797: the repository's own lint policy and deployment policy are two
    files (CI lints with one and judges receivers with the other)."""
    root = os.path.join(os.path.dirname(__file__), "..", "..", ".github")
    lint = _read_policy(os.path.join(root, "custom-rule-policy.yaml"))
    assert set(lint) <= lint_custom_rules.POLICY_KEYS, sorted(set(lint) - lint_custom_rules.POLICY_KEYS)
    with open(os.path.join(root, "deployment-policy.yaml"), encoding="utf-8") as f:
        deploy = yaml.safe_load(f)
    assert set(deploy) == {"allowed_domains"}, deploy
