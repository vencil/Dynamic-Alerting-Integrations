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
    assert r["details"][0].startswith(f"{rel}: tenant tx: `{key}`: {reason}: "), r


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
    value_rejected，但那是 subtree_default_reserved_key／routing 檢查的事：本列不報。"""
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
        _file, tenant, key, reason, _ = line.split(": ", 4)
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
