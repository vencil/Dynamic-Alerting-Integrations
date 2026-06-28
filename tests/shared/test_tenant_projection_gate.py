"""test_tenant_projection_gate.py — security core of the #908 fail-closed
tenantProjections-vs-registry gate.

Tests the PURE ``evaluate()`` of ``helm/vector/files/verify_tenant_projections.py``
with synthetic registry + projection fixtures. The plane-split (registry lives in a
separate conf.d git repo, so CI has no real registry) is exactly why the security
logic must be a pure function validated here in isolation — RFC §8.4 / L6'③: test
the OUTPUT invariant, and follow the multi-tenant audit-log war-story lesson —
assert the accountId is CORRECT, not merely PRESENT.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_GATE = Path(__file__).parent.parent.parent / "helm" / "vector" / "projection-gate" / "verify_tenant_projections.py"


def _load_gate():
    spec = importlib.util.spec_from_file_location("verify_tenant_projections", _GATE)
    assert spec and spec.loader, f"cannot load gate module at {_GATE}"
    mod = importlib.util.module_from_spec(spec)
    # Register before exec: @dataclass introspects sys.modules[cls.__module__], which
    # is None for an unregistered importlib-loaded module → AttributeError at import.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


gate = _load_gate()


def _reg(allocations: dict, schema: str = "v1") -> dict:
    return {"schema_version": schema, "next_account_id": 9999, "allocations": allocations}


def test_gate_module_present():
    """Non-gated guard: the chart asset is where the init-container expects it."""
    assert _GATE.is_file(), f"gate script missing at {_GATE}"
    assert _GATE.stat().st_size > 0, "gate script is empty"


def test_matching_projection_passes():
    v = gate.evaluate(
        _reg({"tenant-alpha": 1000, "tenant-beta": 1001}),
        [{"tenantId": "tenant-alpha", "accountId": 1000}, {"tenantId": "tenant-beta", "accountId": 1001}],
    )
    assert v.ok and v.category == gate.CAT_OK and v.violations == []


def test_unique_but_wrong_accountid_is_caught():
    # THE headline leak this gate exists for: 1001 is unique, an int, >=1000 — so it
    # passes the render-time {{fail}} uniqueness guard AND values.schema.json — but
    # the registry allocates 1000, so tenant-alpha's logs would land in tenant 1001's
    # partition. The existing guards are blind to it; this gate is not.
    v = gate.evaluate(
        _reg({"tenant-alpha": 1000, "tenant-beta": 1001}),
        [{"tenantId": "tenant-alpha", "accountId": 1001}],
    )
    assert not v.ok and v.category == gate.CAT_MISMATCH
    assert any("1001" in s and "1000" in s for s in v.violations), v.violations


def test_unknown_tenant_is_caught():
    v = gate.evaluate(_reg({"tenant-alpha": 1000}), [{"tenantId": "ghost", "accountId": 1000}])
    assert v.category == gate.CAT_MISMATCH


def test_missing_fields_caught():
    v = gate.evaluate(_reg({"tenant-alpha": 1000}), [{"tenantId": "tenant-alpha"}])
    assert v.category == gate.CAT_MISMATCH


def test_non_mapping_projection_caught():
    v = gate.evaluate(_reg({"tenant-alpha": 1000}), ["tenant-alpha"])
    assert v.category == gate.CAT_MISMATCH


def test_unknown_schema_is_registry_unreadable():
    # Fail-closed on a newer/unknown registry rather than validating a shape we don't
    # understand (mirrors Go account.Parse).
    v = gate.evaluate(_reg({}, schema="v2"), [])
    assert v.category == gate.CAT_REGISTRY_UNREADABLE


def test_malformed_allocations_registry_unreadable():
    v = gate.evaluate({"schema_version": "v1", "allocations": ["not", "a", "map"]}, [])
    assert v.category == gate.CAT_REGISTRY_UNREADABLE


def test_empty_projections_pass():
    v = gate.evaluate(_reg({"tenant-alpha": 1000}), [])
    assert v.ok


def test_blank_registry_no_projections_ok():
    # Brand-new cluster: no allocations + no projections = the all-0:0 baseline.
    v = gate.evaluate(_reg({}), [])
    assert v.ok


def test_blank_registry_with_projection_is_caught():
    # A projection exists but the registry has allocated nothing → unknown tenant,
    # NOT a silent pass.
    v = gate.evaluate(_reg({}), [{"tenantId": "tenant-alpha", "accountId": 1000}])
    assert v.category == gate.CAT_MISMATCH


# ── main() integration: the config-dir fragment place/omit behaviour (Approach C) ──
# These exercise the full init-container glue (load files -> evaluate -> populate the
# config-dir). PyYAML 6+ is a standard dep and is installed in the CI Python Tests
# job, so these run there for real (not silently skipped).

_REGISTRY = "schema_version: v1\nnext_account_id: 1002\nallocations:\n  tenant-alpha: 1000\n  tenant-beta: 1001\n"


def _setup(tmp_path: Path, projections_yaml: str):
    reg = tmp_path / "registry.yaml"
    reg.write_text(_REGISTRY, encoding="utf-8")
    proj = tmp_path / "projections.yaml"
    proj.write_text(projections_yaml, encoding="utf-8")
    base = tmp_path / "00-base.staged.yaml"
    base.write_text("sinks: {}\n", encoding="utf-8")  # placeholder; the gate only COPIES it
    frag = tmp_path / "30-tenant.staged.yaml"
    frag.write_text("transforms: {}\n", encoding="utf-8")
    return {
        "reg": reg, "proj": proj, "base": base, "frag": frag,
        "cdir": tmp_path / "config-dir", "metrics": tmp_path / "gate.prom",
    }


def _argv(f: dict, mode: str = "degrade") -> list[str]:
    return [
        "--registry", str(f["reg"]), "--projections", str(f["proj"]),
        "--base-config", str(f["base"]), "--fragment-config", str(f["frag"]),
        "--config-dir", str(f["cdir"]), "--metrics-file", str(f["metrics"]), "--mode", mode,
    ]


def test_main_pass_places_base_and_fragment(tmp_path):
    f = _setup(tmp_path, "- {tenantId: tenant-alpha, accountId: 1000}\n- {tenantId: tenant-beta, accountId: 1001}\n")
    rc = gate.main(_argv(f))
    assert rc == 0
    assert (f["cdir"] / gate._BASE_NAME).is_file()
    assert (f["cdir"] / gate._FRAGMENT_NAME).is_file(), "full mode must place the tenant fragment"
    assert 'category="ok"' in f["metrics"].read_text(encoding="utf-8")


def test_main_mismatch_degrade_drops_fragment(tmp_path):
    f = _setup(tmp_path, "- {tenantId: tenant-alpha, accountId: 1001}\n")  # unique-but-wrong
    rc = gate.main(_argv(f, mode="degrade"))
    assert rc == 0, "degrade must keep Vector up"
    assert (f["cdir"] / gate._BASE_NAME).is_file()
    assert not (f["cdir"] / gate._FRAGMENT_NAME).exists(), "degrade must drop the tenant fragment → 0:0-only"
    assert 'category="mismatch"' in f["metrics"].read_text(encoding="utf-8")


def test_main_mismatch_enforce_fails_pod(tmp_path):
    f = _setup(tmp_path, "- {tenantId: tenant-alpha, accountId: 1001}\n")
    rc = gate.main(_argv(f, mode="enforce"))
    assert rc == 1, "enforce must fail the init-container so the pod will not start"


def test_main_registry_unreadable_degrades_even_in_enforce(tmp_path):
    f = _setup(tmp_path, "- {tenantId: tenant-alpha, accountId: 1000}\n")
    f["reg"].unlink()  # registry missing → infra path, not a config bug
    rc = gate.main(_argv(f, mode="enforce"))
    assert rc == 0, "an infra hiccup must NOT self-DoS, even in enforce mode"
    assert (f["cdir"] / gate._BASE_NAME).is_file()
    assert not (f["cdir"] / gate._FRAGMENT_NAME).exists()
    assert 'category="registry_unreadable"' in f["metrics"].read_text(encoding="utf-8")
