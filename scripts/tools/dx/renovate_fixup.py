#!/usr/bin/env python3
"""Finish a Renovate third-party image PR: write the follow-on edits it cannot (#3799).

Renovate's "third-party container images" group PR bumps the deploy refs
(helm/, k8s/, the nightly scan matrix) and the da-read go.mod requires. Several
copies that guards bind to those refs are out of its reach, so the PR opens red
until someone writes them by hand (#3713 was the last instance):

  1. components/da-read/go.sum — the go.mod requires are moved by a regex
     manager, which never runs `go mod tidy`; da-read then fails to build and
     takes most of CI down with it.
  2. tests/oauth2-proxy/go.mod — the oauth2-proxy library must be the version
     of the oauth2-proxy image the platform deploys (TestLibraryVersionIsTheChartImage); tests/**
     is in Renovate's ignorePaths.
  3. tests/federation-e2e/docker-compose.yml — tag-only mirrors of deployed
     images (tests/ops/test_federation_e2e_mirrors_deploy_sot.py); same reason.
     try-local/docker-compose.yaml — every third-party image it pulls must be
     a `repo:tag` the nightly scan matrix carries
     (test_nightly_scan_matrix_drift.py); no Renovate manager reads compose.
  4. docs/assets/platform-data.json, the portal's hand-kept IMAGE_MIRROR in
     _common/data/images.js (images-fallback-drift.test.ts) and the committed
     docs/assets/dist bundles that embed it.

The deployed refs are read the way platform-data and the nightly scan read
them: check_image_refs_resolve.discover_refs(), through da-crdecode
(DA_CRDECODE_BINARY; `make renovate-fixup` builds .build/da-crdecode).

This program writes 1-4 from the refs already on the branch, in that order
(platform-data needs a da-read that builds). It does NOT fix an upstream API
change that the bump exposes (prometheus v0.316.0 dropping a parser option was
#3713's): once 1 is done such a change shows up as an ordinary compile error,
which is the point of running this first.

    python renovate_fixup.py              # write the edits
    python renovate_fixup.py --dry-run    # print what 2, 3 and 4's mirror would change; run nothing

Exit codes: 0 done (or nothing to change) · 2 a step could not run or its
input could not be read (no go / make on PATH, a step failed, a source file
missing or not in the shape this program reads).
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_THIS_DIR))
import _lib_compat  # noqa: E402,F401  (import-time side effect; see _lib_compat)
from _lib_compat import require_project_root  # noqa: E402  (#2852)

DA_READ_DIR = "components/da-read"
PROXY_LIB_TESTS_DIR = "tests/oauth2-proxy"
PROXY_LIB_MODULE = "github.com/oauth2-proxy/oauth2-proxy/v7"
PROXY_IMAGE_REPO = "quay.io/oauth2-proxy/oauth2-proxy"
IMAGE_REFS_EXTRACTOR = "scripts/ops/check_image_refs_resolve.py"
FEDERATION_COMPOSE = "tests/federation-e2e/docker-compose.yml"
TRY_LOCAL_COMPOSE = "try-local/docker-compose.yaml"
SCAN_WORKFLOW = ".github/workflows/nightly-image-scan.yaml"
# First-party images are the release flow's, and try-local holds them at an
# older release on purpose (the scan guard skips the same prefix).
_FIRST_PARTY_PREFIX = "ghcr.io/vencil/"
PLATFORM_DATA = "docs/assets/platform-data.json"
IMAGES_JS = "tools/portal/src/interactive/tools/_common/data/images.js"

# One `go` invocation can download a module graph; a stuck proxy must not hang
# the run forever.
_GO_TIMEOUT_S = 600
# platform-data builds da-guard / da-read / da-crdecode first; portal-build may
# run `npm ci`.
_MAKE_TIMEOUT_S = 1800

_PROXY_LIB_REQUIRE = re.compile(
    r"(?m)^(?:require\s+|\t)" + re.escape(PROXY_LIB_MODULE) + r" (v\S+)$")


class FixupError(Exception):
    """A step could not run or its input is not what this program reads: exit 2."""


def _repo_root() -> Path:
    """This tree's root, never an enclosing project's (#2852)."""
    return require_project_root(_THIS_DIR)


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise FixupError(f"cannot read {path}: {exc}") from exc


def _yaml(path: Path):
    try:
        return yaml.safe_load(_read(path))
    except yaml.YAMLError as exc:
        raise FixupError(f"{path}: not YAML: {exc}") from exc


# ── deploy SoT ──────────────────────────────────────────────────────────────

def deployed_tags(repo: Path) -> dict[str, str]:
    """Image repo -> tag the platform deploys (digest dropped).

    The deploy refs are read by check_image_refs_resolve.discover_refs(), as
    kubectl / Helm read them (da-crdecode, ADR-036 §12) — the same reading
    platform-data's wizard images and the nightly scan matrix are bound to.
    A repo deployed at two tags is refused: no mirror can follow both.
    """
    path = repo / IMAGE_REFS_EXTRACTOR
    spec = importlib.util.spec_from_file_location("_check_image_refs_resolve", path)
    if spec is None or spec.loader is None:
        raise FixupError(f"cannot load {IMAGE_REFS_EXTRACTOR}")
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
        refs = mod.discover_refs(repo)
    except SystemExit as exc:  # the extractor ends at rc 2 when da-crdecode cannot read a source
        raise FixupError(f"{IMAGE_REFS_EXTRACTOR} could not read the deploy refs "
                         "(its message is above; `make renovate-fixup` builds da-crdecode)") from exc
    out: dict[str, str] = {}
    for ref in sorted(refs):
        image, _, tag = ref.split("@", 1)[0].rpartition(":")
        if image in out and out[image] != tag:
            raise FixupError(f"{image} is deployed at two tags ({out[image]}, {tag}); "
                             "reconcile helm/ and k8s/ first")
        out[image] = tag
    return out


def proxy_chart_tag(deployed: dict[str, str]) -> str:
    """The oauth2-proxy image tag the platform deploys (tenant-api chart)."""
    if PROXY_IMAGE_REPO not in deployed:
        raise FixupError(f"{PROXY_IMAGE_REPO} is not among the deployed images")
    return deployed[PROXY_IMAGE_REPO]


def proxy_library_version(go_mod: str) -> str:
    """The oauth2-proxy version tests/oauth2-proxy/go.mod requires directly."""
    m = _PROXY_LIB_REQUIRE.search(go_mod)
    if m is None:
        raise FixupError(f"{PROXY_LIB_TESTS_DIR}/go.mod: no direct require of {PROXY_LIB_MODULE}")
    return m.group(1)


def federation_plan(compose: dict, deployed: dict[str, str]) -> dict[str, tuple[str, str]]:
    """federation-e2e service -> (old ref, new ref) for each image the platform
    also deploys, at a tag other than the deployed one. Images the platform does
    not deploy (test doubles) are left alone. The refs stay tag-only.
    """
    plan: dict[str, tuple[str, str]] = {}
    for name, svc in (compose.get("services") or {}).items():
        if not isinstance(svc, dict) or not isinstance(svc.get("image"), str):
            continue
        ref = svc["image"].strip().split("@", 1)[0]
        image, _, tag = ref.rpartition(":")
        if image in deployed and deployed[image] != tag:
            plan[name] = (ref, f"{image}:{deployed[image]}")
    return plan


# ── pure rewrites ───────────────────────────────────────────────────────────

def set_compose_image(text: str, service: str, image: str,
                      label: str = FEDERATION_COMPOSE) -> tuple[str, str]:
    """Rewrite `image:` of one compose service, comments untouched.

    Returns (new text, old image). Text-level on purpose: a YAML round trip
    would drop the file's comments, which carry their rationale. The service
    is looked up inside the top-level `services:` block only, so a volume or
    network of the same name is never touched.
    """
    lines = text.splitlines(keepends=True)
    in_services = False
    start = None
    for i, line in enumerate(lines):
        if re.match(r"^\S", line):
            in_services = line.rstrip() == "services:"
            continue
        if in_services and re.match(rf"^  {re.escape(service)}:\s*(#.*)?$", line):
            start = i
            break
    if start is None:
        raise FixupError(f"{label}: no service {service!r}")
    for j in range(start + 1, len(lines)):
        line = lines[j]
        if re.match(r"^ {0,2}\S", line):  # next service, or the next top-level key
            break
        m = re.match(r"^(\s+image:\s*)([\"']?)([^\"'\s#]+)\2(\s*(#.*)?\n?)$", line)
        if m and len(m.group(1)) - len(m.group(1).lstrip()) == 4:
            old = m.group(3)
            lines[j] = f"{m.group(1)}{m.group(2)}{image}{m.group(2)}{m.group(4)}"
            return "".join(lines), old
    raise FixupError(f"{label}: service {service!r} has no image: line")


def scan_matrix_tags(workflow: dict) -> dict[str, set[str]]:
    """repo -> the tags the scan-thirdparty matrix carries for it (digest dropped)."""
    try:
        include = workflow["jobs"]["scan-thirdparty"]["strategy"]["matrix"]["include"]
        refs = [str(e["ref"]).split("@", 1)[0] for e in include]
    except (KeyError, TypeError) as exc:
        raise FixupError(f"{SCAN_WORKFLOW}: no scan-thirdparty matrix include refs") from exc
    tags: dict[str, set[str]] = {}
    for ref in refs:
        repo, _, tag = ref.rpartition(":")
        tags.setdefault(repo, set()).add(tag)
    return tags


def trylocal_plan(compose: dict, matrix: dict[str, set[str]]) -> dict[str, tuple[str, str]]:
    """try-local service -> (old ref, new ref) for each pulled third-party image
    whose `repo:tag` the scan matrix lacks, moved to the matrix's one tag for
    that repo.

    No matrix row for the repo, or several tags for it, is a choice this program
    does not make (the guard's other fix is a matrix row for the old tag): error.
    """
    plan: dict[str, tuple[str, str]] = {}
    for name, svc in (compose.get("services") or {}).items():
        if not isinstance(svc, dict) or "build" in svc or not isinstance(svc.get("image"), str):
            continue
        ref = svc["image"].strip().split("@", 1)[0]
        if ref.startswith(_FIRST_PARTY_PREFIX):
            continue
        repo, _, tag = ref.rpartition(":")
        if tag in matrix.get(repo, set()):
            continue
        if "${" in ref:
            raise FixupError(f"{TRY_LOCAL_COMPOSE}: {name}: {ref!r} is not in the scan "
                             "matrix and is written with ${...}; edit it by hand")
        candidates = sorted(matrix.get(repo, set()))
        if len(candidates) != 1:
            raise FixupError(f"{TRY_LOCAL_COMPOSE}: {name}: {ref!r} is not in the scan "
                             f"matrix, which carries {candidates or 'no'} tag(s) for {repo}; "
                             "align it or add a matrix row by hand")
        plan[name] = (ref, f"{repo}:{candidates[0]}")
    return plan


_MIRROR_BLOCK = re.compile(r"(const IMAGE_MIRROR = \{\n)(.*?)(\n\};)", re.S)
_MIRROR_ENTRY = re.compile(r"^(\s+)([A-Za-z0-9_]+): '([^']*)',$")


def set_image_mirror(text: str, images: dict[str, str]) -> tuple[str, dict[str, tuple[str, str]]]:
    """Rewrite IMAGE_MIRROR's values from platform-data's `images`.

    Returns (new text, {key: (old, new)} for the keys that changed). The two
    must carry the same keys: a key on one side only is a wizard change this
    program does not guess at.
    """
    m = _MIRROR_BLOCK.search(text)
    if m is None:
        raise FixupError(f"{IMAGES_JS}: no `const IMAGE_MIRROR = {{ ... }};` block")
    body = m.group(2).split("\n")
    seen: set[str] = set()
    changed: dict[str, tuple[str, str]] = {}
    for i, line in enumerate(body):
        e = _MIRROR_ENTRY.match(line)
        if e is None:
            raise FixupError(f"{IMAGES_JS}: IMAGE_MIRROR line not `key: 'ref',`: {line!r}")
        key, old = e.group(2), e.group(3)
        seen.add(key)
        if key not in images:
            raise FixupError(f"{IMAGES_JS}: IMAGE_MIRROR key {key!r} is not in {PLATFORM_DATA}")
        if images[key] != old:
            body[i] = f"{e.group(1)}{key}: '{images[key]}',"
            changed[key] = (old, images[key])
    missing = sorted(set(images) - seen)
    if missing:
        raise FixupError(f"{IMAGES_JS}: IMAGE_MIRROR lacks {missing} that {PLATFORM_DATA} carries")
    return text[:m.start(2)] + "\n".join(body) + text[m.end(2):], changed


# ── steps ───────────────────────────────────────────────────────────────────

def _run(cmd: list[str], cwd: Path, timeout: int) -> None:
    print(f"RUN:  ({cwd.name}) {' '.join(cmd)}", flush=True)
    try:
        proc = subprocess.run(cmd, cwd=cwd, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise FixupError(f"`{' '.join(cmd)}` timed out after {timeout}s") from exc
    except OSError as exc:
        raise FixupError(f"`{' '.join(cmd)}` could not start: {exc}") from exc
    if proc.returncode != 0:
        raise FixupError(f"`{' '.join(cmd)}` exited {proc.returncode}")


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8", newline="\n")


def fix_proxy_library(repo: Path, deployed: dict[str, str], dry_run: bool) -> None:
    want = proxy_chart_tag(deployed)
    have = proxy_library_version(_read(repo / PROXY_LIB_TESTS_DIR / "go.mod"))
    if have == want:
        print(f"OK:   {PROXY_LIB_TESTS_DIR}: {PROXY_LIB_MODULE} {have} is the chart's tag")
        return
    print(f"PLAN: {PROXY_LIB_TESTS_DIR}: {PROXY_LIB_MODULE} {have} -> {want}")
    if not dry_run:
        _run(["go", "get", f"{PROXY_LIB_MODULE}@{want}"], repo / PROXY_LIB_TESTS_DIR, _GO_TIMEOUT_S)
        _run(["go", "mod", "tidy"], repo / PROXY_LIB_TESTS_DIR, _GO_TIMEOUT_S)


def fix_federation_compose(repo: Path, deployed: dict[str, str], dry_run: bool) -> None:
    path = repo / FEDERATION_COMPOSE
    text = _read(path)
    plan = federation_plan(_yaml(path) or {}, deployed)
    for service, (old, new) in plan.items():
        text, _ = set_compose_image(text, service, new)
        print(f"PLAN: {FEDERATION_COMPOSE}: {service} {old} -> {new}")
    if plan and not dry_run:
        _write(path, text)


def fix_trylocal_compose(repo: Path, dry_run: bool) -> None:
    path = repo / TRY_LOCAL_COMPOSE
    text = _read(path)
    plan = trylocal_plan(_yaml(path) or {}, scan_matrix_tags(_yaml(repo / SCAN_WORKFLOW)))
    for service, (old, new) in plan.items():
        text, _ = set_compose_image(text, service, new, TRY_LOCAL_COMPOSE)
        print(f"PLAN: {TRY_LOCAL_COMPOSE}: {service} {old} -> {new}")
    if plan and not dry_run:
        _write(path, text)


def fix_image_mirror(repo: Path, dry_run: bool) -> None:
    try:
        images = json.loads(_read(repo / PLATFORM_DATA))["images"]
    except (ValueError, KeyError, TypeError) as exc:
        raise FixupError(f"{PLATFORM_DATA}: no readable `images` map: {exc}") from exc
    path = repo / IMAGES_JS
    text, changed = set_image_mirror(_read(path), images)
    for key, (old, new) in changed.items():
        print(f"PLAN: {IMAGES_JS}: {key} {old} -> {new}")
    if changed and not dry_run:
        _write(path, text)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Write the follow-on edits a Renovate third-party image PR cannot (#3799)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the edits read from files already on disk; run no go/make "
                             "(the IMAGE_MIRROR plan is against the CURRENT platform-data.json)")
    args = parser.parse_args()

    repo = _repo_root()
    if not args.dry_run:
        for tool in ("go", "make"):
            if shutil.which(tool) is None:
                print(f"ERROR: {tool} is not on PATH; run this in the dev container "
                      "(`make dc-run CMD=\"make renovate-fixup\"`).", file=sys.stderr)
                return 2
    try:
        print(f"STEP 1/5: {DA_READ_DIR} go mod tidy")
        if not args.dry_run:
            _run(["go", "mod", "tidy"], repo / DA_READ_DIR, _GO_TIMEOUT_S)
        deployed = deployed_tags(repo)
        print("STEP 2/5: oauth2-proxy library = deployed oauth2-proxy image")
        fix_proxy_library(repo, deployed, args.dry_run)
        print("STEP 3/5: federation-e2e compose = deployed images; try-local = scan matrix tags")
        fix_federation_compose(repo, deployed, args.dry_run)
        fix_trylocal_compose(repo, args.dry_run)
        print("STEP 4/5: make platform-data")
        if not args.dry_run:
            _run(["make", "platform-data"], repo, _MAKE_TIMEOUT_S)
        print("STEP 5/5: portal IMAGE_MIRROR = platform-data images, then make portal-build")
        fix_image_mirror(repo, args.dry_run)
        if not args.dry_run:
            _run(["make", "portal-build"], repo, _MAKE_TIMEOUT_S)
    except FixupError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print("DONE: review `git diff`, then commit. A compile error left after this is an "
          "upstream API change in the bumped module: fix it by hand." if not args.dry_run
          else "DONE (dry run): nothing was written.")
    return 0


if __name__ == "__main__":
    import _lib_exitcodes
    _lib_exitcodes.run(main)
