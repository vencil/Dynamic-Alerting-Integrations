"""test_tenant_api_machine_identity_removed.py — ADR-027 §4, §5, §6, §9 item 5.

There is no machine caller (§4) and no relay (§5), so the tenant-api chart no
longer carries the machine-identity audit, the switch for the API's Unix socket,
or the port-8080 allow entries of the two relays (recipe-preview, da-portal).
A values file that still sets one of the removed keys must fail the render
instead of being silently ignored; the default render must carry none of the
removed variables, no TokenReview grant, and a port-8080 allow list of the
Prometheus scrapers only. (tenant-api itself refuses an unknown TA_* at start —
cmd/server/env_check.go — but the chart must not be the one to hand it one.)
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from _helm_gate import helm_required
from _render_baseline import baseline_args

_REPO = Path(__file__).resolve().parents[2]
_CHART = _REPO / "helm" / "tenant-api"
_HAS_HELM = shutil.which("helm") is not None
_REQUIRE_HELM = helm_required()

#: A removed key, the `--set` that sets it, and the name the refusal names.
REMOVED_SETS = (
    ("machineIdentity.enabled=true", "machineIdentity"),
    ("machineIdentity.audience=tenant-api", "machineIdentity"),
    ("humanSocket.enabled=true", "humanSocket"),
    ("humanSocket.path=/run/x/x.sock", "humanSocket"),
    ("networkPolicy.internalPortAllow.recipePreview.app=recipe-preview", "recipePreview"),
    ("networkPolicy.internalPortAllow.daPortal.app=da-portal", "daPortal"),
)

#: The tenant-api variables ADR-027 §6 removed.
REMOVED_ENV = (
    "TA_MACHINE_IDENTITY_AUDIT",
    "TA_MACHINE_IDENTITY_AUDIENCE",
    "TA_MACHINE_IDENTITY_ISSUER",
)


def _helm_or_skip() -> None:
    if not _HAS_HELM:
        if _REQUIRE_HELM:
            pytest.fail("helm is required in this environment but not on PATH")
        pytest.skip("helm CLI not on PATH")


def _render(*set_args: str) -> subprocess.CompletedProcess:
    cmd = ["helm", "template", "t", str(_CHART), *baseline_args(_CHART)]
    for kv in set_args:
        cmd += ["--set", kv]
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=60)


# adr-check: ADR-027/9.5b
def test_values_yaml_carries_no_removed_key() -> None:
    values = yaml.safe_load((_CHART / "values.yaml").read_text(encoding="utf-8"))
    assert "machineIdentity" not in values
    assert "humanSocket" not in values
    allow = values["networkPolicy"]["internalPortAllow"]
    assert sorted(allow) == ["prometheus", "prometheusOperator"], sorted(allow)


def test_the_machine_identity_template_is_gone() -> None:
    assert not (_CHART / "templates" / "rbac-machine-identity.yaml").exists()
    templates = "\n".join(p.read_text(encoding="utf-8")
                          for p in sorted((_CHART / "templates").iterdir()) if p.is_file())
    # Vacuity guard: the templates were read.
    assert "TA_HUMAN_SOCKET" in templates
    for gone in ("machineIdentity", ".Values.humanSocket", "tokenreviews",
                 "internalPortAllow.recipePreview", "internalPortAllow.daPortal"):
        assert gone not in templates, gone


# adr-check: ADR-027/9.5b
@pytest.mark.parametrize("set_arg,name", REMOVED_SETS, ids=[s for s, _ in REMOVED_SETS])
def test_a_removed_key_fails_the_render(set_arg: str, name: str) -> None:
    _helm_or_skip()
    res = _render(set_arg)
    assert res.returncode != 0, f"{set_arg} rendered:\n{res.stdout[:400]}"
    assert name in res.stderr, res.stderr


def test_the_default_render_carries_none_of_it() -> None:
    _helm_or_skip()
    res = _render()
    assert res.returncode == 0, res.stderr
    docs = [d for d in yaml.safe_load_all(res.stdout) if d]
    names: list[str] = []
    for doc in docs:
        if doc.get("kind") != "Deployment":
            continue
        spec = doc["spec"]["template"]["spec"]
        for c in spec.get("initContainers", []) + spec["containers"]:
            names += [e["name"] for e in c.get("env", [])]
    # Vacuity guard: a render with no env at all would pass trivially.
    assert "TA_HUMAN_SOCKET" in names, names
    assert sorted(set(names) & set(REMOVED_ENV)) == []

    kinds = {d.get("kind") for d in docs}
    assert "Deployment" in kinds, kinds
    assert not kinds & {"ClusterRole", "ClusterRoleBinding"}, kinds
    assert "tokenreviews" not in res.stdout

    netpol = next(d for d in docs if d.get("kind") == "NetworkPolicy"
                  and d["metadata"]["name"].endswith("-netpol"))
    # Whole peers: an empty namespaceSelector would admit matching pods from
    # every namespace, so the namespace half is pinned too.
    peers = [f for rule in netpol["spec"]["ingress"]
             if any(p.get("port") == 8080 for p in rule.get("ports", []))
             for f in rule.get("from", [])]
    monitoring = {"matchLabels": {"kubernetes.io/metadata.name": "monitoring"}}
    assert peers == [
        {"namespaceSelector": monitoring, "podSelector": {"matchLabels": {"app": "prometheus"}}},
        {"namespaceSelector": monitoring,
         "podSelector": {"matchLabels": {"app.kubernetes.io/name": "prometheus"}}},
    ], peers
