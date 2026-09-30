"""test_readme_image_tags.py — image tags named in chart README prose

A chart README that names an image inline (`victoriametrics/victoria-logs:v1.50.0`)
carries a second copy of the tag values.yaml pins, and that copy is not in a
parameter table, so test_readme_values_keys.py never sees it. victorialogs'
README kept saying v1.50.0 after values.yaml (and the nightly scan) moved to
v1.52.0.

This pins: every backticked `repo:tag` in a chart README whose repo is an
image that chart's values.yaml declares (a mapping with `repository` and
`tag`) must carry the tag values.yaml sets. The repo matches exactly, or by
its last path segment when that segment is unique among the chart's images,
so a README may drop the registry prefix.

Not covered, on purpose: a token whose repo the chart does not declare. That
is either not an image at all (`0:0`, `reroute_dropped:false`) or another
component's image (`da-tools:v2.9.0` in vector's README), whose tag this chart
does not own. Digests are not compared.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

_REPO = Path(__file__).resolve().parent.parent.parent
_IMAGE_TOKEN = re.compile(r"`([a-z0-9][a-z0-9./_-]*):([A-Za-z0-9][A-Za-z0-9._-]*)`")


def _declared_images(node, out: dict[str, str]) -> dict[str, str]:
    if isinstance(node, dict):
        if isinstance(node.get("repository"), str) and "tag" in node:
            out[node["repository"]] = str(node["tag"])
        for v in node.values():
            _declared_images(v, out)
    elif isinstance(node, list):
        for v in node:
            _declared_images(v, out)
    return out


def _match(repo: str, images: dict[str, str]) -> str | None:
    """The declared repository a README token names, or None."""
    if repo in images:
        return repo
    tail = repo.rsplit("/", 1)[-1]
    same_tail = [r for r in images if r.rsplit("/", 1)[-1] == tail]
    return same_tail[0] if len(same_tail) == 1 else None


def _mentions():
    for readme in sorted(_REPO.glob("helm/*/README.md")):
        values_file = readme.parent / "values.yaml"
        if not values_file.exists():
            continue
        images = _declared_images(yaml.safe_load(values_file.read_text(encoding="utf-8")) or {}, {})
        for line_no, line in enumerate(readme.read_text(encoding="utf-8").splitlines(), 1):
            for repo, tag in _IMAGE_TOKEN.findall(line):
                declared = _match(repo, images)
                if declared:
                    yield pytest.param(readme, line_no, repo, tag, declared, images[declared],
                                       id=f"{readme.parent.name}:L{line_no}")


_MENTIONS = list(_mentions())


def test_the_scan_finds_the_mentions():
    # Vacuity guard: a regex or values-walk change that stops matching would
    # leave zero parametrized cases and a green run.
    charts = {m.values[0].parent.name for m in _MENTIONS}
    assert {"federation-gateway", "vector", "victorialogs"} <= charts, charts


@pytest.mark.parametrize("readme, line_no, repo, tag, declared, pinned", _MENTIONS)
def test_every_named_image_tag_matches_values(readme, line_no, repo, tag, declared, pinned):
    assert tag == pinned, (
        f"{readme.relative_to(_REPO)}:{line_no} names `{repo}:{tag}` but "
        f"{readme.parent.name}/values.yaml pins {declared}:{pinned}"
    )
