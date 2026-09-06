#!/usr/bin/env python3
"""check_retire_drift — the conf.d side of the RETIRE-ordering gate (#869).

#1603 (extension-SPELLING axis): the gate enumerated `rglob("*.yaml")`, so
a tenant whose carrier is `alpha.yml` or `Alpha.YAML` was not in `declared`
at all. Measured before the fix with byte-identical bodies: `alpha.yaml` →
{alpha: postgres}; `alpha.yml` / `Alpha.YAML` → {} — the same answer as a
tree with no tenant. The consequence is the gate's own failure mode: the
exporter (which reads both spellings) keeps emitting
`tenant_expected_exporter{tenant="alpha"}=1` after the K8s target is
removed, TenantExporterAbsent fires a false-positive critical, and this
gate said rc=0.

Scope: only the spelling axis. Hidden (`.`-prefixed) entries are still
READ by this gate, as before (#1630 is a separate measurement).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(_ROOT, "scripts", "tools", "lint"))

import check_retire_drift as crd  # noqa: E402

_BODY = ("tenants:\n  alpha:\n    pg_connections: 90\n"
         "    _metadata:\n      db_type: postgres\n")


def _tree(root: Path, carrier: str | None) -> Path:
    """One conf.d: reserved defaults + an examples/ template + optional tenant.

    The reserved file and the examples/ subtree each carry a `tenants:` block
    on purpose — that is the only shape in which "still skipped" is
    OBSERVABLE (a carrier without one contributes no tenant either way).
    """
    root.mkdir(parents=True)
    (root / "_defaults.yaml").write_text(
        _BODY.replace("alpha", "ghost_reserved"), encoding="utf-8")
    (root / "examples").mkdir()
    (root / "examples" / "ex.yaml").write_text(
        _BODY.replace("alpha", "ghost_example"), encoding="utf-8")
    if carrier:
        (root / carrier).write_text(_BODY, encoding="utf-8")
    return root


@pytest.mark.parametrize("carrier", ["alpha.yml", "Alpha.YAML"])
def test_declared_tenants_see_yml_and_upper_case_the_same_as_yaml(
        tmp_path: Path, carrier: str) -> None:
    """A `.yml` / `.YAML` tenant must be declared exactly like a `.yaml` one.

    The `notenants` arm is the sensitivity control: it must differ from the
    `yaml` arm, otherwise "same answer" is vacuous.
    """
    control = crd.conf_d_declared_db_type_tenants(_tree(tmp_path / "none", None))
    ref = crd.conf_d_declared_db_type_tenants(_tree(tmp_path / "yaml", "alpha.yaml"))
    assert ref == {"alpha": "postgres"}, ref
    assert control != ref, "fixture is vacuous — removing the tenant changed nothing"
    assert crd.conf_d_declared_db_type_tenants(
        _tree(tmp_path / "other", carrier)) == ref


def test_reserved_and_examples_skips_survive_the_wider_spelling(
        tmp_path: Path) -> None:
    """Widening the extension must not widen WHAT is skipped.

    `_defaults.yml` and `examples/ex.yml` carry tenants; neither may be
    declared, the same way their `.yaml` twins are not.
    """
    root = _tree(tmp_path / "yml", "alpha.yml")
    (root / "_defaults.yml").write_text(
        _BODY.replace("alpha", "ghost_reserved_yml"), encoding="utf-8")
    (root / "examples" / "ex.yml").write_text(
        _BODY.replace("alpha", "ghost_example_yml"), encoding="utf-8")
    assert crd.conf_d_declared_db_type_tenants(root) == {"alpha": "postgres"}


def test_yml_tenant_without_a_k8s_target_is_a_violation(tmp_path: Path) -> None:
    """The operator-facing consequence: a `.yml` tenant stranded without a
    K8s target must now be reported, not pass with rc=0.
    """
    root = _tree(tmp_path / "conf.d", "alpha.yml")
    helm = tmp_path / "helm"
    helm.mkdir()
    result = crd.evaluate(root, helm, tmp_path / "no-namespaces.yaml")
    assert result["declared"] == {"alpha": "postgres"}
    assert len(result["violations"]) == 1, result["violations"]
    assert "'alpha'" in result["violations"][0]
