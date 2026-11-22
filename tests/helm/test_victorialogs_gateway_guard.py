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
     (This is the exact footgun ADR-021 §注入前驗證 warns about.)

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
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)


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


def _collect(node, key) -> list:
    """Collect every value stored under dict key `key`, anywhere in the tree.

    Operates on the PARSED config, so comments mentioning the key by name do
    not count — only an actual matcher/field does.
    """
    out: list = []
    if isinstance(node, dict):
        for k, v in node.items():
            if k == key:
                out.append(v)
            out += _collect(v, key)
    elif isinstance(node, list):
        for v in node:
            out += _collect(v, key)
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
    # CodeRabbit #889: the allowlist must use EXACT-anchored safe_regex, not a
    # path-separated prefix — `path_separated_prefix: /select/logsql/query` also
    # matches `/select/logsql/query/foo`, letting an unknown sub-path slip the
    # allowlist (default-deny regression). Check the PARSED config (the raw text
    # mentions the term in comments): victorialogs carries NO path_separated_prefix
    # MATCHER (the /api/v1/read deny route is prom-label-proxy only), and each
    # allowed endpoint is an anchored `^…/?$` regex.
    assert not _collect(cfg, "path_separated_prefix"), (
        "victorialogs allowlist must use exact-anchored safe_regex, not a "
        "subpath-permitting path_separated_prefix matcher"
    )
    regexes = [v.get("regex") for v in _collect(cfg, "safe_regex") if isinstance(v, dict)]
    assert any(r and r.endswith("/?$") for r in regexes), (
        f"allowlist endpoints must render as exact-anchored ^…/?$ regex; got {regexes}"
    )


@_needs_helm
def test_victorialogs_wrong_audience_aborts(repo_root: Path):
    # Default jwt.audience is tenant-federation (the metrics plane); victorialogs
    # mode must fail-loud rather than accept a metrics token against the log store.
    res = _render(repo_root, {"mode": "victorialogs"})
    assert res.returncode != 0, "victorialogs with a non-logs audience must abort the render"
    assert "tenant-federation-logs" in res.stderr, f"guard message unexpected: {res.stderr}"


# ── /select/logsql/query limit cap (ADR-021 §查詢端點白名單) ───────────────
# The behaviour (400/415 per case) is exercised against real Envoy in
# tests/federation-e2e/test_victorialogs_e2e.py VL11. These pin the wiring
# that behaviour depends on: the filter exists only in victorialogs mode, sits
# after the body buffer (or it cannot see a POST form body) and before the
# router, its file ships in the ConfigMap, and the max is a validated number.
_VL = {"mode": "victorialogs", "jwt.audience": "tenant-federation-logs"}
_QUERY_LIMIT_FILTER = "envoy.filters.http.lua.query_limit"


def _rendered_cm_data(stdout: str) -> dict:
    for doc in yaml.safe_load_all(stdout):
        if isinstance(doc, dict) and doc.get("kind") == "ConfigMap":
            data = doc.get("data") or {}
            if "envoy.yaml" in data:
                return data
    raise AssertionError("no ConfigMap carrying envoy.yaml in render output")


def _http_filter_names(cfg: dict) -> list[str]:
    hcm = cfg["static_resources"]["listeners"][0]["filter_chains"][0]["filters"][0]
    return [f["name"] for f in hcm["typed_config"]["http_filters"]]


@_needs_helm
def test_victorialogs_query_limit_filter_wired_after_buffer(repo_root: Path):
    res = _render(repo_root, _VL)
    assert res.returncode == 0, res.stderr
    data = _rendered_cm_data(res.stdout)
    names = _http_filter_names(yaml.safe_load(data["envoy.yaml"]))
    assert _QUERY_LIMIT_FILTER in names, f"query-limit filter missing: {names}"
    i = names.index(_QUERY_LIMIT_FILTER)
    assert names.index("envoy.filters.http.buffer") < i, (
        f"query-limit filter must run after the buffer to read POST form bodies: {names}")
    assert i == len(names) - 2 and names[-1] == "envoy.filters.http.router", (
        f"query-limit filter must be the last filter before the router: {names}")
    lua = data.get("query_limit.lua", "")
    default = yaml.safe_load((repo_root / _CHART / "values.yaml").read_text(encoding="utf-8"))[
        "victorialogs"]["maxQueryLimit"]
    assert f"local MAX_LIMIT = {int(default)}\n" in lua, (
        "chart default maxQueryLimit not templated into the Lua")
    assert 'QUERY_PATH_RE = "^/select/logsql/query/?$"' in lua


@_needs_helm
def test_victorialogs_query_limit_value_is_templated(repo_root: Path):
    res = _render(repo_root, {**_VL, "victorialogs.maxQueryLimit": "250"})
    assert res.returncode == 0, res.stderr
    assert "local MAX_LIMIT = 250\n" in _rendered_cm_data(res.stdout)["query_limit.lua"]


@_needs_helm
@pytest.mark.parametrize("bad", ["0", "-1", "1.5", "abc", "1000000001"])
def test_victorialogs_query_limit_bad_value_aborts(repo_root: Path, bad):
    res = _render(repo_root, {**_VL, "victorialogs.maxQueryLimit": bad})
    assert res.returncode != 0, f"maxQueryLimit={bad} must abort the render"
    assert "victorialogs.maxQueryLimit" in res.stderr, res.stderr


@_needs_helm
@pytest.mark.parametrize(
    "mode_set, label",
    [({}, "prom-label-proxy (default)"), ({"mode": "vm-cluster"}, "vm-cluster")],
)
def test_other_modes_have_no_query_limit_filter(repo_root: Path, mode_set, label):
    res = _render(repo_root, mode_set)
    assert res.returncode == 0, f"{label} must render: {res.stderr}"
    data = _rendered_cm_data(res.stdout)
    assert "query_limit.lua" not in data, f"query_limit.lua leaked into {label} mode"
    assert _QUERY_LIMIT_FILTER not in _http_filter_names(yaml.safe_load(data["envoy.yaml"]))


@_needs_helm
@pytest.mark.parametrize(
    "mode_set, label",
    [({}, "prom-label-proxy (default)"), ({"mode": "vm-cluster"}, "vm-cluster")],
)
def test_other_modes_have_no_victorialogs_catchall(repo_root: Path, mode_set, label):
    # Both non-victorialogs modes (CodeRabbit #889): the docstring promises the
    # catch-all is ABSENT in the other TWO modes, so render each and assert it.
    res = _render(repo_root, mode_set)
    assert res.returncode == 0, f"{label} must render: {res.stderr}"
    assert _CATCHALL_403 not in res.stdout, (
        f"the victorialogs default-deny route leaked into {label} mode"
    )
