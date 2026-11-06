"""Guard: every CI-lint Docker fallback image must be digest-pinned.

Supply-chain sweep Part 2 (#849 follow-up). The wrappers prefer a binary on PATH
and fall back to a Docker image; that fallback must reference an immutable
`repo:tag@sha256:<digest>` so a re-pushed / tampered tag cannot substitute a
different image. This pins the invariant so a future version bump that drops the
`@sha256:` re-pin fails loudly here instead of silently reintroducing a mutable
tag pull.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "scripts" / "tools" / "lint"))
sys.path.insert(0, str(_REPO / "scripts" / "tools"))

import check_pint  # noqa: E402
import check_iac_helm  # noqa: E402
import check_iac_vibe_rules  # noqa: E402

# repo:tag@sha256:<64 hex> — tag present AND a full-length digest pinned.
_PINNED_RE = re.compile(r"^[^@\s]+:[^@\s]+@sha256:[0-9a-f]{64}$")


@pytest.mark.parametrize("name,image", [
    ("PINT_IMAGE", check_pint.PINT_IMAGE),
    ("KUBE_LINTER_IMAGE", check_iac_helm.KUBE_LINTER_IMAGE),
    ("HELM_IMAGE", check_iac_helm.HELM_IMAGE),
    ("HADOLINT_IMAGE", check_iac_vibe_rules.HADOLINT_IMAGE),
])
def test_fallback_image_is_digest_pinned(name, image):
    assert _PINNED_RE.match(image), (
        f"{name} is not digest-pinned (expected repo:tag@sha256:<64hex>): {image!r}. "
        f"On a version bump, re-resolve via `docker buildx imagetools inspect <image>:<ver>` "
        f"(take the top-level Digest; NOT `docker inspect`, which can be arch-specific)."
    )


def test_k8s_manifests_inherits_pinned_kube_linter():
    # check_k8s_manifests.py locates kube-linter through check_iac_helm's
    # locate_kube_linter (#3611) — make sure its docker fallback is the
    # digest-pinned image (no separate mutable copy).
    sys.path.insert(0, str(_REPO / "scripts" / "tools" / "lint"))
    import check_k8s_manifests  # noqa: E402
    assert check_k8s_manifests.locate_kube_linter is check_iac_helm.locate_kube_linter
    lib = check_k8s_manifests._lib_engine
    real_which = lib.shutil.which
    lib.shutil.which = lambda n: "/usr/bin/docker" if n == "docker" else None
    try:
        located = check_k8s_manifests.locate_kube_linter()
    finally:
        lib.shutil.which = real_which
    assert located.image == check_iac_helm.KUBE_LINTER_IMAGE
    assert "@sha256:" in located.image
