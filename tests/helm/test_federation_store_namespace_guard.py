"""test_federation_store_namespace_guard.py — #1313 store/consumer namespace drift

The federation token store is a ConfigMap that **three** workloads have to agree
on the location of: tenant-api writes it through the API, while
federation-gateway and federation-reconciler each mount it as a projected
VOLUME. A volume can only ever resolve a ConfigMap in the mounting pod's OWN
namespace — mounting is not an API read that can cross namespaces.

Under the documented install paths those namespaces did NOT agree
(`helm install tenant-api … -n tenant-api` in
docs/getting-started/for-platform-engineers.md vs "install the chart in the
`monitoring` namespace" in helm/federation-gateway/README.md), and the failure
was completely silent: the gateway's volume is `optional: true`, so its pod
started Ready with **no** revoked.txt, the Lua filter loaded an EMPTY revoked
set, and every revoked token was honoured until its TTL. Kubernetes emits no
event for a missing optional ConfigMap (verified on a live cluster), and the
Lua's missing-file branch emits no log either (#1236), so nothing anywhere said
so.

`federation.store.namespace` fixes it by letting the store live where its
consumers are. That value now feeds FOUR places, and this file exists because
every one of them is a chance to re-derive it independently:

  * the ConfigMap's own ``metadata.namespace``
  * the Role (a Role only grants access to objects in ITS namespace, so it has
    to follow the ConfigMap)
  * the RoleBinding
  * the ``--federation-namespace`` flag the binary reads

The assertions below are deliberately **equality between the rendered places**
rather than each place against a literal. A literal-per-place test passes when
three of them drift together, which is exactly the shape of the bug: every
individual file looked locally correct, and only the *relationship* between
them was wrong.

The ServiceAccount subject is asserted separately and in the OPPOSITE
direction: it must stay in the release namespace. It is the one thing that must
NOT follow the store — the pod still runs where it always did, and a subject
that "helpfully" moved with the Role would grant nothing at all.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CHART = _REPO_ROOT / "helm" / "tenant-api"

_RELEASE_NS = "tenant-api"
_CONSUMER_NS = "monitoring"

_HAS_HELM = shutil.which("helm") is not None
_needs_helm = pytest.mark.skipif(not _HAS_HELM, reason="helm CLI not on PATH")
# Set to "1" by the CI job that installs helm (ci.yml python-tests-run ``env:``).
# When set, a missing binary is a FAILURE, not a skip — a broken setup-helm step
# would otherwise turn every assertion here into a green no-op (#1300 precedent).
_REQUIRE_HELM = os.environ.get("VIBE_REQUIRE_HELM") == "1"


def test_helm_present_when_required() -> None:
    """Fail-closed guard against silent disarmament."""
    if _REQUIRE_HELM:
        assert _HAS_HELM, (
            "VIBE_REQUIRE_HELM=1 but no `helm` on PATH — the render assertions in "
            "this file would have skipped silently"
        )


def _render(*, store_namespace: str | None) -> list[dict]:
    cmd = [
        "helm", "template", "ta", str(_CHART), "-n", _RELEASE_NS,
        "--set", "federation.enabled=true",
    ]
    if store_namespace is not None:
        cmd += ["--set", f"federation.store.namespace={store_namespace}"]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=60)
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def _store_places(docs: list[dict]) -> dict[str, str]:
    """The four namespaces that must agree, keyed by where they were read from."""
    places: dict[str, str] = {}
    for d in docs:
        kind, meta = d.get("kind"), d["metadata"]
        name = meta.get("name", "")
        if kind == "ConfigMap" and name == "tenant-federation-store":
            places["configmap"] = meta["namespace"]
        elif kind in ("Role", "RoleBinding") and name.endswith("-federation"):
            places[kind.lower()] = meta["namespace"]
        elif kind == "Deployment":
            for c in d["spec"]["template"]["spec"]["containers"]:
                for arg in c.get("args", []):
                    if arg.startswith("--federation-namespace="):
                        places["flag"] = arg.split("=", 1)[1]
    return places


def _subject_namespaces(docs: list[dict]) -> list[str]:
    return [
        s["namespace"]
        for d in docs
        if d.get("kind") == "RoleBinding" and d["metadata"]["name"].endswith("-federation")
        for s in d.get("subjects", [])
    ]


@_needs_helm
@pytest.mark.parametrize("store_ns", [None, ""])
def test_default_keeps_everything_in_the_release_namespace(store_ns: str | None) -> None:
    """Unset and explicitly-empty are the historical behaviour — existing
    installs must not move when they upgrade onto this chart."""
    places = _store_places(_render(store_namespace=store_ns))
    assert set(places) == {"configmap", "role", "rolebinding", "flag"}, (
        f"a place stopped being rendered: {sorted(places)}"
    )
    assert set(places.values()) == {_RELEASE_NS}, places


@_needs_helm
def test_store_namespace_moves_all_four_places_together() -> None:
    """The relationship, not the literal: one value, four consumers of it."""
    places = _store_places(_render(store_namespace=_CONSUMER_NS))
    assert set(places) == {"configmap", "role", "rolebinding", "flag"}, (
        f"a place stopped being rendered: {sorted(places)}"
    )
    assert set(places.values()) == {_CONSUMER_NS}, (
        "the store namespace did not reach every place that needs it — a place "
        f"left behind renders the control silently inert: {places}"
    )


@_needs_helm
def test_service_account_subject_stays_in_the_release_namespace() -> None:
    """The subject must NOT follow the store: the pod still runs in the release
    namespace, and a subject that moved with the Role would bind to a
    ServiceAccount that does not exist."""
    subjects = _subject_namespaces(_render(store_namespace=_CONSUMER_NS))
    assert subjects == [_RELEASE_NS], (
        f"RoleBinding subject namespace(s) {subjects} — must stay {_RELEASE_NS!r} "
        "so the grant reaches the ServiceAccount the pod actually uses"
    )


@_needs_helm
def test_role_moves_rather_than_being_duplicated() -> None:
    """Exactly one Role/RoleBinding pair, in the store's namespace. A copy left
    in the release namespace would keep granting access to an object that is no
    longer there, and read as if the permission model were unchanged."""
    docs = _render(store_namespace=_CONSUMER_NS)
    for kind in ("Role", "RoleBinding"):
        found = [
            d["metadata"]["namespace"]
            for d in docs
            if d.get("kind") == kind and d["metadata"]["name"].endswith("-federation")
        ]
        assert found == [_CONSUMER_NS], f"{kind} rendered {found}, expected exactly [{_CONSUMER_NS!r}]"
