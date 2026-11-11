"""test_oauth2_gitlab_profile.py — PR-0 GitLab CE oauth2-proxy enablement

The tenant-api chart must be able to configure a self-hosted GitLab CE OIDC
deployment. Before PR-0 the oauth2-proxy sidecar had no issuer / scope / group
setting, so ``provider: gitlab`` could not reach a self-hosted instance. This
test pins that:

  * the GitLab profile RENDERS the three settings when the values are set, and
  * the default (``provider: github``, empty issuer) does NOT render them
    (backward-compat regression guard).

oauth2-proxy takes them from its structured config file instead of flags
(ADR-027 §10): the provider entry's ``oidcConfig.issuerURL``, ``scope`` and
``gitlabConfig.group``, asserted on the rendered file below. (da-portal had a
second oauth2-proxy with the same flags; ADR-027 §5 removed it.)
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from _render_baseline import baseline_args

_TENANT_API = "helm/tenant-api"

_HAS_HELM = shutil.which("helm") is not None
_needs_helm = pytest.mark.skipif(not _HAS_HELM, reason="helm CLI not on PATH")

@pytest.fixture(scope="session")
def repo_root() -> Path:
    return Path(__file__).parent.parent.parent


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

