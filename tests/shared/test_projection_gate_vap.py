"""test_projection_gate_vap.py — the #908 PR-3 anti-silent-disarm
ValidatingAdmissionPolicy (k8s/03-monitoring/validatingadmissionpolicy-projection-gate.yaml).

Two tiers, both offline (there is no cluster in the dev loop):

  1. STRUCTURAL — the manifest is a well-formed VAP + Binding with the safety
     invariants that make it shippable blind: the binding is Warn+Audit (never
     Deny — verified against the K8s reference to never block a request), it
     matches apps/v1 DaemonSets on CREATE+UPDATE, scopes to Vector via
     objectSelector, and the CEL keys on the real gate artifacts (the `registry`
     volume + the `projection-gate` init-container) with has()-guards.

  2. LOGIC-MODEL — a Python mirror of the two CEL booleans (matchCondition +
     validation), driven by the container/volume NAMES extracted from the actual
     manifest so it can't silently drift, evaluated over the 4 DaemonSet shapes.
     Proves the INTENDED admission logic: a gated DaemonSet with the init-container
     passes, one that dropped the init-container (disarm) is caught, and an
     ungated (single-tenant) DaemonSet is skipped (no false warning).

The AUTHORITATIVE enforcement check (does the API server actually warn/allow) is a
real-cluster / kubectl-dry-run step done at deploy time — see the runbook. This
file pins the shape + the intended logic so a future edit can't regress them.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

_VAP = (Path(__file__).parent.parent.parent
        / "k8s" / "03-monitoring" / "validatingadmissionpolicy-projection-gate.yaml")


@pytest.fixture(scope="module")
def docs() -> list[dict]:
    return [d for d in yaml.safe_load_all(_VAP.read_text(encoding="utf-8")) if d]


@pytest.fixture(scope="module")
def policy(docs) -> dict:
    return [d for d in docs if d["kind"] == "ValidatingAdmissionPolicy"][0]


@pytest.fixture(scope="module")
def binding(docs) -> dict:
    return [d for d in docs if d["kind"] == "ValidatingAdmissionPolicyBinding"][0]


# ── Structural invariants ────────────────────────────────────────────────────

def test_manifest_present_and_two_gv_documents(docs):
    assert _VAP.is_file(), f"VAP manifest missing at {_VAP}"
    kinds = {d["kind"] for d in docs}
    assert kinds == {"ValidatingAdmissionPolicy", "ValidatingAdmissionPolicyBinding"}
    for d in docs:
        assert d["apiVersion"] == "admissionregistration.k8s.io/v1", "GA API (1.30+), not v1beta1"


def test_binding_is_warn_audit_never_deny(binding):
    """The load-bearing safety invariant: Warn+Audit never blocks a request, so a
    wrong CEL cannot DoS Vector deploys cluster-wide. Deny must NOT be present
    (and Deny+Warn are mutually exclusive per the API)."""
    actions = binding["spec"]["validationActions"]
    assert set(actions) == {"Warn", "Audit"}, f"must be Warn+Audit, got {actions}"
    assert "Deny" not in actions, "Day-1 blast-radius: no Deny until real-cluster validation"


def test_binding_binds_policy_and_scopes_to_vector(binding, policy):
    assert binding["spec"]["policyName"] == policy["metadata"]["name"], "binding must bind the policy"
    sel = binding["spec"]["matchResources"]["objectSelector"]["matchLabels"]
    # Scope to the Vector log-shipper (always-present chart labels), so the policy
    # never evaluates unrelated DaemonSets (kube-proxy, CNI, ...).
    assert sel["app.kubernetes.io/name"] == "vector"
    assert sel["app.kubernetes.io/component"] == "log-shipper"


def test_policy_matches_daemonsets_create_update(policy):
    rule = policy["spec"]["matchConstraints"]["resourceRules"][0]
    assert rule["apiGroups"] == ["apps"] and rule["apiVersions"] == ["v1"]
    assert rule["resources"] == ["daemonsets"]
    assert set(rule["operations"]) == {"CREATE", "UPDATE"}, "catch apply + edit, not delete"


def test_cel_keys_on_real_gate_artifacts_with_has_guards(policy):
    mc = policy["spec"]["matchConditions"][0]["expression"]
    val = policy["spec"]["validations"][0]["expression"]
    # matchCondition = the gate's registry volume (tenantProjections active), guarded.
    assert "has(object.spec.template.spec.volumes)" in mc
    assert "'registry'" in mc
    # validation = the gate init-container present, guarded (absent → false → warn).
    assert "has(object.spec.template.spec.initContainers)" in val
    assert "'projection-gate'" in val


# ── CEL full-text pin — the security-load-bearing expressions, verbatim ───────

def _norm(cel: str) -> str:
    return re.sub(r"\s+", " ", cel).strip()


def test_cel_expressions_pinned_verbatim(policy):
    """PIN the two CEL expressions exactly (whitespace-normalized). The name
    extraction + logic-model below prove the INTENT but cannot catch a boolean
    RESTRUCTURING (e.g. `!has(...) || exists(...)` — which would flip a
    no-initContainers DaemonSet from warned to silently-passed, fail-open) —
    the name substrings and `has(`/`exists(` markers all still match. These are
    security-load-bearing expressions: ANY change must consciously update this
    pin AND re-derive the logic-model expectations (independent-review finding)."""
    assert _norm(policy["spec"]["matchConditions"][0]["expression"]) == (
        "has(object.spec.template.spec.volumes) && "
        "object.spec.template.spec.volumes.exists(v, v.name == 'registry')"
    )
    assert _norm(policy["spec"]["validations"][0]["expression"]) == (
        "has(object.spec.template.spec.initContainers) && "
        "object.spec.template.spec.initContainers.exists(c, c.name == 'projection-gate')"
    )


# ── Logic-model: mirror the two CEL booleans over the 4 DaemonSet shapes ──────

def _extract_quoted_name(cel: str, field: str) -> str:
    """Pull the single-quoted identifier the CEL compares `<field>.name` against,
    so the model uses the SAME names the manifest does. NB this pins NAMES only;
    the boolean STRUCTURE is pinned by test_cel_expressions_pinned_verbatim —
    the model alone cannot detect a restructured expression."""
    m = re.search(rf"{field}\.exists\(\w+,\s*\w+\.name\s*==\s*'([^']+)'\)", cel)
    assert m, f"could not extract the {field} name from CEL: {cel}"
    return m.group(1)


def _daemonset(*, registry_volume: bool, gate_init: bool, vol_name: str, init_name: str) -> dict:
    spec: dict = {"template": {"spec": {}}}
    pod = spec["template"]["spec"]
    if registry_volume:
        pod["volumes"] = [{"name": "config"}, {"name": vol_name}, {"name": "staging"}]
    if gate_init:
        pod["initContainers"] = [{"name": init_name}]
    return {"spec": spec}


def _match_condition(ds: dict, vol_name: str) -> bool:
    """Mirror: has(volumes) && volumes.exists(v, v.name == '<vol_name>')."""
    vols = ds["spec"]["template"]["spec"].get("volumes")
    return vols is not None and any(v["name"] == vol_name for v in vols)


def _validation_passes(ds: dict, init_name: str) -> bool:
    """Mirror: has(initContainers) && initContainers.exists(c, c.name == '<init_name>')."""
    inits = ds["spec"]["template"]["spec"].get("initContainers")
    return inits is not None and any(c["name"] == init_name for c in inits)


def test_admission_logic_over_four_shapes(policy):
    mc_cel = policy["spec"]["matchConditions"][0]["expression"]
    val_cel = policy["spec"]["validations"][0]["expression"]
    vol_name = _extract_quoted_name(mc_cel, "volumes")           # 'registry'
    init_name = _extract_quoted_name(val_cel, "initContainers")  # 'projection-gate'

    def outcome(registry_volume: bool, gate_init: bool) -> str:
        ds = _daemonset(registry_volume=registry_volume, gate_init=gate_init,
                        vol_name=vol_name, init_name=init_name)
        if not _match_condition(ds, vol_name):
            return "skipped"          # policy not evaluated → no warning
        return "ok" if _validation_passes(ds, init_name) else "warned"

    # Gated DaemonSet WITH the gate init-container → healthy, no warning.
    assert outcome(registry_volume=True, gate_init=True) == "ok"
    # Gated DaemonSet that DROPPED the init-container → the disarm case → WARNED.
    assert outcome(registry_volume=True, gate_init=False) == "warned"
    # Ungated (single-tenant) DaemonSet → skipped, never a false warning.
    assert outcome(registry_volume=False, gate_init=False) == "skipped"
    assert outcome(registry_volume=False, gate_init=True) == "skipped"


def test_vap_names_stay_coupled_to_the_chart_daemonset(policy):
    """DRIFT-SEAM: the VAP keys on the volume name 'registry' + init-container name
    'projection-gate' — both OWNED by helm/vector/templates/daemonset.yaml. If the
    chart renames either, the VAP would silently stop matching the real DaemonSet
    (fail-open, undetected). Pin the coupling to the literal `name:` in the template
    (both are literals there, not templated), so a rename breaks this test and forces
    a VAP update. Names are read from the manifest so the seam tracks the CEL."""
    ds_tpl = (Path(__file__).parent.parent.parent
              / "helm" / "vector" / "templates" / "daemonset.yaml").read_text(encoding="utf-8")
    vol_name = _extract_quoted_name(policy["spec"]["matchConditions"][0]["expression"], "volumes")
    init_name = _extract_quoted_name(policy["spec"]["validations"][0]["expression"], "initContainers")
    # newline-terminated so 'projection-gate' does not spuriously match the
    # 'projection-gate-metrics' sidecar container.
    assert f"- name: {vol_name}\n" in ds_tpl, (
        f"VAP keys on volume '{vol_name}' but the daemonset template no longer defines it — update the VAP")
    assert f"- name: {init_name}\n" in ds_tpl, (
        f"VAP keys on init-container '{init_name}' but the daemonset template no longer defines it — update the VAP")
