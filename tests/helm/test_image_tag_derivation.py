"""test_image_tag_derivation.py — #682 chart image-default invariant

The three component charts (threshold-exporter / tenant-api / da-portal)
derive their image tag from ``Chart.appVersion`` via the template
expression::

    {{ .Values.image.tag | default (printf "v%s" .Chart.AppVersion) }}

with ``image.tag: ""`` in values.yaml. This makes the default image a
single source of truth — always ``ghcr.io/vencil/<comp>:v<appVersion>``,
which is exactly the tag the release pipeline's L3 digest-verify step
(``scripts/ops/verify_release_digest.sh``, #445) guarantees to be
pullable. No drift, no per-chart values-prod pin, no hardcoded tag to
rot (see #682; supersedes the earlier values-prod approach #683).

Two layers:
  * static (no helm needed) — guards the template expression + empty tag
    so a regression is caught even on runners without the helm CLI;
  * render (helm-gated) — proves the rendered image is the v-prefixed
    pullable form, including the da-portal tier1/tier2 variants and that
    the local-dev override (environments/local) is unaffected.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

# (chart dir, expected image repository)
_CHARTS = [
    ("helm/threshold-exporter", "ghcr.io/vencil/threshold-exporter"),
    ("helm/tenant-api", "ghcr.io/vencil/tenant-api"),
    ("helm/da-portal", "ghcr.io/vencil/da-portal"),
    ("helm/recipe-preview", "ghcr.io/vencil/recipe-preview"),
]

_DERIVE_RE = re.compile(
    r"\.Values\.image\.tag\s*\|\s*default\s*\(printf\s+\"v%s\"\s+\.Chart\.AppVersion\)"
)

_HAS_HELM = shutil.which("helm") is not None
_needs_helm = pytest.mark.skipif(not _HAS_HELM, reason="helm CLI not on PATH")


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return Path(__file__).parent.parent.parent


def _app_version(repo_root: Path, chart_dir: str) -> str:
    chart = yaml.safe_load((repo_root / chart_dir / "Chart.yaml").read_text(encoding="utf-8"))
    return str(chart["appVersion"])


def _primary_image(manifests: list[dict], repo: str) -> str:
    """Return the image of the first container whose image starts with ``repo``."""
    for doc in manifests:
        spec = (doc or {}).get("spec", {}).get("template", {}).get("spec", {})
        for c in spec.get("containers", []):
            img = c.get("image", "")
            if img.startswith(repo + ":"):
                return img
    raise AssertionError(f"no container image starting with {repo!r} found")


def _render(chart_dir: Path, values_file: Path | None = None) -> list[dict]:
    cmd = ["helm", "template", "t", str(chart_dir), "-n", "monitoring"]
    if values_file is not None:
        cmd += ["-f", str(values_file)]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=30)
    return [d for d in yaml.safe_load_all(out.stdout) if d]


# ── Static layer (no helm required) ──────────────────────────────────────────

@pytest.mark.parametrize("chart_dir,repo", _CHARTS)
def test_values_tag_is_empty(repo_root: Path, chart_dir: str, repo: str) -> None:
    """values.yaml image.tag must be empty so the template derives it."""
    values = yaml.safe_load((repo_root / chart_dir / "values.yaml").read_text(encoding="utf-8"))
    assert values["image"]["tag"] == "", (
        f"{chart_dir}/values.yaml image.tag must be '' (derive from appVersion); "
        f"a hardcoded tag reintroduces drift (#682)"
    )
    assert values["image"]["repository"] == repo


@pytest.mark.parametrize("chart_dir,repo", _CHARTS)
def test_template_derives_tag_from_appversion(repo_root: Path, chart_dir: str, repo: str) -> None:
    """deployment.yaml must contain the appVersion-derive default expression."""
    tmpl = (repo_root / chart_dir / "templates" / "deployment.yaml").read_text(encoding="utf-8")
    assert _DERIVE_RE.search(tmpl), (
        f"{chart_dir} deployment.yaml lost the "
        f"`.Values.image.tag | default (printf \"v%s\" .Chart.AppVersion)` derivation (#682)"
    )


def test_da_portal_tiers_have_empty_tag(repo_root: Path) -> None:
    """tier1/tier2 overlays must not pin a (stale, no-v) image tag."""
    for tier in ("values-tier1.yaml", "values-tier2.yaml"):
        values = yaml.safe_load((repo_root / "helm/da-portal" / tier).read_text(encoding="utf-8"))
        assert values["image"]["tag"] == "", (
            f"helm/da-portal/{tier} image.tag must be '' (derive from appVersion); "
            f"it previously pinned a broken '2.5.0' (no v-prefix, #682)"
        )


# ── Render layer (helm-gated) ────────────────────────────────────────────────

@_needs_helm
@pytest.mark.parametrize("chart_dir,repo", _CHARTS)
def test_rendered_image_is_v_prefixed_appversion(repo_root: Path, chart_dir: str, repo: str) -> None:
    """Default render → ghcr v-prefixed image == v<appVersion> (release-guaranteed pullable)."""
    manifests = _render(repo_root / chart_dir)
    expected = f"{repo}:v{_app_version(repo_root, chart_dir)}"
    assert _primary_image(manifests, repo) == expected


@_needs_helm
@pytest.mark.parametrize("tier", ["values-tier1.yaml", "values-tier2.yaml"])
def test_da_portal_tier_render_is_pullable(repo_root: Path, tier: str) -> None:
    chart = repo_root / "helm/da-portal"
    manifests = _render(chart, values_file=chart / tier)
    expected = f"ghcr.io/vencil/da-portal:v{_app_version(repo_root, 'helm/da-portal')}"
    assert _primary_image(manifests, "ghcr.io/vencil/da-portal") == expected


@_needs_helm
def test_exporter_local_dev_override_preserved(repo_root: Path) -> None:
    """The local kind-load workflow (environments/local) still pins :dev unchanged."""
    chart = repo_root / "helm/threshold-exporter"
    override = repo_root / "environments/local/threshold-exporter.yaml"
    manifests = _render(chart, values_file=override)
    assert _primary_image(manifests, "threshold-exporter") == "threshold-exporter:dev"
