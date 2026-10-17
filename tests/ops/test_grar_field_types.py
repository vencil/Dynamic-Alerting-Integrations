"""#2906：產生器的布林與列舉欄位只依型別與值判斷，型別不對或值不認得時整棵樹拒收。

ADR-036 步驟 4 讓產生器改讀 yaml.v3 之後，未加引號的 `yes`／`no`／`on`／`off` 會從布林
變成字串。為了不讓這個切換靜默改變語意，這支 PR 先在今天的 PyYAML 產生器上把規則定死：

* 布林欄位只收布林（與 null＝未設定）：`_routing_enforced.enabled`、domain policy 的
  `constraints.require_critical_escalation`、receiver 的 `send_resolved`、email
  `require_tls`、webhook `http_config.proxy_from_environment`。加引號的 `"yes"`、`"true"`
  一律是型別錯誤；不再把字串轉回布林（`coerce_yaml_bool` 已刪）。
* 列舉欄位只收定義的值：租戶的 `_severity_dedup` 收 `enable`／`disable`（以及 exporter
  也認得的 `enabled`／`on`／`true`、`disabled`／`off`／`false`，去空白、不分大小寫），
  或布林、null（＝預設 enable）。`"no"`、`"yes"`、`""`、數字一律拒收。
* 拒收＝所有模式（render、`--dry-run`、`--output-configmap`、`--apply`、`--validate`、
  `--strict`）rc 1、不寫出、不套用，走 `tree_refusal` 那一條路；validate-config 的
  schema 列 FAIL。
* 今天 PyYAML 讀成布林的未加引號寫法（`enabled: yes`）仍是布林，照收。

租戶 id 都是 fixture 名稱（CLAUDE.md #9）。
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "tools"))
sys.path.insert(0, str(REPO / "scripts" / "tools" / "ops"))
from _grar_parse import (  # noqa: E402
    SEVERITY_DEDUP_WORDS,
    load_tenant_tree,
    severity_dedup_mode,
)
import generate_alertmanager_routes as gar  # noqa: E402
import validate_config as vc  # noqa: E402

_GAR = REPO / "scripts" / "tools" / "ops" / "generate_alertmanager_routes.py"
_REFUSAL = "config problem(s) — nothing was generated, written or applied"

_DEFAULTS = "defaults:\n  cpu_usage_percent: 80\n"
_HOOK = ("      receiver:\n        type: webhook\n"
         "        url: https://hooks.example.com/{t}\n")


def _tenant(t: str, extra: str = "", receiver: str = _HOOK) -> str:
    return (f"tenants:\n  {t}:\n    cpu_usage_percent: '85'\n{extra}"
            f"    _routing:\n{receiver.format(t=t)}")


def _enforced(value: str) -> dict[str, str]:
    return {"_defaults.yaml": _DEFAULTS + (
        f"_routing_enforced:\n  enabled: {value}\n  receiver:\n"
        "    type: webhook\n    url: https://noc.example.com/a\n")}


def _policy(value: str) -> dict[str, str]:
    return {"_domain_policy.yaml": (
        "domain_policies:\n  fin:\n    tenants: [t-a]\n    constraints:\n"
        f"      require_critical_escalation: {value}\n")}


def _dedup(value: str) -> dict[str, str]:
    return {"t-a.yaml": _tenant("t-a", f"    _severity_dedup: {value}\n")}


def _send_resolved(value: str) -> dict[str, str]:
    return {"t-a.yaml": _tenant("t-a", receiver=_HOOK + f"        send_resolved: {value}\n")}


def _require_tls(value: str) -> dict[str, str]:
    return {"t-a.yaml": _tenant("t-a", receiver=(
        "      receiver:\n        type: email\n        to: [a@example.com]\n"
        "        from: am@example.com\n        smarthost: smtp.example.com:587\n"
        f"        require_tls: {value}\n"))}


def _proxy_from_env(value: str) -> dict[str, str]:
    return {"t-a.yaml": _tenant("t-a", receiver=_HOOK + (
        f"        http_config:\n          proxy_from_environment: {value}\n"))}


def _defaults_send_resolved(value: str) -> dict[str, str]:
    """The same field in `_routing_defaults` (a platform file)."""
    return {"_defaults.yaml": _DEFAULTS + (
        "_routing_defaults:\n  receiver:\n    type: webhook\n"
        f"    url: https://hooks.example.com/d\n    send_resolved: {value}\n")}


def _profile_send_resolved(value: str) -> dict[str, str]:
    """... and in a routing profile."""
    return {"_routing_profiles.yaml": (
        "routing_profiles:\n  p1:\n    receiver:\n      type: webhook\n"
        f"      url: https://hooks.example.com/p\n      send_resolved: {value}\n")}


def _override_send_resolved(value: str) -> dict[str, str]:
    """... and in an `overrides` entry's receiver."""
    return {"t-a.yaml": _tenant("t-a", receiver=_HOOK + (
        "      overrides:\n        - alertname: X\n          receiver:\n"
        "            type: webhook\n            url: https://hooks.example.com/o\n"
        f"            send_resolved: {value}\n"))}


# field → (files builder, kind, field the finding names, accepted values,
# refused values). Values are YAML text as written in the file.
FIELDS = {
    "routing_enforced.enabled": (
        _enforced, "routing_enforced_enabled_invalid", "_routing_enforced.enabled",
        ["true", "false", "yes", "off"], ['"true"', "'yes'", "n", "1", "~"]),
    "require_critical_escalation": (
        _policy, "domain_policy_unusable", "constraints.require_critical_escalation",
        ["true", "false", "~", "yes"], ['"true"', "'no'", "1", "[pagerduty]"]),
    "_severity_dedup": (
        _dedup, "severity_dedup_invalid", "_severity_dedup",
        ["enable", "disable", '"Disabled"', "' off '", "false", "no", "~"],
        ['"no"', '"yes"', "''", "1", "maybe", "[disable]"]),
    "send_resolved": (
        _send_resolved, "invalid_receiver_field", "receiver.send_resolved",
        ["true", "false", "yes", "~"], ['"yes"', '"true"', "'Off'", "maybe", "1"]),
    "require_tls": (
        _require_tls, "invalid_receiver_field", "receiver.require_tls",
        ["true", "no"], ['"true"', "'no'"]),
    "proxy_from_environment": (
        _proxy_from_env, "invalid_receiver_field",
        "receiver.http_config.proxy_from_environment",
        ["true", "on"], ['"yes"', "'true'"]),
    "routing_defaults.send_resolved": (
        _defaults_send_resolved, "invalid_receiver_field",
        "_routing_defaults.receiver.send_resolved", ["false"], ['"false"']),
    "routing_profile.send_resolved": (
        _profile_send_resolved, "invalid_receiver_field",
        "routing_profiles.p1.receiver.send_resolved", ["false"], ['"no"']),
    "override.send_resolved": (
        _override_send_resolved, "invalid_receiver_field",
        "overrides[0].receiver.send_resolved", ["true"], ['"true"']),
}


def _write(tmp_path: Path, files: dict[str, str]) -> Path:
    d = tmp_path / "conf.d"
    d.mkdir(parents=True, exist_ok=True)
    base = {"_defaults.yaml": _DEFAULTS, "t-a.yaml": _tenant("t-a"),
            "t-b.yaml": _tenant("t-b")}
    for rel, text in {**base, **files}.items():
        (d / rel).write_text(text, encoding="utf-8")
    return d


def _fake_kubectl(tmp_path: Path) -> tuple[Path, Path]:
    """A `kubectl` that records every call: --apply must never reach it."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    calls = tmp_path / "kubectl-calls.log"
    exe = bindir / "kubectl"
    exe.write_text(f"#!/bin/sh\necho \"$@\" >> '{calls}'\nexit 0\n", encoding="utf-8")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    return bindir, calls


def _gar(d: Path, *args: str, path: str | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    if path is not None:
        env["PATH"] = path
    return subprocess.run(
        [sys.executable, str(_GAR), "--config-dir", str(d), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=300, env=env)


def _errors(d: Path):
    return load_tenant_tree(str(d)).field_type_errors


_CASES = [(name, v) for name, spec in FIELDS.items() for v in spec[4]]
_OK = [(name, v) for name, spec in FIELDS.items() for v in spec[3]]


# ── the reader's verdict, value by value ─────────────────────────────────

@pytest.mark.parametrize("name,value", _CASES, ids=[f"{n}={v}" for n, v in _CASES])
def test_a_refused_value_is_one_field_type_error(tmp_path, name, value):
    build, kind, field, _ok, _bad = FIELDS[name]
    errs = _errors(_write(tmp_path, build(value)))
    assert [(e.kind, e.field) for e in errs] == [(kind, field)], errs


@pytest.mark.parametrize("name,value", _OK, ids=[f"{n}={v}" for n, v in _OK])
def test_an_accepted_value_is_no_error_and_renders(tmp_path, name, value):
    build = FIELDS[name][0]
    d = _write(tmp_path, build(value))
    assert _errors(d) == []
    p = _gar(d, "--dry-run")
    assert p.returncode == 0, p.stderr
    assert _REFUSAL not in p.stderr


# ── every mode refuses, nothing is written or applied ────────────────────

_MODES = {
    "render": lambda out: ["-o", str(out)],
    "render-stdout": lambda out: [],
    "dry-run": lambda out: ["--dry-run"],
    "output-configmap": lambda out: ["--output-configmap", "-o", str(out)],
    "output-configmap-dry-run": lambda out: ["--output-configmap", "--dry-run"],
    "apply": lambda out: ["--apply", "--yes"],
    "validate": lambda out: ["--validate"],
    "strict": lambda out: ["--strict", "-o", str(out)],
    "validate-strict": lambda out: ["--validate", "--strict"],
}
# One quoted string per field: the form this PR newly refuses.
_QUOTED = {"routing_enforced.enabled": '"true"', "require_critical_escalation": '"true"',
           "_severity_dedup": '"no"', "send_resolved": '"yes"', "require_tls": '"true"',
           "proxy_from_environment": '"yes"', "routing_defaults.send_resolved": '"false"',
           "routing_profile.send_resolved": '"no"', "override.send_resolved": '"true"'}


@pytest.mark.parametrize("name", sorted(_QUOTED))
def test_every_mode_refuses_and_writes_nothing(tmp_path, name):
    build, kind, field, _ok, _bad = FIELDS[name]
    d = _write(tmp_path, build(_QUOTED[name]))
    out = tmp_path / "out.yaml"
    bindir, calls = _fake_kubectl(tmp_path)
    for mode, argv in _MODES.items():
        p = _gar(d, *argv(out), path=f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")
        assert p.returncode == 1, (mode, p.returncode, p.stderr)
        assert _REFUSAL in p.stderr, (mode, p.stderr)
        assert "ERROR (field type):" in p.stderr, (mode, p.stderr)
        assert not out.exists(), f"{mode} wrote {out}"
        # Nothing rendered, not even the healthy tenant's route.
        assert "tenant-t-b" not in p.stdout, (mode, p.stdout)
        assert "OK:" not in p.stdout, (mode, p.stdout)
    assert not calls.exists(), f"--apply ran kubectl: {calls.read_text(encoding='utf-8')}"


@pytest.mark.parametrize("name", sorted(_QUOTED))
def test_the_findings_document_names_the_field(tmp_path, name):
    build, kind, field, _ok, _bad = FIELDS[name]
    d = _write(tmp_path, build(_QUOTED[name]))
    doc_path = tmp_path / "findings.json"
    p = _gar(d, "--dry-run", "--findings-json", str(doc_path))
    assert p.returncode == 1, p.stderr
    doc = json.loads(doc_path.read_text(encoding="utf-8"))
    assert doc["exit_code"] == 1
    items = [r for r in doc["findings"] if r["kind"] != "refusal_summary"]
    assert [(r["kind"], r["field"], r["severity"], r["blocks"]) for r in items] == [
        (kind, field, "error", "always")], items


def test_every_error_is_listed_in_one_refusal(tmp_path):
    files = {**_enforced('"true"'), **_policy('"true"')}
    files["t-a.yaml"] = _tenant("t-a", '    _severity_dedup: "no"\n',
                                receiver=_HOOK + '        send_resolved: "yes"\n')
    p = _gar(_write(tmp_path, files), "--validate")
    assert p.returncode == 1, p.stderr
    assert f"FAIL: 4 {_REFUSAL}" in p.stderr, p.stderr
    assert p.stderr.count("ERROR (field type):") == 4, p.stderr


# ── the shared refusal and validate-config ───────────────────────────────

@pytest.mark.parametrize("name", sorted(_QUOTED))
def test_tree_refusal_refuses_it_for_every_consumer(tmp_path, name):
    """explain-route --trace, notification-tester and policy-engine refuse
    through `tree_refusal`, so they see the same refusal and rc."""
    d = _write(tmp_path, FIELDS[name][0](_QUOTED[name]))
    tree = load_tenant_tree(str(d))
    rc, lines = gar.tree_refusal(tree.files_read, tree.tenant_file_errors,
                                 tree.duplicate_tenants, tree.routing_tree_problems,
                                 tree.invalid_tenant_ids, tree.field_type_errors)
    assert rc == 1 and _REFUSAL in lines[0], lines


@pytest.mark.parametrize("name", sorted(_QUOTED))
def test_validate_config_schema_row_fails(tmp_path, name):
    d = _write(tmp_path, FIELDS[name][0](_QUOTED[name]))
    row = vc.check_schema(str(d))
    assert row["status"] == vc.FAIL, row
    assert any("ERROR (field type):" in m for m in row["details"]), row


# ── _severity_dedup: what each accepted value means ──────────────────────

@pytest.mark.parametrize("value,mode", [
    (None, "enable"), (True, "enable"), (False, "disable"),
    *((w, "enable") for w in SEVERITY_DEDUP_WORDS[:4]),
    *((w, "disable") for w in SEVERITY_DEDUP_WORDS[4:]),
    (" Disable ", "disable"), ("ENABLED", "enable"),
    ("no", None), ("yes", None), ("", None), (0, None), (1, None),
    (["disable"], None), ({"a": 1}, None),
])
def test_severity_dedup_mode(value, mode):
    assert severity_dedup_mode(value) == mode


@pytest.mark.parametrize("value,dedup", [("disable", "disable"), ("off", "disable"),
                                         ("no", "disable"), ("enable", "enable")])
def test_unquoted_dedup_words_keep_todays_meaning(tmp_path, value, dedup):
    """`off` / `no` unquoted are the boolean False to PyYAML: still disable."""
    tree = load_tenant_tree(str(_write(tmp_path, _dedup(value))))
    assert tree.field_type_errors == []
    assert tree.dedup_configs["t-a"] == dedup


def test_quoted_bool_word_is_no_longer_written_as_a_boolean(tmp_path):
    """Before #2906 `send_resolved: "yes"` was rendered as `send_resolved:
    true`; an unquoted `yes` (a boolean to PyYAML) still is."""
    d = _write(tmp_path, _send_resolved("yes"))
    p = _gar(d, "--dry-run")
    assert p.returncode == 0, p.stderr
    assert "send_resolved: true" in p.stdout, p.stdout
