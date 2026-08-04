"""Bind the federation-e2e compose stack to the images we actually deploy.

`tests/federation-e2e/docker-compose.yml` mirrors the real deployment by hand —
its own comments say so ("Args mirror helm/federation-proxy (the SoT) verbatim").
Nothing enforced it, and BOTH mirrored components had rotted by the time anyone
looked:

  * prom-label-proxy — chart bumped to v0.15.0 in PR #1333, compose left on
    v0.14.0 in that same PR's own diff.
  * prom/prometheus  — pinned at v3.11.2 by the harness commit (#544) and never
    touched since, against a shipped v3.13.2. Two minors of PromQL/storage
    behaviour the E2E was silently NOT exercising.

Every gate stayed green through both, because a working container at the WRONG
version passes every scenario. That is the failure mode this module exists for:
Federation E2E kept certifying ADR-020's request path against binaries we do not
ship, and reported success while doing it.

WHY THE FIX IS NOT "ADD IT TO RENOVATE". The three customManagers in
renovate.json glob `helm/**`, `k8s/**` and the scan-matrix workflow only, and
their matchStrings are anchored on an `@sha256:` digest — these refs are
deliberately tag-only, so even widening the globs would not match. Widening is
actively WRONG besides: `test_coverage_is_complete_and_exact` asserts Renovate
covers EXACTLY the 15 #902 L2 deploy refs, and
`test_scan_matrix_and_deploy_refs_share_depnames` chains that same set to the
nightly CVE scan matrix. Pulling a test fixture in would either break both tests
or drag an e2e-only image into the production supply-chain scan matrix. The
fixture needs a binding to the SoT, not membership in it — which is this module.

Runs in the normal Python Tests lane, whose path filter covers `helm/**`,
`k8s/**` and `tests/**` — i.e. it fires on exactly the image-bump PRs that
create this drift, not only on PRs that happen to touch the compose file.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
COMPOSE = REPO / "tests" / "federation-e2e" / "docker-compose.yml"
PROXY_CHART_VALUES = REPO / "helm" / "federation-proxy" / "values.yaml"
PROXY_CHART_DEPLOYMENT = REPO / "helm" / "federation-proxy" / "templates" / "deployment.yaml"
GATEWAY_CHART_VALUES = REPO / "helm" / "federation-gateway" / "values.yaml"
PROMETHEUS_MANIFEST = REPO / "k8s" / "03-monitoring" / "deployment-prometheus.yaml"

# `fixture-exporter` (nginx) is absent from MIRRORED ON PURPOSE: a pure test
# double with no deployed counterpart, so there is nothing to track. Recorded
# here rather than left implicit, because "has no SoT" and "has an SoT we forgot
# to wire up" look identical from the outside.
NO_DEPLOY_COUNTERPART = {"fixture-exporter"}


def _compose_services() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]


def _chart_image(values_path: Path) -> str:
    """`repository:tag` from a chart's values.yaml (digest deliberately dropped)."""
    image = yaml.safe_load(values_path.read_text(encoding="utf-8"))["image"]
    return f"{image['repository']}:{image['tag']}"


def _manifest_image(manifest_path: Path, container: str) -> str:
    """`repository:tag` for one container in a k8s Deployment, digest stripped."""
    for doc in yaml.safe_load_all(manifest_path.read_text(encoding="utf-8")):
        if not doc:
            continue
        spec = doc.get("spec", {}).get("template", {}).get("spec", {}) or {}
        for c in spec.get("containers", []) or []:
            if c["name"] == container:
                return c["image"].split("@", 1)[0]
    raise AssertionError(f"container {container!r} not found in {manifest_path.name}")


MIRRORED = {
    "federation-proxy": lambda: _chart_image(PROXY_CHART_VALUES),
    "federation-gateway": lambda: _chart_image(GATEWAY_CHART_VALUES),
    "prometheus": lambda: _manifest_image(PROMETHEUS_MANIFEST, "prometheus"),
}


@pytest.mark.parametrize("service", sorted(MIRRORED))
def test_compose_image_matches_deploy_sot(service: str) -> None:
    expected = MIRRORED[service]()
    actual = _compose_services()[service]["image"]
    assert actual == expected, (
        f"{COMPOSE.name} runs a different {service} than we deploy.\n"
        f"  deploy SoT: {expected}\n"
        f"  compose:    {actual}\n"
        "Bump the compose ref — an E2E on the wrong version certifies nothing."
    )


@pytest.mark.parametrize("service", sorted(MIRRORED))
def test_compose_image_stays_tag_only(service: str) -> None:
    """No digest here on purpose — a pinned fixture digest would rot invisibly.

    The deploy side pins digests (#902 L2, authoritative over the tag). Copying
    one into the fixture would make `image:` disagree with the `repository:tag`
    form compared above, and a stale fixture digest still pulls and still passes.
    """
    assert "@sha256:" not in _compose_services()[service]["image"]


def test_every_compose_service_is_classified() -> None:
    """Guard the guard: a NEW mirrored service must not default to unchecked.

    Without this, adding a service to the compose stack silently opts it out of
    every assertion above — the same shape of hole this module was written to
    close.
    """
    imaged = {name for name, svc in _compose_services().items() if "image" in svc}
    unclassified = imaged - set(MIRRORED) - NO_DEPLOY_COUNTERPART
    assert not unclassified, (
        f"unclassified compose service(s): {sorted(unclassified)} — add each to "
        "MIRRORED (with its deploy SoT) or to NO_DEPLOY_COUNTERPART (with why)."
    )


# --------------------------------------------------------------------------
# federation-proxy only: the compose ALSO hand-copies the chart's args.
# --------------------------------------------------------------------------

# `-upstream` is the ONE flag that legitimately differs: the chart points at the
# in-cluster DNS name, the compose at the sibling compose service. Exempt it by
# name so the divergence is declared rather than absorbed by a loose comparison.
VALUE_EXEMPT_FLAGS = {"upstream"}


def _chart_arg_lines() -> list[str]:
    """Raw `- -flag=...` lines from the proxy container's args: block.

    Text-scanned, not YAML-parsed: the file is a Go template, so `{{ ... }}`
    makes it invalid YAML. The block ends at the first non-comment line indented
    at or above `args:` itself.
    """
    lines = PROXY_CHART_DEPLOYMENT.read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    args_indent: int | None = None
    for line in lines:
        if args_indent is None:
            m = re.match(r"^(\s*)args:\s*$", line)
            if m:
                args_indent = len(m.group(1))
            continue
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if indent <= args_indent:
            break
        out.append(line.strip())
    assert out, f"found no args: block in {PROXY_CHART_DEPLOYMENT.name}"
    return out


def _flag_name(entry: str) -> str:
    """`- -header-name={{ ... }}` / `-enable-label-apis` -> `header-name`."""
    return entry.lstrip("- ").lstrip("-").split("=", 1)[0]


def test_proxy_flag_set_matches_chart() -> None:
    """Same flags, so a chart-side enforcement flag cannot skip the E2E.

    `-enable-label-apis` is the live example: without it the proxy stops
    enforcing /api/v1/labels and /api/v1/label/<n>/values (#506 AC). A fixture
    missing a flag the chart hardcodes would certify a weaker proxy than ships.
    """
    chart = {_flag_name(a) for a in _chart_arg_lines()}
    compose = {_flag_name(a) for a in _compose_services()["federation-proxy"]["command"]}
    assert compose == chart, (
        f"flag drift between {COMPOSE.name} and {PROXY_CHART_DEPLOYMENT.name}\n"
        f"  only in chart:   {sorted(chart - compose)}\n"
        f"  only in compose: {sorted(compose - chart)}"
    )


def test_proxy_flag_values_match_chart_values() -> None:
    """Flags whose chart value is a plain `.Values.*` lookup must agree.

    The subtler half of the same class: `-header-name` drifting from
    `tenant.headerName` would leave the E2E asserting tenant isolation on a
    header the gateway never sets.
    """
    values = yaml.safe_load(PROXY_CHART_VALUES.read_text(encoding="utf-8"))
    command = _compose_services()["federation-proxy"]["command"]
    compose_by_flag = {_flag_name(a): a.split("=", 1)[-1] for a in command if "=" in a}

    def _sub(m: re.Match[str]) -> str:
        node = values
        for part in m.group(1).split("."):
            node = node[part]
        return str(node)

    checked = 0
    for entry in _chart_arg_lines():
        flag = _flag_name(entry)
        if flag in VALUE_EXEMPT_FLAGS or "=" not in entry:
            continue
        # Substitute every `{{ .Values.a.b }}` with the real values.yaml value;
        # anything still templated (conditionals, pipelines) is skipped rather
        # than guessed at.
        rendered, n = re.subn(r"\{\{\s*\.Values\.([\w.]+)\s*\}\}", _sub, entry.split("=", 1)[1])
        if n == 0 or "{{" in rendered:
            continue
        assert compose_by_flag.get(flag) == rendered, (
            f"-{flag} drifted from the chart: chart renders {rendered!r}, "
            f"compose has {compose_by_flag.get(flag)!r}"
        )
        checked += 1

    # Guard the guard: if the template stops using plain `.Values.*` lookups this
    # test would pass vacuously.
    assert checked >= 3, f"expected to check >=3 value-bearing flags, checked {checked}"
