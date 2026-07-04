"""test_oauth2_gitlab_profile.py — PR-0 GitLab CE oauth2-proxy enablement

The tenant-api and da-portal charts must be able to configure a self-hosted
GitLab CE OIDC deployment. Before PR-0 the oauth2-proxy sidecar args had no
``--oidc-issuer-url`` / ``--scope`` / ``--gitlab-group``, so ``provider: gitlab``
could not reach a self-hosted instance. This test pins that:

  * the GitLab profile RENDERS the three flags when the values are set, and
  * the default (``provider: github``, empty issuer) does NOT render them
    (backward-compat regression guard).

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

_CHARTS = {
    "tenant-api": "helm/tenant-api",
    "da-portal": "helm/da-portal",
}
_TEMPLATES = {
    "tenant-api": "helm/tenant-api/templates/deployment.yaml",
    "da-portal": "helm/da-portal/templates/deployment.yaml",
}

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
    cmd = ["helm", "template", "t", str(repo_root / _CHARTS[chart])]
    for kv in set_args:
        cmd += ["--set", kv]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60)


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
