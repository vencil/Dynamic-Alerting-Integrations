"""validate-config `values_not_served`（#2065）：/metrics 沒有照寫的值送出的閾值值。

判定全部取自 `da-guard effective` 的 `not_served`（exporter 自己的 resolver／build 記錄），
本列只挑 da-guard `value_not_served` 會擋的那幾種（`value_not_served_as_written`）。
"""
from __future__ import annotations

import json
import subprocess

import pytest

import _lib_tenant_values as tv
import validate_config as vc

pytestmark = pytest.mark.usefixtures("da_guard_env")

_ROOT = "defaults:\n  mysql_connections: 80\n  mysql_threads_running: 30\n"

# 每種 shape：(team/_defaults.yaml 或 None, 租戶 tx 的 body, 預期的 (file, key, reason))
_SHAPES = {
    "unparsed": (None, '    mysql_connections: "abc"\n',
                 ("team/t.yaml", "mysql_connections", "value_unparsed")),
    "unparsed-severity": (None, '    mysql_connections: "7O:critical"\n',
                          ("team/t.yaml", "mysql_connections", "value_unparsed")),
    "dropped": (None, "    mysql_connections_critical: abc\n",
                ("team/t.yaml", "mysql_connections_critical", "value_unparsed_dropped")),
    "window-start-equals-end": (
        None, '    mysql_connections:\n      default: "70"\n      overrides:\n'
              '        - window: "05:00-05:00"\n          value: "1000"\n',
        ("team/t.yaml", "mysql_connections", "window_invalid")),
    "window-hour-24": (
        None, '    mysql_connections:\n      default: "70"\n      overrides:\n'
              '        - window: "01:00-24:00"\n          value: "1000"\n',
        ("team/t.yaml", "mysql_connections", "window_invalid")),
    "window-missing": (
        None, '    mysql_connections:\n      default: "70"\n      overrides:\n        - value: "1000"\n',
        ("team/t.yaml", "mysql_connections", "window_invalid")),
    "subtree-rejected": (
        'defaults:\n  mysql_connections:\n    default: "70"\n    overrides: "01:00-09:00"\n',
        '    mysql_threads_running: "31"\n',
        ("team/_defaults.yaml", "mysql_connections", "value_rejected")),
    # #2031：同一 mapping 把一個閾值寫成兩種拼法；送出的是 canonical 那個，另一個點名（照原文）。
    "spelling-duplicate": (
        None, "    'mysql_connections{q=\"a\"}': 5\n    \"mysql_connections{q='a'}\": 6\n",
        ("team/t.yaml", "mysql_connections{q='a'}", "spelling_duplicate")),
}


def _tree(tmp_path, sub, body):
    d = tmp_path / "conf.d"
    (d / "team").mkdir(parents=True)
    (d / "_defaults.yaml").write_text(_ROOT, encoding="utf-8")
    if sub is not None:
        (d / "team" / "_defaults.yaml").write_text(sub, encoding="utf-8")
    (d / "team" / "t.yaml").write_text("tenants:\n  tx:\n" + body, encoding="utf-8")
    return d


@pytest.mark.parametrize("shape", sorted(_SHAPES))
def test_each_shape_fails_and_is_named(tmp_path, shape):
    sub, body, (rel, key, reason) = _SHAPES[shape]
    r = vc.check_values_not_served(str(_tree(tmp_path, sub, body)))
    assert r["status"] == vc.FAIL, r
    assert len(r["details"]) == 1, r
    assert r["details"][0] == f"{rel}: tenant tx: `{key}`: {reason}", r


@pytest.mark.parametrize("body", [
    '    mysql_connections: "70"\n',
    '    mysql_connections: "60:critical"\n',
    "    mysql_connections: disable\n",
    '    mysql_connections:\n      default: "70"\n      overrides:\n'
    '        - window: "22:00-06:00"\n          value: "1000"\n',
    '    mysql_connections:\n      default: "70"\n      overrides:\n'
    '        - window: "05:00-05:01"\n          value: "1000"\n',
])
def test_values_served_as_written_pass(tmp_path, body):
    r = vc.check_values_not_served(str(_tree(tmp_path, None, body)))
    assert r["status"] == vc.PASS, r


def test_reserved_subtree_key_is_not_this_rows(tmp_path):
    """子目錄 `_defaults.yaml` 的保留鍵（mapping 值）同樣被 overlay 拒收、effective 也標
    value_rejected，但那是 subtree_default_reserved_key（routing 鍵則是 route generator）的事：本列不報。"""
    d = _tree(tmp_path, "defaults:\n  _routing_defaults:\n    receiver: {type: webhook}\n",
              '    mysql_connections: "70"\n')
    eff = tv.load_effective(d)
    assert eff["tx"].not_served.get("_routing_defaults") == tv.NotServedKey(
        "value_rejected", "team/_defaults.yaml"), eff["tx"].not_served
    r = vc.check_values_not_served(str(d))
    assert r["status"] == vc.PASS, r


def test_row_matches_da_guard_value_not_served(tmp_path, da_guard_binary):
    """同一棵樹：本列點名的 (tenant, key, reason) 集合 == da-guard 主 gate 的
    value_not_served findings——Python 的挑選式與 Go 的 ValueNotServedAsWritten 同一個答案。"""
    d = tmp_path / "conf.d"
    (d / "team").mkdir(parents=True)
    (d / "_defaults.yaml").write_text(_ROOT, encoding="utf-8")
    (d / "team" / "_defaults.yaml").write_text(
        'defaults:\n  mysql_threads_running:\n    default: "33"\n    overrides: "01:00-09:00"\n'
        "  _routing_defaults:\n    receiver: {type: webhook}\n", encoding="utf-8")
    (d / "team" / "tx.yaml").write_text(
        'tenants:\n  tx:\n    mysql_connections: "7O:critical"\n    mysql_connections_critical: abc\n',
        encoding="utf-8")
    (d / "team" / "ty.yaml").write_text(
        'tenants:\n  ty:\n    mysql_connections:\n      default: "70"\n      overrides:\n'
        '        - window: "05:00-05:00"\n          value: "1"\n    mysql_threads_running: "31"\n',
        encoding="utf-8")
    r = vc.check_values_not_served(str(d))
    assert r["status"] == vc.FAIL, r
    row = set()
    for line in r["details"]:
        _file, tenant, key, reason = line.split(": ", 3)
        row.add((tenant.removeprefix("tenant "), key.strip("`"), reason))
    proc = subprocess.run([da_guard_binary, "--config-dir", str(d), "--format", "json"],
                          capture_output=True, check=False, timeout=600)
    assert proc.returncode == 1, proc.stderr
    gate = {(f["tenant_id"], f["field"], f["message"].split(":", 1)[0])
            for f in json.loads(proc.stdout)["report"]["findings"] if f["kind"] == "value_not_served"}
    assert row == gate and len(row) == 4, (row, gate)


def test_end_to_end_exits_1(tmp_path, capsys, cli_argv):
    sub, body, (rel, _key, _reason) = _SHAPES["window-start-equals-end"]
    cli_argv("validate_config", "--config-dir", str(_tree(tmp_path, sub, body)))
    with pytest.raises(SystemExit) as exc:
        vc.main()
    out = capsys.readouterr().out
    assert exc.value.code == 1, out
    assert "[FAIL] values_not_served" in out and rel in out, out


# #3209：custom alert 的 `threshold:` 被 exporter 拒收（Go 在 served-values 標 `refused`）——
# 與閾值 row 拒收同一列 FAIL；本列不自己判定，只列出 Go 的標記。
_CA = "    _custom_alerts:\n      - {recipe: threshold, name: q_high, metric: qd, op: '>', window: %s, threshold: %s}\n"


@pytest.mark.parametrize("threshold, refused", [
    ("abc", "unparsed"), ("'NaN'", "non_finite"), ("'-Inf:critical'", "non_finite"), ("'70:high'", "unparsed"),
])
def test_refused_custom_alert_threshold_fails(tmp_path, threshold, refused):
    r = vc.check_values_not_served(str(_tree(tmp_path, None, _CA % ("5m", threshold))))
    assert r["status"] == vc.FAIL, r
    assert len(r["details"]) == 1, r
    assert r["details"][0].startswith(
        "team/t.yaml: tenant tx: `_custom_alerts[0]` (`q_high`): rejected (value_not_served): "), r


@pytest.mark.parametrize("window, threshold", [
    ("5m", "'80:critical'"), ("5m", "disable"), ("5m", "'disable:critical'"),
])
def test_custom_alert_threshold_not_refused_passes(tmp_path, window, threshold):
    r = vc.check_values_not_served(str(_tree(tmp_path, None, _CA % (window, threshold))))
    assert r["status"] == vc.PASS, r


# #3234、#3235：exporter 不送的每一筆 custom alert——門檻值拒收、與前一筆重複、
# 因其他欄位（window、quantile、objective……）拒收——都是本列的 FAIL，列出的就是
# Go 印在 served-values `custom_alerts[].finding` 的那個 kind；關掉而「打開才會被拒」
# 的那筆是 exporter 認可的 opt-out，不是 finding。
_UNSERVED = (
    "    _custom_alerts:\n"
    "      - {recipe: threshold, name: a, metric: m1, op: '>', window: 5m, threshold: 'NaN'}\n"
    "      - {recipe: threshold, name: b, metric: m2, op: '>', window: 5m, threshold: '80'}\n"
    "      - {recipe: threshold, name: c, metric: m3, op: '>', window: 5m, threshold: abc}\n"
    "      - {recipe: threshold, name: b_again, metric: m2, op: '>', window: 5m, threshold: '90'}\n"
    "      - {recipe: threshold, name: w, metric: m4, op: '>', window: 5x, threshold: '80'}\n"
    "      - {recipe: p99_latency, name: lat, metric: req_seconds_bucket, quantile: '1.5', threshold: '0.5', window: 5m}\n"
    "      - {recipe: slo_burn_rate, name: avail, metric: e, denominator_metric: r, objective: '100'}\n"
    "      - {recipe: slo_burn_rate, name: soffbad, metric: e3, denominator_metric: r3, objective: disable, mode: loud}\n"
    "      - {recipe: threshold, name: off, metric: m5, op: '>', window: 5m, threshold: disable}\n")
_UNSERVED_FINDINGS = {
    ("tx", "_custom_alerts[0]", "value_not_served"), ("tx", "_custom_alerts[2]", "value_not_served"),
    ("tx", "_custom_alerts[3]", "custom_alert_duplicate_series"), ("tx", "_custom_alerts[4]", "custom_alert_rejected"),
    ("tx", "_custom_alerts[5]", "custom_alert_rejected"), ("tx", "_custom_alerts[6]", "custom_alert_rejected"),
}


def _custom_alert_lines(row):
    out = set()
    for line in row["details"]:
        _file, tenant, rest = line.split(": ", 2)
        field, _name, _status, kind = rest.split(" ", 3)
        out.add((tenant.removeprefix("tenant "), field.strip("`"), kind.split(")", 1)[0].strip("(")))
    return out


def test_custom_alert_lines_match_da_guard_findings(tmp_path, da_guard_binary):
    """同一棵樹：本列點名的 custom alert (tenant, field, kind) == da-guard 主 gate 對
    `_custom_alerts[i]` 的 findings，三種 kind 都在。"""
    d = _tree(tmp_path, None, _UNSERVED)
    r = vc.check_values_not_served(str(d))
    assert r["status"] == vc.FAIL, r
    proc = subprocess.run([da_guard_binary, "--config-dir", str(d), "--format", "json"],
                          capture_output=True, check=False, timeout=600)
    assert proc.returncode == 1, proc.stderr
    gate = {(f["tenant_id"], f["field"], f["kind"]) for f in json.loads(proc.stdout)["report"]["findings"]
            if f["field"].startswith("_custom_alerts[")}
    assert _custom_alert_lines(r) == gate == _UNSERVED_FINDINGS, (r, gate)


@pytest.mark.parametrize("entry, kind", [
    # #3234：同一 shape 與 severity 的第二筆被 exporter 丟掉。
    ("{recipe: threshold, name: q_again, metric: qd, op: '>', window: 5m, threshold: '90'}",
     "custom_alert_duplicate_series"),
    # #3235：image 裡沒有 jsonschema 時，這些只有本列擋得到。
    ("{recipe: threshold, name: w, metric: qd2, op: '>', window: 5x, threshold: '80'}", "custom_alert_rejected"),
    ("{recipe: p99_latency, name: lat, metric: req_seconds_bucket, quantile: '1.5', threshold: '0.5', window: 5m}",
     "custom_alert_rejected"),
    ("{recipe: slo_burn_rate, name: avail, metric: e, denominator_metric: r, objective: '100'}",
     "custom_alert_rejected"),
])
def test_unserved_custom_alert_end_to_end_exits_1(tmp_path, capsys, cli_argv, entry, kind):
    body = _CA % ("5m", "'80'") + "      - " + entry + "\n"
    cli_argv("validate_config", "--config-dir", str(_tree(tmp_path, None, body)))
    with pytest.raises(SystemExit) as exc:
        vc.main()
    out = capsys.readouterr().out
    assert exc.value.code == 1, out
    assert "[FAIL] values_not_served" in out and f"`_custom_alerts[1]`" in out and f"({kind})" in out, out


def test_refused_custom_alert_end_to_end_exits_1(tmp_path, capsys, cli_argv):
    cli_argv("validate_config", "--config-dir", str(_tree(tmp_path, None, _CA % ("5m", "'NaN'"))))
    with pytest.raises(SystemExit) as exc:
        vc.main()
    out = capsys.readouterr().out
    assert exc.value.code == 1, out
    assert "[FAIL] values_not_served" in out and "`_custom_alerts[0]`" in out, out


def test_tree_served_values_refuses_is_warn_not_fail(tmp_path):
    """effective 讀得了、served-values 拒收的樹（兩個 key 擁有同一條 series）：閾值 key 照判，
    custom alert 那半沒檢查——WARN 並說明，不是 FAIL（不新增結束碼變化）。"""
    d = tmp_path / "conf.d"
    d.mkdir()
    (d / "_defaults.yaml").write_text(
        "defaults:\n  mysql_connections: 80\n  mysql_connections_critical: 95\n", encoding="utf-8")
    (d / "t.yaml").write_text("tenants:\n  tx:\n    mysql_connections_critical: 95\n", encoding="utf-8")
    r = vc.check_values_not_served(str(d))
    assert r["status"] == vc.WARN, r
    assert r["details"][-1].startswith("custom alerts not checked: "), r
    assert r["hint"] == vc._VALUES_NOT_SERVED_CUSTOM_UNCHECKED_HINT, r


def test_a_custom_alerts_block_that_does_not_decode_fails(tmp_path):
    """#3235：整段 `_custom_alerts` 解不開時 exporter 一筆都不送；Go 印在 served-values
    的 `custom_alerts_finding`，本列照列（先前 da-guard 無 finding、本列 PASS）。"""
    r = vc.check_values_not_served(str(_tree(tmp_path, None, "    _custom_alerts: 'not: [a list'\n")))
    assert r["status"] == vc.FAIL, r
    assert any("`_custom_alerts`: does not decode (custom_alert_rejected)" in d for d in r["details"]), r


def test_a_da_guard_without_the_finding_field_is_a_caller_error(tmp_path, monkeypatch, da_guard_binary):
    """#3234：舊版 da-guard 的 served-values 沒有 `finding`——不是「沒有 finding」，是
    沒有判定：本列 caller error（結束碼 2），不再是 WARN 讓不送的告警整棵樹通過。"""
    import sys as _sys
    fake = tmp_path / "old-da-guard"
    fake.write_text(
        f"#!{_sys.executable}\n"
        "import json, subprocess, sys\n"
        f"p = subprocess.run([{da_guard_binary!r}] + sys.argv[1:], capture_output=True, text=True)\n"
        "out = p.stdout\n"
        "if len(sys.argv) > 1 and sys.argv[1] == 'served-values':\n"
        "    doc = json.loads(out)\n"
        "    for tv in doc.get('tenants', {}).values():\n"
        "        tv.pop('custom_alerts_finding', None)\n"
        "        for e in tv.get('custom_alerts', []):\n"
        "            e.pop('finding', None)\n"
        "    out = json.dumps(doc)\n"
        "sys.stdout.write(out); sys.stderr.write(p.stderr); sys.exit(p.returncode)\n", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("DA_GUARD_BINARY", str(fake))
    r = vc.check_values_not_served(str(_tree(tmp_path, None, _CA % ("5x", "'80'"))))
    assert r["status"] == vc.FAIL and r["caller_error"], r
