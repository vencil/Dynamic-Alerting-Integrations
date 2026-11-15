"""validate-config `routes` 列上的共用鍵判決（ADR-038）：沒有任何讀取端讀的鍵。

判決來自共用判決（`da-guard key-verdict`，經 `_lib_key_verdict` 讀），由
`generate-routes --validate` 報出；`routes` 列就是那支指令（#3788），所以判決的每一行、
擋不擋，都是那支指令的（不變式釘在 `test_validate_config_routes_is_validate.py`）。
#3873 起 validate-config 不再另有 `unread_keys` 列：本檔釘的是判決的行出現在 `routes`
列、以及該列針對這些行給的建議（`_key_verdict_hint`）——判不了的鍵、判決給不出來、平台
什麼都沒宣告時，都不能叫人「刪掉沒人讀的鍵」。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import _lib_tenant_values as tv
import validate_config as vc
from _platform_fs import require_shebang_scripts

pytestmark = pytest.mark.usefixtures("da_guard_env", "da_crdecode_env", "da_read_env")

_DEFAULTS = "defaults:\n  mysql_connection_usage_percent: 80\n  mysql_slave_lag: 30\n  redis_memory: 70\n"
_CLI_DOCS = "docs/cli-reference.md#validate-config"


def _tree(root: Path, files: dict[str, str]) -> str:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    return str(root)


def _key_lines(row: dict) -> list[str]:
    """The row's `key verdict:` lines without their level word, once each
    (`--validate` repeats a blocking line under its `FAIL:` summary)."""
    out: list[str] = []
    for d in row["details"]:
        _, sep, line = d.partition("key verdict: ")
        if sep and line not in out:
            out.append(line)
    return out


def _cli(conf_d: str) -> tuple[subprocess.CompletedProcess, dict]:
    p = subprocess.run([sys.executable, vc.__file__, "--config-dir", conf_d, "--json"],
                       capture_output=True, encoding="utf-8", timeout=600)
    return p, {r["check"]: r for r in json.loads(p.stdout)}


def test_each_finding_is_one_line_and_an_error_fails(tmp_path):
    r = vc.check_routes(_tree(tmp_path, {
        "_defaults.yaml": _DEFAULTS,
        "db-a.yaml": "tenants:\n  db-a:\n    mysql_conections: 70\n    mysql_slave_lagg: disable\n"
                     "    redis_memory: disable\n    redis_memory_critical: 90\n"}))
    assert r["status"] == vc.FAIL and not r["caller_error"]
    assert _key_lines(r) == [
        "db-a.yaml: error: unread `mysql_conections` (no_row) — tenant(s) db-a",
        "db-a.yaml: warning: disable_noop `mysql_slave_lagg` (disable_noop) — tenant(s) db-a",
        "db-a.yaml: warning: disable_cascade `redis_memory_critical` (switched off with its base"
        " `redis_memory`) — tenant(s) db-a",
    ]  # #3046: db-a's own critical tier is switched off with its base — a warning


def test_an_inherited_sibling_switched_off_with_its_base_is_no_finding(tmp_path):
    """#3046: the inherited `redis_memory_critical` is switched off with
    `redis_memory: disable` — read by the exporter, nothing to report."""
    r = vc.check_routes(_tree(tmp_path, {
        "_defaults.yaml": _DEFAULTS,
        "team/_defaults.yaml": "defaults:\n  redis_memory_critical: 95\n",
        "team/db-a.yaml": "tenants:\n  db-a:\n    redis_memory: disable\n"}))
    assert _key_lines(r) == [], r["details"]
    assert r["status"] == vc.PASS, r


def test_a_platform_side_finding_is_one_line_with_its_tenants(tmp_path):
    files = {"_defaults.yaml": _DEFAULTS, "_profiles.yaml": "profiles:\n  p1:\n    mysql_conections: 70\n"}
    for t in ("ta", "tb"):
        files[f"{t}.yaml"] = f"tenants:\n  {t}:\n    _profile: p1\n"
    r = vc.check_routes(_tree(tmp_path, files))
    assert _key_lines(r) == ["_profiles.yaml: error: unread `mysql_conections` (no_row) — tenant(s) ta, tb"]
    assert r["status"] == vc.FAIL  # #3394: the severity is the defect's, wherever it is written


def test_a_profile_key_no_reader_reads_is_an_error_on_the_profile(tmp_path):
    """The `profiles` row judges no key names (#3042); a `_` key the registry
    does not list, written in a profile, is reported in the key verdict instead,
    on the profile file — and `_custom_alerts`, which a profile carries, is not."""
    r = vc.check_routes(_tree(tmp_path, {
        "_defaults.yaml": _DEFAULTS,
        "_profiles.yaml": "profiles:\n  p1:\n    _unknown_thing: bad\n    _custom_alerts:\n"
                          "      - {recipe: threshold, name: q, metric: m, op: \">\", window: 5m,"
                          " threshold: \"1:warning\"}\n",
        "ta.yaml": "tenants:\n  ta:\n    _profile: p1\n"}))
    assert _key_lines(r) == ["_profiles.yaml: error: unread `_unknown_thing` (unregistered) — tenant(s) ta"]


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
    r = vc.check_routes(_tree(tmp_path, {"_defaults.yaml": _DEFAULTS,
                                         "_profiles.yaml": _PROFILE_KEYS, "ta.yaml": text}))
    who = "tenant(s) ta" if "_profile: p1" in text else "no tenant"
    got = [d for d in _key_lines(r) if d.startswith("_profiles.yaml:")]
    assert got == [
        f"_profiles.yaml: error: unread `_namespaces` (not_read_in_profile) — {who}",
        f"_profiles.yaml: error: unread `_profile` (not_read_in_profile) — {who}",
        f"_profiles.yaml: error: unread `_unknown_thing` (unregistered) — {who}",
        # p2 is bound by no tenant: its threshold key is not judged (#3395).
        "_profiles.yaml: notice: profile_not_judged `mysql_connection_usage_percent` (profile_unbound) — no tenant",
    ], r["details"]


def test_an_undecidable_key_fails(tmp_path):
    """#3016 option A: a threshold key no root platform file lets the
    verdict judge fails the row, as tenant-api refuses such a write (422)."""
    r = vc.check_routes(_tree(tmp_path, {"ta.yaml": "tenants:\n  ta:\n    mysql_conections: 70\n"}))
    assert r["status"] == vc.FAIL and not r["caller_error"]
    assert _key_lines(r) == [
        "no root platform file was read: 1 threshold key(s) cannot be judged (undecidable)"]
    # Nothing was found unread: the advice is about the root file.
    assert r["hint"] == vc.KEY_VERDICT_UNDECIDABLE_HINT


def test_a_finding_gets_the_key_advice(tmp_path):
    """A key found unread gets the key advice (fix or remove it), not the
    routes row's generic line about `_routing`, and the page that explains it."""
    r = vc.check_routes(_tree(tmp_path, {
        "_defaults.yaml": _DEFAULTS, "db-a.yaml": "tenants:\n  db-a:\n    mysql_conections: 70\n"}))
    assert r["status"] == vc.FAIL and r["hint"] == vc.KEY_VERDICT_ROUTES_HINT, r
    assert "misspelt key" in r["hint"] and r["hint_docs"] == _CLI_DOCS, r


def test_a_key_warning_alone_gets_the_key_advice(tmp_path):
    """A `disable` that switches nothing off is a warning: the row is WARN, and
    its advice is the key advice, not the generic `_routing` line."""
    r = vc.check_routes(_tree(tmp_path, {
        "_defaults.yaml": _DEFAULTS, "db-a.yaml": "tenants:\n  db-a:\n    mysql_slave_lagg: disable\n"}))
    assert r["status"] == vc.WARN and r["hint"] == vc.KEY_VERDICT_ROUTES_HINT, r


_BAD_RECEIVER = ("    _routing:\n      receiver:\n        type: webhook\n"
                 "        url: https://hooks.example.com/a\n        max_alerts: 3\n")


def test_a_key_finding_next_to_a_routing_one_gets_both_pieces_of_advice(tmp_path):
    """Both kinds of line in one row: the advice on each, the routing one first."""
    r = vc.check_routes(_tree(tmp_path, {
        "_defaults.yaml": _DEFAULTS,
        "db-a.yaml": "tenants:\n  db-a:\n    mysql_conections: \"70\"\n" + _BAD_RECEIVER}))
    assert r["status"] == vc.FAIL, r
    assert r["hint"] == f"{vc._CHECK_HINTS['routes'][0]} {vc.KEY_VERDICT_ROUTES_HINT}", r


def test_a_tree_with_only_routing_is_no_undecidable_failure(tmp_path):
    """No root platform file but no threshold key either: nothing the
    verdict could not judge, so nothing fails (ADR-038 decision 7)."""
    r = vc.check_routes(_tree(tmp_path, {
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
    規則）是一般 FAIL；建議講 da-guard，不講「修拼錯的鍵」。"""
    def boom(*_a, **_k):
        raise exc
    monkeypatch.setattr(vc.key_verdict, "judge_tree", boom)
    verdicts: list = []
    r = vc.check_routes(_tree(tmp_path, {"_defaults.yaml": _DEFAULTS}), verdict_out=verdicts)
    assert r["status"] == vc.FAIL and r["caller_error"] is caller_error
    assert any(d.startswith("  ERROR: key verdict incomplete: ") for d in r["details"]), r
    assert r["hint"] == vc.KEY_VERDICT_INCOMPLETE_HINT
    # The no_alert row is handed the reason, not a verdict.
    assert verdicts == [exc], verdicts


def test_an_unread_key_fails_the_run_on_the_routes_row(tmp_path):
    """#3016：判到就擋——拼錯的閾值鍵讓 routes 列 FAIL，整支 exit 1；#3873 起沒有
    `unread_keys` 列。"""
    p, rows = _cli(_tree(tmp_path, {"_defaults.yaml": _DEFAULTS,
                                    "db-a.yaml": "tenants:\n  db-a:\n    mysql_conections: \"70\"\n"}))
    assert rows["routes"]["status"] == vc.FAIL
    assert "unread_keys" not in rows, sorted(rows)
    assert p.returncode == 1, p.stderr


def test_a_clean_nested_tree_passes_without_a_flat_read_caveat(tmp_path):
    """The verdict reads the whole tree through da-guard: a subdirectory is
    not something the row skipped, so it is no WARN and no
    `skipped_nested_files`."""
    p, rows = _cli(_tree(tmp_path, {"_defaults.yaml": _DEFAULTS,
                                    "sub/_defaults.yaml": "defaults:\n  mysql_connection_usage_percent: 75\n",
                                    "sub/ta.yaml": "tenants:\n  ta:\n    mysql_connection_usage_percent: \"70\"\n"}))
    row = rows["routes"]
    assert row["status"] == vc.PASS, row
    assert not row.get("skipped_nested_files"), row
    # The root-only `_policies` note is policy_dsl's to print, once.
    assert p.stderr.count("`_policies` are read from the root") <= 1, p.stderr


def test_a_rule_naming_a_profile_key_no_tenant_gets_does_not_count(tmp_path):
    """Policy-as-Code reads a tenant's view: a rule naming `_namespaces` reads
    nothing from a profile no tenant binds, so the key is still reported."""
    defaults = _DEFAULTS + ("_policies:\n  - {name: ns, target: _namespaces, operator: required,"
                            " severity: error}\n")
    r = vc.check_routes(_tree(tmp_path, {
        "_defaults.yaml": defaults,
        "_profiles.yaml": "profiles:\n  p1:\n    _namespaces: [x]\n",
        "ta.yaml": "tenants:\n  ta:\n    mysql_connection_usage_percent: 70\n"}))
    assert "_profiles.yaml: error: unread `_namespaces` (not_read_in_profile) — no tenant" in _key_lines(r), r


def test_the_exporters_key_checks_are_printed_from_their_fields(tmp_path):
    """ADR-038 §11: a `version` label the exporter refuses and a retired
    spelling are the exporter's own checks (da-guard served-values'
    key_checks); this tool judges no key name itself. #3703: the line names
    the metric and the key's stand-in, never the key as written."""
    r = vc.check_routes(_tree(tmp_path, {
        "_defaults.yaml": "defaults:\n  container_cpu: 80\n  mysql_threads_running: 30\n",
        "db-a.yaml": "tenants:\n  db-a:\n    mysql_cpu: \"40\"\n"
                     "    container_cpu{version=\"V2!\"}: \"5\"\n"}))
    # The dimensional key's `version` label is undeclared: it is unread too.
    assert r["status"] == vc.FAIL, r
    assert any("key check version_label_charset: metric container_cpu, key <key:" in d
               and "tenant=db-a" in d for d in r["details"]), r["details"]
    assert any("key check renamed: metric mysql_cpu, key mysql_cpu, replacement metric "
               "mysql_threads_running" in d for d in r["details"]), r["details"]
    assert not any("V2!" in d for d in r["details"]), r["details"]


class TestAPlatformDeclaringNothingIsNotAdvisedAway:
    """⛔ With no platform defaults declared, the verdict reads every tenant
    threshold key as unread, and the generic key advice — remove a key
    nothing reads — would delete a working config. The row says the
    platform side is missing instead (the hint the `unread_keys` row carried
    until #3873, and the `schema` row until ADR-038 §11)."""

    @pytest.mark.parametrize("body", [
        "_routing_defaults:\n  group_wait: 30s\n",  # no `defaults:` key
        "defaults: {}\n",
    ], ids=["no-defaults-key", "empty-defaults"])
    def test_the_destructive_hint_is_replaced(self, tmp_path, body):
        r = vc.check_routes(_tree(tmp_path, {
            "_defaults.yaml": body,
            "db-a.yaml": "tenants:\n  db-a:\n    mysql_threads_running: \"90\"\n"}))
        assert r["status"] == vc.FAIL, r
        # no-defaults-key: its `_routing_defaults` without a receiver is a
        # routing line too, whose advice comes first.
        assert r["hint"].endswith(vc.NO_DECLARED_DEFAULTS_HINT), r
        assert vc.KEY_VERDICT_ROUTES_HINT not in r["hint"], r

    def test_the_hint_survives_a_routing_finding_beside_it(self, tmp_path):
        """A routing line in the same row gets its own advice; the ⛔ one is
        still there after it."""
        r = vc.check_routes(_tree(tmp_path, {
            "_defaults.yaml": "defaults: {}\n",
            "db-a.yaml": "tenants:\n  db-a:\n    mysql_threads_running: \"90\"\n" + _BAD_RECEIVER}))
        assert r["status"] == vc.FAIL, r
        assert r["hint"] == f"{vc._CHECK_HINTS['routes'][0]} {vc.NO_DECLARED_DEFAULTS_HINT}", r

    def test_a_real_typo_keeps_the_normal_advice(self, tmp_path):
        """Must-still-fire control: a platform that declares defaults gets
        the generic key advice — always returning the hint would pass above."""
        r = vc.check_routes(_tree(tmp_path, {
            "_defaults.yaml": _DEFAULTS,
            "db-a.yaml": "tenants:\n  db-a:\n    mysql_conections: \"70\"\n"}))
        assert r["status"] == vc.FAIL and r["hint"] == vc.KEY_VERDICT_ROUTES_HINT, r

    def test_declared_optional_overrides_count_as_declared(self, tmp_path):
        """A platform declaring keys without values declares something."""
        r = vc.check_routes(_tree(tmp_path, {
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
    r = vc.check_routes(_tree(tmp_path, {"t-a.yaml": "tenants:\n  t-a:\n    mysql_cpu: \"40\"\n"}))
    assert any("renamed" in d for d in r["details"]), r["details"]
    assert r["hint"] == vc.KEY_VERDICT_UNDECIDABLE_HINT, r


def test_an_unbound_profiles_threshold_key_is_a_notice_not_a_failure(tmp_path):
    """#3395: a threshold key of a profile no tenant binds is not judged;
    the row says so (a notice) and does not fail."""
    r = vc.check_routes(_tree(tmp_path, {
        "_defaults.yaml": _DEFAULTS,
        "_profiles.yaml": "profiles:\n  gold:\n    redis_memory: \"60\"\n  silver:\n    redis_evictd: \"5\"\n",
        "db-a.yaml": "tenants:\n  db-a:\n    _profile: gold\n"}))
    assert r["status"] != vc.FAIL, r
    assert "_profiles.yaml: notice: profile_not_judged `redis_evictd` (profile_unbound) — no tenant" in \
        _key_lines(r), r


def test_a_tree_refused_before_the_keys_are_judged_is_judged_for_no_alert(tmp_path):
    """A tenant declared in two files: `--validate` refuses the tree before it
    judges any key, so the routes row hands no verdict on, and the no_alert row
    asks for one itself (here da-guard refuses the tree too, and says so)."""
    _p, rows = _cli(_tree(tmp_path, {
        "_defaults.yaml": _DEFAULTS,
        "ta.yaml": "tenants:\n  ta:\n    mysql_connection_usage_percent: \"70\"\n",
        "other.yaml": "tenants:\n  ta:\n    mysql_connection_usage_percent: \"60\"\n"}))
    assert rows["routes"]["status"] == vc.FAIL, rows["routes"]
    assert not any("key verdict" in d for d in rows["routes"]["details"]), rows["routes"]
    (line,) = rows["no_alert"]["details"]
    assert line.startswith("not checked: ") and "KeyVerdictError" in line, line


@pytest.mark.parametrize("tenant", [
    pytest.param("tenants:\n  db-a:\n    mysql_connection_usage_percent: \"70\"\n", id="clean"),
    pytest.param("tenants:\n  db-a:\n    mysql_conections: \"70\"\n", id="unread-key"),
])
def test_one_report_judges_the_keys_once(tmp_path, monkeypatch, tenant):
    """#3873：一次 validate-config 只跑一次 `da-guard key-verdict`——routes 列判出的判決
    交給 no_alert 列，判決有給出時也一樣（不只是給不出來的時候）。以計數 wrapper 記下
    每次 da-guard 的子命令。"""
    require_shebang_scripts()  # the counting wrapper is a `#!` script
    conf_d = _tree(tmp_path / "conf.d", {"_defaults.yaml": _DEFAULTS, "db-a.yaml": tenant})
    log = tmp_path / "calls.log"
    wrapper = tmp_path / "dg-wrap.sh"
    wrapper.write_text(f'#!/bin/sh\necho "$1" >> "{log}"\nexec "$DA_GUARD_REAL" "$@"\n',
                       encoding="utf-8")
    wrapper.chmod(0o755)
    monkeypatch.setenv("DA_GUARD_REAL", os.environ["DA_GUARD_BINARY"])
    monkeypatch.setenv("DA_GUARD_BINARY", str(wrapper))
    _p, rows = _cli(conf_d)
    assert rows["no_alert"]["status"] in (vc.PASS, vc.WARN), rows["no_alert"]
    assert not rows["no_alert"]["details"][0].startswith("not checked"), rows["no_alert"]
    calls = log.read_text(encoding="utf-8").split()
    assert calls.count("key-verdict") == 1, calls


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
    r = vc.check_routes(d)
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
