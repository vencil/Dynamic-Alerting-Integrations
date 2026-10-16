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
    kinds = {(f.kind, f.key, f.severity) for f in t.findings}
    assert kinds == {("unread", "mysql_conections", "error"), ("unread", "_routng", "error"),
                     ("disable_noop", "mysql_slave_lagg", "warning"),
                     ("disable_partial", "redis_memory", "warning")}
    assert not t.undecidable


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


def test_an_unregistered_key_the_exporter_serves_is_a_platform_violation(tmp_path):
    """根 defaults 宣告了沒登記的 `_myth`，exporter 照樣送出：平台的問題，不算租戶拼錯。"""
    t = kv.judge_tree(_tree(tmp_path, {
        "_defaults.yaml": "defaults:\n  mysql_connections: 80\n  _myth: 5\n",
        "ta.yaml": "tenants:\n  ta:\n    _myth: 7\n",
        "tb.yaml": "tenants:\n  tb:\n    mysql_connections: 60\n"}))
    a, b = _by_key(t, "ta")["_myth"], _by_key(t, "tb")["_myth"]
    assert (a.verdict, a.severity) == ("platform_violation", "notice")
    assert (b.verdict, b.severity, b.file) == ("platform_violation", "notice", "_defaults.yaml")
    # 一筆，報在宣告它的平台檔，兩個租戶都列；不算在 ta 自己的檔上。
    found = [f for f in t.findings if f.key == "_myth"]
    assert [(f.kind, f.file, f.tenants) for f in found] == [
        ("platform_violation", "_defaults.yaml", ("ta", "tb"))]


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


@pytest.mark.parametrize("target", ["owner_tag", "*_tag", "owner_tag.sub"])
def test_a_key_a_policy_rule_names_is_read(tmp_path, target):
    t = kv.judge_tree(_tree(tmp_path, {
        "_defaults.yaml": "defaults:\n  mysql_connections: 80\n_policies:\n"
                          f"  - {{name: p, target: '{target}', operator: required, severity: error}}\n",
        "ta.yaml": "tenants:\n  ta:\n    owner_tag: team-a\n    other_tag2: x\n"}))
    keys = _by_key(t, "ta")
    assert keys["owner_tag"].read_by == ("policy_engine",) and keys["owner_tag"].verdict == "read"
    # 對照：規則沒有指名的鍵照樣沒人讀（`*_tag` 不含 `other_tag2`）
    assert keys["other_tag2"].verdict == "unread"


def test_a_declared_reader_counts_only_in_its_layer(tmp_path):
    """`_custom_alerts` 寫在 profile：exporter 讀、編譯器不讀（登錄表的層）。"""
    recipe = ("      - {recipe: threshold, name: n1, metric: m1, op: '>', window: 5m, "
              "threshold: '10:warning'}\n")
    t = kv.judge_tree(_tree(tmp_path, {
        "_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
        "_profiles.yaml": "profiles:\n  p1:\n    _custom_alerts:\n" + recipe.replace("      -", "      -"),
        "ta.yaml": "tenants:\n  ta:\n    _profile: p1\n",
        "tb.yaml": "tenants:\n  tb:\n    _custom_alerts:\n" + recipe}))
    assert _by_key(t, "ta")["_custom_alerts"].read_by == ("exporter",)
    assert _by_key(t, "tb")["_custom_alerts"].read_by == ("custom_alert_compiler", "exporter")


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
