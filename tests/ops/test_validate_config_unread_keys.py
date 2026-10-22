"""validate-config `unread_keys`（ADR-038 第二步）：沒有任何讀取端讀的鍵，只報不擋。

判決來自共用判決 `_lib_key_verdict`；這一列只負責「怎麼報」：一律 WARN、不影響
結束碼，da-guard 不可用或失敗時說判決未完成，不拒收（決定 5）。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import _lib_tenant_values as tv
import validate_config as vc

pytestmark = pytest.mark.usefixtures("da_guard_env")

_DEFAULTS = "defaults:\n  mysql_connections: 80\n  mysql_slave_lag: 30\n  redis_memory: 70\n"


def _tree(root: Path, files: dict[str, str]) -> str:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    return str(root)


def test_each_finding_is_one_warn_line(tmp_path):
    r = vc.check_unread_keys(_tree(tmp_path, {
        "_defaults.yaml": _DEFAULTS,
        "db-a.yaml": "tenants:\n  db-a:\n    mysql_conections: 70\n    mysql_slave_lagg: disable\n"
                     "    redis_memory: disable\n    redis_memory_critical: 90\n"}))
    assert r["status"] == vc.WARN
    assert r["details"] == [
        "db-a.yaml: error: unread `mysql_conections` (no_row) — tenant(s) db-a",
        "db-a.yaml: warning: disable_noop `mysql_slave_lagg` (disable_noop) — tenant(s) db-a",
    ]  # `redis_memory_critical` is db-a's own: keeping the critical tier only (#3046)


def test_disable_partial_names_where_the_still_served_sibling_comes_from(tmp_path):
    r = vc.check_unread_keys(_tree(tmp_path, {
        "_defaults.yaml": _DEFAULTS,
        "team/_defaults.yaml": "defaults:\n  redis_memory_critical: 95\n",
        "team/db-a.yaml": "tenants:\n  db-a:\n    redis_memory: disable\n"}))
    assert r["details"] == [
        "team/db-a.yaml: warning: disable_partial `redis_memory`; still served: redis_memory_critical"
        " (from team/_defaults.yaml) — tenant(s) db-a",
    ]


def test_a_platform_side_finding_is_one_line_with_its_tenants(tmp_path):
    files = {"_defaults.yaml": _DEFAULTS, "_profiles.yaml": "profiles:\n  p1:\n    mysql_conections: 70\n"}
    for t in ("ta", "tb"):
        files[f"{t}.yaml"] = f"tenants:\n  {t}:\n    _profile: p1\n"
    r = vc.check_unread_keys(_tree(tmp_path, files))
    assert r["details"] == ["_profiles.yaml: notice: unread `mysql_conections` (no_row) — tenant(s) ta, tb"]


def test_a_profile_key_no_reader_reads_is_a_notice_on_the_profile(tmp_path):
    """The `profiles` row judges no key names (#3042); a `_` key the registry
    does not list, written in a profile, is reported here instead, on the
    profile file — and `_custom_alerts`, which a profile carries, is not."""
    r = vc.check_unread_keys(_tree(tmp_path, {
        "_defaults.yaml": _DEFAULTS,
        "_profiles.yaml": "profiles:\n  p1:\n    _unknown_thing: bad\n    _custom_alerts:\n"
                          "      - {recipe: threshold, name: q, metric: m, op: \">\", window: 5m,"
                          " threshold: \"1:warning\"}\n",
        "ta.yaml": "tenants:\n  ta:\n    _profile: p1\n"}))
    assert r["details"] == ["_profiles.yaml: notice: unread `_unknown_thing` (unregistered) — tenant(s) ta"]


_PROFILE_KEYS = ("profiles:\n  p1:\n    _unknown_thing: bad\n    _namespaces: [x]\n"
                 "    _profile: p2\n    _metadata: {owner: t}\n    _silent_mode: warning\n"
                 "  p2:\n    mysql_connections: 70\n")


@pytest.mark.parametrize("text", [
    pytest.param("tenants:\n  ta:\n    mysql_connections: 70\n", id="unbound"),
    pytest.param("tenants:\n  ta:\n    _profile: p1\n", id="bound"),
    pytest.param("tenants:\n  ta:\n    _profile: p1\n    _namespaces: [own]\n", id="shadowed"),
])
def test_a_profile_key_is_judged_from_the_profile_layer_bound_or_not(tmp_path, text):
    """#3042: a `_` key no reader reads in a profile is reported on the
    profile file whether or not a tenant binds the profile, and when the
    tenant's own value shadows it; `_profile` in a profile never takes
    effect. `_metadata` and `_silent_mode` are read in a profile: no line.
    The reason is the profile layer's, bound or not (`not_read_in_profile`,
    never the tenant path's `not_exporter_key`)."""
    r = vc.check_unread_keys(_tree(tmp_path, {"_defaults.yaml": _DEFAULTS,
                                              "_profiles.yaml": _PROFILE_KEYS, "ta.yaml": text}))
    who = "tenant(s) ta" if "_profile: p1" in text else "no tenant"
    got = [d for d in r["details"] if d.startswith("_profiles.yaml:")]
    assert got == [
        f"_profiles.yaml: notice: unread `_namespaces` (not_read_in_profile) — {who}",
        f"_profiles.yaml: notice: unread `_profile` (not_read_in_profile) — {who}",
        f"_profiles.yaml: notice: unread `_unknown_thing` (unregistered) — {who}",
    ], r["details"]


def test_a_clean_tree_passes(tmp_path):
    r = vc.check_unread_keys(_tree(tmp_path, {
        "_defaults.yaml": _DEFAULTS, "db-a.yaml": "tenants:\n  db-a:\n    mysql_connections: 70\n"}))
    assert r["status"] == vc.PASS


def test_undecidable_is_said(tmp_path):
    r = vc.check_unread_keys(_tree(tmp_path, {"ta.yaml": "tenants:\n  ta:\n    mysql_conections: 70\n"}))
    assert r["status"] == vc.WARN
    assert r["details"][-1] == "no root platform file was read: 1 threshold key(s) cannot be judged (undecidable)"
    # Nothing was found unread: the advice is about the root file.
    assert r["hint"] == vc._UNREAD_KEYS_UNDECIDABLE_HINT


def test_a_finding_keeps_the_key_advice(tmp_path):
    """A key found unread gets the check-level advice (fix or remove it)."""
    r = vc.check_unread_keys(_tree(tmp_path, {
        "_defaults.yaml": _DEFAULTS, "db-a.yaml": "tenants:\n  db-a:\n    mysql_conections: 70\n"}))
    assert r["status"] == vc.WARN and r["hint"] is None
    assert "misspelt key" in vc._CHECK_HINTS["unread_keys"][0]


@pytest.mark.parametrize("exc", [
    tv.DaGuardNotFoundError("no da-guard"),
    tv.DaGuardError("boom", 2, "boom"),
    RuntimeError("anything else"),
])
def test_a_verdict_that_cannot_be_given_is_a_warn_not_a_refusal(tmp_path, monkeypatch, exc):
    """da-guard 不可用或失敗：只說判決未完成，不 FAIL、不 caller error（決定 5）。"""
    def boom(*_a, **_k):
        raise exc
    monkeypatch.setattr(vc.key_verdict, "judge_tree", boom)
    monkeypatch.setattr(vc, "_effective_tree", lambda _d: None)
    r = vc.check_unread_keys(_tree(tmp_path, {"_defaults.yaml": _DEFAULTS}))
    assert r["status"] == vc.WARN and not r["caller_error"]
    assert r["details"][0].startswith("verdict incomplete: ")
    # No key was judged: the advice is about da-guard, not "fix a misspelt key".
    assert r["hint"] == vc._UNREAD_KEYS_INCOMPLETE_HINT


def test_the_row_does_not_change_the_exit_code(tmp_path):
    """只報不擋：拼錯的閾值鍵今天只是其他列的 WARN，加上這一列仍是 0。"""
    d = _tree(tmp_path, {"_defaults.yaml": _DEFAULTS,
                         "db-a.yaml": "tenants:\n  db-a:\n    mysql_conections: \"70\"\n"})
    p = subprocess.run([sys.executable, vc.__file__, "--config-dir", d, "--json"],
                       capture_output=True, encoding="utf-8", timeout=600)
    rows = {r["check"]: r for r in json.loads(p.stdout)}
    assert rows["unread_keys"]["status"] == vc.WARN
    assert p.returncode == 0, p.stderr


def test_a_clean_nested_tree_passes_without_a_flat_read_caveat(tmp_path):
    """The verdict reads the whole tree through da-guard: a subdirectory is
    not something this row skipped, so it is no WARN and no
    `skipped_nested_files`."""
    d = _tree(tmp_path, {"_defaults.yaml": _DEFAULTS,
                         "sub/_defaults.yaml": "defaults:\n  mysql_connections: 75\n",
                         "sub/ta.yaml": "tenants:\n  ta:\n    mysql_connections: \"70\"\n"})
    p = subprocess.run([sys.executable, vc.__file__, "--config-dir", d, "--json"],
                       capture_output=True, encoding="utf-8", timeout=600)
    row = {r["check"]: r for r in json.loads(p.stdout)}["unread_keys"]
    assert row["status"] == vc.PASS, row
    assert not row.get("skipped_nested_files"), row
    # The root-only `_policies` note is policy_dsl's to print, once.
    assert p.stderr.count("`_policies` are read from the root") <= 1, p.stderr


def test_a_rule_naming_a_profile_key_no_tenant_gets_does_not_count(tmp_path):
    """Policy-as-Code reads a tenant's view: a rule naming `_namespaces` reads
    nothing from a profile no tenant binds, so the key is still reported."""
    defaults = _DEFAULTS + ("_policies:\n  - {name: ns, target: _namespaces, operator: required,"
                            " severity: error}\n")
    r = vc.check_unread_keys(_tree(tmp_path, {
        "_defaults.yaml": defaults,
        "_profiles.yaml": "profiles:\n  p1:\n    _namespaces: [x]\n",
        "ta.yaml": "tenants:\n  ta:\n    mysql_connections: 70\n"}))
    assert "_profiles.yaml: notice: unread `_namespaces` (not_read_in_profile) — no tenant" in r["details"], r
