"""projection-gate image: build recipe and chart tag must move together.

helm/vector renders the gate image as ``<repository>:<tag>`` with an empty
``digest`` by default and ``pullPolicy: IfNotPresent``. Nothing publishes the
image — operators build it from ``helm/vector/projection-gate/`` and push it
themselves — so the tag is the only thing that tells a node "this is a
different build". If the build changes and the tag does not, ``helm upgrade``
renders a byte-identical pod spec: no rollout, and every node keeps serving
the image it already pulled while the nightly scan reports the new one.

That happened in #1278: the runtime base moved alpine 3.23.5 -> 3.23.6 (the
libcrypto3 fix) with the tag still at 0.1.0. The federation audit sidecar has
had the same guard since #1337
(``tests/shared/test_federation_audit_metrics.py``); this is its twin.

⛔ Unlike the sidecar, this image COPYs scripts from the build context, so the
Dockerfile alone does not describe the build. The digest covers the
Dockerfile's instruction lines (comments/blanks stripped, so prose edits do not
force a bump) plus the exact bytes of every file a context ``COPY`` reads. The
COPY sources are parsed out of the Dockerfile, not listed here, so a new COPY
is covered without editing this test.

NOT GUARDED: base-image content behind an unchanged ``FROM`` tag (an upstream
re-push of ``alpine:3.23.6`` changes the image without changing anything this
test reads), and ``apk add`` resolving newer package versions on rebuild.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
_CONTEXT = "helm/vector/projection-gate"
_VALUES = "helm/vector/values.yaml"

_RECIPE_DIGEST = "170d79df559a91901b43cf1ed7dfd0af44ddd5eb804d4e5239f3426dec203e52"
_EXPECTED_TAG = "0.1.1"


def _instruction_lines(dockerfile: Path) -> list[str]:
    lines = [ln.rstrip() for ln in dockerfile.read_text(encoding="utf-8").splitlines()]
    body = [ln for ln in lines if ln.strip() and not ln.lstrip().startswith("#")]
    assert body, "no instruction lines parsed out of the Dockerfile — parser broken"
    return body


def _context_copy_sources(body: list[str]) -> list[str]:
    """Source paths of every COPY/ADD that reads the build context (not --from)."""
    sources: list[str] = []
    for ln in body:
        m = re.match(r"^(COPY|ADD)\s+(.*)$", ln.strip(), re.IGNORECASE)
        if not m:
            continue
        args = [a for a in m.group(2).split() if not a.startswith("--")]
        if any(a.startswith("--from") for a in m.group(2).split()):
            continue
        assert len(args) >= 2, f"cannot parse COPY/ADD line: {ln!r}"
        sources.extend(args[:-1])
    return sources


def _recipe_digest(repo_root: Path) -> str:
    context = repo_root / _CONTEXT
    body = _instruction_lines(context / "Dockerfile")
    sources = _context_copy_sources(body)
    assert sources, (
        "no context COPY found — the gate image ships scripts from this "
        "directory, so an empty list means the parser broke, not that there "
        "is nothing to cover"
    )
    h = hashlib.sha256("\n".join(body).encode("utf-8"))
    for src in sorted(sources):
        path = context / src
        assert path.is_file(), f"COPY source {src!r} is not a file under {_CONTEXT}"
        h.update(f"\n--- {src}\n".encode("utf-8"))
        h.update(path.read_bytes())
    return h.hexdigest()


def _gate_image_tag(values_text: str) -> str:
    block = values_text.split("\nprojectionGate:", 1)
    assert len(block) == 2, f"no top-level projectionGate: block in {_VALUES}"
    image = block[1].split("\n  image:", 1)
    assert len(image) == 2, f"no projectionGate.image block in {_VALUES}"
    tags = re.findall(r'^    tag:\s*"([^"]+)"\s*$', image[1].split("\n  registry:", 1)[0], re.MULTILINE)
    assert len(tags) == 1, f"expected one projectionGate.image.tag, found {tags}"
    return tags[0]


def test_gate_recipe_change_forces_a_tag_bump() -> None:
    """Pure file parsing; runs everywhere."""
    tag = _gate_image_tag((REPO_ROOT / _VALUES).read_text(encoding="utf-8"))
    actual = _recipe_digest(REPO_ROOT)

    assert tag == _EXPECTED_TAG, (
        f"{_VALUES} projectionGate.image.tag is {tag!r}, this guard expects "
        f"{_EXPECTED_TAG!r}. If you bumped the tag deliberately, update "
        "_EXPECTED_TAG (and _RECIPE_DIGEST if the build changed too)."
    )
    assert actual == _RECIPE_DIGEST, (
        "the projection-gate image's BUILD changed "
        f"({_RECIPE_DIGEST[:12]}… -> {actual[:12]}…): its Dockerfile "
        "instructions or a file it COPYs.\n"
        "⛔ Bump projectionGate.image.tag in the same change (and the chart "
        "version), then update _EXPECTED_TAG and _RECIPE_DIGEST here. Leaving the "
        "tag alone renders a byte-identical pod spec: no rollout, and "
        "`IfNotPresent` keeps the OLD image on every node. (Dockerfile comment "
        "edits do not reach this digest.)"
    )


def test_copy_parser_sees_both_shipped_scripts() -> None:
    """The digest is only as good as the COPY parser; pin what it finds today."""
    body = _instruction_lines(REPO_ROOT / _CONTEXT / "Dockerfile")
    assert sorted(_context_copy_sources(body)) == [
        "serve_metrics.py",
        "verify_tenant_projections.py",
    ]
