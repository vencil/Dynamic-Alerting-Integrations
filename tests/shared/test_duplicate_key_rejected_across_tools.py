"""A conf.d file with a key written twice is refused by every judging tool,
the way that tool refuses a syntax error (#2123).

The exporter (yaml.v3) rejects such a file; before #2123 every tool in
`_TOOLS` took PyYAML's last value and exited 0. The fixture shapes
(`_TENANT_VARIANTS`, `_DEFAULTS_VARIANTS`) are ones the exporter was
measured to reject. #2231 added `_profiles.yaml` as a third target
(`_PROFILES_VARIANTS`) and the readers that diff, list, resolve or generate
from a conf.d rather than judge it.

⛔ The expected outcome is NOT written down per tool. Each tool is run on the
same tree with a real syntax error in the same file, and a duplicate must
take exactly that path: same exit code, file named or not named the same
way. So "every tool uses its existing 'YAML is invalid' path" is what is
asserted, and a tool whose syntax-error handling changes carries this test
with it. The anti-vacuity half: the clean tree must come out DIFFERENTLY
from the syntax-error tree (else "same as syntax" would hold for a tool
that ignores the file entirely).
"""
from __future__ import annotations

import functools
import os
import pathlib
import subprocess
import sys
import tempfile

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
TOOLS = REPO / "scripts" / "tools"

# #2115 0-B: policy_engine / policy_opa_bridge read the tree through da-guard
# (the exporter's own load); the subprocess finds this repo's build via
# `$DA_GUARD_BINARY`.
pytestmark = pytest.mark.usefixtures("da_guard_env")

_DEFAULTS = "defaults:\n  mysql_connections: 80\n"
# policy_engine only reads tenant files when a policy exists to evaluate.
_POLICY = ("_policies:\n  - name: mc\n    target: mysql_connections\n"
           "    operator: required\n    severity: error\n")
_TENANT = 'tenants:\n  tenant-a:\n    mysql_connections: "70"\n'

_TENANT_VARIANTS = {
    "clean": _TENANT,
    "syntax": "tenants:\n  tenant-a: [unclosed\n",
    "dup_key": ('tenants:\n  tenant-a:\n    mysql_connections: "70"\n'
                '    mysql_connections: "90"\n'),
    "dup_tenant_id": ('tenants:\n  tenant-a:\n    mysql_connections: "70"\n'
                      '  tenant-a:\n    mysql_connections: "90"\n'),
    "dup_tenants_block": ('tenants:\n  tenant-a:\n    mysql_connections: "70"\n'
                          'tenants:\n  tenant-a:\n    mysql_connections: "90"\n'),
    # #2114: two spellings of ONE tenant id — the exporter keys tenants by
    # the raw text, so `123` and `"123"` are the same key (yaml.v3:
    # `mapping key "123" already defined`). Every tool, including the ones
    # that read tenant ids as raw text, must refuse it like bad syntax.
    "dup_tenant_id_by_spelling": ('tenants:\n  tenant-a:\n    mysql_connections: "70"\n'
                                  '  123:\n    mysql_connections: "1"\n'
                                  '  "123":\n    mysql_connections: "2"\n'),
}
_DEFAULTS_VARIANTS = {
    "clean": "",
    "syntax": "defaults:\n  mysql_connections: [80\n",
    "dup_key": "defaults:\n  mysql_connections: 80\n  mysql_connections: 90\n",
}
# #2231: written (and the tenant given `_profile: p1`) only for a tool whose
# targets include "profiles", so no other tool's fixture changes.
_PROFILES = "profiles:\n  p1:\n    mysql_connections: 60\n"
_PROFILES_VARIANTS = {
    "clean": _PROFILES,
    "syntax": "profiles:\n  p1: [unclosed\n",
    "dup_key": "profiles:\n  p1:\n    mysql_connections: 60\n    mysql_connections: 65\n",
}
# Read by generate_tenant_mapping_rules, whose `--validate` checks the ids
# it names against the tenant files.
_INSTANCE_MAPPING = ("instance_tenant_mapping:\n  i1:\n"
                     "    - tenant: tenant-a\n      filter: 'db=\"x\"'\n")
_TARGET_FILE = {"tenant": "tenant-a.yaml", "defaults": "_defaults.yaml",
                "profiles": "_profiles.yaml"}

# (script, argv after it — {d} is the conf.d, {out} a scratch dir, {old} a
# clean copy of the same tree for the tools that diff against a baseline),
# which files of the tree it reads (its targets), whether the fixture needs a
# policy. A tool is given "profiles" only where a `_profiles.yaml` syntax
# error was measured to change its outcome; elsewhere that row would fail
# its own anti-vacuity half, proving nothing.
_T, _TD, _TDP = (frozenset({"tenant"}), frozenset({"tenant", "defaults"}),
                 frozenset({"tenant", "defaults", "profiles"}))
_TOOLS = {
    "validate_config": ("ops/validate_config.py", ["--config-dir", "{d}"], _TDP, False),
    "check_confd_schema": ("lint/check_confd_schema.py", ["--config-dir", "{d}"], _TDP, False),
    "check_routing_profiles": ("lint/check_routing_profiles.py", ["--config-dir", "{d}"],
                               _TDP, False),
    "generate_routes_validate": ("ops/generate_alertmanager_routes.py",
                                 ["--config-dir", "{d}", "--validate"], _TDP, False),
    "compile_custom_alerts": ("dx/compile_custom_alerts.py",
                              ["--config-dir", "{d}", "--out", "{out}/pack.yaml"], _TDP, False),
    "policy_engine": ("ops/policy_engine.py", ["--config-dir", "{d}", "--ci"], _TD, True),
    "gitops_check_local": ("ops/gitops_check.py", ["local", "--dir", "{d}"], _TD, False),
    "describe_tenant": ("dx/describe_tenant.py", ["tenant-a", "--conf-d", "{d}"], _TD, False),
    "tenant_verify": ("dx/tenant_verify.py", ["tenant-a", "--conf-d", "{d}"], _TD, False),
    "assemble_config_dir_validate": ("ops/assemble_config_dir.py",
                                     ["--sources", "{d}", "--output", "{out}/asm", "--validate"],
                                     _TDP, False),
    # --dry-run: stops at the OPA input document, no OPA needed.
    "policy_opa_bridge": ("ops/policy_opa_bridge.py", ["--config-dir", "{d}", "--dry-run"],
                          _TD, False),
    "offboard_tenant_precheck": ("ops/offboard_tenant.py", ["tenant-a", "--config-dir", "{d}"],
                                 _TDP, False),
    # #2231 — readers that give no pass/fail on the file but whose output,
    # exit code or write followed PyYAML's last value.
    "config_diff": ("ops/config_diff.py", ["--old-dir", "{old}", "--new-dir", "{d}"],
                    frozenset({"tenant", "profiles"}), False),
    # rc 0 all three ways; told apart by naming the file (its WARNING line).
    "generate_tenant_metadata": ("dx/generate_tenant_metadata.py",
                                 ["--config-dir", "{d}", "--dry-run"], _T, False),
    "diagnose_show_inheritance": ("ops/diagnose.py",
                                  ["tenant-a", "--config-dir", "{d}", "--show-inheritance"],
                                  _TDP, False),
    # Port 9 (discard) refuses at once; --skip-if-unavailable then exits 0
    # for a tree with changes, so only the read path can give it rc 2.
    "backtest_threshold": ("ops/backtest_threshold.py",
                           ["--config-dir", "{d}", "--baseline", "{old}",
                            "--prometheus", "http://127.0.0.1:9", "--skip-if-unavailable"],
                           _T, False),
    "generate_tenant_mapping_rules_validate": ("ops/generate_tenant_mapping_rules.py",
                                               ["--config-dir", "{d}", "--validate",
                                                "--dry-run"], _T, False),
    # The `--config-dir` branch reads through load_tenant_configs (strict
    # since #2123); the single-file branch is its own read.
    "analyze_rule_pack_gaps_file": ("ops/analyze_rule_pack_gaps.py",
                                    ["--tenant-config", "{d}/tenant-a.yaml"], _T, False),
    "migrate_conf_d": ("dx/migrate_conf_d.py", ["--conf-d", "{d}"], _T, False),
}

# A tool whose (rc, file named) could be the same for a clean tree and a
# syntax error — its pre-check names every file — is told apart by the marker
# its "cannot read this file" line prints. (Since #2179 its pre-check also
# exits 1 on an unreadable file; the marker still pins which path it took.)
_UNREADABLE_MARKER = {
    "offboard_tenant_precheck": "無法讀取",
    # Names every layer's source file in the chain and exits 0 either way;
    # an unreadable layer is its skip line.
    "diagnose_show_inheritance": "WARN: skip",
}


def _write_tree(d: str, tool: str, target: str, variant: str) -> None:
    """The fixture conf.d for *tool*, with *variant* in *target*'s file."""
    _script, _argv, targets, policy = _TOOLS[tool]
    pol = _POLICY if policy else ""
    defaults = _DEFAULTS + pol
    tenant = _TENANT
    profiles = _PROFILES
    if target == "tenant":
        tenant = _TENANT_VARIANTS[variant]
    elif target == "defaults" and variant != "clean":
        defaults = _DEFAULTS_VARIANTS[variant] + pol
    elif target == "profiles":
        profiles = _PROFILES_VARIANTS[variant]
    os.makedirs(d)
    pathlib.Path(d, "_defaults.yaml").write_text(defaults, encoding="utf-8")
    if "profiles" in targets:
        # Only the first `tenant-a:` gets it: a duplicate stays a duplicate.
        tenant = tenant.replace("  tenant-a:\n", "  tenant-a:\n    _profile: p1\n", 1)
        pathlib.Path(d, "_profiles.yaml").write_text(profiles, encoding="utf-8")
    if tool == "generate_tenant_mapping_rules_validate":
        pathlib.Path(d, "_instance_mapping.yaml").write_text(_INSTANCE_MAPPING,
                                                              encoding="utf-8")
    pathlib.Path(d, "tenant-a.yaml").write_text(tenant, encoding="utf-8")


@functools.lru_cache(maxsize=None)
def _outcome(tool: str, target: str, variant: str) -> tuple[int, bool, bool]:
    """(exit code, names the broken file?, prints its unreadable marker?)."""
    script, argv, _targets, _policy = _TOOLS[tool]
    with tempfile.TemporaryDirectory() as tmp:
        d = os.path.join(tmp, "conf.d")
        old = os.path.join(tmp, "old")
        _write_tree(d, tool, target, variant)
        _write_tree(old, tool, "tenant", "clean")
        r = subprocess.run(
            [sys.executable, str(TOOLS / script)]
            + [a.format(d=d, out=tmp, old=old) for a in argv],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=tmp, timeout=180)
    out = r.stdout + r.stderr
    named = _TARGET_FILE[target] in out
    marker = _UNREADABLE_MARKER.get(tool)
    return r.returncode, named, bool(marker) and marker in out


_CASES = (
    [(t, "tenant", v) for t in _TOOLS for v in ("dup_key", "dup_tenant_id", "dup_tenants_block",
                                                 "dup_tenant_id_by_spelling")]
    + [(t, g, "dup_key") for t, spec in _TOOLS.items()
       for g in ("defaults", "profiles") if g in spec[2]]
)


@pytest.mark.parametrize("tool,target,variant", _CASES,
                         ids=[f"{t}-{g}-{v}" for t, g, v in _CASES])
def test_a_duplicate_key_takes_the_syntax_error_path(tool, target, variant):
    syntax = _outcome(tool, target, "syntax")
    clean = _outcome(tool, "tenant", "clean")
    assert syntax != clean, (
        f"{tool}: a syntax error in {target} comes out the same as a clean tree "
        f"({syntax}) — the tool does not read that file, so this row proves nothing")
    got = _outcome(tool, target, variant)
    assert got == syntax, (
        f"{tool}: a duplicate key ({variant} in {target}) gave (rc, file named, marker) = "
        f"{got}; a syntax error in the same file gives {syntax}, the clean tree {clean}. "
        f"A key written twice is invalid YAML — the exporter rejects the file — so "
        f"the tool must refuse it the way it refuses bad syntax (#2123).")
