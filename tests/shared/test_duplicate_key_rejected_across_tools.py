"""A conf.d file with a key written twice is refused by every judging tool,
the way that tool refuses a syntax error (#2123).

The exporter (yaml.v3) rejects such a file; before #2123 every tool below
took PyYAML's last value and exited 0 (measured on the #2123 fixture: 10
tools, all rc 0). The three shapes are the ones the exporter was measured to
reject: one threshold twice under a tenant, one tenant id twice, the
top-level `tenants:` twice — plus a repeated key in `_defaults.yaml` for the
tools that read it.

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
}
_DEFAULTS_VARIANTS = {
    "clean": "",
    "syntax": "defaults:\n  mysql_connections: [80\n",
    "dup_key": "defaults:\n  mysql_connections: 80\n  mysql_connections: 90\n",
}

# (script, argv after it — {d} is the conf.d, {out} a scratch dir), whether
# it reads `_defaults.yaml`, whether the fixture needs a policy.
_TOOLS = {
    "validate_config": ("ops/validate_config.py", ["--config-dir", "{d}"], True, False),
    "check_confd_schema": ("lint/check_confd_schema.py", ["--config-dir", "{d}"], True, False),
    "check_routing_profiles": ("lint/check_routing_profiles.py", ["--config-dir", "{d}"], True, False),
    "generate_routes_validate": ("ops/generate_alertmanager_routes.py",
                                 ["--config-dir", "{d}", "--validate"], True, False),
    "compile_custom_alerts": ("dx/compile_custom_alerts.py",
                              ["--config-dir", "{d}", "--out", "{out}/pack.yaml"], True, False),
    "policy_engine": ("ops/policy_engine.py", ["--config-dir", "{d}", "--ci"], True, True),
    "gitops_check_local": ("ops/gitops_check.py", ["local", "--dir", "{d}"], True, False),
    "describe_tenant": ("dx/describe_tenant.py", ["tenant-a", "--conf-d", "{d}"], True, False),
    "tenant_verify": ("dx/tenant_verify.py", ["tenant-a", "--conf-d", "{d}"], True, False),
    "assemble_config_dir_validate": ("ops/assemble_config_dir.py",
                                     ["--sources", "{d}", "--output", "{out}/asm", "--validate"],
                                     True, False),
    # --dry-run: stops at the OPA input document, no OPA needed.
    "policy_opa_bridge": ("ops/policy_opa_bridge.py", ["--config-dir", "{d}", "--dry-run"],
                          True, False),
    "offboard_tenant_precheck": ("ops/offboard_tenant.py", ["tenant-a", "--config-dir", "{d}"],
                                 True, False),
}

# A tool whose (rc, file named) is the same for a clean tree and a syntax
# error — its pre-check names every file and exits 0 either way — is told
# apart by the marker its "cannot read this file" line prints.
_UNREADABLE_MARKER = {
    "offboard_tenant_precheck": "無法讀取",
}


@functools.lru_cache(maxsize=None)
def _outcome(tool: str, target: str, variant: str) -> tuple[int, bool, bool]:
    """(exit code, names the broken file?, prints its unreadable marker?)."""
    script, argv, _reads_defaults, policy = _TOOLS[tool]
    defaults = _DEFAULTS + (_POLICY if policy else "")
    tenant = _TENANT
    if target == "tenant":
        tenant = _TENANT_VARIANTS[variant]
    elif variant != "clean":
        defaults = _DEFAULTS_VARIANTS[variant] + (_POLICY if policy else "")
    with tempfile.TemporaryDirectory() as tmp:
        d = os.path.join(tmp, "conf.d")
        os.makedirs(d)
        pathlib.Path(d, "_defaults.yaml").write_text(defaults, encoding="utf-8")
        pathlib.Path(d, "tenant-a.yaml").write_text(tenant, encoding="utf-8")
        r = subprocess.run(
            [sys.executable, str(TOOLS / script)]
            + [a.format(d=d, out=tmp) for a in argv],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=tmp, timeout=180)
    out = r.stdout + r.stderr
    named = ("tenant-a.yaml" if target == "tenant" else "_defaults.yaml") in out
    marker = _UNREADABLE_MARKER.get(tool)
    return r.returncode, named, bool(marker) and marker in out


_CASES = (
    [(t, "tenant", v) for t in _TOOLS for v in ("dup_key", "dup_tenant_id", "dup_tenants_block")]
    + [(t, "defaults", "dup_key") for t, spec in _TOOLS.items() if spec[2]]
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
