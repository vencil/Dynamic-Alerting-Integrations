"""test_federation_inflight_limit.py — per-token in-flight limit wiring (ADR-020 §Gateway 驗章、撤銷與限流)

The behaviour (429 over the cap, release on completion, lease expiry after a
client reset) is exercised against real Envoy in tests/federation-e2e
(test_victorialogs_e2e.py VL11 / VL12). This file guards what the chart has to
get right for that behaviour to mean what values.yaml says it means:

  * the filter sits LAST before the router — a slot is taken at the moment the
    request is forwarded, the same moment the route timeout starts;
  * the per-worker count is maxPerToken / concurrency, so the sum over workers
    is a real per-instance upper bound, and a value that cannot be split evenly
    fails the render instead of silently enforcing less;
  * the lease TTL is longer than every route timeout — otherwise a slot could
    expire while its query is still running and the cap would under-count.

Static layer needs no helm; the render layer is gated on the helm CLI (same
split as test_revoked_set_contract_guard.py).
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

_CHART = "helm/federation-gateway"
_LUA = "helm/federation-gateway/files/inflight_limit.lua"
_FILTER = "envoy.filters.http.lua.inflight"

_HAS_HELM = shutil.which("helm") is not None
_needs_helm = pytest.mark.skipif(not _HAS_HELM, reason="helm CLI not on PATH")


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return Path(__file__).parent.parent.parent


# ── static layer (no helm) ───────────────────────────────────────────────────
def test_the_lua_takes_its_numbers_from_the_chart(repo_root: Path):
    """A number hard-coded in the filter would make every render-time guard
    below check a value the enforcement plane does not use."""
    lua = (repo_root / _LUA).read_text(encoding="utf-8")
    assert re.search(r'^local MAX_PER_WORKER = \{\{ include "federation-gateway.inflightPerWorker" \. \}\}$', lua, re.M)
    assert re.search(r'^local LEASE_TTL = \{\{ include "federation-gateway.inflightLeaseSeconds" \. \}\}$', lua, re.M)


def test_the_lua_reads_the_direct_response_marker(repo_root: Path):
    """The route metadata the render test below checks is only worth anything
    if the filter actually reads that key."""
    lua = (repo_root / _LUA).read_text(encoding="utf-8")
    assert 'handle:metadata():get("direct_response")' in lua


# ── render layer (helm-gated) ────────────────────────────────────────────────
def _render(repo_root: Path, sets: dict[str, str] | None = None,
            set_strings: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    cmd = ["helm", "template", "t", str(repo_root / _CHART)]
    for k, v in (sets or {}).items():
        cmd += ["--set", f"{k}={v}"]
    for k, v in (set_strings or {}).items():
        cmd += ["--set-string", f"{k}={v}"]
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)


def _envoy_cm(stdout: str) -> dict:
    for doc in yaml.safe_load_all(stdout):
        if isinstance(doc, dict) and doc.get("kind") == "ConfigMap" and "envoy.yaml" in (doc.get("data") or {}):
            return doc["data"]
    raise AssertionError("no ConfigMap carrying envoy.yaml in render output")


def _hcm(envoy: dict) -> dict:
    listener = envoy["static_resources"]["listeners"][0]
    return listener["filter_chains"][0]["filters"][0]["typed_config"]


def _lua_const(lua: str, name: str) -> int:
    m = re.search(rf"^local {name} = (\d+)$", lua, re.M)
    assert m, f"{name} not rendered as an integer literal"
    return int(m.group(1))


@_needs_helm
def test_the_filter_is_last_before_the_router(repo_root: Path):
    res = _render(repo_root)
    assert res.returncode == 0, res.stderr
    names = [f["name"] for f in _hcm(yaml.safe_load(_envoy_cm(res.stdout)["envoy.yaml"]))["http_filters"]]
    assert names[-1] == "envoy.filters.http.router"
    assert names[-2] == _FILTER, f"in-flight limit must sit directly before the router: {names}"
    assert names.index("envoy.filters.http.buffer") < names.index(_FILTER)
    assert names.index("envoy.filters.http.local_ratelimit.per_token") < names.index(_FILTER)


@_needs_helm
@pytest.mark.parametrize("max_per_token,concurrency,per_worker", [
    (4, 2, 2), (4, 1, 4), (8, 4, 2), (6, 3, 2),
    (5, 2, 2),   # floor: instance bound 4, below the configured 5
    (4, 3, 1),   # floor: instance bound 3
    (4, 8, 1),   # floor would be 0 -> 1: instance bound 8, above the configured 4
])
def test_the_cap_is_split_over_the_workers(repo_root: Path, max_per_token, concurrency, per_worker):
    """perWorker = max(1, floor(maxPerToken / concurrency)). No concurrency
    may fail the render: older values advice was to align it with the CPU
    limit, and `helm upgrade` must not break for an operator who did."""
    res = _render(repo_root, {"inflightLimit.maxPerToken": str(max_per_token), "concurrency": str(concurrency)})
    assert res.returncode == 0, res.stderr
    assert _lua_const(_envoy_cm(res.stdout)["inflight_limit.lua"], "MAX_PER_WORKER") == per_worker


def _routes(envoy: dict) -> list[dict]:
    return _hcm(envoy)["route_config"]["virtual_hosts"][0]["routes"]


def _marked(route: dict) -> bool:
    md = ((route.get("metadata") or {}).get("filter_metadata") or {}).get(_FILTER) or {}
    return md.get("direct_response") is True


@_needs_helm
@pytest.mark.parametrize("sets", [
    {},
    {"emergencyGlobalBlock": "true"},
    {"mode": "victorialogs", "jwt.audience": "tenant-federation-logs"},
    {"mode": "vm-cluster"},
])
def test_only_direct_response_routes_skip_the_slot(repo_root: Path, sets):
    """A route Envoy answers itself must carry the marker the Lua reads, or a
    token at its cap gets 429 instead of the route's own 403/503. A FORWARDING
    route carrying it would bypass the cap entirely, so that direction is the
    one that must never happen."""
    res = _render(repo_root, sets)
    assert res.returncode == 0, res.stderr
    routes = _routes(yaml.safe_load(_envoy_cm(res.stdout)["envoy.yaml"]))
    for r in routes:
        if "direct_response" in r:
            assert _marked(r), f"direct_response route without the in-flight skip marker: {r['match']}"
        else:
            assert "route" in r
            assert not _marked(r), f"forwarding route marked to skip the in-flight cap: {r['match']}"
    assert any("route" in r for r in routes) or sets.get("emergencyGlobalBlock") == "true"


@_needs_helm
def test_the_shipped_default_is_four_per_instance(repo_root: Path):
    """ADR-020's starting value: 4 per token per instance, over the default 2 workers."""
    res = _render(repo_root)
    assert res.returncode == 0, res.stderr
    assert _lua_const(_envoy_cm(res.stdout)["inflight_limit.lua"], "MAX_PER_WORKER") == 2


@_needs_helm
def test_a_release_without_the_block_still_gets_the_limit(repo_root: Path):
    """`helm upgrade --reuse-values` from an older chart carries no inflightLimit
    map at all; the limit must still render (on, 4), not error or vanish."""
    res = _render(repo_root, {"inflightLimit": "null"})
    assert res.returncode == 0, res.stderr
    data = _envoy_cm(res.stdout)
    assert _lua_const(data["inflight_limit.lua"], "MAX_PER_WORKER") == 2
    assert _FILTER in [f["name"] for f in _hcm(yaml.safe_load(data["envoy.yaml"]))["http_filters"]]


@_needs_helm
def test_the_lease_outlives_every_route_timeout(repo_root: Path):
    """A lease shorter than a route timeout would expire under a query that is
    still legitimately running, and the cap would admit one more."""
    for mode_sets in ({}, {"mode": "victorialogs", "jwt.audience": "tenant-federation-logs"}, {"mode": "vm-cluster"}):
        res = _render(repo_root, mode_sets)
        assert res.returncode == 0, res.stderr
        data = _envoy_cm(res.stdout)
        ttl = _lua_const(data["inflight_limit.lua"], "LEASE_TTL")
        routes = _hcm(yaml.safe_load(data["envoy.yaml"]))["route_config"]["virtual_hosts"][0]["routes"]
        timeouts = [r["route"]["timeout"] for r in routes if "route" in r]
        assert timeouts, f"no forwarding route rendered for {mode_sets}"
        for t in timeouts:
            assert t.endswith("s"), t
            assert ttl > int(t[:-1]), f"lease TTL {ttl}s does not outlive route timeout {t} ({mode_sets})"


@_needs_helm
def test_disabled_renders_neither_filter_nor_script(repo_root: Path):
    res = _render(repo_root, {"inflightLimit.enabled": "false", "inflightLimit.maxPerToken": "5"})
    assert res.returncode == 0, res.stderr
    data = _envoy_cm(res.stdout)
    assert "inflight_limit.lua" not in data
    names = [f["name"] for f in _hcm(yaml.safe_load(data["envoy.yaml"]))["http_filters"]]
    assert _FILTER not in names


@_needs_helm
@pytest.mark.parametrize("sets", [
    {"inflightLimit.maxPerToken": "0"},
    {"inflightLimit.maxPerToken": "-1"},
    {"inflightLimit.maxPerToken": "2.5"},
])
def test_an_invalid_cap_fails_the_render(repo_root: Path, sets):
    res = _render(repo_root, sets)
    assert res.returncode != 0, f"render accepted {sets}"
    assert "inflightLimit.maxPerToken" in res.stderr


@_needs_helm
@pytest.mark.parametrize("set_strings", [{"inflightLimit.maxPerToken": "4"}, {"inflightLimit.enabled": "true"}])
def test_a_string_value_fails_the_render(repo_root: Path, set_strings):
    res = _render(repo_root, set_strings=set_strings)
    assert res.returncode != 0, f"render accepted {set_strings}"
    assert "inflightLimit." in res.stderr
