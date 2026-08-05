#!/usr/bin/env python3
"""Guard: the try-local demo stack must reference images by a pinned release (#1337 ①).

`try-local/` is not a toy. `README.md` points prospective customers at it as the
one-command way to see the product, and `.github/workflows/try-local-smoke.yaml`
runs the whole stack nightly. A reference that moves under both of them means
nobody can say what the demo actually ran.

⛔ THE RULE IS DERIVED, NOT ENUMERATED. The first version of this module asked
"is the tag one of {latest, main, master, edge, stable, …}?" — a denylist, and
denylists of syntax always have an n+1 member. Measured on the real file:
`alpine/git:v2`, `curlimages/curl:8` and `grafana/grafana:12.4` all passed, and
all three are tags upstream re-publishes on every patch. This version instead
asks a question with a derivable answer: **does this reference name one
immutable thing?** Two ways to satisfy it — a digest, or a tag carrying a
MAJOR.MINOR.PATCH release triple. Everything else fails without needing to be
listed, including shapes nobody has thought of yet.

⛔ SCOPE — deliberately not checked, do not read a green run as more than it is:

* **Not version parity with the deploy tree.** try-local holds its first-party
  images at an older release on purpose: `try-local/docker-compose.yaml:84-90`
  keeps a `.monitoring` network alias that only the pinned v2.8.0 portal image
  needs, so bumping `PORTAL_TAG` without deleting the alias is a coupled edit
  this module cannot see.
  ⚠️ Do NOT cite the other two rationale comments in that file as support — both
  went stale and are corrected in this change: `--dev-bypass-auth` DOES now ship
  in a published image (`tenant-api/v2.9.7`+), and the portal healthcheck
  override's stated trigger was fixed in `portal/v2.9.0`. Whether try-local
  should therefore stop building tenant-api from source is a real question and
  deliberately NOT settled here — it changes what the demo runs, not how it is
  pinned.
* **Not currency.** A release triple from two years ago passes. Nothing offline
  can tell the difference; that needs a registry round-trip.
* **Not CVE coverage.** try-local images are outside the nightly scan matrix.
  ⚠️ Not because they are undeployable — `test_nightly_scan_matrix_drift.py`'s
  `_DEPLOYABLE_TREES` already lists `try-local` ("installed on a cluster or
  handed to a customer"). The reason is mechanical: the matrix is set-equal to
  what `scripts/ops/check_image_refs_resolve.py` extracts, and its `SOURCE_GLOBS`
  cover only `helm/*/values*.yaml` and `k8s/**`.
* **Not an update path.** Nothing bumps these refs — `try-local/**` matches no
  Renovate `managerFilePatterns`. That is why the pins here are release TAGS and
  not digests: a digest with no updater cannot inherit an upstream rebuild, so
  it would freeze the demo on a known-vulnerable layer permanently, while a
  release tag at least tracks the publisher's own rebuilds of that release.
  ⛔ The opposite choice is correct on the deploy surface (`k8s/**`, `helm/**`),
  which IS Renovate-managed — same reasoning as #1345, applied to whoever owns
  the bump loop.
"""
from __future__ import annotations

import os
import re

import pytest
import yaml

TESTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_ROOT = os.path.dirname(TESTS_DIR)
TRY_LOCAL = os.path.join(REPO_ROOT, "try-local")
COMPOSE = os.path.join(TRY_LOCAL, "docker-compose.yaml")
ENV_EXAMPLE = os.path.join(TRY_LOCAL, ".env.example")

# Services that build from source. Named explicitly so that adding `build:` to
# an existing service fails LOUDLY instead of quietly dropping it from the
# parametrized checks below — measured: without this, adding `build:` to
# `seed-metrics` while leaving `image: curlimages/curl:latest` produced one
# fewer test case and zero failures.
BUILT_FROM_SOURCE = frozenset({"tenant-api", "recipe-preview"})

# A pinned release: MAJOR.MINOR.PATCH, optional `v`, optional suffix
# (`-alpine`, `-distroless-libc`, …). Rejects `latest`, `v2`, `8`, `12.4`,
# `alpine`, `` and anything else that names a moving target — by shape, not by
# membership in a list.
_RELEASE_TAG = re.compile(r"^v?\d+\.\d+\.\d+(?:[-.+][\w.\-+]*)?$")

# ⛔ Escape hatch, fail-closed: an upstream that genuinely publishes no release
# triple (e.g. `ubuntu:24.04`). Each entry needs a reason, and the entry is
# keyed by SERVICE so it cannot silently widen to other images.
_TAG_SHAPE_EXEMPT: dict[str, str] = {}

# ⛔ There is deliberately NO denylist constant here. An earlier draft kept one
# "as defence in depth" while referencing it nowhere — a dead constant that reads
# like a second check and is not one. If the shape rule ever needs help, wire the
# helper in and test it; do not park a list next to it.

# `${VAR:-default}` / `${VAR-default}`, anywhere in the string. NOT anchored:
# compose interpolates mid-value, and `repo:${TAG:-latest}` is the likelier
# shape than a whole-value substitution.
_SUBST = re.compile(r"\$\{(?P<name>[A-Za-z_]\w*)(?::?-(?P<default>[^}]*))?\}")


def _compose() -> dict:
    with open(COMPOSE, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _services() -> dict:
    return _compose().get("services", {})


def _imaged() -> dict:
    return {n: s for n, s in _services().items() if s.get("image")}


def _pulled() -> dict:
    return {n: s for n, s in _imaged().items() if n not in BUILT_FROM_SOURCE}


def _effective_ref(image: str) -> str:
    """What `docker compose up` resolves, substituting every interpolation.

    A `${VAR}` with no default has nothing to substitute and survives, so the
    caller sees a leftover `$` and treats the ref as unresolved.
    """
    return _SUBST.sub(lambda m: (m.group("default") or "${%s}" % m.group("name")).strip(),
                      image.strip())


def _classify(image: str) -> str:
    """The single decision every check below shares.

    Returned states: `unresolved` | `digest` | `release` | `moving`.
    ⛔ The parametrized test and the control test MUST both call this. The
    previous version had the control test re-implement the chain, so deleting
    the real assertion left the control green.
    """
    ref = _effective_ref(image)
    if "$" in ref:
        return "unresolved"
    if re.search(r"@sha256:[0-9a-f]{64}$", ref):
        return "digest"
    name = ref.rsplit("/", 1)[-1]
    tag = name.rsplit(":", 1)[1] if ":" in name else ""
    return "release" if _RELEASE_TAG.match(tag) else "moving"


def _interpolated_names(image: str) -> set[str]:
    return {m.group("name") for m in _SUBST.finditer(image)}


# ── structural guards: the checks below must actually have subjects ──────────

def test_no_override_or_include_can_smuggle_images_past_this_module():
    """`docker compose up` auto-merges an override file and follows `include:`.

    ⛔ The previous floor compared two views of the SAME file, so moving services
    into `docker-compose.override.yaml` decremented both sides and passed — while
    `try-local-smoke.yaml` runs a bare `docker compose up -d`, which merges it.
    """
    strays = sorted(
        fn for fn in os.listdir(TRY_LOCAL)
        if re.fullmatch(r"(docker-)?compose\.override\.ya?ml", fn)
        or (re.fullmatch(r"(docker-)?compose.*\.ya?ml", fn)
            and fn != os.path.basename(COMPOSE))
    )
    assert not strays, (
        f"compose file(s) this guard does not read: {strays}. `docker compose "
        f"up` merges an override automatically, so images there would ship "
        f"unchecked — either fold them in or teach this module to read them")
    assert "include" not in _compose(), (
        "compose `include:` pulls in services from another file, adding zero "
        "`image:` lines here — every check in this module would stay green")


def test_every_image_bearing_service_is_classified():
    """Adding `build:` must be loud, not a silent exit from the parametrize.

    Also floors the number of PULLED services independently of the parametrize
    source: with zero parameter sets pytest marks the test **skipped** and the
    run still exits 0, so "no failures" would not mean "nothing floating".
    """
    imaged, pulled = set(_imaged()), set(_pulled())
    assert BUILT_FROM_SOURCE <= imaged, (
        f"BUILT_FROM_SOURCE names service(s) that no longer exist: "
        f"{sorted(BUILT_FROM_SOURCE - imaged)}")
    # ⛔ Compare the LEDGER against the compose file's actual `build:` keys.
    # Comparing `imaged - pulled` to the ledger would be a TAUTOLOGY — `_pulled()`
    # is *defined* by subtracting the ledger, so those two sides can never
    # disagree. (I wrote that version first; mutating the file proved it stayed
    # green while a service gained `build:`.)
    actually_built = {n for n, s in _services().items() if "build" in s}
    assert actually_built == BUILT_FROM_SOURCE, (
        f"`build:` keys and BUILT_FROM_SOURCE disagree — "
        f"gained: {sorted(actually_built - BUILT_FROM_SOURCE)}, "
        f"lost: {sorted(BUILT_FROM_SOURCE - actually_built)}. A service that "
        f"starts building from source still needs a decision about the registry "
        f"ref it leaves behind in `image:`")
    assert len(pulled) >= 8, (
        f"only {len(pulled)} pulled service(s) — the parametrized check below "
        f"has lost most of its subjects")


# ── the actual rule ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("service", sorted(_pulled()))
def test_pulled_image_names_one_immutable_thing(service):
    image = _pulled()[service]["image"]
    verdict = _classify(image)
    if service in _TAG_SHAPE_EXEMPT:
        pytest.skip(f"exempt: {_TAG_SHAPE_EXEMPT[service]}")
    assert verdict in ("release", "digest"), (
        f"{service}: {image!r} resolves to {_effective_ref(image)!r} which is "
        f"{verdict!r}. Pin a MAJOR.MINOR.PATCH release (or a digest). ⚠️ `:v2`, "
        f"`:8` and `:12.4` are NOT pins — upstream re-publishes them on every "
        f"patch.")


def test_env_example_supplies_every_variable_the_images_interpolate():
    """Bind the env file to what compose actually reads, not to a name suffix.

    `try-local-smoke.yaml:26` does `cp .env.example .env` before `compose up`,
    so `.env.example` is the operative value. ⛔ The previous version keyed on
    `k.endswith("_TAG")`, so `CURL_VERSION=latest` sailed through — and its floor
    was satisfiable by two knobs no compose service reads.
    """
    with open(ENV_EXAMPLE, encoding="utf-8") as fh:
        env = dict(re.findall(r"^([A-Za-z_]\w*)=(\S*)", fh.read(), re.MULTILINE))
    used = {n for s in _imaged().values() for n in _interpolated_names(s["image"])}
    assert used, "no image interpolates a variable — this check has no subject"
    for name in sorted(used):
        assert name in env, (
            f"{name} is interpolated into an image but absent from "
            f".env.example, which CI copies to .env verbatim")
        ref_with_env = f"repo/x:{env[name]}"
        assert _classify(ref_with_env) in ("release", "digest"), (
            f".env.example sets {name}={env[name]!r}, which is not a pinned "
            f"release — CI copies this file and it wins over the compose default")


def test_the_classifier_actually_classifies():
    """Positive AND negative control for `_classify`, the shared decision.

    ⛔ This calls the SAME function the assertions above call. The previous
    control test re-implemented the chain, so deleting the real check left it
    green.
    """
    moving = [
        "curlimages/curl:latest", "alpine/git",            # denylisted / tagless
        "alpine/git:v2", "curlimages/curl:8",              # truncated semver
        "grafana/grafana:12.4",                            # major.minor only
        "curlimages/curl:alpine", "x/y:release", "x/y:",   # non-version / empty
        "curlimages/curl:${CURL_TAG:-latest}",             # partial interpolation
        "curlimages/curl:${CURL_TAG-latest}",
    ]
    for image in moving:
        assert _classify(image) == "moving" or _classify(image) == "unresolved", \
            f"{image!r} classified {_classify(image)!r}, expected moving"
    assert _classify("curlimages/curl:${CURL_TAG}") == "unresolved"
    assert _classify("${UNSET}") == "unresolved"

    pinned = [
        "prom/prometheus:v3.11.2", "curlimages/curl:8.21.0",
        "grafana/grafana:12.4.6", "prom/alertmanager:v0.33.1",
        "registry.local:5000/team/app:1.2.3",              # registry port
        "timberio/vector:0.57.0-distroless-libc",          # release + suffix
        "ghcr.io/x/y:${TAG:-v1.2.3}",                      # concrete default
    ]
    for image in pinned:
        assert _classify(image) == "release", \
            f"{image!r} classified {_classify(image)!r}, expected release"

    # A digest names one immutable thing — it is the STRONGEST pin, not a
    # missing tag. The previous version rejected it with the factually wrong
    # message "Docker resolves that to :latest".
    assert _classify("alpine/git@sha256:" + "0" * 64) == "digest"
    assert _classify("alpine/git:v2.54.0@sha256:" + "0" * 64) == "digest"
