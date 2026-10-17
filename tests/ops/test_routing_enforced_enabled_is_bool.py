"""#2164: `_routing_enforced.enabled` is honoured only when it IS a boolean.

Measured before the fix: PyYAML reads an unquoted `n` / `y` as the STRING
'n' / 'y', `_grar_parse` tested truthiness, and so `enabled: n` switched the
platform-enforced (NOC) route ON — `generate-routes --validate` rc 0,
`validate-config` rc 0. Same for a quoted `'yes'` / `'no'`.

After: a non-boolean `enabled` is a configuration error. It is NOT enabled,
and validate-config's schema row is FAIL. #2906: the generator refuses the
WHOLE tree on it in every mode (rc 1, nothing written) — it used to print a
`WARN … skipping` line and render without the NOC route at rc 0 — and a
quoted `'true'` is refused like any other string (judged by type, ADR-036
§2). `true` / `false` (and YAML 1.1's unquoted `yes` / `no`, which PyYAML
reads as booleans) are unchanged.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

import validate_config as vc
from generate_alertmanager_routes import load_tenant_configs

# #2115 0-B／B3: validate-config 的 profiles 列經 da-guard 讀租戶，每次都需要 da-guard
# （conftest 以 `go build` 建出；建不起來就 fail、不 skip）。
pytestmark = pytest.mark.usefixtures("da_guard_env")

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_GEN = os.path.join(_REPO, "scripts", "tools", "ops", "generate_alertmanager_routes.py")

_TENANT = ("tenants:\n  t1:\n    mysql_connections: \"70\"\n"
           "    _routing:\n      receiver:\n        type: webhook\n"
           "        url: \"https://t1.example.com/h\"\n")


def _tree(tmp_path, enabled: str) -> str:
    d = tmp_path / "conf.d"
    d.mkdir()
    (d / "_defaults.yaml").write_text(
        "defaults:\n  mysql_connections: 80\n"
        f"_routing_enforced:\n  enabled: {enabled}\n  receiver:\n"
        "    type: webhook\n    url: \"https://noc.example.com/h\"\n",
        encoding="utf-8")
    (d / "t1.yaml").write_text(_TENANT, encoding="utf-8")
    return str(d)


def _gen(config_dir: str, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(  # subprocess-timeout: ignore
        [sys.executable, _GEN, "--config-dir", config_dir, *extra],
        capture_output=True, text=True, encoding="utf-8")


_NOT_BOOL = ["n", "y", "'yes'", "'no'", "'true'", "1", "~"]
_BOOL = [("true", True), ("false", False), ("yes", True), ("no", False)]


@pytest.mark.parametrize("enabled", _NOT_BOOL)
def test_a_non_boolean_enabled_does_not_enable_noc(enabled, tmp_path):
    _r, _d, warnings, enforced, _m = load_tenant_configs(_tree(tmp_path, enabled))
    assert enforced is None
    hits = [w for w in warnings if "_routing_enforced" in w]
    assert len(hits) == 1, warnings
    assert hits[0].lstrip().startswith("ERROR (field type):"), hits[0]
    assert "'enabled' must be a boolean" in hits[0]
    assert (hits[0].kind, hits[0].blocks) == ("routing_enforced_enabled_invalid", "always")


@pytest.mark.parametrize("enabled", _NOT_BOOL)
def test_validate_fails_on_it(enabled, tmp_path):
    p = _gen(_tree(tmp_path, enabled), "--validate")
    assert p.returncode == 1, p.stdout + p.stderr
    assert "'enabled' must be a boolean" in p.stderr


@pytest.mark.parametrize("enabled", ["n", "'yes'", "'true'"])
def test_render_refuses_the_tree_and_writes_nothing(enabled, tmp_path):
    """#2906: render used to print the WARN and write a config without the
    NOC route at rc 0; now the whole tree is refused."""
    out = tmp_path / "am.yaml"
    p = _gen(_tree(tmp_path, enabled), "-o", str(out))
    assert p.returncode == 1, p.stderr
    assert "field value(s) of the wrong type or not recognised" in p.stderr
    assert not out.exists()


@pytest.mark.parametrize("enabled,on", _BOOL)
def test_a_boolean_enabled_is_unchanged(enabled, on, tmp_path):
    d = _tree(tmp_path, enabled)
    _r, _d, warnings, enforced, _m = load_tenant_configs(d)
    assert (enforced is not None) is on
    assert not [w for w in warnings if "_routing_enforced" in w]
    assert _gen(d, "--validate").returncode == 0
    out = tmp_path / "am.yaml"
    assert _gen(d, "-o", str(out)).returncode == 0
    assert ("platform-enforced" in out.read_text(encoding="utf-8")) is on


def _vc_rows(config_dir, cli_argv, capsys):
    cli_argv("validate_config", "--config-dir", config_dir, "--json")
    with pytest.raises(SystemExit) as exc:
        vc.main()
    return exc.value.code, {r["check"]: r for r in json.loads(capsys.readouterr().out)}


@pytest.mark.parametrize("enabled", ["n", "'yes'"])
def test_validate_config_fails_the_schema_row(enabled, tmp_path, cli_argv, capsys):
    rc, rows = _vc_rows(_tree(tmp_path, enabled), cli_argv, capsys)
    assert rc == 1, rows
    assert rows["schema"]["status"] == vc.FAIL, rows["schema"]
    assert any("'enabled' must be a boolean" in d for d in rows["schema"]["details"])


@pytest.mark.parametrize("enabled", ["true", "false"])
def test_validate_config_passes_a_boolean(enabled, tmp_path, cli_argv, capsys):
    rc, rows = _vc_rows(_tree(tmp_path, enabled), cli_argv, capsys)
    assert rc == 0, rows
    assert rows["schema"]["status"] == vc.PASS, rows["schema"]


def _slack_tenant(channel: str) -> str:
    return ("tenants:\n  t1:\n    mysql_connections: \"70\"\n"
            "    _routing:\n      receiver:\n        type: slack\n"
            "        api_url: \"https://hooks.slack.com/services/x\"\n"
            f"        channel: {channel}\n")


@pytest.mark.parametrize("channel", ["yes", "off", "123", "~"])
def test_validate_config_yaml_quoting_row_fails(channel, tmp_path, cli_argv, capsys):
    d = tmp_path / "conf.d"
    d.mkdir()
    (d / "_defaults.yaml").write_text("defaults:\n  mysql_connections: 80\n",
                                      encoding="utf-8")
    (d / "t1.yaml").write_text(_slack_tenant(channel), encoding="utf-8")
    rc, rows = _vc_rows(str(d), cli_argv, capsys)
    assert rc == 1, rows
    row = rows["yaml_quoting"]
    assert row["status"] == vc.FAIL
    assert len(row["details"]) == 1
    assert row["details"][0].startswith(
        "t1.yaml:8: /tenants/t1/_routing/receiver/channel: "), row["details"]


@pytest.mark.parametrize("channel", ['"yes"', "'off'", '"123"', "y", "n"])
def test_validate_config_yaml_quoting_row_passes(channel, tmp_path, cli_argv, capsys):
    d = tmp_path / "conf.d"
    d.mkdir()
    (d / "_defaults.yaml").write_text("defaults:\n  mysql_connections: 80\n",
                                      encoding="utf-8")
    (d / "t1.yaml").write_text(_slack_tenant(channel), encoding="utf-8")
    _rc, rows = _vc_rows(str(d), cli_argv, capsys)
    assert rows["yaml_quoting"]["status"] == vc.PASS, rows["yaml_quoting"]


def test_validate_config_yaml_quoting_covers_defaults_routing(tmp_path, cli_argv, capsys):
    d = tmp_path / "conf.d"
    d.mkdir()
    (d / "_defaults.yaml").write_text(
        "defaults:\n  mysql_connections: 80\n"
        "_routing_defaults:\n  receiver:\n    type: pagerduty\n"
        "    service_key: yes\n", encoding="utf-8")
    (d / "t1.yaml").write_text("tenants:\n  t1:\n    mysql_connections: \"70\"\n",
                               encoding="utf-8")
    rc, rows = _vc_rows(str(d), cli_argv, capsys)
    assert rc == 1
    assert rows["yaml_quoting"]["details"][0].startswith(
        "_defaults.yaml:6: /_routing_defaults/receiver/service_key: "), rows
