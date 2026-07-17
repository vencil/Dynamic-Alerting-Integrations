"""Promtool pin parity gate — recipe-preview Dockerfile vs ci.yml (#657 PR-D2).

The would-fire verdict (`firing` / `inactive` / `error`) is classified from
promtool's *return code + output format*, which is version-bound:
`_recipe_preview.classify_promtool_result` hard-codes the `FAILED:` /
`got:[` markers (version-bound; spike-verified stable through promtool 3.12.x). The recipe-preview IMAGE bundles a SHA-pinned promtool; the CI
rule-pack gate (`.github/workflows/ci.yml`) installs its OWN pinned promtool.

If those two pins ever skew, the image could classify a verdict differently from
what CI validated — a silent correctness drift with NO other guard (promtool
verifies an *expr*, never *which* promtool runs in prod). This test fails loudly
the moment the Dockerfile `PROM_VERSION` / amd64 digest diverge from ci.yml's.

Pure file parsing — no promtool, no Docker — so it runs in the standard
`Python Tests` job, not just promtool-gated runs.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_DOCKERFILE = _REPO / "components" / "recipe-preview" / "Dockerfile"
_CI_YML = _REPO / ".github" / "workflows" / "ci.yml"
_DEVCONTAINER_SH = _REPO / ".devcontainer" / "install-promtool.sh"


def _one(pattern: str, text: str, what: str) -> str:
    """Return the single capture group for `pattern`, asserting exactly one match."""
    matches = re.findall(pattern, text, re.MULTILINE)
    assert len(matches) == 1, (
        f"expected exactly one {what} match for /{pattern}/, found {len(matches)}: {matches}"
    )
    return matches[0]


def test_files_exist() -> None:
    assert _DOCKERFILE.is_file(), f"missing {_DOCKERFILE}"
    assert _CI_YML.is_file(), f"missing {_CI_YML}"


def test_promtool_version_pin_matches_ci() -> None:
    """Dockerfile ARG PROM_VERSION must equal ci.yml's PROM_VERSION."""
    df = _DOCKERFILE.read_text(encoding="utf-8")
    ci = _CI_YML.read_text(encoding="utf-8")

    # Capture the bare version token, tolerant of optional surrounding quotes and
    # a trailing comment/whitespace ([^\s"'#]+ stops at the first of those), so a
    # cosmetic `="2.53.2"` or `=2.53.2  # note` on either side does NOT produce a
    # false skew (or a 0-match crash). Both sides go through the SAME extraction,
    # so equal versions compare equal regardless of quoting style.
    df_ver = _one(r"""^ARG PROM_VERSION=["']?([^\s"'#]+)""", df, "Dockerfile PROM_VERSION")
    ci_ver = _one(r"""^\s*PROM_VERSION=["']?([^\s"'#]+)""", ci, "ci.yml PROM_VERSION")

    assert df_ver == ci_ver, (
        f"promtool VERSION skew: recipe-preview Dockerfile pins {df_ver!r} but "
        f"ci.yml pins {ci_ver!r}. The verdict format is version-bound — bump BOTH "
        f"together (and refresh the per-arch SHA-256 digests)."
    )


def test_promtool_amd64_digest_matches_ci() -> None:
    """Dockerfile amd64 digest must equal ci.yml's (same artifact, same integrity)."""
    df = _DOCKERFILE.read_text(encoding="utf-8")
    ci = _CI_YML.read_text(encoding="utf-8")

    df_sha = _one(r"^ARG PROM_SHA256_amd64=([0-9a-f]{64})", df,
                  "Dockerfile PROM_SHA256_amd64")
    ci_sha = _one(r"^\s*PROM_SHA256=([0-9a-f]{64})", ci, "ci.yml PROM_SHA256")

    assert df_sha == ci_sha, (
        f"promtool amd64 SHA-256 skew: Dockerfile pins {df_sha} but ci.yml pins "
        f"{ci_sha}. Both download the same prometheus-<ver>.linux-amd64 tarball — "
        f"the digests MUST match."
    )


def test_devcontainer_promtool_pin_matches_ci() -> None:
    """Dev-container install script must pin the SAME promtool version+digest as ci.yml.

    Root cause #1134: a hand-installed dev-container promtool 2.53.2 diverged from the
    CI pin 3.x — Prometheus 3.0's left-open lookback flips boundary-sample semantics, so
    fixtures verified locally against 2.x failed the pinned CI promtool. The dev-container
    install is now codified (.devcontainer/install-promtool.sh) and this guard keeps it
    from silently drifting off the CI pin again.
    """
    assert _DEVCONTAINER_SH.is_file(), f"missing {_DEVCONTAINER_SH}"
    dc = _DEVCONTAINER_SH.read_text(encoding="utf-8")
    ci = _CI_YML.read_text(encoding="utf-8")

    dc_ver = _one(r"""^PROM_VERSION=["']?([^\s"'#]+)""", dc,
                  "install-promtool.sh PROM_VERSION")
    ci_ver = _one(r"""^\s*PROM_VERSION=["']?([^\s"'#]+)""", ci, "ci.yml PROM_VERSION")
    assert dc_ver == ci_ver, (
        f"promtool VERSION skew: .devcontainer/install-promtool.sh pins {dc_ver!r} but "
        f"ci.yml pins {ci_ver!r}. Lookback/verdict semantics are version-bound — bump "
        f"BOTH together (and refresh the SHA-256)."
    )

    dc_sha = _one(r"^PROM_SHA256=([0-9a-f]{64})", dc, "install-promtool.sh PROM_SHA256")
    ci_sha = _one(r"^\s*PROM_SHA256=([0-9a-f]{64})", ci, "ci.yml PROM_SHA256")
    assert dc_sha == ci_sha, (
        f"promtool amd64 SHA-256 skew: install-promtool.sh pins {dc_sha} but ci.yml "
        f"pins {ci_sha}. Both download the same prometheus-<ver>.linux-amd64 tarball — "
        f"the digests MUST match."
    )


def test_dockerfile_pins_arm64_digest() -> None:
    """Multi-arch: the arm64 digest must also be pinned (no unverified download)."""
    df = _DOCKERFILE.read_text(encoding="utf-8")
    arm = _one(r"^ARG PROM_SHA256_arm64=([0-9a-f]{64})", df,
               "Dockerfile PROM_SHA256_arm64")
    assert len(arm) == 64


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
