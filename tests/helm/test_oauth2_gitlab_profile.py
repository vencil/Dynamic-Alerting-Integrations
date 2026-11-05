"""test_oauth2_gitlab_profile.py — PR-0 GitLab CE oauth2-proxy enablement

The tenant-api and da-portal charts must be able to configure a self-hosted
GitLab CE OIDC deployment. Before PR-0 the oauth2-proxy sidecar args had no
``--oidc-issuer-url`` / ``--scope`` / ``--gitlab-group``, so ``provider: gitlab``
could not reach a self-hosted instance. This test pins that:

  * the GitLab profile RENDERS the three flags when the values are set, and
  * the default (``provider: github``, empty issuer) does NOT render them
    (backward-compat regression guard).

tenant-api's oauth2-proxy takes them from its structured config file instead
of flags (ADR-027 §10): the provider entry's ``oidcConfig.issuerURL``,
``scope`` and ``gitlabConfig.group``, asserted on the rendered file below.
da-portal still passes legacy flags.

Two layers, mirroring test_single_writer_guard.py (#682):
  * static (no helm) — the templates carry the conditional arg blocks, so a
    regression is caught even on helm-less runners;
  * render (helm-gated) — proves the flags actually appear / are absent.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from _render_baseline import baseline_args

_CHARTS = {
    "da-portal": "helm/da-portal",
}
_TEMPLATES = {
    "da-portal": "helm/da-portal/templates/deployment.yaml",
}
_TENANT_API = "helm/tenant-api"

_HAS_HELM = shutil.which("helm") is not None
_needs_helm = pytest.mark.skipif(not _HAS_HELM, reason="helm CLI not on PATH")

# The conditional arg blocks the templates must carry (static-layer assertion).
_EXPECTED_BLOCKS = (
    "with .Values.oauth2Proxy.oidcIssuerUrl",
    "- --oidc-issuer-url=",
    "with .Values.oauth2Proxy.scope",
    "- --scope=",
    "range .Values.oauth2Proxy.gitlabGroups",
    "- --gitlab-group=",
)


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return Path(__file__).parent.parent.parent


# ── static layer (no helm needed) ────────────────────────────────────────────
@pytest.mark.parametrize("chart", sorted(_TEMPLATES))
def test_template_carries_gitlab_arg_blocks(repo_root: Path, chart: str):
    txt = (repo_root / _TEMPLATES[chart]).read_text(encoding="utf-8")
    for needle in _EXPECTED_BLOCKS:
        assert needle in txt, f"{chart} template missing GitLab-profile block: {needle!r}"


# ── render layer (helm-gated) ────────────────────────────────────────────────
def _render(repo_root: Path, chart: str, *set_args: str) -> subprocess.CompletedProcess:
    cmd = ["helm", "template", "t", str(repo_root / _CHARTS[chart]), *baseline_args(_CHARTS[chart])]
    for kv in set_args:
        cmd += ["--set", kv]
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)


@_needs_helm
@pytest.mark.parametrize("chart", sorted(_CHARTS))
def test_render_gitlab_profile_emits_flags(repo_root: Path, chart: str):
    res = _render(
        repo_root, chart,
        "oauth2Proxy.provider=gitlab",
        "oauth2Proxy.oidcIssuerUrl=https://gitlab.acme.internal",
        "oauth2Proxy.scope=openid profile email",
        "oauth2Proxy.gitlabGroups={acme/sre}",
    )
    assert res.returncode == 0, f"{chart} gitlab render failed: {res.stderr}"
    out = res.stdout
    assert "--provider=gitlab" in out, f"{chart}: --provider=gitlab missing"
    assert "--oidc-issuer-url=https://gitlab.acme.internal" in out, f"{chart}: issuer flag missing"
    assert "--scope=openid profile email" in out, f"{chart}: scope flag missing"
    assert "--gitlab-group=acme/sre" in out, f"{chart}: gitlab-group flag missing"


@_needs_helm
@pytest.mark.parametrize("chart", sorted(_CHARTS))
def test_render_default_omits_gitlab_flags(repo_root: Path, chart: str):
    # Default values (provider: github, empty issuer) must NOT render the OIDC
    # flags — proves the conditionals are gated and github deployments are unchanged.
    res = _render(repo_root, chart)
    assert res.returncode == 0, f"{chart} default render failed: {res.stderr}"
    out = res.stdout
    assert "--provider=github" in out, f"{chart}: default should be github"
    assert "--oidc-issuer-url" not in out, f"{chart}: issuer flag leaked into default render"
    assert "--scope=" not in out, f"{chart}: scope flag leaked into default render"
    assert "--gitlab-group=" not in out, f"{chart}: gitlab-group leaked into default render"


# ── tenant-api: the provider entry of the oauth2-proxy config file ───────────
def _tenant_api_provider(repo_root: Path, *set_args: str) -> tuple[dict, list[str]]:
    cmd = ["helm", "template", "t", str(repo_root / _TENANT_API), *baseline_args(_TENANT_API)]
    for kv in set_args:
        cmd += ["--set", kv]
    res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                         errors="replace", timeout=60)
    assert res.returncode == 0, res.stderr
    provider, args = None, None
    for doc in yaml.safe_load_all(res.stdout):
        if not doc:
            continue
        if doc.get("kind") == "ConfigMap" and "-oauth2-proxy-" in doc["metadata"]["name"]:
            providers = yaml.safe_load(doc["data"]["oauth2-proxy.yaml"])["providers"]
            assert len(providers) == 1, providers
            provider = providers[0]
        if doc.get("kind") == "Deployment":
            for c in doc["spec"]["template"]["spec"]["containers"]:
                if c["name"] == "oauth2-proxy":
                    args = c["args"]
    assert provider is not None and args is not None, "no oauth2-proxy config or container"
    return provider, args


@_needs_helm
def test_tenant_api_gitlab_profile_in_config_file(repo_root: Path):
    provider, args = _tenant_api_provider(
        repo_root,
        "oauth2Proxy.provider=gitlab",
        "oauth2Proxy.oidcIssuerUrl=https://gitlab.acme.internal",
        "oauth2Proxy.scope=openid profile email",
        "oauth2Proxy.gitlabGroups={acme/sre}",
    )
    assert provider["provider"] == "gitlab"
    assert provider["oidcConfig"] == {"issuerURL": "https://gitlab.acme.internal"}
    assert provider["scope"] == "openid profile email"
    assert provider["gitlabConfig"] == {"group": ["acme/sre"]}
    # oauth2-proxy refuses these legacy flags beside --alpha-config.
    legacy = ("--provider", "--oidc-issuer-url", "--scope", "--gitlab-group", "--upstream",
              "--http-address", "--pass-user-headers", "--set-xauthrequest", "--client-id")
    assert not [a for a in args if a.split("=", 1)[0] in legacy], args


@_needs_helm
def test_tenant_api_default_has_no_gitlab_settings(repo_root: Path):
    provider, _ = _tenant_api_provider(repo_root)
    assert provider["provider"] == "github"
    assert not {"oidcConfig", "scope", "gitlabConfig"} & set(provider), provider

