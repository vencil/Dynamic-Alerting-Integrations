"""test_identity_single_hop.py — ADR-027 §5, §9 items 3 and 5.

A user's identity crosses exactly one hop: oauth2-proxy → tenant-api's Unix
socket, inside the tenant-api pod. Two properties of everything this repo can
deploy say so:

  * §9 item 3 — the whole deployment has one oauth2-proxy: across every chart
    under helm/ (default values and each of its ci/ value sets, which switch on
    every branch a workload template has) and every raw manifest under k8s/,
    the only oauth2-proxy container is the one in the tenant-api pod.
  * §9 item 5 — no configuration forwards an identity header over the network:
    the header names oauth2-proxy can inject (the ones the tenant-api chart's
    oauth2-proxy config file injects, read from the render — never a hand
    list — plus oauth2-proxy's own fixed X-Forwarded-* / X-Auth-Request-*
    families) appear nowhere in the rendered output except that config file,
    and nowhere in k8s/ or rule-packs/.

What is scanned is what `helm template` produces, not the chart sources:
comments in a template that explain the old topology are not configuration.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from _helm_gate import helm_required
from _render_baseline import baseline_args
from _tree import repo_files

_REPO = Path(__file__).resolve().parents[2]
_HAS_HELM = shutil.which("helm") is not None
_REQUIRE_HELM = helm_required()

_CHARTS = sorted(p.parent for p in (_REPO / "helm").glob("*/Chart.yaml"))
_WORKLOAD_KINDS = {"Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "Job", "CronJob", "Pod"}
_OAUTH2_PROXY_IMAGE = "oauth2-proxy/oauth2-proxy"
_OWNER_CHART = "tenant-api"
#: Charts whose default render refuses on purpose, with the refusal expected:
#: they need a value to install at all, so their ci/ sets are the only view.
#: Any other default-render failure fails the fixture.
_REFUSES_DEFAULTS = {
    "victorialogs": "needs a Secret for every consumer in auth.existingSecrets",
}
#: One more tenant-api render: an OIDC provider with an extra claim header, so
#: the configured-header list below is shown to come from the config file
#: (a name no fixed list carries).
_CLAIM_HEADER = "X-Tenant-Org"
_CLAIM_SETS = ("oauth2Proxy.provider=oidc", "oauth2Proxy.oidcIssuerUrl=https://issuer.invalid",
               f"oauth2Proxy.claimHeaders.org={_CLAIM_HEADER}")

#: Headers oauth2-proxy sets on its own (legacy flags / X-Auth-Request
#: response headers). The configured ones are read from the render below.
_FIXED_FAMILY = (
    "X-Forwarded-Email", "X-Forwarded-User", "X-Forwarded-Groups",
    "X-Forwarded-Preferred-Username", "X-Forwarded-Access-Token",
    "X-Auth-Request-Email", "X-Auth-Request-User", "X-Auth-Request-Groups",
    "X-Auth-Request-Preferred-Username", "X-Auth-Request-Access-Token",
)


def _helm_or_skip() -> None:
    if not _HAS_HELM:
        if _REQUIRE_HELM:
            pytest.fail("helm is required in this environment but not on PATH")
        pytest.skip("helm CLI not on PATH")


def _render(chart: Path, values: Path | None, *sets: str) -> subprocess.CompletedProcess:
    cmd = ["helm", "template", "t", str(chart), *baseline_args(chart)]
    if values is not None:
        cmd += ["-f", str(values)]
    for kv in sets:
        cmd += ["--set", kv]
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=120)


@pytest.fixture(scope="module")
def renders() -> dict[str, list[dict]]:
    """`<chart>[:<ci value set>]` → rendered documents.

    Every ci/ value set must render. A default render may fail only for a
    chart in _REFUSES_DEFAULTS, and only with the refusal named there; then
    its ci/ sets are the only view, and there must be at least one.
    """
    _helm_or_skip()
    out: dict[str, list[dict]] = {}
    for chart in _CHARTS:
        name = chart.name
        res = _render(chart, None)
        if name in _REFUSES_DEFAULTS:
            assert res.returncode != 0, f"{name}: default render now succeeds; drop it from _REFUSES_DEFAULTS"
            assert _REFUSES_DEFAULTS[name] in res.stderr, f"{name}: unexpected refusal: {res.stderr}"
        else:
            assert res.returncode == 0, f"{name} (defaults): {res.stderr}"
            out[name] = [d for d in yaml.safe_load_all(res.stdout) if d]
        sets = sorted((chart / "ci").glob("*.yaml"))
        for values in sets:
            res = _render(chart, values)
            assert res.returncode == 0, f"{name} -f ci/{values.name}: {res.stderr}"
            out[f"{name}:{values.name}"] = [d for d in yaml.safe_load_all(res.stdout) if d]
        assert any(k.split(":")[0] == name for k in out), f"{name}: nothing rendered"
    res = _render(_REPO / "helm" / _OWNER_CHART, None, *_CLAIM_SETS)
    assert res.returncode == 0, res.stderr
    out[f"{_OWNER_CHART}:claim-header"] = [d for d in yaml.safe_load_all(res.stdout) if d]
    return out


def _pod_specs(doc: dict) -> list[dict]:
    if doc.get("kind") not in _WORKLOAD_KINDS:
        return []
    spec = doc.get("spec") or {}
    if doc["kind"] == "Pod":
        return [spec]
    if doc["kind"] == "CronJob":
        spec = spec["jobTemplate"]["spec"]
    return [spec["template"]["spec"]]


def _containers(doc: dict) -> list[dict]:
    return [c for s in _pod_specs(doc) for c in s.get("initContainers", []) + s["containers"]]


def _repo_yaml(*prefixes: str) -> list[Path]:
    """Tracked .yaml files under the given repo-relative directories."""
    return [p for p in repo_files(".yaml")
            if p.relative_to(_REPO).as_posix().startswith(prefixes)]


def _k8s_manifests() -> list[tuple[Path, dict]]:
    docs = []
    for p in sorted(_repo_yaml("k8s/")):
        for d in yaml.safe_load_all(p.read_text(encoding="utf-8")):
            if isinstance(d, dict):
                docs.append((p, d))
    return docs


def _is_oauth2_proxy_config(doc: dict) -> bool:
    return (doc.get("kind") == "ConfigMap"
            and "oauth2-proxy.yaml" in (doc.get("data") or {}))


# ── §9 item 3: one oauth2-proxy ─────────────────────────────────────────────

# adr-check: ADR-027/9.3a
def test_the_only_oauth2_proxy_is_in_the_tenant_api_pod(renders: dict[str, list[dict]]) -> None:
    found: dict[str, list[str]] = {}
    for key, docs in renders.items():
        for doc in docs:
            for c in _containers(doc):
                if _OAUTH2_PROXY_IMAGE in c.get("image", ""):
                    found.setdefault(key, []).append(f"{doc['kind']}/{doc['metadata']['name']}:{c['name']}")
    # Vacuity guard: the one that must exist is found in the default render.
    assert found.get(_OWNER_CHART) == ["Deployment/tenant-api:oauth2-proxy"], found
    strays = {k: v for k, v in found.items() if k.split(":")[0] != _OWNER_CHART}
    assert not strays, f"a second oauth2-proxy (ADR-027 §5 allows one): {strays}"
    for key, where in found.items():
        assert len(where) == 1, f"{key}: {where}"


# adr-check: ADR-027/9.3a
def test_no_raw_manifest_runs_an_oauth2_proxy() -> None:
    docs = _k8s_manifests()
    assert any(_containers(d) for _, d in docs), "k8s/: no workload found — vacuous"
    strays = [f"{p.relative_to(_REPO)}: {d['metadata']['name']}:{c['name']}"
              for p, d in docs for c in _containers(d)
              if _OAUTH2_PROXY_IMAGE in c.get("image", "")]
    assert not strays, strays


# ── §9 item 5: no identity header leaves the pod ────────────────────────────

def _identity_headers(renders: dict[str, list[dict]]) -> set[str]:
    names = set(_FIXED_FAMILY)
    configs = 0
    for key, docs in renders.items():
        if key.split(":")[0] != _OWNER_CHART:
            continue
        for doc in docs:
            if _is_oauth2_proxy_config(doc):
                cfg = yaml.safe_load(doc["data"]["oauth2-proxy.yaml"])
                names |= {h["name"] for h in cfg.get("injectRequestHeaders", [])}
                configs += 1
    assert configs, "no tenant-api oauth2-proxy config rendered — the header list would be hand-made"
    return names


def _pattern(names: set[str]) -> re.Pattern[str]:
    # nginx spells a request header `$http_x_forwarded_email`; match both.
    alts = sorted({re.escape(n.lower()).replace("\\-", "[-_]") for n in names})
    return re.compile("|".join(alts), re.IGNORECASE)


def test_the_header_list_comes_from_the_rendered_config(renders: dict[str, list[dict]]) -> None:
    names = _identity_headers(renders)
    assert _CLAIM_HEADER in names, sorted(names)


# adr-check: ADR-027/9.5a
def test_no_rendered_object_but_the_oauth2_proxy_config_names_an_identity_header(
        renders: dict[str, list[dict]]) -> None:
    pat = _pattern(_identity_headers(renders))
    hits = []
    for key, docs in renders.items():
        for doc in docs:
            if key.split(":")[0] == _OWNER_CHART and _is_oauth2_proxy_config(doc):
                continue
            m = pat.search(yaml.safe_dump(doc))
            if m:
                hits.append(f"{key}: {doc['kind']}/{doc['metadata']['name']} names {m.group(0)}")
    assert not hits, "an identity header outside oauth2-proxy's own config:\n" + "\n".join(hits)


# adr-check: ADR-027/9.5a
def test_no_raw_manifest_or_rule_pack_names_an_identity_header(
        renders: dict[str, list[dict]]) -> None:
    pat = _pattern(_identity_headers(renders))
    files = sorted(_repo_yaml("k8s/", "rule-packs/"))
    assert len(files) > 10, files
    hits = [f"{p.relative_to(_REPO)}: {m.group(0)}" for p in files
            if (m := pat.search(p.read_text(encoding="utf-8")))]
    assert not hits, hits


def test_the_pattern_catches_both_spellings() -> None:
    pat = _pattern({"X-Forwarded-Email"})
    assert pat.search("proxy_set_header X-Forwarded-Email $x;")
    assert pat.search("proxy_set_header Foo $http_x_forwarded_email;")
    assert not pat.search("X-Forwarded-For")
