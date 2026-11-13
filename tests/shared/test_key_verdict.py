"""ADR-038 決定 4–7：共用判決（`da-guard key-verdict`，經 `_lib_key_verdict` 讀）。

每棵樹都用真的 da-guard 跑：判決是 Go 的（`pkg/keyverdict`），這裡釘住它對
真實的樹給出的答案；樹到不了的分支在 `pkg/keyverdict/keyverdict_test.go`。
"""
from __future__ import annotations

from pathlib import Path

import pytest

import _lib_key_verdict as kv
import _lib_tenant_values as tv

pytestmark = pytest.mark.usefixtures("da_guard_env")


def _tree(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    return root


def _by_key(verdict, tenant):
    return {v.key: v for v in verdict.keys[tenant]}


_ADR_DEFAULTS = "defaults:\n  mysql_connection_usage_percent: 80\n  mysql_slave_lag: 30\n  redis_memory: 70\n"
_ADR_TENANT = ("tenants:\n  db-a:\n"
               "    mysql_conections: \"70\"\n"
               "    mysql_slave_lagg: disable\n"
               "    _routng: disable\n"
               "    redis_memory: disable\n"
               "    redis_memory_critical: \"90\"\n"
               "    _namespaces: [prod-ns]\n")


def test_the_adr_example(tmp_path):
    """ADR-038「範例」那張表：判決、等級、讀取端與 disable 欄位逐列相同。"""
    t = kv.judge_tree(_tree(tmp_path, {"_defaults.yaml": _ADR_DEFAULTS, "db-a.yaml": _ADR_TENANT}))
    got = {k: (v.read_by, v.verdict, v.severity, v.disable) for k, v in _by_key(t, "db-a").items()}
    assert got == {
        "mysql_conections": ((), "unread", "error", None),
        "mysql_slave_lagg": ((), "unread", "warning", "noop"),
        "_routng": ((), "unread", "error", "noop"),
        "redis_memory": (("exporter",), "read", None, "effective"),
        "redis_memory_critical": (("exporter",), "read", None, None),
        "_namespaces": (("annotation",), "read", None, None),
        # 繼承來的閾值：exporter 有送
        "mysql_connection_usage_percent": (("exporter",), "read", None, None),
        "mysql_slave_lag": (("exporter",), "read", None, None),
    }
    # `redis_memory: disable` 連同 `redis_memory_critical` 一起關掉（#3046）：
    # 那條 critical 是 exporter 讀了、跟著 base 關掉的，算 read；它是 db-a 自己檔裡
    # 寫的，舊語意下會照送，所以報 disable_cascade（warning）。
    kinds = {(f.kind, f.key, f.severity) for f in t.findings}
    assert kinds == {("unread", "mysql_conections", "error"), ("unread", "_routng", "error"),
                     ("disable_noop", "mysql_slave_lagg", "warning"),
                     ("disable_cascade", "redis_memory_critical", "warning")}
    assert not t.undecidable


def test_a_disable_switches_off_its_whole_family_wherever_it_comes_from(tmp_path):
    """#3046：base 寫 `disable` 連帶關掉同族的 `_critical` 與維度鍵，不分同族鍵
    來自哪一層（租戶檔、子目錄 `_defaults.yaml`、profile、根平台檔 `tenants:`、
    根 defaults 的維度預設值，#1231 舊拼法亦同）。這些同族鍵 exporter 讀了才關，
    判 read、沒有 finding；/metrics 不送。"""
    t = kv.judge_tree(_tree(tmp_path, {
        "_defaults.yaml": "dimensions:\n  mysql_connection_usage_percent: [db]\n  mysql_threads_running: [db]\n"
                          "defaults:\n  redis_memory: 70\n  mysql_connection_usage_percent: 80\n"
                          "  mysql_connection_usage_percent{db=\"x\"}: 12\n  pg_conn: 50\n  mysql_threads_running: 50\n",
        "_platform.yaml": "tenants:\n  tp:\n    redis_memory_critical: \"95\"\n",
        "team/_defaults.yaml": "defaults:\n  redis_memory_critical: 95\n",
        "_profiles.yaml": "profiles:\n  p1:\n    pg_conn_critical: 96\n",
        "t.yaml": "tenants:\n  ta:\n    redis_memory: disable\n    redis_memory_critical: \"90\"\n"
                  "    mysql_connection_usage_percent: disable\n    mysql_connection_usage_percent{db=\"y\"}: \"7\"\n"
                  "  tc:\n    _profile: p1\n    pg_conn: disable\n",
        "team/tb.yaml": "tenants:\n  tb:\n    redis_memory: disable\n",
        "tp.yaml": "tenants:\n  tp:\n    redis_memory: disable\n",
        "own.yaml": "tenants:\n  town:\n    mysql_cpu: disable\n    mysql_cpu_critical: \"90\"\n"
                    "    mysql_cpu{db=\"z\"}: \"4\"\n",
    }))
    family = {"ta": ("redis_memory_critical", 'mysql_connection_usage_percent{db="y"}'),
              "tb": ("redis_memory_critical",), "tc": ("pg_conn_critical",), "tp": ("redis_memory_critical",),
              "town": ("mysql_cpu_critical", 'mysql_cpu{db="z"}')}
    for tenant, keys in family.items():
        by = _by_key(t, tenant)
        for k in keys:
            v = by[k]
            assert (v.read_by, v.verdict, v.severity, v.reason, v.served) == \
                (("exporter",), "read", None, None, None), (tenant, k, v)
    # 只有租戶自己檔裡寫的同族鍵報 disable_cascade（warning，不擋），繼承來的不報。
    got = {(f.kind, f.key, f.tenants, f.base, f.blocks) for f in t.findings}
    assert got == {
        ("disable_cascade", "redis_memory_critical", ("ta",), "redis_memory", False),
        ("disable_cascade", 'mysql_connection_usage_percent{db="y"}', ("ta",), "mysql_connection_usage_percent", False),
        ("disable_cascade", "mysql_cpu_critical", ("town",), "mysql_cpu", False),
        ("disable_cascade", 'mysql_cpu{db="z"}', ("town",), "mysql_cpu", False),
    }, t.findings


def test_a_platform_side_typo_is_one_error_naming_the_file_and_its_tenants(tmp_path):
    """profile 裡的錯字：每個選用它的租戶都讀不到，報成一筆 error，指名 profile 檔
    （#3394：等級描述缺陷，不因寫在平台側而降級；誰被擋看檔，決定 6）。"""
    files = {"_defaults.yaml": "defaults:\n  mysql_connection_usage_percent: 80\n",
             "_profiles.yaml": "profiles:\n  p1:\n    mysql_conections: 70\n"}
    for t in ("ta", "tb", "tc"):
        files[f"{t}.yaml"] = f"tenants:\n  {t}:\n    _profile: p1\n"
    t = kv.judge_tree(_tree(tmp_path, files))
    for tenant in ("ta", "tb", "tc"):
        v = _by_key(t, tenant)["mysql_conections"]
        assert (v.verdict, v.severity, v.file) == ("unread", "error", "_profiles.yaml")
    unread = [f for f in t.findings if f.key == "mysql_conections"]
    assert len(unread) == 1 and unread[0].tenants == ("ta", "tb", "tc") and unread[0].blocks
    assert t.blocks


def test_a_tenant_own_typo_is_one_error_per_tenant(tmp_path):
    files = {"_defaults.yaml": "defaults:\n  mysql_connection_usage_percent: 80\n"}
    for t in ("ta", "tb"):
        files[f"{t}.yaml"] = f"tenants:\n  {t}:\n    mysql_conections: 70\n"
    t = kv.judge_tree(_tree(tmp_path, files))
    unread = [f for f in t.findings if f.key == "mysql_conections"]
    assert sorted(f.tenants for f in unread) == [("ta",), ("tb",)]
    assert all(f.severity == "error" for f in unread)


def test_an_unregistered_key_the_root_declares_is_unread(tmp_path):
    """#3045：根 defaults 宣告了沒登記的 `_myth`，exporter 也不送（任何一層都不送）。
    所以它和其他沒人讀的鍵一樣是 `unread`、error：寫在租戶自己的檔的算在該租戶；
    只從根 defaults 繼承的報一筆在根 defaults，列出它到達的租戶（#3394）。"""
    t = kv.judge_tree(_tree(tmp_path, {
        "_defaults.yaml": "defaults:\n  mysql_connection_usage_percent: 80\n  _myth: 5\n",
        "ta.yaml": "tenants:\n  ta:\n    _myth: 7\n",
        "tb.yaml": "tenants:\n  tb:\n    mysql_connection_usage_percent: 60\n"}))
    a, b = _by_key(t, "ta")["_myth"], _by_key(t, "tb")["_myth"]
    assert (a.read_by, a.verdict, a.severity, a.reason, a.file) == (
        (), "unread", "error", "unregistered", "ta.yaml")
    assert (b.read_by, b.verdict, b.severity, b.reason, b.file) == (
        (), "unread", "error", "unregistered", "_defaults.yaml")
    assert a.served is None and b.served is None
    found = sorted((f.kind, f.severity, f.file, f.tenants) for f in t.findings if f.key == "_myth")
    assert found == [("unread", "error", "_defaults.yaml", ("tb",)),
                     ("unread", "error", "ta.yaml", ("ta",))]


def test_no_root_platform_file_leaves_threshold_keys_undecidable(tmp_path):
    """客戶 CI 只檢出租戶 repo：閾值鍵無從判斷；保留鍵照常判（決定 7）。"""
    t = kv.judge_tree(_tree(tmp_path, {"ta.yaml": "tenants:\n  ta:\n    mysql_conections: 70\n"
                                                  "    _routng: x\n    _namespaces: [n]\n"}))
    got = {k: (v.verdict, v.severity) for k, v in _by_key(t, "ta").items()}
    assert got == {"mysql_conections": ("undecidable", None), "_routng": ("unread", "error"),
                   "_namespaces": ("read", None)}
    assert t.undecidable


def test_a_root_platform_file_without_thresholds_still_decides(tmp_path):
    """只有 profiles 的樹也讀到了根平台檔：照常判，不降級成無從判斷（決定 7）。"""
    t = kv.judge_tree(_tree(tmp_path, {"_profiles.yaml": "profiles:\n  p1: {}\n",
                                       "ta.yaml": "tenants:\n  ta:\n    mysql_conections: 70\n"}))
    assert _by_key(t, "ta")["mysql_conections"].verdict == "unread"
    assert not t.undecidable


@pytest.mark.parametrize("target", ["owner_tag", "owner_tag.sub"])
def test_a_key_a_policy_rule_names_is_read(tmp_path, target):
    t = kv.judge_tree(_tree(tmp_path, {
        "_defaults.yaml": "defaults:\n  mysql_connection_usage_percent: 80\n_policies:\n"
                          f"  - {{name: p, target: '{target}', operator: required, severity: error}}\n",
        "ta.yaml": "tenants:\n  ta:\n    owner_tag: team-a\n    other_tag: x\n"}))
    keys = _by_key(t, "ta")
    assert keys["owner_tag"].read_by == ("policy_engine",) and keys["owner_tag"].verdict == "read"
    # 對照：規則沒有指名的鍵照樣沒人讀
    assert keys["other_tag"].verdict == "unread"


def test_a_wildcard_rule_names_no_key(tmp_path):
    """`mysql_*` 會比到拼錯的 `mysql_conections`；萬用字元不算指名，不替錯字背書。"""
    t = kv.judge_tree(_tree(tmp_path, {
        "_defaults.yaml": "defaults:\n  mysql_connection_usage_percent: 80\n_policies:\n"
                          "  - {name: p, target: 'mysql_*', operator: lte, value: 60, severity: error}\n",
        "ta.yaml": "tenants:\n  ta:\n    mysql_conections: 70\n"}))
    assert _by_key(t, "ta")["mysql_conections"].verdict == "unread"


def test_a_rule_credits_no_tenant_it_excludes_and_no_unregistered_reserved_key(tmp_path):
    t = kv.judge_tree(_tree(tmp_path, {
        "_defaults.yaml": "defaults:\n  mysql_connection_usage_percent: 80\n_policies:\n"
                          "  - {name: p, target: owner_tag, operator: required, severity: error,"
                          " exclude_tenants: [tb]}\n"
                          "  - {name: q, target: _typo_reserved, operator: required, severity: error}\n",
        "ta.yaml": "tenants:\n  ta:\n    owner_tag: a\n    _typo_reserved: 1\n",
        "tb.yaml": "tenants:\n  tb:\n    owner_tag: b\n"}))
    assert _by_key(t, "ta")["owner_tag"].verdict == "read"
    assert _by_key(t, "tb")["owner_tag"].verdict == "unread"
    assert _by_key(t, "ta")["_typo_reserved"].verdict == "unread"


def test_a_reserved_key_a_subtree_writes_is_not_judged_here(tmp_path):
    """子目錄 `_defaults.yaml` 不帶保留鍵（#3341）：exporter 不套用、effective 不顯示，所以共用
    判決看不到它，改由 da-guard 的 subtree_default_reserved_key error 報。#3341 以前登錄表說
    exporter 讀這一層的 `_state_<f>`、`_severity_dedup`，這裡判成 read。"""
    t = kv.judge_tree(_tree(tmp_path, {
        "_defaults.yaml": "defaults:\n  mysql_connection_usage_percent: 80\n"
                          "state_filters:\n  maint: {reasons: [X], severity: critical}\n",
        "team/_defaults.yaml": "defaults:\n  _state_maint: enable\n  _severity_dedup: enable\n"
                               "  _silent_mode: warning\n",
        "team/ta.yaml": "tenants:\n  ta:\n    mysql_connection_usage_percent: 70\n"}))
    keys = _by_key(t, "ta")
    assert "mysql_connection_usage_percent" in keys  # 不是恆真：這個租戶真的被判了
    for k in ("_state_maint", "_severity_dedup", "_silent_mode"):
        assert k not in keys, (k, keys[k])


def test_an_undeclared_state_filter_is_a_platform_notice(tmp_path):
    """租戶寫了平台沒宣告的 `_state_foo`：缺的是平台的宣告，報成平台側 notice（決定 7）。"""
    t = kv.judge_tree(_tree(tmp_path, {
        "_defaults.yaml": "defaults:\n  mysql_connection_usage_percent: 80\n",
        "ta.yaml": "tenants:\n  ta:\n    _state_foo: enable\n",
        "tb.yaml": "tenants:\n  tb:\n    _state_foo: enable\n"}))
    v = _by_key(t, "ta")["_state_foo"]
    assert (v.verdict, v.severity, v.reason) == ("unread", "notice", "state_filter_undeclared")
    found = [f for f in t.findings if f.key == "_state_foo"]
    assert [(f.file, f.tenants) for f in found] == [("_defaults.yaml", ("ta", "tb"))]


def test_a_declared_reader_counts_only_in_its_layer(tmp_path):
    """`_severity_dedup` 寫在租戶檔：exporter 與路由產生器都讀（登錄表的層）。寫在沒有
    `defaults:` 的子目錄 `_defaults.yaml` 頂層的那份不進 effective（#3341；之前判成只有
    exporter 讀），一層一讀取端的對應由下方的合成輸入釘住。"""
    t = kv.judge_tree(_tree(tmp_path, {
        "_defaults.yaml": "defaults:\n  mysql_connection_usage_percent: 80\n",
        "team/_defaults.yaml": "_severity_dedup: enable\n",
        "team/ta.yaml": "tenants:\n  ta:\n    mysql_connection_usage_percent: 70\n",
        "tb.yaml": "tenants:\n  tb:\n    _severity_dedup: enable\n"}))
    assert "_severity_dedup" not in _by_key(t, "ta")
    assert _by_key(t, "tb")["_severity_dedup"].read_by == ("exporter", "generator")


def test_custom_alerts_have_the_same_readers_in_every_layer(tmp_path):
    """#3042：編譯器照 exporter 的讀法編，`_custom_alerts` 寫在 profile 或租戶檔，
    兩個讀取端都讀。"""
    recipe = ("      - {recipe: threshold, name: n1, metric: m1, op: '>', window: 5m, "
              "threshold: '10:warning'}\n")
    t = kv.judge_tree(_tree(tmp_path, {
        "_defaults.yaml": "defaults:\n  mysql_connection_usage_percent: 80\n",
        "_profiles.yaml": "profiles:\n  p1:\n    _custom_alerts:\n" + recipe,
        "ta.yaml": "tenants:\n  ta:\n    _profile: p1\n",
        "tb.yaml": "tenants:\n  tb:\n    _custom_alerts:\n" + recipe}))
    for tenant in ("ta", "tb"):
        assert _by_key(t, tenant)["_custom_alerts"].read_by == ("custom_alert_compiler", "exporter")


@pytest.mark.parametrize("rule_key, written", [
    ("mysql_threads_running_critical", "mysql_" + "cpu_critical"),
    ("mysql_" + "cpu_critical", "mysql_threads_running_critical"),
    # #3489 review: a dimensional `_critical` — the exporter serves the
    # retired spelling as written, Policy-as-Code still reads it canonically.
    ('mysql_threads_running_critical{db="a"}', "mysql_" + 'cpu_critical{db="a"}'),
    ("mysql_" + 'cpu_critical{db="a"}', 'mysql_threads_running_critical{db="a"}'),
])
def test_a_rule_names_a_key_in_either_alias_spelling(tmp_path, rule_key, written):
    """Policy-as-Code 以別名表比對規則與 key；共用判決用同一個比法。"""
    t = kv.judge_tree(_tree(tmp_path, {
        "_defaults.yaml": "defaults:\n  mysql_connection_usage_percent: 80\n_policies:\n"
                          f"  - {{name: p, target: '{rule_key}', operator: required, severity: error}}\n",
        "ta.yaml": f"tenants:\n  ta:\n    {written}: abc\n"}))
    assert "policy_engine" in _by_key(t, "ta")[written].read_by


def test_an_inherited_undeclared_state_filter_is_the_root_files_notice(tmp_path):
    """平台檔 `tenants:` 給的 `_state_ghost`（#3341 以前用子目錄 `_defaults.yaml`，那一層
    已不帶保留鍵）：缺的是根目錄的宣告。"""
    t = kv.judge_tree(_tree(tmp_path, {
        "_defaults.yaml": "defaults:\n  mysql_connection_usage_percent: 80\n",
        "_platform.yaml": "tenants:\n  ta:\n    _state_ghost: enable\n",
        "team/ta.yaml": "tenants:\n  ta:\n    mysql_connection_usage_percent: 70\n"}))
    v = _by_key(t, "ta")["_state_ghost"]
    assert (v.verdict, v.severity, v.reason) == ("unread", "notice", "state_filter_undeclared")
    assert [f.file for f in t.findings if f.key == "_state_ghost"] == ["_defaults.yaml"]


# ── 子集模式（ADR-038 §5：一筆寫入的租戶）────────────────────────────────

_TWO_TENANTS = {
    "_defaults.yaml": _ADR_DEFAULTS,
    "db-a.yaml": _ADR_TENANT,
    "db-b.yaml": "tenants:\n  db-b:\n    mysql_connection_usage_percent: \"50\"\n    mysql_conections_b: \"1\"\n",
}


def test_a_subset_verdict_is_the_whole_trees_for_those_tenants(tmp_path):
    """`tenants=` 判的就是全樹判決裡那些租戶的部分：每個鍵的判決相同，
    finding 只剩它們的，沒要求的租戶不出現、也不被當成「沒人讀」。"""
    conf_d = _tree(tmp_path, _TWO_TENANTS)
    full = kv.judge_tree(conf_d)
    sub = kv.judge_tree(conf_d, tenants=["db-b"])
    assert sorted(sub.keys) == ["db-b"]
    assert sub.keys["db-b"] == full.keys["db-b"]
    assert sub.findings == [f for f in full.findings if f.tenants == ("db-b",)]
    assert {f.key for f in sub.findings} == {"mysql_conections_b"}, sub.findings


def test_a_requested_tenant_the_tree_lacks_has_no_key(tmp_path):
    conf_d = _tree(tmp_path, _TWO_TENANTS)
    sub = kv.judge_tree(conf_d, tenants=["db-gone"])
    assert sub.keys == {} and sub.findings == []


def test_a_bare_string_is_not_read_as_its_characters(tmp_path):
    """`tenants="db-b"` 不能被拆成 `-`、`b`、`d` 三個不存在的租戶——那樣
    什麼都沒判、判決卻是空的、看起來像通過。"""
    conf_d = _tree(tmp_path, _TWO_TENANTS)
    with pytest.raises(TypeError):
        kv.judge_tree(conf_d, tenants="db-b")


def test_a_profile_finding_in_a_subset_is_judged_over_the_requested_tenants(tmp_path):
    """profile 鍵的 finding 不屬於單一租戶：子集裡只看得到要求的租戶。這裡
    規則讀 `_namespaces`、但排除 db-a，所以全樹裡 db-b 讀到它、profile 不出
    notice；只要求 db-a 時看不到 db-b，profile notice 就出現了。這是 ADR-038
    §5 記下的差異，不是「每個租戶跟全樹一樣」的那部分。"""
    conf_d = _tree(tmp_path, {
        "_defaults.yaml": "defaults:\n  mysql_connection_usage_percent: 80\nprofiles:\n  gold:\n    _namespaces: [a]\n"
                          "_policies:\n  - {name: p, target: _namespaces, operator: required, severity: error,"
                          " exclude_tenants: [db-a]}\n",
        "db-a.yaml": "tenants:\n  db-a:\n    _profile: gold\n",
        "db-b.yaml": "tenants:\n  db-b:\n    _profile: gold\n",
    })
    reasons = lambda v: [(f.key, f.file, f.tenants, f.reason) for f in v.findings if f.key == "_namespaces"]  # noqa: E731
    assert reasons(kv.judge_tree(conf_d)) == [
        ("_namespaces", "_defaults.yaml", ("db-a",), "not_exporter_key")]
    assert reasons(kv.judge_tree(conf_d, tenants=["db-a"])) == [
        ("_namespaces", "_defaults.yaml", ("db-a",), "not_read_in_profile")]


def test_rules_policy_engine_refuses_are_judged_by_no_one(tmp_path):
    """#3489：da-guard 讀規則時讀得到 `target` 就算，不驗規則；規則是不是無效，
    是 Policy-as-Code 自己的事。`judge_tree` 先用 policy_engine 載入，它拒收的樹
    （`InvalidPolicyRules`）不判——無效的規則從來不執行，不能拿來把鍵算成有人讀。"""
    conf_d = _tree(tmp_path, {
        "_defaults.yaml": "defaults:\n  mysql_connection_usage_percent: 80\n"
                          "_policies:\n  - {name: p, target: owner_tag, check: lte, value: 3}\n",
        "ta.yaml": "tenants:\n  ta:\n    owner_tag: x\n"})
    with pytest.raises(ValueError) as e:
        kv.judge_tree(conf_d)
    assert type(e.value).__name__ == "InvalidPolicyRules" and e.value.problems, e.value


def _da_guard_answering(tmp_path, document: str) -> str:
    """A da-guard that answers `key-verdict` with *document* and runs the real
    one for everything else (parse-tree, which every read asks first)."""
    import os
    import stat
    real = os.environ["DA_GUARD_BINARY"]
    (tmp_path / "doc.json").write_text(document, encoding="utf-8")
    fake = tmp_path / "da-guard"
    fake.write_text(f'#!/bin/sh\nif [ "$1" = key-verdict ]; then cat "{tmp_path / "doc.json"}"; exit 0; fi\n'
                    f'exec "{real}" "$@"\n', encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    return str(fake)


@pytest.mark.parametrize("document", [
    # The document of the step that ported only the ERROR layer.
    '{"schema": "da-guard.key-verdict/v1", "errors": []}',
    # The first whole-verdict document, before it carried parse_failed.
    '{"schema": "da-guard.key-verdict/v2", "at": "x", "undecidable": false, "tenants": {},'
    ' "findings": [], "key_checks": {}}',
    # Before the gates blocked (#3016): no `blocks`, no `undecidable_blocks`.
    '{"schema": "da-guard.key-verdict/v2", "at": "x", "undecidable": false, "tenants": {},'
    ' "findings": [], "key_checks": {}, "parse_failed": [], "unreadable": []}',
    # `blocks` but not `undecidable_blocks` (#3016 review).
    '{"schema": "da-guard.key-verdict/v2", "at": "x", "undecidable": false, "tenants": {},'
    ' "findings": [], "key_checks": {}, "parse_failed": [], "unreadable": [], "blocks": false}',
    # #3046: a da-guard that still judges a partial disable.
    '{"schema": "da-guard.key-verdict/v2", "at": "x", "undecidable": false, "tenants": {"t": [{"key": "k",'
    ' "layer": "tenant", "file": "a.yaml", "read_by": ["exporter"], "verdict": "read", "severity": null,'
    ' "reason": null, "disable": "partial", "served_family": ["k_critical"], "value": "disable",'
    ' "served": null}]}, "findings": [], "key_checks": {}, "parse_failed": [], "unreadable": [],'
    ' "blocks": false, "undecidable_blocks": false}',
    '{"schema": "da-guard.key-verdict/v2", "at": "x", "undecidable": false, "tenants": {},'
    ' "findings": [{"severity": "warning", "kind": "disable_partial", "key": "k", "file": "a.yaml",'
    ' "tenants": ["t"], "reason": null, "served_family": ["k_critical"], "served_from": ["b.yaml"],'
    ' "blocks": false}], "key_checks": {}, "parse_failed": [], "unreadable": [], "blocks": false,'
    ' "undecidable_blocks": false}',
    # #3502: before rows carried dead_entries.
    '{"schema": "da-guard.key-verdict/v2", "at": "x", "undecidable": false, "tenants": {"t": [{"key": "k",'
    ' "layer": "tenant", "file": "a.yaml", "read_by": ["exporter"], "verdict": "read", "severity": null,'
    ' "reason": null, "disable": null, "switched_off_with": null, "value": "70", "served": 70}]},'
    ' "findings": [], "key_checks": {}, "parse_failed": [], "unreadable": [],'
    ' "blocks": false, "undecidable_blocks": false}',
    # #3829: before findings carried a retired threshold key's hint.
    '{"schema": "da-guard.key-verdict/v2", "at": "x", "undecidable": false, "tenants": {},'
    ' "findings": [{"severity": "error", "kind": "unread", "key": "k", "file": "a.yaml",'
    ' "tenants": ["t"], "reason": "no_row", "base": null, "dead_entries": null, "blocks": true}],'
    ' "key_checks": {}, "parse_failed": [], "unreadable": [], "blocks": true,'
    ' "undecidable_blocks": false}',
], ids=["v1", "v2-without-parse_failed", "v2-without-blocks", "v2-without-undecidable_blocks",
        "partial-disable", "disable_partial-finding", "v2-without-dead_entries", "v2-without-hint"])
def test_an_older_da_guard_is_named_older_not_broken(tmp_path, document):
    """#3489 審查：舊版 da-guard 的文件要說「比這支工具舊」（stale），不是「壞掉」——
    呼叫端與使用者據此知道要升級 da-guard，而不是去查樹。"""
    import _lib_tenant_values as tv
    conf_d = _tree(tmp_path / "conf.d", {"_defaults.yaml": _ADR_DEFAULTS})
    with pytest.raises(tv.DaGuardError) as e:
        kv.judge_tree(conf_d, binary=_da_guard_answering(tmp_path, document))
    assert e.value.stale and not e.value.broken, e.value
    assert "older than this tool" in str(e.value)


_ROW = {"key": "k", "layer": "tenant", "file": "a.yaml", "read_by": [], "verdict": "unread",
        "severity": "error", "reason": "no_row", "disable": None, "switched_off_with": None,
        "dead_entries": [], "value": 1, "served": None}
_FINDING = {"severity": "error", "kind": "unread", "key": "k", "file": "a.yaml", "tenants": ["t"],
            "reason": "no_row", "hint": None, "base": None, "dead_entries": None, "blocks": True}
_DEAD = {"entry": "overrides[1]", "window": "01:00-03:00", "value": "5", "reason": "window_covered"}


def _doc(**over):
    doc = {"undecidable": False, "tenants": {"t": [dict(_ROW)]}, "findings": [dict(_FINDING)],
           "key_checks": {"t": {"version_labels": [], "renames": []}}, "blocks": True,
           "undecidable_blocks": False}
    doc.update(over)
    return doc


@pytest.mark.parametrize("doc", [
    _doc(tenants=None),
    _doc(key_checks=[]),
    _doc(findings=None),
    _doc(tenants={"t": [dict(_ROW, verdict=None)]}),
    _doc(tenants={"t": [dict(_ROW, verdict="maybe")]}),
    _doc(tenants={"t": [dict(_ROW, verdict="maybe", severity=None)]}),  # no severity to give it away
    _doc(tenants={"t": [dict(_ROW, verdict="read")]}),          # read, with a severity
    _doc(tenants={"t": [dict(_ROW, severity=None)]}),           # unread, with none
    _doc(tenants={"t": [dict(_ROW, read_by=[1])]}),
    _doc(tenants={"t": [dict(_ROW, disable="off")]}),
    _doc(findings=[dict(_FINDING, severity=None)]),
    _doc(findings=[dict(_FINDING, kind="typo")]),
    _doc(undecidable="no"),
    _doc(blocks="yes"),
    _doc(undecidable_blocks=None),
    _doc(findings=[dict(_FINDING, blocks=None)]),
    _doc(findings=[dict(_FINDING, hint=1)]),  # #3829: a hint is text or null
    # #3502: a dead_entry carries its entries, no other kind does; a reason
    # outside the closed set is not read as one inside it.
    _doc(findings=[dict(_FINDING, kind="dead_entry")]),
    _doc(findings=[dict(_FINDING, kind="dead_entry", dead_entries=[])]),
    _doc(findings=[dict(_FINDING, dead_entries=[_DEAD])]),
    _doc(findings=[dict(_FINDING, kind="dead_entry", dead_entries=[dict(_DEAD, reason="covered")])]),
    _doc(findings=[dict(_FINDING, kind="dead_entry", dead_entries=[dict(_DEAD, window=None)])]),
    _doc(tenants={"t": [dict(_ROW, dead_entries=None)]}),
    _doc(tenants={"t": [dict(_ROW, dead_entries=[dict(_DEAD, reason="covered")])]}),
], ids=lambda d: next(iter(k for k in d if d[k] != _doc()[k]), "?"))
def test_a_document_this_reader_cannot_read_is_refused_not_read_as_another_answer(doc):
    """#3489 審查：讀不懂的值不能被讀成「判完、沒有 error」——產生器容器以
    `verdict == undecidable`、`severity == error` 決定擋不擋。"""
    assert kv._read(_doc(), None).findings  # the valid document reads
    with pytest.raises((TypeError, ValueError, KeyError)):
        kv._read(doc, None)


def test_a_value_unread_is_charged_to_the_layer_that_writes_it(tmp_path):
    """#3591：讀不懂的保留鍵值在寫它的那一層判。profile 與根平台檔 `tenants:`
    的錯字是 notice、不擋；只有租戶自己檔裡的才是 error、會擋。"""
    files = {
        "_defaults.yaml": "defaults:\n  mysql_connection_usage_percent: 80\nprofiles:\n  gold:\n"
                          "    _silent_mode: purple\n    _metadata: {owner: p, bogus_field: x}\n",
        "_platform.yaml": "tenants:\n  ta:\n    _severity_dedup: maybe\n",
        "ta.yaml": "tenants:\n  ta:\n    _profile: gold\n",
        "tb.yaml": "tenants:\n  tb:\n    _profile: gold\n",
    }
    t = kv.judge_tree(_tree(tmp_path / "inherited", files))
    got = sorted((f.severity, f.file, f.key, f.tenants) for f in t.findings if f.kind == "value_unread")
    assert got == [
        (kv.NOTICE, "_defaults.yaml", "_metadata", ("ta", "tb")),
        (kv.NOTICE, "_defaults.yaml", "_silent_mode", ("ta", "tb")),
        (kv.NOTICE, "_platform.yaml", "_severity_dedup", ("ta",)),
    ]
    assert not t.blocks

    own = dict(files, **{"tb.yaml": "tenants:\n  tb:\n    _profile: gold\n    _metadata: {dbType: x}\n"})
    t = kv.judge_tree(_tree(tmp_path / "own", own))
    errors = [f for f in t.findings if f.kind == "value_unread" and f.severity == kv.ERROR]
    assert [(f.file, f.key, f.tenants, f.blocks) for f in errors] == [("tb.yaml", "_metadata", ("tb",), True)]
    assert "dbType" in errors[0].reason
    assert t.blocks


def test_a_schedule_entry_that_never_serves_is_an_error_on_the_file_that_writes_it(tmp_path):
    """#3502：排程裡永遠不生效的條目（被前面 window 完全遮住的 window、被 window
    蓋滿整天的 default）在寫它的檔報 error 並擋；部分重疊的 window 照常合法。"""
    files = {
        "_defaults.yaml": "defaults:\n  mysql_connection_usage_percent: 80\n  redis_memory: 70\n",
        "a.yaml": ("tenants:\n  t1:\n    mysql_connection_usage_percent:\n      default: \"70\"\n      overrides:\n"
                   "        - window: \"22:00-06:00\"\n          value: \"1000\"\n"
                   "        - window: \"01:00-03:00\"\n          value: \"5\"\n"
                   "    redis_memory:\n      default: \"60\"\n      overrides:\n"
                   "        - window: \"22:00-06:00\"\n          value: \"61\"\n"
                   "        - window: \"05:00-08:00\"\n          value: \"62\"\n"),
    }
    verdict = kv.judge_tree(_tree(tmp_path, files), at="2026-07-01T03:00:00Z")
    dead = [f for f in verdict.findings if f.kind == "dead_entry"]
    assert [(f.severity, f.key, f.file, f.tenants, f.blocks) for f in dead] == [
        ("error", "mysql_connection_usage_percent", "a.yaml", ("t1",), True)]
    assert dead[0].dead_entries == (tv.DeadEntry("overrides[1]", "01:00-03:00", "5", "window_covered"),)
    assert verdict.blocks
    row = {v.key: v for v in verdict.keys["t1"]}
    assert row["mysql_connection_usage_percent"].dead_entries == dead[0].dead_entries
    assert row["redis_memory"].dead_entries == ()
    assert kv.finding_line(dead[0], 5) == (
        "a.yaml: error: dead_entry `mysql_connection_usage_percent` (never served: overrides[1] `01:00-03:00` → '5': "
        "window_covered) — tenant(s) t1")


def test_a_profile_key_the_platform_does_not_fill_still_reads(tmp_path):
    """#3502 審查：profile 寫了、根只宣告不給值的鍵（profile_not_filled）那一列
    也要帶 `dead_entries: []`；帶 null 時整份判決讀不進來（validate-config 變成
    verdict incomplete）。"""
    files = {
        "_defaults.yaml": "defaults:\n  mysql_connection_usage_percent: 80\noptional_overrides:\n  - kafka_lag\n"
                          "profiles:\n  gold:\n    kafka_lag: \"5\"\n",
        "tx.yaml": "tenants:\n  tx:\n    _profile: gold\n",
    }
    verdict = kv.judge_tree(_tree(tmp_path, files), at="2026-07-01T03:00:00Z")
    row = {v.key: v for v in verdict.keys["tx"]}["kafka_lag"]
    assert (row.reason, row.dead_entries) == ("profile_not_filled", ())
