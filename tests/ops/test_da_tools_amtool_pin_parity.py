"""The amtool bundled in the da-tools image is the Alertmanager production runs (#2294).

``generate-routes`` hands the config it writes / applies / ``--validate``s to
``amtool check-config`` when amtool is on PATH (#2219 / #2260), and the da-tools
image carries amtool so the CI templates ``da-tools init --ci`` generates get
that check by default. The check is only worth anything if amtool is the SAME
parser the cluster loads the config with: an older amtool can accept a field a
newer Alertmanager rejects (or the reverse), and the verdict would be green on a
config production refuses.

The version SSOT is ``k8s/03-monitoring/deployment-alertmanager.yaml``. The
Dockerfile takes amtool with ``COPY --from=<that image ref>``, and this test
requires the two refs to be byte-identical — repository, tag AND digest — not
merely the same tag: a tag is mutable, the digest is what actually ran.

Pure file parsing — no Docker — so it runs in the standard Python Tests lane.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

_REPO = Path(__file__).resolve().parents[2]
_DOCKERFILE = _REPO / "components" / "da-tools" / "app" / "Dockerfile"
_AM_DEPLOYMENT = _REPO / "k8s" / "03-monitoring" / "deployment-alertmanager.yaml"
_THIRD_PARTY = _REPO / "components" / "da-tools" / "app" / "third_party" / "alertmanager"

# `COPY --from=<ref> ...` where <ref> names the Alertmanager image. Anchored to
# the instruction (not a free-text search) so a comment quoting a ref cannot
# satisfy it.
_COPY_FROM_AM = re.compile(
    r"^\s*COPY\s+(?:--\S+\s+)*--from=(prom/alertmanager:\S+)\s+(\S+)\s+(\S+)\s*$",
    re.MULTILINE | re.IGNORECASE,
)


def _deployed_alertmanager_image() -> str:
    """The Alertmanager container's image, read STRUCTURALLY from the manifest
    (container named ``alertmanager``), so the config-reloader sidecar or a
    comment cannot be picked up instead."""
    images = []
    for doc in yaml.safe_load_all(_AM_DEPLOYMENT.read_text(encoding="utf-8")):
        spec = ((doc or {}).get("spec") or {}).get("template", {}).get("spec", {})
        for container in spec.get("containers") or []:
            image = str(container.get("image", ""))
            if image.startswith("prom/alertmanager:"):
                images.append(image)
    assert len(images) == 1, (
        f"expected exactly one prom/alertmanager container in "
        f"{_AM_DEPLOYMENT.relative_to(_REPO).as_posix()}, found {images}")
    return images[0]


def _dockerfile_amtool_copy() -> tuple[str, str, str]:
    matches = _COPY_FROM_AM.findall(_DOCKERFILE.read_text(encoding="utf-8"))
    assert len(matches) == 1, (
        f"expected exactly one `COPY --from=prom/alertmanager:...` in "
        f"{_DOCKERFILE.relative_to(_REPO).as_posix()}, found {matches}. "
        "The da-tools image must bundle amtool from the deployed Alertmanager "
        "image (#2294).")
    return matches[0]


def test_dockerfile_amtool_ref_is_the_deployed_alertmanager_ref() -> None:
    ref, _src, _dst = _dockerfile_amtool_copy()
    deployed = _deployed_alertmanager_image()
    assert ref == deployed, (
        f"da-tools Dockerfile copies amtool from {ref!r}, but "
        f"{_AM_DEPLOYMENT.relative_to(_REPO).as_posix()} deploys {deployed!r}. "
        "Copy the manifest's ref verbatim (tag AND digest) into the Dockerfile "
        "`COPY --from=` — amtool must be the parser production runs.")


def test_ref_is_digest_pinned() -> None:
    ref, _src, _dst = _dockerfile_amtool_copy()
    assert re.fullmatch(r"prom/alertmanager:v[0-9][^@\s]*@sha256:[0-9a-f]{64}", ref), (
        f"amtool source {ref!r} is not tag@sha256-pinned")


def test_amtool_lands_on_path() -> None:
    _ref, src, dst = _dockerfile_amtool_copy()
    assert src == "/bin/amtool", f"upstream image ships amtool at /bin/amtool, not {src!r}"
    assert dst == "/usr/local/bin/amtool", (
        f"amtool must land on PATH as `amtool` (generate-routes looks it up with "
        f"shutil.which), got {dst!r}")


def test_upstream_license_and_notice_ship_with_amtool() -> None:
    """Apache-2.0 §4(a)/(d): redistributing amtool carries its LICENSE and NOTICE.
    The upstream image has neither, so they are vendored and COPYed in.

    ⚠️ On an Alertmanager bump (Renovate moves the manifest and the Dockerfile
    ref together), re-check the vendored pair against the NEW release's tarball
    (`cmp` its LICENSE / NOTICE) and replace them if upstream changed either.
    This test only proves the files exist and are shipped — it cannot see that
    they came from the same release as the ref, and the Dockerfile deliberately
    names no version so it cannot go stale on a bump."""
    for name in ("LICENSE", "NOTICE"):
        vendored = _THIRD_PARTY / name
        assert vendored.is_file(), f"missing vendored {vendored.relative_to(_REPO).as_posix()}"
    assert "Apache License" in (_THIRD_PARTY / "LICENSE").read_text(encoding="utf-8")
    assert "Prometheus Alertmanager" in (_THIRD_PARTY / "NOTICE").read_text(encoding="utf-8")
    dockerfile = _DOCKERFILE.read_text(encoding="utf-8")
    assert re.search(
        r"^COPY\s+third_party/alertmanager/LICENSE\s+third_party/alertmanager/NOTICE\s+"
        r"/usr/share/doc/amtool/\s*$", dockerfile, re.MULTILINE), (
        "Dockerfile must COPY the vendored LICENSE and NOTICE into /usr/share/doc/amtool/")
