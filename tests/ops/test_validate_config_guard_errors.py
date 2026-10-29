"""validate-config `guard_errors`（#3387）：da-guard 自己那支檢查的每一筆 error finding。

判定全部取自 `da-guard --config-dir <dir> --format json` 的 `report.findings`：
`severity: error` 不論 kind 一律 FAIL，嚴重度只有 da-guard 一個來源。這列取代了
先前逐列手寫的鏡像（`values_not_served`、`defaults_wrapper`、`schedule_null`、
`custom_alert_cap`，與 `root_defaults` 裡對應 root_default_refused／
root_defaults_reserved_key／root_defaults_unwrapped 的那半），那些列的代表情境改寫在這裡。

da-guard 結束碼的對應：0／4（路由與 domain policy 由 `routes` 列判）不 FAIL；1 FAIL；
3（exporter 的 load 丟掉的檔）FAIL 並點名；2 與 binary 本身失敗走 load-failure 列
（`_tenant_load_failure_row`，與 `profiles` 同一慣例）。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from _platform_fs import require_shebang_scripts

import _lib_tenant_values as tv
import validate_config as vc

pytestmark = pytest.mark.usefixtures("da_guard_env")

_ROOT = "defaults:\n  mysql_connections: 80\n  mysql_threads_running: 30\n"


def _tree(tmp_path: Path, files: dict[str, str]) -> str:
    d = tmp_path / "conf.d"
    for rel, body in files.items():
        (d / rel).parent.mkdir(parents=True, exist_ok=True)
        (d / rel).write_text(body, encoding="utf-8")
    return str(d)


def _lines(row) -> list[tuple[str, str, str]]:
    """(kind, field, tenants) of each `error:` line of the row."""
    out = []
    for line in row["details"]:
        if not line.startswith("error: "):
            continue
        kind, _, rest = line.removeprefix("error: ").partition(" ")
        field = rest.split("`")[1] if rest.startswith("`") else ""
        who = rest.split(" — tenant(s) ", 1)[1].split(": ", 1)[0] if " — tenant(s) " in rest else ""
        out.append((kind.rstrip(":"), field, who))
    return out


def _run_main(cli_argv, capsys, conf_d: str) -> tuple[int, dict[str, dict]]:
    cli_argv("validate_config", "--config-dir", conf_d, "--json")
    with pytest.raises(SystemExit) as exc:
        vc.main()
    return exc.value.code, {r["check"]: r for r in json.loads(capsys.readouterr().out)}


# ── #3387 本身：子目錄 `_defaults.yaml` 寫了根層沒宣告的閾值鍵 ──────────────────────

_FINANCE = {
    "_defaults.yaml": _ROOT,
    "finance/_defaults.yaml": "defaults:\n  redis_evicted_keys: 100\n",
    "finance/fa.yaml": 'tenants:\n  fa:\n    mysql_connections: "70"\n',
    "finance/fb.yaml": 'tenants:\n  fb:\n    mysql_connections: "70"\n',
}


def test_an_undeclared_subtree_key_fails_one_line_for_every_tenant(tmp_path):
    """同一檔同一鍵影響兩個租戶：一行，列出兩個租戶。"""
    r = vc.check_guard_errors(_tree(tmp_path, _FINANCE))
    assert r["status"] == vc.FAIL and r["caller_error"] is False, r
    assert _lines(r) == [("subtree_default_undeliverable", "redis_evicted_keys", "fa, fb")], r
    assert "Declare `redis_evicted_keys` in the conf.d root `_defaults.yaml`" in r["details"][0], r


def test_an_undeclared_subtree_key_exits_1_end_to_end(tmp_path, cli_argv, capsys):
    """量測（main e2a24667）：同一棵樹 rc 0、只有 unread_keys 列的 notice。現在 rc 1。"""
    rc, rows = _run_main(cli_argv, capsys, _tree(tmp_path, _FINANCE))
    assert rc == 1, rows
    assert rows["guard_errors"]["status"] == vc.FAIL, rows["guard_errors"]
    assert [r for r, v in rows.items() if v["status"] == vc.FAIL] == ["guard_errors"], rows
    # ADR-038 的共用判決列不動：仍是 WARN，不是 FAIL。
    assert rows["unread_keys"]["status"] == vc.WARN, rows["unread_keys"]


def test_an_undeclared_subtree_key_switched_off_is_not_a_finding(tmp_path):
    files = dict(_FINANCE, **{"finance/_defaults.yaml": "defaults:\n  redis_evicted_keys: disable\n"})
    r = vc.check_guard_errors(_tree(tmp_path, files))
    assert r["status"] == vc.PASS, r


def test_more_than_five_tenants_are_counted_not_listed(tmp_path):
    files = {"_defaults.yaml": _ROOT, "finance/_defaults.yaml": "defaults:\n  redis_evicted_keys: 100\n"}
    files.update({f"finance/t{i}.yaml": f"tenants:\n  t{i}: {{}}\n" for i in range(7)})
    r = vc.check_guard_errors(_tree(tmp_path, files))
    assert _lines(r) == [("subtree_default_undeliverable", "redis_evicted_keys",
                          "t0, t1, t2, t3, t4 (+2 more)")], r


# ── 根層與租戶層的值：root_default_refused、value_not_served ─────────────────────────

def test_a_root_value_the_exporter_refuses_fails_here_not_in_root_defaults(tmp_path):
    d = _tree(tmp_path, {"_defaults.yaml": "defaults:\n  mysql_connections: abc\n  container_cpu: 80\n",
                         "fa.yaml": "tenants:\n  fa:\n    container_cpu: 70\n"})
    r = vc.check_guard_errors(d)
    assert r["status"] == vc.FAIL, r
    assert _lines(r) == [("root_default_refused", "_defaults.yaml:defaults.mysql_connections", "")], r
    # root_defaults 不再鏡像這個 finding（#3387）：它自己的三條規則都沒命中。
    assert vc.check_root_defaults(d)["status"] == vc.PASS


@pytest.mark.parametrize("value", ["NaN", "-Inf", ".nan", "disable", "true", "0x10", '"70:critical"'])
def test_every_refused_root_scalar_is_root_default_refused(tmp_path, value):
    d = _tree(tmp_path, {"_defaults.yaml": f"defaults:\n  container_cpu: 80\n  mysql_connections: {value}\n",
                         "fa.yaml": "tenants:\n  fa:\n    container_cpu: 70\n"})
    r = vc.check_guard_errors(d)
    assert ("root_default_refused", "_defaults.yaml:defaults.mysql_connections", "") in _lines(r), r


@pytest.mark.parametrize("sub, body, rel, why", [
    (None, '    mysql_connections: "abc"\n', "team/t.yaml", "value_unparsed"),
    (None, '    mysql_connections: "7O:critical"\n', "team/t.yaml", "value_unparsed"),
    (None, "    mysql_connections_critical: abc\n", "team/t.yaml", "value_unparsed_dropped"),
    (None, '    mysql_connections:\n      default: "70"\n      overrides:\n'
           '        - window: "05:00-05:00"\n          value: "1000"\n', "team/t.yaml", "window_invalid"),
    ("defaults:\n  mysql_connections: abc\n", '    mysql_threads_running: "31"\n',
     "team/_defaults.yaml", "value_rejected"),
    ('defaults:\n  mysql_connections:\n    default: "70"\n    overrides: "01:00-09:00"\n',
     '    mysql_threads_running: "31"\n', "team/_defaults.yaml", "value_rejected"),
    (None, "    'mysql_connections{q=\"a\"}': 5\n    \"mysql_connections{q='a'}\": 6\n",
     "team/t.yaml", "spelling_duplicate"),
], ids=["tenant-abc", "tenant-bad-severity", "critical-dropped", "window", "subtree-abc",
        "subtree-rejected", "spelling-duplicate"])
def test_a_value_not_served_as_written_fails(tmp_path, sub, body, rel, why):
    """先前 `values_not_served` 列的每一種形狀：訊息以 not_served 原因開頭、點名檔案。"""
    files = {"_defaults.yaml": _ROOT, "team/t.yaml": "tenants:\n  tx:\n" + body}
    if sub is not None:
        files["team/_defaults.yaml"] = sub
    r = vc.check_guard_errors(_tree(tmp_path, files))
    assert r["status"] == vc.FAIL, r
    hits = [ln for ln in r["details"] if ln.startswith("error: value_not_served ")]
    assert len(hits) == 1, r
    assert "— tenant(s) tx: " + why + ": " + rel + " writes " in hits[0], hits


@pytest.mark.parametrize("body", [
    '    mysql_connections: "70"\n',
    '    mysql_connections: "60:critical"\n',
    "    mysql_connections: disable\n",
    '    mysql_connections:\n      default: "70"\n      overrides:\n'
    '        - window: "22:00-06:00"\n          value: "1000"\n',
])
def test_values_served_as_written_pass(tmp_path, body):
    r = vc.check_guard_errors(_tree(tmp_path, {"_defaults.yaml": _ROOT, "team/t.yaml": "tenants:\n  tx:\n" + body}))
    assert r["status"] == vc.PASS, r


# ── custom alerts：value_not_served、custom_alert_duplicate_series、
#    custom_alert_rejected、custom_alert_over_cap ──────────────────────────────────

_UNSERVED = (
    "    _custom_alerts:\n"
    "      - {recipe: threshold, name: a, metric: m1, op: '>', window: 5m, threshold: 'NaN'}\n"
    "      - {recipe: threshold, name: b, metric: m2, op: '>', window: 5m, threshold: '80'}\n"
    "      - {recipe: threshold, name: b_again, metric: m2, op: '>', window: 5m, threshold: '90'}\n"
    "      - {recipe: threshold, name: w, metric: m4, op: '>', window: 5x, threshold: '80'}\n"
    "      - {recipe: threshold, name: off, metric: m5, op: '>', window: 5m, threshold: disable}\n")


def test_unserved_custom_alerts_fail_with_their_kinds(tmp_path):
    r = vc.check_guard_errors(_tree(tmp_path, {"_defaults.yaml": _ROOT, "t.yaml": "tenants:\n  tx:\n" + _UNSERVED}))
    assert r["status"] == vc.FAIL, r
    assert sorted(_lines(r)) == [
        ("custom_alert_duplicate_series", "_custom_alerts[2]", "tx"),
        ("custom_alert_rejected", "_custom_alerts[3]", "tx"),
        ("value_not_served", "_custom_alerts[0]", "tx"),
    ], r


def test_a_custom_alerts_block_that_does_not_decode_fails(tmp_path):
    r = vc.check_guard_errors(_tree(tmp_path, {"_defaults.yaml": _ROOT,
                                               "t.yaml": "tenants:\n  tx:\n    _custom_alerts: 'not: [a list'\n"}))
    assert r["status"] == vc.FAIL, r
    assert any(k == "custom_alert_rejected" for k, _f, _t in _lines(r)), r


def _thr(i: int) -> str:
    return f"      - {{recipe: threshold, name: r{i}, metric: m{i}, op: '>', window: 5m, threshold: '10'}}\n"


@pytest.mark.parametrize("where, over", [
    ("tenant", True), ("platform", True), ("profile", False),
])
def test_custom_alert_cap_counts_a_tenants_own_list_only(tmp_path, where, over):
    """先前 `custom_alert_cap` 列：租戶檔與根平台檔 `tenants:` 的清單算租戶自己的；
    profile 的是它的租戶共用的形狀，不計。"""
    cap = json.loads((Path(__file__).resolve().parents[2] / "components/threshold-exporter/app/pkg/config"
                      / "recipe_contract.json").read_text(encoding="utf-8"))["custom_alert_cap"]
    alerts = "    _custom_alerts:\n" + "".join(_thr(i) for i in range(cap + 1))
    files = {"_defaults.yaml": _ROOT, "t1.yaml": "tenants:\n  t1:\n    mysql_connections: 70\n"}
    if where == "tenant":
        files["t1.yaml"] = "tenants:\n  t1:\n" + alerts
    elif where == "platform":
        files["_platform.yaml"] = "tenants:\n  t1:\n" + alerts
    else:
        files["_profiles.yaml"] = "profiles:\n  p1:\n" + alerts
        files["t1.yaml"] = "tenants:\n  t1:\n    _profile: p1\n"
    r = vc.check_guard_errors(_tree(tmp_path, files))
    assert (("custom_alert_over_cap", "_custom_alerts", "t1") in _lines(r)) is over, r


# ── defaults 載體的包裝：root_defaults_unwrapped、defaults_toplevel_ignored、
#    root_defaults_reserved_key ─────────────────────────────────────────────────────

_TX = "tenants:\n  tenant-x:\n    container_cpu: \"70\"\n"


@pytest.mark.parametrize("body", [
    "mysql_connections: 80\ncontainer_cpu: 60\n",
    "defaults:\nmysql_connections: 80\ncontainer_cpu: 60\n",
    "_severity_dedup: disable\n",
], ids=["no-defaults-key", "defaults-with-no-value", "reserved-key"])
def test_an_unwrapped_root_carrier_fails(tmp_path, body):
    d = _tree(tmp_path, {"_defaults.yaml": body, "tenant-x.yaml": _TX})
    r = vc.check_guard_errors(d)
    assert ("root_defaults_unwrapped", "_defaults.yaml", "") in _lines(r), r
    # root_defaults 不再鏡像它（#3387）。
    assert vc.check_root_defaults(d)["status"] == vc.PASS


@pytest.mark.parametrize("body", [
    "defaults:\n  container_cpu: 80\n",
    "state_filters: {}\n_routing_defaults: {}\n",
    "_metadata:\n  owner: dba\n_x: &x 1\n",
    "# placeholder\n",
], ids=["wrapped", "decoded-and-reserved-only", "metadata-and-anchor", "comment-only"])
def test_root_carrier_shapes_that_must_stay_green(tmp_path, body):
    r = vc.check_guard_errors(_tree(tmp_path, {"_defaults.yaml": body, "tenant-x.yaml": _TX}))
    assert not any(k == "root_defaults_unwrapped" for k, _f, _t in _lines(r)), r


@pytest.mark.parametrize("key", ["_namespaces", "_state_maintenance", "_severity_dedup"])
def test_a_registered_reserved_key_under_root_defaults_fails(tmp_path, key):
    d = _tree(tmp_path, {"_defaults.yaml": f"defaults:\n  container_cpu: 80\n  {key}: 1\n",
                         "tenant-x.yaml": _TX})
    r = vc.check_guard_errors(d)
    assert ("root_defaults_reserved_key", f"_defaults.yaml:defaults.{key}", "") in _lines(r), r
    # root_defaults 只剩 #3045 的 WARN（`_` 鍵不是閾值），不再 FAIL。
    assert vc.check_root_defaults(d)["status"] == vc.WARN


@pytest.mark.parametrize("sub, fails", [
    ("defaults: {}\n_severity_dedup: disable\nmysql_connections: 70\n", True),
    ("defaults: {}\nmysql_connections: 70\n", True),
    ("_severity_dedup: disable\nmysql_connections: 70\n", False),
    ("defaults:\n  mysql_connections: 70\n", False),
], ids=["mapping-plus-top-keys", "mapping-plus-threshold", "unwrapped", "keys-under-defaults"])
def test_a_defaults_mapping_beside_top_level_keys_fails(tmp_path, sub, fails):
    """先前 `defaults_wrapper` 列：`defaults:` 是 mapping 時 merge 只讀它。"""
    r = vc.check_guard_errors(_tree(tmp_path, {"_defaults.yaml": _ROOT, "team/_defaults.yaml": sub,
                                               "team/tx.yaml": "tenants:\n  tx: {}\n"}))
    assert (("defaults_toplevel_ignored", "team/_defaults.yaml", "") in _lines(r)) is fails, r


# ── schedule：schedule_null_value（先前的 `schedule_null` 列）─────────────────────────

_WINDOW_NULL = '{default: "50", overrides: [{window: "00:00-23:59", value: null}]}'
_SCHED = {
    "_defaults.yaml": "defaults:\n  mysql_connections: 30\n",
    "_profiles.yaml": "profiles:\n  std:\n    other: \"1\"\n",
    "sub/_defaults.yaml": "defaults:\n  mysql_connections: 40\n",
    "sub/tx.yaml": "tenants:\n  tx:\n    _profile: std\n",
}


@pytest.mark.parametrize("layer, rel, where, tenant", [
    ("tenant", "sub/tx.yaml", "tenants.tx.mysql_connections", "tx"),
    ("subtree", "sub/_defaults.yaml", "defaults.mysql_connections", ""),
    ("platform", "_platform.yaml", "tenants.tx.mysql_connections", "tx"),
    ("profile", "_profiles.yaml", "profiles.std.mysql_connections", ""),
])
def test_a_null_beside_windows_fails_at_every_layer(tmp_path, layer, rel, where, tenant):
    files = dict(_SCHED)
    line = f"    mysql_connections: {_WINDOW_NULL}\n"
    if layer == "tenant":
        files["sub/tx.yaml"] += line
    elif layer == "subtree":
        files["sub/_defaults.yaml"] = f"defaults:\n  mysql_connections: {_WINDOW_NULL}\n"
    elif layer == "platform":
        files["_platform.yaml"] = "tenants:\n  tx:\n" + line
    else:
        files["_profiles.yaml"] = "profiles:\n  std:\n" + line
    r = vc.check_guard_errors(_tree(tmp_path, files))
    assert ("schedule_null_value", f"{rel}:{where}", tenant) in _lines(r), r
    assert any('`overrides[0]` (window "00:00-23:59") has `value: null`' in ln for ln in r["details"]), r


@pytest.mark.parametrize("value", [
    "null", "{default: null}", '{default: "50", overrides: [{window: "00:00-23:59", value: "70"}]}',
])
def test_no_window_null_and_numeric_schedules_pass(tmp_path, value):
    files = dict(_SCHED)
    files["sub/tx.yaml"] += f"    mysql_connections: {value}\n"
    assert vc.check_guard_errors(_tree(tmp_path, files))["status"] == vc.PASS


def test_files_no_plane_reads_are_not_judged(tmp_path):
    files = dict(_SCHED)
    files["sub/_extra.yaml"] = f"tenants:\n  tx:\n    mysql_connections: {_WINDOW_NULL}\n"
    files["_defaults.yml"] = f"defaults:\n  mysql_connections: {_WINDOW_NULL}\n"
    assert vc.check_guard_errors(_tree(tmp_path, files))["status"] == vc.PASS


# ── 這列 == da-guard：同一棵樹，(kind, field, tenant) 集合相同 ────────────────────────

def test_the_row_is_every_da_guard_error_and_nothing_else(tmp_path, da_guard_binary):
    files = dict(_FINANCE)
    files["_defaults.yaml"] = "defaults:\n  mysql_connections: 80\n  mysql_threads_running: abc\n"
    files["finance/fa.yaml"] = 'tenants:\n  fa:\n    mysql_connections: "7O:critical"\n' + _UNSERVED
    files["finance/fb.yaml"] = "tenants:\n  fb:\n    mysql_connections: 70\n    container_cpu: 5\n"
    d = _tree(tmp_path, files)
    proc = subprocess.run([da_guard_binary, "--config-dir", d, "--format", "json"],
                          capture_output=True, check=False, timeout=600)
    assert proc.returncode == 1, proc.stderr
    findings = json.loads(proc.stdout)["report"]["findings"]
    gate = {(f["kind"], f.get("field", ""), f.get("tenant_id", ""))
            for f in findings if f["severity"] == "error"}
    r = vc.check_guard_errors(d)
    row = {(k, fld, t) for k, fld, who in _lines(r) for t in (who.split(", ") if who else [""])}
    assert row == gate and len(gate) >= 5, (row, gate)


# ── da-guard 結束碼的對應 ──────────────────────────────────────────────────────────

def test_exit_4_routing_only_is_not_a_fail(tmp_path, cli_argv, capsys):
    """路由由 route generator 判（da-guard 回 4）：本列 PASS，並說 routes 列判它。"""
    d = _tree(tmp_path, {"_defaults.yaml": _ROOT, "fa.yaml":
                         "tenants:\n  fa:\n    mysql_connections: 70\n    _routing:\n"
                         "      receiver: {type: webhook, url: 'https://x.example'}\n"})
    rep = tv.load_guard_report(d)
    assert rep.returncode == 4 and rep.undetermined == 1, rep
    r = vc.check_guard_errors(d)
    assert r["status"] == vc.PASS, r
    assert any("da-guard exit 4" in ln and "`routes` row" in ln for ln in r["details"]), r
    _rc, rows = _run_main(cli_argv, capsys, d)
    assert rows["guard_errors"]["status"] == vc.PASS, rows["guard_errors"]


def test_exit_4_with_an_error_finding_still_fails(tmp_path):
    """1 優先於 4：有 error 的樹 da-guard 回 1，本列照 FAIL。"""
    d = _tree(tmp_path, {"_defaults.yaml": _ROOT, "fa.yaml":
                         "tenants:\n  fa:\n    mysql_connections: abc\n    _routing:\n"
                         "      receiver: {type: webhook, url: 'https://x.example'}\n"})
    assert tv.load_guard_report(d).returncode == 1
    assert vc.check_guard_errors(d)["status"] == vc.FAIL


def test_exit_3_names_the_dropped_files_and_fails(tmp_path):
    """3：exporter 的 load 丟掉的檔——FAIL（樹的問題，不是 caller error），點名檔案、附
    da-guard 的 stderr；其餘檔的 error finding 照列。"""
    d = _tree(tmp_path, {"_defaults.yaml": _ROOT,
                         "fa.yaml": "tenants:\n  fa:\n    mysql_connections: 70\n  fa:\n    x: 1\n",
                         "fb.yaml": "tenants:\n  fb:\n    mysql_connections: abc\n"})
    r = vc._run_check("guard_errors", vc.check_guard_errors, d, _config_dir=d)
    assert r["status"] == vc.FAIL and r["caller_error"] is False, r
    assert ("value_not_served", "mysql_connections", "fb") in _lines(r), r
    assert any(ln.startswith("da-guard (exit 3): the exporter's load drops 1 path(s): fa.yaml (does not decode)")
               for ln in r["details"]), r
    assert any(ln.startswith(tv.DA_GUARD_PREFIX) for ln in r["details"]), r
    assert r["hint"] == vc._GUARD_ERRORS_TREE_UNREADABLE_HINT, r


def test_no_da_guard_is_the_load_failure_rows_caller_error(tmp_path, monkeypatch):
    """da-guard 不可用：與 `profiles`／先前 `values_not_served` 同一慣例——caller error FAIL，
    提示說去哪裡拿 da-guard。（`main()` 在任何列之前就以 parse-tree 要求 da-guard，
    所以 CLI 是 rc 2、只有 config_dir 一列；這裡測的是列本身。）"""
    d = _tree(tmp_path, _FINANCE)
    monkeypatch.setenv("DA_GUARD_BINARY", str(tmp_path / "no-such-da-guard"))
    r = vc.check_guard_errors(d)
    assert r["status"] == vc.FAIL and r["caller_error"] is True, r
    assert r["hint"] == vc._GUARD_ERRORS_NO_DA_GUARD_HINT, r


def _fake(tmp_path: Path, real: str, *, rc: int | None = None, stdout: str | None = None,
          accepts_help: bool = True) -> str:
    """A da-guard that runs the real one and replaces its exit code / stdout."""
    require_shebang_scripts()
    p = tmp_path / "fake-da-guard"
    p.write_text(
        f"#!{sys.executable}\n"
        "import subprocess, sys\n"
        f"if '-h' in sys.argv[1:]:\n    sys.exit(0 if {accepts_help!r} else 2)\n"
        f"r = subprocess.run([{real!r}] + sys.argv[1:], capture_output=True, text=True)\n"
        f"out = r.stdout if {stdout!r} is None else {stdout!r}\n"
        f"sys.stdout.write(out); sys.stderr.write(r.stderr)\n"
        f"sys.exit(r.returncode if {rc!r} is None else {rc!r})\n", encoding="utf-8")
    p.chmod(0o755)
    return str(p)


@pytest.mark.parametrize("rc, stdout, accepts, hint", [
    (7, None, True, "broken"),                                    # an exit code the check never uses
    (0, "not json", True, "broken"),                              # output that is not its JSON
    (1, '{"undetermined": [], "report": {"findings": []}}', True, "broken"),   # 1 with no error
    (0, None, True, "broken"),                                    # 0 with an error finding
    (3, '{"undetermined": [], "report": null}', True, "broken"),  # 3 naming no file
    (2, "", False, "stale"),                                      # does not accept the flags
], ids=["exit-7", "no-json", "exit-1-no-error", "exit-0-with-error", "exit-3-no-file", "too-old"])
def test_a_da_guard_that_does_not_answer_as_da_guard_is_the_binarys_fault(
        tmp_path, monkeypatch, da_guard_binary, rc, stdout, accepts, hint):
    d = _tree(tmp_path, _FINANCE)   # da-guard: exit 1, one error finding per tenant
    monkeypatch.setenv("DA_GUARD_BINARY", _fake(tmp_path, da_guard_binary, rc=rc, stdout=stdout,
                                                accepts_help=accepts))
    with pytest.raises(tv.GuardReportError) as ei:
        tv.load_guard_report(d)
    assert getattr(ei.value, hint) is True, ei.value
    r = vc.check_guard_errors(d)
    assert r["status"] == vc.FAIL and r["caller_error"] is True, r
    assert r["hint"] == (vc._DA_GUARD_BROKEN_HINT if hint == "broken" else vc._DA_GUARD_TOO_OLD_HINT), r


def test_exit_2_from_a_da_guard_that_accepts_the_flags_is_not_the_binarys(tmp_path, monkeypatch,
                                                                           da_guard_binary):
    d = _tree(tmp_path, _FINANCE)
    monkeypatch.setenv("DA_GUARD_BINARY", _fake(tmp_path, da_guard_binary, rc=2, stdout=""))
    with pytest.raises(tv.GuardReportError) as ei:
        tv.load_guard_report(d)
    assert not ei.value.binary_fault, ei.value
    r = vc.check_guard_errors(d)
    assert r["status"] == vc.FAIL and r["caller_error"] is False, r


def test_warnings_are_not_this_rows(tmp_path):
    """da-guard 的 warning（這裡是 F0 的一筆）不在本列：PASS，只說有幾筆。"""
    r = vc.check_guard_errors(_tree(tmp_path, {
        "_defaults.yaml": "defaults:\n  mysql_connections: 80\n  mysql_connections_critical: 95\n",
        "t.yaml": "tenants:\n  tenant-a:\n    mysql_connections_critical: 95\n"}))
    assert r["status"] == vc.PASS, r
    assert r["details"] == ["no error finding from da-guard (1 warning(s), not this row's)"], r


def test_the_deleted_mirror_rows_are_gone(tmp_path, cli_argv, capsys):
    """#3387：逐列手寫的鏡像已刪，嚴重度只剩 da-guard 一個來源。"""
    _rc, rows = _run_main(cli_argv, capsys, _tree(tmp_path, _FINANCE))
    for gone in ("values_not_served", "defaults_wrapper", "schedule_null", "custom_alert_cap"):
        assert gone not in rows, sorted(rows)
        assert not hasattr(vc, f"check_{gone}"), gone
