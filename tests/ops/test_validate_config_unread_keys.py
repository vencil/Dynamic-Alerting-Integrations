"""validate-config `unread_keys`（ADR-038）：沒有任何讀取端讀的鍵。

判決來自共用判決（`da-guard key-verdict`，經 `_lib_key_verdict` 讀）；這一列只負責
「怎麼報」。#3016 起判到就擋：文件的 `blocks`（error，或判不了的閾值鍵）是 FAIL，
只有 warning／notice 是 WARN；判決給不出來也是 FAIL（da-guard 失敗時 exit 2），與
tenant-api「判不了就不收」一致（決定 5）。
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

_DEFAULTS = "defaults:\n  mysql_connection_usage_percent: 80\n  mysql_slave_lag: 30\n  redis_memory: 70\n"


def _tree(root: Path, files: dict[str, str]) -> str:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    return str(root)


def test_each_finding_is_one_line_and_an_error_fails(tmp_path):
    r = vc.check_unread_keys(_tree(tmp_path, {
        "_defaults.yaml": _DEFAULTS,
        "db-a.yaml": "tenants:\n  db-a:\n    mysql_conections: 70\n    mysql_slave_lagg: disable\n"
                     "    redis_memory: disable\n    redis_memory_critical: 90\n"}))
    assert r["status"] == vc.FAIL and not r["caller_error"]
    assert r["details"] == [
        "db-a.yaml: error: unread `mysql_conections` (no_row) — tenant(s) db-a",
        "db-a.yaml: warning: disable_noop `mysql_slave_lagg` (disable_noop) — tenant(s) db-a",
        "db-a.yaml: warning: disable_cascade `redis_memory_critical` (switched off with its base"
        " `redis_memory`) — tenant(s) db-a",
    ]  # #3046: db-a's own critical tier is switched off with its base — a warning


def test_an_inherited_sibling_switched_off_with_its_base_is_no_finding(tmp_path):
    """#3046: the inherited `redis_memory_critical` is switched off with
    `redis_memory: disable` — read by the exporter, nothing to report."""
    r = vc.check_unread_keys(_tree(tmp_path, {
        "_defaults.yaml": _DEFAULTS,
        "team/_defaults.yaml": "defaults:\n  redis_memory_critical: 95\n",
        "team/db-a.yaml": "tenants:\n  db-a:\n    redis_memory: disable\n"}))
    assert r["details"] == ["1 tenant(s): every key is read by some reader"]
    assert r["status"] == vc.PASS


def test_a_platform_side_finding_is_one_line_with_its_tenants(tmp_path):
    files = {"_defaults.yaml": _DEFAULTS, "_profiles.yaml": "profiles:\n  p1:\n    mysql_conections: 70\n"}
    for t in ("ta", "tb"):
        files[f"{t}.yaml"] = f"tenants:\n  {t}:\n    _profile: p1\n"
    r = vc.check_unread_keys(_tree(tmp_path, files))
    assert r["details"] == ["_profiles.yaml: error: unread `mysql_conections` (no_row) — tenant(s) ta, tb"]
    assert r["status"] == vc.FAIL  # #3394: the severity is the defect's, wherever it is written


def test_a_profile_key_no_reader_reads_is_an_error_on_the_profile(tmp_path):
    """The `profiles` row judges no key names (#3042); a `_` key the registry
    does not list, written in a profile, is reported here instead, on the
    profile file — and `_custom_alerts`, which a profile carries, is not."""
    r = vc.check_unread_keys(_tree(tmp_path, {
        "_defaults.yaml": _DEFAULTS,
        "_profiles.yaml": "profiles:\n  p1:\n    _unknown_thing: bad\n    _custom_alerts:\n"
                          "      - {recipe: threshold, name: q, metric: m, op: \">\", window: 5m,"
                          " threshold: \"1:warning\"}\n",
        "ta.yaml": "tenants:\n  ta:\n    _profile: p1\n"}))
    assert r["details"] == ["_profiles.yaml: error: unread `_unknown_thing` (unregistered) — tenant(s) ta"]


_PROFILE_KEYS = ("profiles:\n  p1:\n    _unknown_thing: bad\n    _namespaces: [x]\n"
                 "    _profile: p2\n    _metadata: {owner: t}\n    _silent_mode: warning\n"
                 "  p2:\n    mysql_connection_usage_percent: 70\n")


@pytest.mark.parametrize("text", [
    pytest.param("tenants:\n  ta:\n    mysql_connection_usage_percent: 70\n", id="unbound"),
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
        f"_profiles.yaml: error: unread `_namespaces` (not_read_in_profile) — {who}",
        f"_profiles.yaml: error: unread `_profile` (not_read_in_profile) — {who}",
        f"_profiles.yaml: error: unread `_unknown_thing` (unregistered) — {who}",
        # p2 is bound by no tenant: its threshold key is not judged (#3395).
        "_profiles.yaml: notice: profile_not_judged `mysql_connection_usage_percent` (profile_unbound) — no tenant",
    ], r["details"]


def test_a_clean_tree_passes(tmp_path):
    r = vc.check_unread_keys(_tree(tmp_path, {
        "_defaults.yaml": _DEFAULTS, "db-a.yaml": "tenants:\n  db-a:\n    mysql_connection_usage_percent: 70\n"}))
    assert r["status"] == vc.PASS


def test_an_undecidable_key_fails(tmp_path):
    """#3016 option A: a threshold key no root platform file lets the
    verdict judge fails the row, as tenant-api refuses such a write (422)."""
    r = vc.check_unread_keys(_tree(tmp_path, {"ta.yaml": "tenants:\n  ta:\n    mysql_conections: 70\n"}))
    assert r["status"] == vc.FAIL and not r["caller_error"]
    assert r["details"][-1] == "no root platform file was read: 1 threshold key(s) cannot be judged (undecidable)"
    # Nothing was found unread: the advice is about the root file.
    assert r["hint"] == vc._UNREAD_KEYS_UNDECIDABLE_HINT


def test_a_finding_keeps_the_key_advice(tmp_path):
    """A key found unread gets the check-level advice (fix or remove it)."""
    r = vc.check_unread_keys(_tree(tmp_path, {
        "_defaults.yaml": _DEFAULTS, "db-a.yaml": "tenants:\n  db-a:\n    mysql_conections: 70\n"}))
    assert r["status"] == vc.FAIL and r["hint"] is None
    assert "misspelt key" in vc._CHECK_HINTS["unread_keys"][0]


def test_a_tree_with_only_routing_is_no_undecidable_failure(tmp_path):
    """No root platform file but no threshold key either: nothing the
    verdict could not judge, so nothing fails (ADR-038 decision 7)."""
    r = vc.check_unread_keys(_tree(tmp_path, {
        "ta.yaml": "tenants:\n  ta:\n    _routing: {receiver: {type: webhook, url: \"https://x.example\"}}\n"}))
    assert r["status"] != vc.FAIL, r


@pytest.mark.parametrize("exc, caller_error", [
    (tv.DaGuardNotFoundError("no da-guard"), True),
    (tv.DaGuardError("boom", 139, "boom", broken=True), True),
    (tv.DaGuardError("old", 2, "old", stale=True), True),
    # da-guard refuses the tree itself (a tenant in two files): the tree's
    # fault, a FAIL the row naming it already carries — exit 1, not 2.
    (tv.DaGuardError("config rejected", 2, "duplicate tenant ID"), False),
    (RuntimeError("anything else"), False),
], ids=["not-found", "broken", "stale", "tree-refused", "other"])
def test_a_verdict_that_cannot_be_given_fails(tmp_path, monkeypatch, exc, caller_error):
    """#3016：判決給不出來不算通過（與 tenant-api 判不了就不收一致）。da-guard
    本身失敗是 caller error（exit 2），樹的內容擋住判決（例如 policy_engine 拒收的
    規則）是一般 FAIL。"""
    def boom(*_a, **_k):
        raise exc
    monkeypatch.setattr(vc.key_verdict, "judge_tree", boom)
    r = vc.check_unread_keys(_tree(tmp_path, {"_defaults.yaml": _DEFAULTS}))
    assert r["status"] == vc.FAIL and r["caller_error"] is caller_error
    assert r["details"][0].startswith("verdict incomplete: ")
    # No key was judged: the advice is about da-guard, not "fix a misspelt key".
    assert r["hint"] == vc._UNREAD_KEYS_INCOMPLETE_HINT


def test_an_unread_key_fails_the_run(tmp_path):
    """#3016：判到就擋——拼錯的閾值鍵讓這一列 FAIL，整支 exit 1。"""
    d = _tree(tmp_path, {"_defaults.yaml": _DEFAULTS,
                         "db-a.yaml": "tenants:\n  db-a:\n    mysql_conections: \"70\"\n"})
    p = subprocess.run([sys.executable, vc.__file__, "--config-dir", d, "--json"],
                       capture_output=True, encoding="utf-8", timeout=600)
    rows = {r["check"]: r for r in json.loads(p.stdout)}
    assert rows["unread_keys"]["status"] == vc.FAIL
    assert p.returncode == 1, p.stderr


def test_a_clean_nested_tree_passes_without_a_flat_read_caveat(tmp_path):
    """The verdict reads the whole tree through da-guard: a subdirectory is
    not something this row skipped, so it is no WARN and no
    `skipped_nested_files`."""
    d = _tree(tmp_path, {"_defaults.yaml": _DEFAULTS,
                         "sub/_defaults.yaml": "defaults:\n  mysql_connection_usage_percent: 75\n",
                         "sub/ta.yaml": "tenants:\n  ta:\n    mysql_connection_usage_percent: \"70\"\n"})
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
        "ta.yaml": "tenants:\n  ta:\n    mysql_connection_usage_percent: 70\n"}))
    assert "_profiles.yaml: error: unread `_namespaces` (not_read_in_profile) — no tenant" in r["details"], r


def test_the_exporters_key_checks_are_printed_as_it_words_them(tmp_path):
    """ADR-038 §11: a `version` label the exporter refuses and a retired
    spelling are the exporter's own lines (da-guard served-values'
    key_checks); this tool judges no key name itself."""
    r = vc.check_unread_keys(_tree(tmp_path, {
        "_defaults.yaml": "defaults:\n  container_cpu: 80\n  mysql_threads_running: 30\n",
        "db-a.yaml": "tenants:\n  db-a:\n    mysql_cpu: \"40\"\n"
                     "    container_cpu{version=\"V2!\"}: \"5\"\n"}))
    # The dimensional key's `version` label is undeclared: it is unread too.
    assert r["status"] == vc.FAIL, r
    assert any('version "V2!"' in d and "tenant=db-a" in d for d in r["details"]), r["details"]
    assert any('"mysql_cpu" was renamed to "mysql_threads_running"' in d for d in r["details"]), r["details"]


class TestAPlatformDeclaringNothingIsNotAdvisedAway:
    """⛔ With no platform defaults declared, the verdict reads every tenant
    threshold key as unread, and the row's generic advice — remove a key
    nothing reads — would delete a working config. The row says the
    platform side is missing instead (the hint the `schema` row carried
    until ADR-038 §11 moved key judgement to this row)."""

    @pytest.mark.parametrize("body", [
        "_routing_defaults:\n  group_wait: 30s\n",  # no `defaults:` key
        "defaults: {}\n",
    ], ids=["no-defaults-key", "empty-defaults"])
    def test_the_destructive_hint_is_replaced(self, tmp_path, body):
        r = vc.check_unread_keys(_tree(tmp_path, {
            "_defaults.yaml": body,
            "db-a.yaml": "tenants:\n  db-a:\n    mysql_threads_running: \"90\"\n"}))
        assert r["status"] == vc.FAIL, r
        assert r["hint"] == vc.NO_DECLARED_DEFAULTS_HINT, r

    def test_a_real_typo_keeps_the_normal_advice(self, tmp_path):
        """Must-still-fire control: a platform that declares defaults gets
        the generic advice — always returning the hint would pass above."""
        r = vc.check_unread_keys(_tree(tmp_path, {
            "_defaults.yaml": _DEFAULTS,
            "db-a.yaml": "tenants:\n  db-a:\n    mysql_conections: \"70\"\n"}))
        assert r["status"] == vc.FAIL and r["hint"] is None, r

    def test_declared_optional_overrides_count_as_declared(self, tmp_path):
        """A platform declaring keys without values declares something."""
        r = vc.check_unread_keys(_tree(tmp_path, {
            "_defaults.yaml": "optional_overrides:\n  - mysql_threads_running\n",
            "db-a.yaml": "tenants:\n  db-a:\n    mysql_conections: \"70\"\n"}))
        assert r["hint"] != vc.NO_DECLARED_DEFAULTS_HINT, r

    def test_the_helper_survives_a_tree_gone_mid_run(self, tmp_path, monkeypatch):
        """`_parse_config_files` exits the process when the directory is gone
        (a ConfigMap volume swapped mid-run): the helper degrades to the
        generic advice, never takes the run with it."""
        import generate_alertmanager_routes as gen

        def gone(_d):
            sys.exit(2)
        monkeypatch.setattr(gen, "_parse_config_files", gone)
        assert vc._no_declared_defaults_hint(str(tmp_path)) is None


def test_undecidable_keeps_its_advice_next_to_a_key_check(tmp_path):
    """No root platform file and a renamed key: the rename line is the
    exporter's key check, not a key found unread, so the advice stays about
    the root file — never "remove a key nothing reads"."""
    r = vc.check_unread_keys(_tree(tmp_path, {"t-a.yaml": "tenants:\n  t-a:\n    mysql_cpu: \"40\"\n"}))
    assert any("renamed" in d for d in r["details"]), r["details"]
    assert r["hint"] == vc._UNREAD_KEYS_UNDECIDABLE_HINT, r


def test_an_unbound_profiles_threshold_key_is_a_notice_not_a_failure(tmp_path):
    """#3395: a threshold key of a profile no tenant binds is not judged;
    the row says so (a notice) and does not fail."""
    r = vc.check_unread_keys(_tree(tmp_path, {
        "_defaults.yaml": _DEFAULTS,
        "_profiles.yaml": "profiles:\n  gold:\n    redis_memory: \"60\"\n  silver:\n    redis_evictd: \"5\"\n",
        "db-a.yaml": "tenants:\n  db-a:\n    _profile: gold\n"}))
    assert r["status"] == vc.WARN, r
    assert any("notice: profile_not_judged `redis_evictd` (profile_unbound)" in d for d in r["details"]), r


# ── #3778: a declared-only key a tenant writes alone is served ─────────────
#
# mysql_threads_running carries no platform value (a healthy running-thread
# count scales with the host's CPU count). It used to sit in the registry's
# defaults tier with `arming: operator`, which renders it in NO runtime face:
# a tenant writing it alone got no /metrics row (served-values `no_row`,
# validate-config rc 1) and the alert went silent on upgrade. The
# optional_overrides tier puts it on the shipped declared list instead. These
# run against the REAL dev `_defaults.yaml` (generated from the registry), so
# moving the key back to the defaults tier and regenerating turns them red.
_DEV_DEFAULTS = (Path(__file__).resolve().parents[2] / "components" / "threshold-exporter"
                 / "config" / "conf.d" / "_defaults.yaml")


def _served(confd: str, tenant: str) -> dict:
    import os
    p = subprocess.run([os.environ["DA_GUARD_BINARY"], "served-values", "--config-dir", confd,
                        "--tenant", tenant], capture_output=True, encoding="utf-8", timeout=600)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)["tenants"][tenant]


def test_a_tenant_writing_mysql_threads_running_alone_is_served(tmp_path):
    d = _tree(tmp_path, {
        "_defaults.yaml": _DEV_DEFAULTS.read_text(encoding="utf-8"),
        "db-x.yaml": "tenants:\n  db-x:\n    mysql_threads_running: \"40\"\n"})
    t = _served(d, "db-x")
    assert t["keys"]["mysql_threads_running"] == {"exporter": True}, t["keys"]
    assert t["values"]["mysql_threads_running"] == 40
    assert {"component": "mysql", "metric": "threads_running", "severity": "warning",
            "tenant": "db-x"} in [s["labels"] for s in t["series"]["mysql_threads_running"]]
    r = vc.check_unread_keys(d)
    assert r["status"] == vc.PASS, r["details"]


def test_its_critical_tier_needs_a_base_in_the_defaults(tmp_path):
    """The critical row is admitted on `defaults[<base>]` only: a tenant base
    alone serves the warning row, not the critical one, until a defaults
    carrier holds a base value (what the docs and changelog say)."""
    defaults = _DEV_DEFAULTS.read_text(encoding="utf-8")
    tenant = ("tenants:\n  db-x:\n    mysql_threads_running: \"40\"\n"
              "    mysql_threads_running_critical: \"50\"\n")
    alone = _served(_tree(tmp_path / "alone", {"_defaults.yaml": defaults, "db-x.yaml": tenant}),
                    "db-x")
    assert alone["keys"]["mysql_threads_running_critical"] == {
        "exporter": False, "reason": "no_row"}, alone["keys"]
    based = _served(_tree(tmp_path / "based", {
        "_defaults.yaml": defaults.replace("\ndefaults:\n", "\ndefaults:\n  mysql_threads_running: 30\n", 1),
        "db-x.yaml": tenant}), "db-x")
    assert based["keys"]["mysql_threads_running_critical"] == {"exporter": True}, based["keys"]
