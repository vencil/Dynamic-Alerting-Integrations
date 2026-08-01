"""Vector pin parity gate — the version that VALIDATES the VRL must be the
version that RUNS it (#1293 follow-up).

Three copies of "which Vector" exist, and until now nothing bound them:

  * ``.github/workflows/ci.yml`` installs a pinned CLI, and `vector test` over
    ``helm/vector/tests/projection_tests.yaml`` is the only place the shipped VRL
    is EXECUTED rather than string-matched.
  * ``.devcontainer/install-vector.sh`` installs the same CLI locally.
  * ``helm/vector`` DEPLOYS an image (``values.yaml`` ``image.tag``, restated by
    ``Chart.yaml`` ``appVersion`` and the chart README).

VRL is a versioned language — 0.55 rejects error-coalescing on infallible calls
that earlier releases accepted, and the chart's own VRL carries comments about
that — so a gate running a DIFFERENT vector than production can green-light
syntax or semantics the deployed binary will not honour. The drift was real
before this gate: the CLI was pinned to 0.55.0 while the chart shipped 0.57.0,
and both ``Chart.yaml`` appVersion and the chart README still claimed 0.55.0.

Pure file parsing — no vector, no helm, no network — so it runs in the standard
Python Tests job rather than only on binary-gated runs.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

_REPO = Path(__file__).resolve().parents[2]
_CI_YML = _REPO / ".github" / "workflows" / "ci.yml"
_DEVCONTAINER_SH = _REPO / ".devcontainer" / "install-vector.sh"
_VALUES = _REPO / "helm" / "vector" / "values.yaml"
_CHART = _REPO / "helm" / "vector" / "Chart.yaml"
_README = _REPO / "helm" / "vector" / "README.md"
_NIGHTLY = _REPO / ".github" / "workflows" / "nightly-image-scan.yaml"


def _one(pattern: str, text: str, what: str) -> str:
    """Return the single capture group for `pattern`, asserting exactly one match."""
    matches = re.findall(pattern, text, re.MULTILINE)
    assert len(matches) == 1, (
        f"expected exactly one {what} match for /{pattern}/, found {len(matches)}: {matches}"
    )
    return matches[0]


def _ci_version() -> str:
    return _one(r"""^\s*VECTOR_VERSION=["']?([^\s"'#]+)""",
                _CI_YML.read_text(encoding="utf-8"), "ci.yml VECTOR_VERSION")


def test_files_exist() -> None:
    for p in (_CI_YML, _DEVCONTAINER_SH, _VALUES, _CHART, _README, _NIGHTLY):
        assert p.is_file(), f"missing {p}"


def test_devcontainer_pin_matches_ci() -> None:
    """Dev-container install script must pin the SAME version AND digest as ci.yml.

    Same shape as the promtool guard (tests/preview/test_promtool_pin_parity.py):
    a locally-installed vector that differs from the CI pin lets a VRL change pass
    locally and fail CI, or worse pass both while production behaves differently.
    """
    dc = _DEVCONTAINER_SH.read_text(encoding="utf-8")

    dc_ver = _one(r"""^VECTOR_VERSION=["']?([^\s"'#]+)""", dc,
                  "install-vector.sh VECTOR_VERSION")
    assert dc_ver == _ci_version(), (
        f"vector VERSION skew: .devcontainer/install-vector.sh pins {dc_ver!r} but "
        f"ci.yml pins {_ci_version()!r}. VRL semantics are version-bound — bump BOTH "
        f"together (and refresh the SHA-256)."
    )

    dc_sha = _one(r"^VECTOR_SHA256=([0-9a-f]{64})", dc, "install-vector.sh VECTOR_SHA256")
    ci_sha = _one(r"^\s*VECTOR_SHA256=([0-9a-f]{64})", _CI_YML.read_text(encoding="utf-8"),
                  "ci.yml VECTOR_SHA256")
    assert dc_sha == ci_sha, (
        f"vector amd64 SHA-256 skew: install-vector.sh pins {dc_sha} but ci.yml pins "
        f"{ci_sha}. Both download the same vector-<ver>-x86_64-unknown-linux-gnu "
        f"tarball — the digests MUST match."
    )


def test_ci_pin_matches_the_deployed_image_tag() -> None:
    """⛔ The load-bearing one: the CLI that validates the VRL must be the version
    the chart actually deploys.

    ``image.tag`` carries a variant suffix (``-distroless-libc``); only the numeric
    release is comparable, so the suffix is stripped rather than matched.

    ⛔ Read via ``yaml.safe_load``, NOT a ``^\\s*tag:`` regex: values.yaml holds a
    SECOND ``tag:`` key (the projection-gate exposer image), so a text match here
    is ambiguous and would silently compare the wrong one. Structured file →
    parse the structure.
    """
    values = yaml.safe_load(_VALUES.read_text(encoding="utf-8"))
    tag = values["image"]["tag"]
    m = re.match(r"^(\d+\.\d+\.\d+)", tag)
    assert m, (
        f"values.yaml image.tag {tag!r} does not start with a semver release — this "
        f"gate cannot compare it to the CI pin. Keep the tag in <version>[-variant] form."
    )
    assert m.group(1) == _ci_version(), (
        f"vector RUNTIME skew: the chart deploys {tag!r} but CI validates the VRL with "
        f"{_ci_version()!r}. `vector test` would then prove nothing about the binary "
        f"that actually runs the shipped VRL. Bump ci.yml + "
        f".devcontainer/install-vector.sh to match the image (or vice versa) — and "
        f"remember image.digest in values.yaml is AUTHORITATIVE over the tag."
    )


def test_nightly_scan_ref_matches_the_chart_image() -> None:
    """The nightly CVE scan must scan the image the chart actually deploys.

    ``nightly-image-scan.yaml`` hand-copies the full pinned ref (repository, tag
    AND digest) into its matrix, annotated ``# helm/vector/values.yaml``. Nothing
    bound the two: ``image-ref-resolve.yaml`` verifies the values.yaml refs
    RESOLVE in the registry but never compares them to the matrix, and its own
    header comment (lines 6-8) names ``matrix↔values drift`` as a gap that
    "slips past" the nightly scan. So a digest bumped in one file and not the
    other would leave the chart deploying image A while CVE scanning image B —
    and, because ``image.digest`` is AUTHORITATIVE over the tag at deploy time,
    that also lets the deployed binary drift away from the version every other
    pin in this file agrees on, without any of them going red.

    Binding repository+tag+digest is checkable offline; "does this digest really
    belong to release X" is not (it needs a registry round-trip), so this gate
    deliberately proves consistency between the two in-repo copies rather than
    provenance of the digest itself.

    ⚠️ Scope: vector only. The same matrix↔values drift exists for every other
    entry in that scan (victoria-logs, envoy, …) — a repo-wide version of this
    check is tracked as #1302.
    """
    values = yaml.safe_load(_VALUES.read_text(encoding="utf-8"))
    image = values["image"]
    nightly = _NIGHTLY.read_text(encoding="utf-8")

    ref = _one(r"""^\s*ref:\s*["'](timberio/vector:[^"']+)["']""", nightly,
               "nightly-image-scan vector matrix ref")
    expected = f"{image['repository']}:{image['tag']}"
    digest = image.get("digest")
    if digest:
        expected = f"{expected}@{digest}"

    assert ref == expected, (
        f"nightly-image-scan.yaml scans {ref!r} but helm/vector/values.yaml deploys "
        f"{expected!r}. The scan matrix is a hand-copy of that file (its own comment "
        f"says so) — a drift means the CVE scan covers an image the cluster never "
        f"runs. Update both together."
    )


def test_chart_appversion_matches_the_deployed_image_tag() -> None:
    """Chart.yaml appVersion restates the deployed Vector release; it drifted to a
    stale 0.55.0 while the image shipped 0.57.0, so pin it."""
    app = _one(r"""^appVersion:\s*["']?([^\s"'#]+)""", _CHART.read_text(encoding="utf-8"),
               "Chart.yaml appVersion")
    assert app == _ci_version(), (
        f"Chart.yaml appVersion is {app!r} but the pinned Vector release is "
        f"{_ci_version()!r}. appVersion documents the app being deployed — a stale "
        f"value misleads anyone reading the chart for the runtime version."
    )


def test_chart_readme_image_ref_matches() -> None:
    """The chart README states the image reference in prose; it drifted too."""
    ref = _one(r"timberio/vector:([^\s`'\"]+)", _README.read_text(encoding="utf-8"),
               "README timberio/vector image ref")
    m = re.match(r"^(\d+\.\d+\.\d+)", ref)
    assert m and m.group(1) == _ci_version(), (
        f"chart README names timberio/vector:{ref} but the pinned Vector release is "
        f"{_ci_version()!r}. Prose copies of a version drift silently — update it."
    )


def test_download_cache_key_carries_the_pinned_version() -> None:
    """ci.yml's own comment warns a stale cache key silently loses the cache benefit.
    Bind the key to the pin so a version bump cannot forget it."""
    ci = _CI_YML.read_text(encoding="utf-8")
    key = _one(r"^\s*key:\s*(\$\{\{ runner\.os \}\}-vibe-dl-pytests-vector-\S+)", ci,
               "ci.yml vector download-cache key")
    assert f"-vector-{_ci_version()}-" in key, (
        f"download-cache key {key!r} does not carry the pinned vector version "
        f"{_ci_version()!r} — the key would exact-hit the old entry and the new "
        f"tarball would never be cached (ci.yml says so in its own comment)."
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
