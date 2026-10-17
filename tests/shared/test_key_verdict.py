"""ADR-038 決定 4–7：共用判決 `_lib_key_verdict`。

每棵樹都用真的 da-guard（served-values 與 effective）跑，判決只來自兩份輸出
與登錄表，不自己讀 YAML。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import _lib_key_verdict as kv

_REGISTRY = (Path(__file__).resolve().parents[2] / "components" / "threshold-exporter" / "app"
             / "pkg" / "config" / "reserved_keys.json")

pytestmark = pytest.mark.usefixtures("da_guard_env")


def _tree(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    return root


def _by_key(verdict, tenant):
    return {v.key: v for v in verdict.keys[tenant]}


_ADR_DEFAULTS = "defaults:\n  mysql_connections: 80\n  mysql_slave_lag: 30\n  redis_memory: 70\n"
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
        "redis_memory": (("exporter",), "read", None, "partial"),
        "redis_memory_critical": (("exporter",), "read", None, None),
        "_namespaces": (("annotation",), "read", None, None),
        # 繼承來的閾值：exporter 有送
        "mysql_connections": (("exporter",), "read", None, None),
        "mysql_slave_lag": (("exporter",), "read", None, None),
    }
    # `redis_memory` 是 partial（`redis_memory_critical` 照送），但那條 critical
    # 是租戶自己寫的：「只要 critical」，不報（#3046）。
    kinds = {(f.kind, f.key, f.severity) for f in t.findings}
    assert kinds == {("unread", "mysql_conections", "error"), ("unread", "_routng", "error"),
                     ("disable_noop", "mysql_slave_lagg", "warning")}
    assert not t.undecidable


def test_disable_partial_names_the_inherited_siblings_and_their_files(tmp_path):
    """#3046：base 寫 `disable` 之後仍送出的同族鍵，只有不是租戶檔自己寫的才報，
    並指名它來自哪個檔。"""
    t = kv.judge_tree(_tree(tmp_path, {
        "_defaults.yaml": "defaults:\n  redis_memory: 70\n  mysql_connections: 80\n"
                          "  mysql_connections{db=\"x\"}: 12\n  pg_conn: 50\n",
        "team/_defaults.yaml": "defaults:\n  redis_memory_critical: 95\n",
        "_profiles.yaml": "profiles:\n  p1:\n    pg_conn_critical: 96\n",
        "t.yaml": "tenants:\n  ta:\n    redis_memory: disable\n    redis_memory_critical: \"90\"\n"
                  "    mysql_connections: disable\n  tc:\n    _profile: p1\n    pg_conn: disable\n",
        "team/tb.yaml": "tenants:\n  tb:\n    redis_memory: disable\n",
    }))
    got = {(f.key, f.file, f.tenants, f.served_family, f.served_from)
           for f in t.findings if f.kind == "disable_partial"}
    assert got == {
        ("mysql_connections", "t.yaml", ("ta",), ('mysql_connections{db="x"}',), ("_defaults.yaml",)),
        ("pg_conn", "t.yaml", ("tc",), ("pg_conn_critical",), ("_profiles.yaml",)),
        ("redis_memory", "team/tb.yaml", ("tb",), ("redis_memory_critical",), ("team/_defaults.yaml",)),
    }
    # ta 的 redis_memory 仍是 partial（事實照列），只是不成為 finding。
    assert _by_key(t, "ta")["redis_memory"].disable == "partial"


def test_disable_partial_sources_cover_platform_tenants_and_old_spellings(tmp_path):
    """#3046：根平台檔 `tenants:` 給的同族鍵算繼承；同族鍵用舊拼法（#1231）寫時，
    served-values 回的是新拼法，仍要認得出是不是租戶自己寫的、來自哪個檔。"""
    t = kv.judge_tree(_tree(tmp_path, {
        "_defaults.yaml": "defaults:\n  redis_memory: 70\n  mysql_threads_running: 50\n",
        "_platform.yaml": "tenants:\n  tp:\n    redis_memory_critical: \"95\"\n",
        "team/_defaults.yaml": "defaults:\n  mysql_cpu_critical: 95\n",
        "tp.yaml": "tenants:\n  tp:\n    redis_memory: disable\n",
        "own.yaml": "tenants:\n  town:\n    mysql_cpu: disable\n    mysql_cpu_critical: \"90\"\n",
        "team/tl.yaml": "tenants:\n  tl:\n    mysql_cpu: disable\n",
    }))
    got = {(f.key, f.tenants, f.served_from) for f in t.findings if f.kind == "disable_partial"}
    assert got == {("redis_memory", ("tp",), ("_platform.yaml",)),
                   ("mysql_cpu", ("tl",), ("team/_defaults.yaml",))}


def test_disable_partial_old_spelling_dimensional_siblings(tmp_path):
    """維度鍵的舊拼法：根 defaults 給的算繼承（tdim 報），租戶自己寫的不報（tdown）。"""
    t = kv.judge_tree(_tree(tmp_path, {
        "_defaults.yaml": "defaults:\n  mysql_threads_running: 50\n",
        "tdim.yaml": "tenants:\n  tdim:\n    mysql_cpu: disable\n",
        "tdown.yaml": "tenants:\n  tdown:\n    mysql_cpu: disable\n    mysql_cpu{db=\"y\"}: \"4\"\n",
        "dims/_defaults.yaml": "defaults:\n  mysql_cpu{db=\"x\"}: 3\n",
        "dims/tsub.yaml": "tenants:\n  tsub:\n    mysql_cpu: disable\n",
    }))
    got = {(f.key, f.tenants, f.served_family, f.served_from)
           for f in t.findings if f.kind == "disable_partial"}
    assert got == {("mysql_cpu", ("tsub",), ('mysql_threads_running{db="x"}',), ("dims/_defaults.yaml",))}


def test_one_platform_side_disable_is_one_finding_naming_every_source(tmp_path):
    """profile 寫的 `disable` 影響的租戶，從不同檔繼承同一個同族鍵：仍是一筆，
    來源檔合併列出。"""
    t = kv.judge_tree(_tree(tmp_path, {
        "_defaults.yaml": "defaults:\n  redis_memory: 70\n",
        "_profiles.yaml": "profiles:\n  p1:\n    redis_memory: disable\n",
        "t1/_defaults.yaml": "defaults:\n  redis_memory_critical: 95\n",
        "t2/_defaults.yaml": "defaults:\n  redis_memory_critical: 96\n",
        "t1/ta.yaml": "tenants:\n  ta:\n    _profile: p1\n",
        "t2/tb.yaml": "tenants:\n  tb:\n    _profile: p1\n",
        # td 另外繼承一個根 defaults 的維度鍵：同族鍵集合不同，仍併成同一筆。
        "t3/_defaults.yaml": "defaults:\n  redis_memory_critical: 97\n",
        "t3/td.yaml": "tenants:\n  td:\n    _profile: p1\n    redis_memory{x=\"a\"}: 5\n",
    }))
    got = [(f.file, f.tenants, f.served_family, f.served_from)
           for f in t.findings if f.kind == "disable_partial"]
    assert got == [("_profiles.yaml", ("ta", "tb", "td"), ("redis_memory_critical",),
                    ("t1/_defaults.yaml, t2/_defaults.yaml, t3/_defaults.yaml",))]


def test_a_platform_side_typo_is_one_notice_naming_the_file_and_its_tenants(tmp_path):
    """profile 裡的錯字：每個選用它的租戶都讀不到，報成一筆 notice，指名 profile 檔。"""
    files = {"_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
             "_profiles.yaml": "profiles:\n  p1:\n    mysql_conections: 70\n"}
    for t in ("ta", "tb", "tc"):
        files[f"{t}.yaml"] = f"tenants:\n  {t}:\n    _profile: p1\n"
    t = kv.judge_tree(_tree(tmp_path, files))
    for tenant in ("ta", "tb", "tc"):
        v = _by_key(t, tenant)["mysql_conections"]
        assert (v.verdict, v.severity, v.file) == ("unread", "notice", "_profiles.yaml")
    unread = [f for f in t.findings if f.key == "mysql_conections"]
    assert len(unread) == 1 and unread[0].tenants == ("ta", "tb", "tc")


def test_a_tenant_own_typo_is_one_error_per_tenant(tmp_path):
    files = {"_defaults.yaml": "defaults:\n  mysql_connections: 80\n"}
    for t in ("ta", "tb"):
        files[f"{t}.yaml"] = f"tenants:\n  {t}:\n    mysql_conections: 70\n"
    t = kv.judge_tree(_tree(tmp_path, files))
    unread = [f for f in t.findings if f.key == "mysql_conections"]
    assert sorted(f.tenants for f in unread) == [("ta",), ("tb",)]
    assert all(f.severity == "error" for f in unread)


def test_an_unregistered_key_the_root_declares_is_unread(tmp_path):
    """#3045：根 defaults 宣告了沒登記的 `_myth`，exporter 也不送（任何一層都不送）。
    所以它和其他沒人讀的鍵一樣是 `unread`：寫在租戶自己的檔是 error、算在該租戶；
    只從根 defaults 繼承的是 notice，報一筆在根 defaults，列出它到達的租戶。"""
    t = kv.judge_tree(_tree(tmp_path, {
        "_defaults.yaml": "defaults:\n  mysql_connections: 80\n  _myth: 5\n",
        "ta.yaml": "tenants:\n  ta:\n    _myth: 7\n",
        "tb.yaml": "tenants:\n  tb:\n    mysql_connections: 60\n"}))
    a, b = _by_key(t, "ta")["_myth"], _by_key(t, "tb")["_myth"]
    assert (a.read_by, a.verdict, a.severity, a.reason, a.file) == (
        (), "unread", "error", "unregistered", "ta.yaml")
    assert (b.read_by, b.verdict, b.severity, b.reason, b.file) == (
        (), "unread", "notice", "unregistered", "_defaults.yaml")
    assert a.served is None and b.served is None
    found = sorted((f.kind, f.severity, f.file, f.tenants) for f in t.findings if f.key == "_myth")
    assert found == [("unread", "error", "ta.yaml", ("ta",)),
                     ("unread", "notice", "_defaults.yaml", ("tb",))]


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
        "_defaults.yaml": "defaults:\n  mysql_connections: 80\n_policies:\n"
                          f"  - {{name: p, target: '{target}', operator: required, severity: error}}\n",
        "ta.yaml": "tenants:\n  ta:\n    owner_tag: team-a\n    other_tag: x\n"}))
    keys = _by_key(t, "ta")
    assert keys["owner_tag"].read_by == ("policy_engine",) and keys["owner_tag"].verdict == "read"
    # 對照：規則沒有指名的鍵照樣沒人讀
    assert keys["other_tag"].verdict == "unread"


def test_a_wildcard_rule_names_no_key(tmp_path):
    """`mysql_*` 會比到拼錯的 `mysql_conections`；萬用字元不算指名，不替錯字背書。"""
    t = kv.judge_tree(_tree(tmp_path, {
        "_defaults.yaml": "defaults:\n  mysql_connections: 80\n_policies:\n"
                          "  - {name: p, target: 'mysql_*', operator: lte, value: 60, severity: error}\n",
        "ta.yaml": "tenants:\n  ta:\n    mysql_conections: 70\n"}))
    assert _by_key(t, "ta")["mysql_conections"].verdict == "unread"


def test_a_rule_credits_no_tenant_it_excludes_and_no_unregistered_reserved_key(tmp_path):
    t = kv.judge_tree(_tree(tmp_path, {
        "_defaults.yaml": "defaults:\n  mysql_connections: 80\n_policies:\n"
                          "  - {name: p, target: owner_tag, operator: required, severity: error,"
                          " exclude_tenants: [tb]}\n"
                          "  - {name: q, target: _typo_reserved, operator: required, severity: error}\n",
        "ta.yaml": "tenants:\n  ta:\n    owner_tag: a\n    _typo_reserved: 1\n",
        "tb.yaml": "tenants:\n  tb:\n    owner_tag: b\n"}))
    assert _by_key(t, "ta")["owner_tag"].verdict == "read"
    assert _by_key(t, "tb")["owner_tag"].verdict == "unread"
    assert _by_key(t, "ta")["_typo_reserved"].verdict == "unread"


def test_a_reserved_key_inherited_from_a_layer_the_exporter_reads_is_read(tmp_path):
    """子目錄 `_defaults.yaml` 的 `_state_<f>`、`_severity_dedup`：登錄表說 exporter 讀這一層，
    不論值是什麼。"""
    t = kv.judge_tree(_tree(tmp_path, {
        "_defaults.yaml": "defaults:\n  mysql_connections: 80\n"
                          "state_filters:\n  maint: {reasons: [X], severity: critical}\n",
        "team/_defaults.yaml": "defaults:\n  _state_maint: enable\n  _severity_dedup: enable\n",
        "team/ta.yaml": "tenants:\n  ta:\n    mysql_connections: 70\n"}))
    keys = _by_key(t, "ta")
    for k in ("_state_maint", "_severity_dedup"):
        if k in keys:
            assert keys[k].verdict == "read", (k, keys[k])
    assert "_state_maint" in keys  # 不是恆真：至少一個真的被判了


def test_an_undeclared_state_filter_is_a_platform_notice(tmp_path):
    """租戶寫了平台沒宣告的 `_state_foo`：缺的是平台的宣告，報成平台側 notice（決定 7）。"""
    t = kv.judge_tree(_tree(tmp_path, {
        "_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
        "ta.yaml": "tenants:\n  ta:\n    _state_foo: enable\n",
        "tb.yaml": "tenants:\n  tb:\n    _state_foo: enable\n"}))
    v = _by_key(t, "ta")["_state_foo"]
    assert (v.verdict, v.severity, v.reason) == ("unread", "notice", "state_filter_undeclared")
    found = [f for f in t.findings if f.key == "_state_foo"]
    assert [(f.file, f.tenants) for f in found] == [("_defaults.yaml", ("ta", "tb"))]


def test_a_declared_reader_counts_only_in_its_layer(tmp_path):
    """`_severity_dedup` 寫在子目錄 `_defaults.yaml`：exporter 讀、路由產生器不讀
    （登錄表的層）；寫在租戶檔兩者都讀。"""
    t = kv.judge_tree(_tree(tmp_path, {
        "_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
        "team/_defaults.yaml": "_severity_dedup: enable\n",
        "team/ta.yaml": "tenants:\n  ta:\n    mysql_connections: 70\n",
        "tb.yaml": "tenants:\n  tb:\n    _severity_dedup: enable\n"}))
    assert _by_key(t, "ta")["_severity_dedup"].read_by == ("exporter",)
    assert _by_key(t, "tb")["_severity_dedup"].read_by == ("exporter", "generator")


def test_custom_alerts_have_the_same_readers_in_every_layer(tmp_path):
    """#3042：編譯器照 exporter 的讀法編，`_custom_alerts` 寫在 profile 或租戶檔，
    兩個讀取端都讀。"""
    recipe = ("      - {recipe: threshold, name: n1, metric: m1, op: '>', window: 5m, "
              "threshold: '10:warning'}\n")
    t = kv.judge_tree(_tree(tmp_path, {
        "_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
        "_profiles.yaml": "profiles:\n  p1:\n    _custom_alerts:\n" + recipe,
        "ta.yaml": "tenants:\n  ta:\n    _profile: p1\n",
        "tb.yaml": "tenants:\n  tb:\n    _custom_alerts:\n" + recipe}))
    for tenant in ("ta", "tb"):
        assert _by_key(t, tenant)["_custom_alerts"].read_by == ("custom_alert_compiler", "exporter")


def test_a_reserved_key_in_a_layer_the_exporter_does_not_read_is_unread(tmp_path):
    """子目錄 `_defaults.yaml` 的 `_silent_mode`：effective 顯示它，但 exporter 不讀那一層。
    served-values 的 `values` 一律列出 `_silent_mode`，不能拿來當讀過的證據。"""
    t = kv.judge_tree(_tree(tmp_path, {
        "_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
        "team/_defaults.yaml": "defaults:\n  _silent_mode: warning\n",
        "team/ta.yaml": "tenants:\n  ta:\n    mysql_connections: 70\n"}))
    v = _by_key(t, "ta")["_silent_mode"]
    assert (v.read_by, v.verdict, v.severity, v.file) == ((), "unread", "notice", "team/_defaults.yaml")


def test_without_disable_effects_a_switched_off_key_is_taken_as_read(tmp_path):
    t = kv.judge_tree(_tree(tmp_path, {"_defaults.yaml": _ADR_DEFAULTS, "db-a.yaml": _ADR_TENANT}),
                      disable_effects=False)
    v = _by_key(t, "db-a")["mysql_slave_lagg"]
    assert (v.verdict, v.disable) == ("read", None)
    # 拼錯的 `_` 鍵不靠 disable 判：照樣沒人讀
    assert _by_key(t, "db-a")["_routng"].verdict == "unread"


def test_registered_readers_is_the_registry():
    """Python 的查找與 Go 的 config.RegisteredReaders 同一個規則，各自對 JSON 釘住。"""
    with open(_REGISTRY, encoding="utf-8") as f:
        reg = json.load(f)
    for k, e in reg["keys"].items():
        got, ok = kv.registered_readers(k)
        assert ok and {r: list(ls) for r, ls in got.items()} == e["read_by"], k
    for p, e in reg["prefixes"].items():
        got, ok = kv.registered_readers(p + "zz")
        assert ok and {r: list(ls) for r, ls in got.items()} == e["read_by"], p
    for k in reg["retired"]:
        assert kv.registered_readers(k) == ({}, False)
    assert kv.registered_readers("_no_such_key") == ({}, False)
    assert kv.registered_readers("_routing_profile")[0] == {"generator": ("tenant", "platform_tenants")}


def test_a_subtree_defaults_source_is_the_registry_layer_subtree_defaults():
    """effective 的 defaults 層（level > 0）對應登錄表的 subtree_defaults：今天沒有非 exporter
    的讀取端宣告這一層，所以以合成輸入釘住對應本身。"""
    import _lib_tenant_values as tv
    sk = tv.ServedKey(False, "not_exporter_key", True, {"generator": ("subtree_defaults",)})
    served = {"ta": tv.TenantValues("ta", {}, {}, {"_x": 1}, {}, None, {}, {}, {}, None, {"_x": sk})}

    def eff(level):
        return {"ta": tv.TenantEffective("ta", {"_x": 1}, {"_x": tv.KeySource("defaults", "f.yaml", level)},
                                         None, "", "", "", ["_defaults.yaml"], [], [], [], {})}
    sub = kv.judge(served, eff(1), [], undecidable=False)
    root = kv.judge(served, eff(0), [], undecidable=False)
    assert sub.keys["ta"][0].read_by == ("generator",)
    assert root.keys["ta"][0].read_by == ()


@pytest.mark.parametrize("rule_key, written", [
    ("mysql_threads_running_critical", "mysql_" + "cpu_critical"),
    ("mysql_" + "cpu_critical", "mysql_threads_running_critical"),
])
def test_a_rule_names_a_key_in_either_alias_spelling(tmp_path, rule_key, written):
    """Policy-as-Code 以別名表比對規則與 key；共用判決用同一個比法。"""
    t = kv.judge_tree(_tree(tmp_path, {
        "_defaults.yaml": "defaults:\n  mysql_connections: 80\n_policies:\n"
                          f"  - {{name: p, target: {rule_key}, operator: required, severity: error}}\n",
        "ta.yaml": f"tenants:\n  ta:\n    {written}: abc\n"}))
    assert "policy_engine" in _by_key(t, "ta")[written].read_by


def test_an_inherited_undeclared_state_filter_is_the_root_files_notice(tmp_path):
    t = kv.judge_tree(_tree(tmp_path, {
        "_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
        "team/_defaults.yaml": "defaults:\n  _state_ghost: enable\n",
        "team/ta.yaml": "tenants:\n  ta:\n    mysql_connections: 70\n"}))
    v = _by_key(t, "ta")["_state_ghost"]
    assert (v.verdict, v.severity, v.reason) == ("unread", "notice", "state_filter_undeclared")
    assert [f.file for f in t.findings if f.key == "_state_ghost"] == ["_defaults.yaml"]
