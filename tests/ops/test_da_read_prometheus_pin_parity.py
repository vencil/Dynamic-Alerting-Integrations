"""`da-read` links the Prometheus the platform deploys (#3206, ADR-036 §12).

`da-read prometheus` reads a prometheus.yml with Prometheus' own `config.Load`
and `da-read prometheus-rules` reads a rule file with its own `rulefmt.Parse`.
That reading is only Prometheus' while the Go module linked is the release the
platform runs: another release can read a key differently, or refuse a file the
deployed one accepts (and the reverse).

The version SSOT is the Prometheus container of
``k8s/03-monitoring/deployment-prometheus.yaml``. Prometheus' Go module carries
the image's version in a different spelling — image ``v3.15.0`` is module
``v0.315.0`` (v3 releases are tagged ``v0.3MM.P`` for Go) — and this test
requires the module ``components/da-read/go.mod`` requires to be the image's,
read from both files (no version written here).

Scope: only the deployment manifest. The other places that pin a Prometheus
image have their own guards against that manifest (the nightly scan matrix:
``test_nightly_scan_matrix_drift.py``; the federation e2e harness:
``test_federation_e2e_mirrors_deploy_sot.py``) or deliberately none
(``tests/e2e-bench/docker-compose.yml``, ``try-local/``).

Pure file parsing, so it runs in the standard Python Tests lane.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

_REPO = Path(__file__).resolve().parents[2]
_DEPLOYMENT = _REPO / "k8s" / "03-monitoring" / "deployment-prometheus.yaml"
_DA_READ_GOMOD = _REPO / "components" / "da-read" / "go.mod"
_MODULE = "github.com/prometheus/prometheus"


def _rel(p: Path) -> str:
    return p.relative_to(_REPO).as_posix()


def _deployed_prometheus_tag() -> str:
    """The tag of the container named ``prometheus``, read structurally (the
    config-reloader sidecar or a comment cannot be picked up instead)."""
    images = []
    for doc in yaml.safe_load_all(_DEPLOYMENT.read_text(encoding="utf-8")):
        spec = ((doc or {}).get("spec") or {}).get("template", {}).get("spec", {})
        for container in spec.get("containers") or []:
            if container.get("name") == "prometheus":
                images.append(str(container.get("image", "")))
    assert len(images) == 1, (
        f"expected exactly one container named prometheus in {_rel(_DEPLOYMENT)}, found {images}")
    m = re.fullmatch(r"prom/prometheus:(v[0-9][^@\s]*)(@sha256:[0-9a-f]{64})?", images[0])
    assert m, f"{_rel(_DEPLOYMENT)}: {images[0]!r} is not a prom/prometheus:<tag> reference"
    return m.group(1)


def _da_read_prometheus_version() -> str:
    """The version go.mod's `require` directive (single-line or block form)
    names for the Prometheus module — not a free-text search, so a comment
    naming a version cannot satisfy it."""
    versions = []
    in_block = False
    for raw in _DA_READ_GOMOD.read_text(encoding="utf-8").splitlines():
        line = raw.split("//", 1)[0].strip()
        if in_block:
            if line == ")":
                in_block = False
                continue
            fields = line.split()
        elif line.startswith("require ("):
            in_block = True
            continue
        elif line.startswith("require "):
            fields = line.split()[1:]
        else:
            continue
        if len(fields) >= 2 and fields[0] == _MODULE:
            versions.append(fields[1])
    assert len(versions) == 1, (
        f"expected exactly one {_MODULE} requirement in {_rel(_DA_READ_GOMOD)}, found {versions}")
    return versions[0]


def _module_version_of(image_tag: str) -> str:
    """Prometheus' Go module version for a v3 image tag: v3.MM.P → v0.3MM.P."""
    m = re.fullmatch(r"v3\.(\d+)\.(\d+)", image_tag)
    assert m, (
        f"{_rel(_DEPLOYMENT)} deploys Prometheus {image_tag}; only v3.MM.P release tags map to "
        "a Go module version here (v0.3MM.P). Teach _module_version_of the new scheme.")
    return f"v0.{300 + int(m.group(1))}.{m.group(2)}"


def test_module_version_mapping() -> None:
    assert _module_version_of("v3.15.0") == "v0.315.0"
    assert _module_version_of("v3.4.12") == "v0.304.12"


def test_da_read_links_the_deployed_prometheus() -> None:
    tag = _deployed_prometheus_tag()
    want = _module_version_of(tag)
    got = _da_read_prometheus_version()
    assert got == want, (
        f"{_rel(_DA_READ_GOMOD)} requires {_MODULE} {got}, but {_rel(_DEPLOYMENT)} deploys "
        f"Prometheus {tag} (Go module {want}). Bump them together (`go get "
        f"{_MODULE}@{want} && go mod tidy` in components/da-read).")
