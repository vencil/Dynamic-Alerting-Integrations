"""test_victorialogs_gateway_guard.py — ADR-021 #609 PR-2 victorialogs gateway guards

The federation-gateway ``victorialogs`` mode is the authorization plane for
tenant log queries (ADR-021). Three security-critical invariants must survive
any future values/template refactor; this test pins them so a silent regression
goes red rather than shipping a cross-tenant breach:

  1. fail-loud audience guard — ``mode=victorialogs`` with
     ``jwt.audience != tenant-federation-logs`` aborts the render. A metrics-pull
     token (aud ``tenant-federation``) must never be accepted against the log
     store (capability model B).
  2. default-deny — the rendered envoy config carries the catch-all 403 route,
     and that route is ABSENT in the other two modes (no cross-mode leakage).
  3. no ``AccountID`` / ``ProjectID`` in any ``request_headers_to_remove`` — the
     verified header the Lua injects must reach VictoriaLogs. Route-level removal
     runs in the router AFTER the Lua and would strip the injected value ->
     no AccountID -> VictoriaLogs default partition 0 = cross-tenant breach.
     (This is the exact footgun ADR-021 §Blast radius was amended to warn about.)

Two layers (mirrors test_single_writer_guard.py): a static layer that needs no
helm (so helm-less runners still catch a regression) and a render layer gated on
the helm CLI.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

_CHART = "helm/federation-gateway"
_ENVOY_FILE = "helm/federation-gateway/files/envoy.yaml"
_HELPERS = "helm/federation-gateway/templates/_helpers.tpl"

_HAS_HELM = shutil.which("helm") is not None
_needs_helm = pytest.mark.skipif(not _HAS_HELM, reason="helm CLI not on PATH")

# The catch-all 403 body — the one-line fingerprint of the default-deny route.
_CATCHALL_403 = "endpoint not permitted for tenant log query"


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return Path(__file__).parent.parent.parent


# ── static layer (no helm) ───────────────────────────────────────────────────
def test_helpers_has_audience_guard(repo_root: Path):
    txt = (repo_root / _HELPERS).read_text(encoding="utf-8")
    assert "victorialogs" in txt, "_helpers must allow the victorialogs mode"
    assert "tenant-federation-logs" in txt, "_helpers must pin the logs audience"
    assert "fail" in txt, "the audience guard must `fail` the render"


def test_envoy_source_has_catchall_and_no_header_strip(repo_root: Path):
    txt = (repo_root / _ENVOY_FILE).read_text(encoding="utf-8")
    assert _CATCHALL_403 in txt, "victorialogs default-deny catch-all 403 missing from envoy.yaml"
    # AccountID/ProjectID appear in COMMENTS (explaining why they are not removed)
    # and the lowercase `account_id:` access-log field — none of which is a
    # removal-list entry. Assert specifically that neither is a YAML list item
    # under request_headers_to_remove (a `- AccountID` / `- ProjectID` line).
    for bad in ("- AccountID", "- ProjectID"):
        assert bad not in txt, f"{bad!r} must never be a request_headers_to_remove entry"


# ── render layer (helm-gated) ────────────────────────────────────────────────
def _render(repo_root: Path, sets: dict[str, str]) -> subprocess.CompletedProcess:
    cmd = ["helm", "template", "t", str(repo_root / _CHART)]
    for k, v in sets.items():
        cmd += ["--set", f"{k}={v}"]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60)


def _envoy_config(stdout: str) -> dict:
    """Extract + parse the embedded envoy.yaml from the rendered ConfigMap."""
    for doc in yaml.safe_load_all(stdout):
        if isinstance(doc, dict) and doc.get("kind") == "ConfigMap":
            data = doc.get("data") or {}
            if "envoy.yaml" in data:
                return yaml.safe_load(data["envoy.yaml"])
    raise AssertionError("no ConfigMap carrying envoy.yaml in render output")


def _all_header_removals(node) -> list[str]:
    """Collect every request_headers_to_remove entry anywhere in the config."""
    out: list[str] = []
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "request_headers_to_remove" and isinstance(v, list):
                out += [str(x) for x in v]
            else:
                out += _all_header_removals(v)
    elif isinstance(node, list):
        for v in node:
            out += _all_header_removals(v)
    return out


@_needs_helm
def test_victorialogs_renders_and_pins_invariants(repo_root: Path):
    res = _render(repo_root, {"mode": "victorialogs", "jwt.audience": "tenant-federation-logs"})
    assert res.returncode == 0, f"victorialogs render must succeed: {res.stderr}"
    assert _CATCHALL_403 in res.stdout, "rendered victorialogs config missing the catch-all 403"
    cfg = _envoy_config(res.stdout)
    removals = _all_header_removals(cfg)
    assert "AccountID" not in removals and "ProjectID" not in removals, (
        "AccountID/ProjectID must not be stripped (router-after-Lua would drop the "
        f"injected value -> partition 0 breach); request_headers_to_remove = {removals}"
    )


@_needs_helm
def test_victorialogs_wrong_audience_aborts(repo_root: Path):
    # Default jwt.audience is tenant-federation (the metrics plane); victorialogs
    # mode must fail-loud rather than accept a metrics token against the log store.
    res = _render(repo_root, {"mode": "victorialogs"})
    assert res.returncode != 0, "victorialogs with a non-logs audience must abort the render"
    assert "tenant-federation-logs" in res.stderr, f"guard message unexpected: {res.stderr}"


@_needs_helm
def test_other_modes_have_no_victorialogs_catchall(repo_root: Path):
    res = _render(repo_root, {})  # default mode = prom-label-proxy
    assert res.returncode == 0, f"default mode must render: {res.stderr}"
    assert _CATCHALL_403 not in res.stdout, (
        "the victorialogs default-deny route leaked into a non-victorialogs mode"
    )
