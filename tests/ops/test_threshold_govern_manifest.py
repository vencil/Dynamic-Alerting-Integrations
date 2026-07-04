"""Manifest contract for the threshold-govern CronJob's machine-identity token.

ADR-027 PR-1b-ii-b (#962): the governance CronJob mounts an audience-bound
projected ServiceAccount token so tenant-api's KSAResolver can audit WHICH
workload is calling. These are static YAML assertions (no cluster / helm) that
pin the security-critical shape — above all the HARD audience binding, which is
the single point that stops a default-audience pod token from being accepted by
tenant-api's TokenReview (ADR-027 G4). A wrong/blank audience is the headline
failure mode, so it gets a dedicated assertion.
"""
from __future__ import annotations

import pathlib

import pytest
import yaml

_MANIFEST = (
    pathlib.Path(__file__).resolve().parents[2]
    / "k8s" / "03-monitoring" / "cronjob-threshold-govern.yaml"
)
_TOKEN_AUDIENCE = "tenant-api"
_TOKEN_MOUNT = "/var/run/secrets/tokens"
_TOKEN_FILE = "tenant-api-token"


@pytest.fixture(scope="module")
def docs():
    with open(_MANIFEST, encoding="utf-8") as fh:
        return [d for d in yaml.safe_load_all(fh) if d]


@pytest.fixture(scope="module")
def sa(docs):
    return next(d for d in docs if d["kind"] == "ServiceAccount")


@pytest.fixture(scope="module")
def pod_spec(docs):
    cj = next(d for d in docs if d["kind"] == "CronJob")
    return cj["spec"]["jobTemplate"]["spec"]["template"]["spec"]


def test_dedicated_sa_not_default(sa):
    # A dedicated SA (not monitoring `default`) so the caller has a distinct,
    # allowlisted workload identity (ksa_resolver.go monitoring:threshold-govern).
    assert sa["metadata"]["name"] == "threshold-govern"
    assert sa["metadata"]["namespace"] == "monitoring"


def test_sa_automount_disabled(sa):
    # No broad apiserver-audience token — only the explicit projected token below.
    assert sa["automountServiceAccountToken"] is False


def test_pod_uses_the_sa_and_disables_automount(pod_spec):
    assert pod_spec["serviceAccountName"] == "threshold-govern"
    assert pod_spec["automountServiceAccountToken"] is False


def test_projected_token_is_audience_bound(pod_spec):
    # THE hard-gate: the projected token MUST be bound to the tenant-api audience.
    # A blank/default audience yields a token valid only for the apiserver, which
    # tenant-api's TokenReview rejects (G4) — so pin the audience literally.
    vol = next(v for v in pod_spec["volumes"] if v["name"] == "tenant-api-token")
    src = vol["projected"]["sources"][0]["serviceAccountToken"]
    assert src["audience"] == _TOKEN_AUDIENCE
    assert isinstance(src.get("expirationSeconds"), int) and src["expirationSeconds"] >= 600
    assert src["path"] == _TOKEN_FILE


def test_token_mounted_readonly(pod_spec):
    c = pod_spec["containers"][0]
    mount = next(m for m in c["volumeMounts"] if m["name"] == "tenant-api-token")
    assert mount["mountPath"] == _TOKEN_MOUNT
    assert mount["readOnly"] is True


def test_cli_reads_token_from_the_mounted_path(pod_spec):
    # The --auth-token-file arg must point at the mounted token file, or the token
    # is mounted but never presented (audit would silently see no_token).
    args = pod_spec["containers"][0]["args"]
    assert "--auth-token-file" in args
    assert args[args.index("--auth-token-file") + 1] == f"{_TOKEN_MOUNT}/{_TOKEN_FILE}"


def test_identity_headers_still_present(pod_spec):
    # audit-only: the token AUGMENTS, never replaces, the header identity that
    # drives RBAC. Both must stay wired.
    args = pod_spec["containers"][0]["args"]
    assert "--identity-groups" in args
    assert args[args.index("--identity-groups") + 1] == "threshold-governance"
