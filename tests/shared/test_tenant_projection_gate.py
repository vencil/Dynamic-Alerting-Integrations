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
import json
import sys
from pathlib import Path

import pytest

_GATE = Path(__file__).parent.parent.parent / "helm" / "vector" / "projection-gate" / "verify_tenant_projections.py"


def _load_gate():
    # The image puts the script and its _lib_yaml_strict.py side by side.
    if str(_GATE.parent) not in sys.path:
        sys.path.insert(0, str(_GATE.parent))
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
    # The leak DESTINATION named in the message must be the PROJECTED partition (1001,
    # where Vector actually routes), not the registry's allocated 1000 — else the
    # message points on-call at the wrong partition (CodeRabbit #950).
    assert any("partition 1001" in s for s in v.violations), v.violations
    assert any("1000" in s for s in v.violations), v.violations  # allocated value, for comparison


def test_unknown_tenant_is_caught():
    v = gate.evaluate(_reg({"tenant-alpha": 1000}), [{"tenantId": "ghost", "accountId": 1000}])
    assert v.category == gate.CAT_MISMATCH


def test_missing_fields_caught():
    v = gate.evaluate(_reg({"tenant-alpha": 1000}), [{"tenantId": "tenant-alpha"}])
    assert v.category == gate.CAT_MISMATCH


def test_non_mapping_projection_caught():
    v = gate.evaluate(_reg({"tenant-alpha": 1000}), ["tenant-alpha"])
    assert v.category == gate.CAT_MISMATCH


def test_malformed_allocations_registry_unreadable():
    v = gate.evaluate({"schema_version": "v1", "allocations": ["not", "a", "map"]}, [])
    assert v.category == gate.CAT_REGISTRY_UNREADABLE


@pytest.mark.parametrize("bad_value", ["1000", True, 1000.5, None])
def test_malformed_registry_VALUE_is_registry_unreadable(bad_value):
    # An entry of another shape than tenant → int is no decoding tenant-api
    # prints → registry_unreadable (degrade), NOT a mismatch that would
    # hard-fail the pod in enforce mode on an infra problem. (CodeRabbit #950 —
    # comparing int accountId vs string "1000" mis-fired as mismatch.) bool is
    # excluded explicitly because isinstance(True, int) is True. The id's RANGE
    # is tenant-api's to judge (#4164), not the gate's.
    v = gate.evaluate(_reg({"tenant-alpha": bad_value}), [{"tenantId": "tenant-alpha", "accountId": 1000}])
    assert v.category == gate.CAT_REGISTRY_UNREADABLE, f"value {bad_value!r} should be registry_unreadable"


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
# These exercise the full init-container glue: what the registry-reader
# init-container leaves (`tenant-api state show`'s JSON, rc, stderr; #4164) ->
# evaluate -> populate the config-dir.

_RECORD = {"schema_version": "v1", "next_account_id": 1002,
           "allocations": {"tenant-alpha": 1000, "tenant-beta": 1001}}


def _state(d: Path, record=_RECORD, problem=None, rc=0, err="", schema=None, key=None):
    """Write what `tenant-api state show --from /conf.d` leaves in the
    registry-state emptyDir (components/tenant-api/cmd/server/state_cmd.go)."""
    d.mkdir(parents=True, exist_ok=True)
    doc = {"schema": schema or gate.STATE_SHOW_SCHEMA, "tenant_api_version": "test",
           "records": [{"key": key or gate.REGISTRY_KEY,
                        "record": None if problem else record, "problem": problem}]}
    (d / gate.STATE_JSON).write_text(json.dumps(doc), encoding="utf-8")
    (d / gate.STATE_RC).write_text(f"{rc}\n", encoding="utf-8")
    (d / gate.STATE_ERR).write_text(err, encoding="utf-8")
    return d


def _setup(tmp_path: Path, projections):
    state = _state(tmp_path / "registry-state")
    proj = tmp_path / "projections.json"
    proj.write_text(json.dumps(projections), encoding="utf-8")
    base = tmp_path / "00-base.staged.yaml"
    base.write_text("sinks: {}\n", encoding="utf-8")  # placeholder; the gate only COPIES it
    frag = tmp_path / "30-tenant.staged.yaml"
    frag.write_text("transforms: {}\n", encoding="utf-8")
    return {
        "state": state, "proj": proj, "base": base, "frag": frag,
        "cdir": tmp_path / "config-dir", "metrics": tmp_path / "gate.prom",
    }


def _argv(f: dict, mode: str = "degrade") -> list[str]:
    return [
        "--registry-state", str(f["state"]), "--projections", str(f["proj"]),
        "--base-config", str(f["base"]), "--fragment-config", str(f["frag"]),
        "--config-dir", str(f["cdir"]), "--metrics-file", str(f["metrics"]), "--mode", mode,
    ]


_ALPHA_OK = [{"tenantId": "tenant-alpha", "accountId": 1000}]
_ALPHA_WRONG = [{"tenantId": "tenant-alpha", "accountId": 1001}]


def test_main_pass_places_base_and_fragment(tmp_path):
    f = _setup(tmp_path, _ALPHA_OK + [{"tenantId": "tenant-beta", "accountId": 1001}])
    rc = gate.main(_argv(f))
    assert rc == 0
    assert (f["cdir"] / gate._BASE_NAME).is_file()
    assert (f["cdir"] / gate._FRAGMENT_NAME).is_file(), "full mode must place the tenant fragment"
    assert 'category="ok"' in f["metrics"].read_text(encoding="utf-8")


def test_main_mismatch_degrade_drops_fragment(tmp_path):
    f = _setup(tmp_path, _ALPHA_WRONG)  # unique-but-wrong
    rc = gate.main(_argv(f, mode="degrade"))
    assert rc == 0, "degrade must keep Vector up"
    assert (f["cdir"] / gate._BASE_NAME).is_file()
    assert not (f["cdir"] / gate._FRAGMENT_NAME).exists(), "degrade must drop the tenant fragment → 0:0-only"
    assert 'category="mismatch"' in f["metrics"].read_text(encoding="utf-8")


def test_main_mismatch_enforce_fails_pod(tmp_path):
    f = _setup(tmp_path, _ALPHA_WRONG)
    rc = gate.main(_argv(f, mode="enforce"))
    assert rc == 1, "enforce must fail the init-container so the pod will not start"


def _unreadable(f):
    rc = gate.main(_argv(f, mode="enforce"))
    assert rc == 0, "an infra hiccup must NOT self-DoS, even in enforce mode"
    assert (f["cdir"] / gate._BASE_NAME).is_file()
    assert not (f["cdir"] / gate._FRAGMENT_NAME).exists()
    return f["metrics"].read_text(encoding="utf-8")


def test_main_registry_unreadable_degrades_even_in_enforce(tmp_path):
    f = _setup(tmp_path, _ALPHA_OK)
    (f["state"] / gate.STATE_RC).unlink()  # the reader never ran
    assert 'category="registry_unreadable"' in _unreadable(f)


@pytest.mark.parametrize("state", [
    # tenant-api refuses the registry: its verdict is the gate's (#4164 —
    # the Python gate passed all three of these)
    {"problem": {"kind": "rejected", "message": 'account registry corrupt: tenants "tenant-alpha" '
                                                'and "tenant-beta" both hold id 1000 (cross-tenant log merge)'}},
    {"problem": {"kind": "rejected", "message": "account registry corrupt: tenant \"tenant-alpha\" "
                                                "holds id 1000 at/above next_account_id 1000"}},
    {"problem": {"kind": "unreadable", "message": "parse account registry: cannot unmarshal"}},
    # the reader failed: an absent registry ConfigMap, an unknown TA_* env…
    {"rc": 1, "err": "state show: /conf.d holds no records: not a tenant-api state backup"},
    {"rc": 2, "err": "unknown environment variable TA_FOO"},
    # a document this gate does not know is never trusted
    {"schema": "tenant-api.state-show/v2"},
    {"key": "_groups.yaml"},
])
def test_what_tenant_api_will_not_vouch_for_degrades(tmp_path, state):
    f = _setup(tmp_path, _ALPHA_OK)
    _state(f["state"], **state)
    assert 'category="registry_unreadable"' in _unreadable(f)


def test_the_registry_record_is_read_as_tenant_api_decoded_it(tmp_path):
    f = _setup(tmp_path, _ALPHA_OK)
    _state(f["state"], record={"schema_version": "v1", "next_account_id": 1001,
                               "allocations": {"tenant-alpha": 1001}})
    assert gate.main(_argv(f)) == 0
    assert 'category="mismatch"' in f["metrics"].read_text(encoding="utf-8")


def test_blank_projections_are_none(tmp_path):
    f = _setup(tmp_path, [])
    f["proj"].write_text("", encoding="utf-8")
    assert gate.main(_argv(f)) == 0
    assert 'category="ok"' in f["metrics"].read_text(encoding="utf-8")


def test_projections_that_are_no_json_list_are_a_mismatch(tmp_path):
    f = _setup(tmp_path, [])
    for text in ('{"tenantId": "tenant-alpha"}', "- {tenantId: tenant-alpha, accountId: 1000}\n"):
        f["proj"].write_text(text, encoding="utf-8")
        assert gate.main(_argv(f)) == 0
        assert 'category="mismatch"' in f["metrics"].read_text(encoding="utf-8")


def test_the_gate_image_carries_no_yaml_reader():
    """#4164: the registry is tenant-api's to read and the projections are
    JSON, so the image needs no PyYAML and no copy of the repo's YAML reader."""
    dockerfile = (_GATE.parent / "Dockerfile").read_text(encoding="utf-8")
    assert "py3-yaml" not in dockerfile and "_lib_yaml_strict" not in dockerfile
    assert not (_GATE.parent / "_lib_yaml_strict.py").exists()
    src = _GATE.read_text(encoding="utf-8")
    assert "import yaml" not in src and "_lib_yaml_strict" not in src
