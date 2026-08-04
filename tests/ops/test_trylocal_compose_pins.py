#!/usr/bin/env python3
"""Guard: the try-local demo stack must not reference a floating image tag (#1337 ①).

`try-local/` is not a toy. `README.md` points prospective customers at it as the
one-command way to see the product, and `.github/workflows/try-local-smoke.yaml`
runs the whole stack nightly. A floating tag there moves under both of them with
no change on our side.

⛔ SCOPE — this checks SHAPE (floating vs concrete), and deliberately nothing else:

* **Not version parity with the deploy tree.** try-local pins some first-party
  images to an OLDER release ON PURPOSE, and the compose file says so in three
  places (the `.monitoring` network alias and the portal healthcheck override
  both exist *because* `PORTAL_TAG` is still v2.8.0, and tenant-api builds from
  source because `--dev-bypass-auth` ships in no published image yet). A parity
  assertion would go red on arrival and the only way to green it would be a
  coupled three-part edit this file cannot see. Deliberately not asserted.
* **Not currency.** A concrete tag that is two years stale passes here. Nothing
  offline can tell the difference; that needs a registry round-trip.
* **Not CVE coverage.** try-local images are deliberately out of the nightly
  scan matrix — that matrix is set-equal to the *deployed* ref set
  (`tests/ops/test_nightly_scan_matrix_drift.py`), so enrolling a demo-only
  image would require first declaring it deployed.

What it does buy: `:latest` (and friends) is the one thing that makes every
other mechanism — a human reading the diff, a registry pull, any future
updater — unable to say what actually ran. That is worth a gate on its own.
"""
from __future__ import annotations

import os
import re
import sys

import pytest
import yaml

TESTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_ROOT = os.path.dirname(TESTS_DIR)
COMPOSE = os.path.join(REPO_ROOT, "try-local", "docker-compose.yaml")
ENV_EXAMPLE = os.path.join(REPO_ROOT, "try-local", ".env.example")

# Tags that name a moving target rather than a release. Kept small on purpose:
# the tagless case below is handled structurally, not by adding to this list.
FLOATING_TAGS = frozenset({"latest", "main", "master", "edge", "stable",
                           "nightly", "dev", "devel", "head", "canary"})

# `${VAR:-default}` / `${VAR-default}` — compose's own shell-style substitution.
_SUBST = re.compile(r"^\$\{[A-Za-z_][A-Za-z0-9_]*:?-(?P<default>[^}]*)\}$")


def _load_services() -> dict:
    with open(COMPOSE, encoding="utf-8") as fh:
        return (yaml.safe_load(fh) or {}).get("services", {})


def _effective_ref(image: str) -> str:
    """The ref a plain `docker compose up` would actually resolve.

    A bare `${VAR}` with no default is returned unchanged so it fails the
    concreteness check below — an unset variable resolves to an empty image
    name, which is worse than a floating tag, not better.
    """
    m = _SUBST.match(image.strip())
    return m.group("default").strip() if m else image.strip()


def _tag_of(ref: str) -> str | None:
    """The tag portion, or None when the ref names no tag at all.

    Splits on the LAST colon after the final `/` so a registry port
    (`registry:5000/x`) is not mistaken for a tag. A digest pin
    (`repo:tag@sha256:...`) keeps its tag.
    """
    ref = ref.split("@", 1)[0]
    last_segment = ref.rsplit("/", 1)[-1]
    return last_segment.rsplit(":", 1)[1] if ":" in last_segment else None


def _pulled_services() -> dict:
    """Services whose image is PULLED — i.e. excluding locally-built ones.

    The `build:` key is the discriminator, not a name pattern: try-local's two
    built services also carry an `image:` (as the local build tag), so keying on
    "has an image" would demand a registry tag for something never pushed.
    """
    return {n: s for n, s in _load_services().items()
            if s.get("image") and "build" not in s}


def test_compose_is_parseable_and_not_empty():
    """Anti-vacuity floor, derived independently of the YAML walk.

    Counts raw `image:` lines in the text and requires the parsed service map to
    account for all of them. A future refactor that moves images somewhere the
    walk cannot see (an `extends:`, an override file) fails here rather than
    silently shrinking every assertion below to a no-op.
    """
    with open(COMPOSE, encoding="utf-8") as fh:
        raw_image_lines = [ln for ln in fh if re.match(r"^\s*image:\s", ln)]
    parsed = [s for s in _load_services().values() if s.get("image")]
    assert len(raw_image_lines) >= 8, "compose shrank unexpectedly — check the path"
    assert len(parsed) == len(raw_image_lines), (
        f"{len(raw_image_lines)} `image:` lines in the file but the parsed service "
        f"map only accounts for {len(parsed)} — an image moved somewhere this "
        f"guard cannot see")


@pytest.mark.parametrize("service", sorted(_pulled_services()))
def test_pulled_image_names_a_concrete_tag(service):
    """Every pulled image must name a tag, and it must not be a floating one."""
    ref = _effective_ref(_pulled_services()[service]["image"])
    tag = _tag_of(ref)
    assert tag is not None, (
        f"{service}: {ref!r} names no tag — Docker resolves that to `:latest`, "
        f"so the omission is a floating reference written a different way")
    assert tag not in FLOATING_TAGS, (
        f"{service}: {ref!r} uses the floating tag {tag!r}. Pin a released "
        f"version; add a digest too if upstream re-pushes the version tag "
        f"(alpine/git does — see .github/workflows/nightly-image-scan.yaml)")


def test_env_example_tags_are_concrete():
    """`.env.example` is the operative value, not the compose default.

    `try-local-smoke.yaml` does `cp .env.example .env` before `compose up`, so a
    floating tag here beats every `${VAR:-default}` in the compose file. Asserting
    only the compose defaults would check the branch that never runs in CI.
    """
    with open(ENV_EXAMPLE, encoding="utf-8") as fh:
        pairs = dict(re.findall(r"^([A-Z_]+)=(\S+)", fh.read(), re.MULTILINE))
    tags = {k: v for k, v in pairs.items() if k.endswith("_TAG")}
    assert len(tags) >= 3, f"expected several *_TAG knobs, found {sorted(tags)}"
    bad = {k: v for k, v in tags.items() if v.lstrip("v") .split("@")[0] in FLOATING_TAGS
           or v in FLOATING_TAGS}
    assert not bad, f".env.example carries floating tag(s): {bad}"


def test_the_detector_actually_detects():
    """Positive AND negative control for the two helpers above.

    Without this, a `_tag_of` that returned None for everything, or a regex that
    matched nothing, would make every assertion above pass vacuously.
    """
    # must be flagged
    assert _tag_of("alpine/git") is None                       # tagless
    assert _tag_of("curlimages/curl:latest") == "latest"
    assert _effective_ref("${PORTAL_TAG:-ghcr.io/x/y:latest}") == "ghcr.io/x/y:latest"
    # the two helpers must COMPOSE — a substituted default is what gets tagged
    assert _tag_of(_effective_ref("${FOO:-bar/baz:latest}")) == "latest"
    # must NOT be flagged
    assert _tag_of("prom/prometheus:v3.11.2") == "v3.11.2"
    assert _tag_of("alpine/git:v2.54.0@sha256:" + "0" * 64) == "v2.54.0"
    assert _tag_of("registry.local:5000/team/app:1.2.3") == "1.2.3", \
        "a registry port must not be mistaken for a tag"
    # a bare ${VAR} with no default stays unresolved so it fails concreteness
    assert _tag_of(_effective_ref("${UNSET_VAR}")) is None
