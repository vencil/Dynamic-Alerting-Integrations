#!/usr/bin/env python3
"""Verify every concrete container image ref in helm values + k8s manifests
actually RESOLVES in its registry (#902 L1-B).

Motivation: #897 shipped a typo'd, non-existent tag (`mariadb:11.8.1`) in a chart
values file → fresh-deploy `ImagePullBackOff`. Nothing caught it at PR time: the
nightly scan (scan-thirdparty) resolves the refs in the WORKFLOW MATRIX, not the
ones in `values.yaml`, so a values typo (or matrix↔values drift) slips through.
This lint closes that gap — it parses the actual deployment sources and checks
each concrete ref against the registry.

Parsing (NOT grep — comments/prose would yield phantom refs): yaml.safe_load each
file and walk the tree, collecting
  * any mapping with string `repository` + non-empty `tag` -> "<registry>/<repo>:<tag>"
  * any `image:` key whose value is a "repo:tag" string
Empty / templated ({{ ... }}) tags are SKIPPED: first-party `tag: ""` resolves to
the chart appVersion (built at release, exists by construction), and Helm template
expressions aren't real refs.

Resolution: `skopeo inspect docker://<ref>` (preferred) or `docker manifest
inspect <ref>`. If NEITHER tool is available the check SKIPS (exit 0) with a loud
note — so a dev box without skopeo/docker doesn't false-fail; CI installs skopeo.

Exit: 0 = all concrete refs resolve (or resolver unavailable / nothing to check);
1 = at least one ref does not resolve (the #897 class).

Two SCOPES, deliberately separate sets (see `--scope` and `delivered_refs`):
  * `deploy`    (default) — what WE install: helm values + k8s manifests.
  * `delivered` — what a CUSTOMER installs: the third-party refs `da-tools init`
                  writes into their repo, imported from init_project.py.

There is deliberately NO `--scope all`; see the ⛔ note next to _SCOPES.

Usage:
  check_image_refs_resolve.py [--root DIR] [--list] [--timeout SECS]
                              [--scope {deploy,delivered}]
    --root    repo root to scan (default: cwd)
    --list    print the discovered concrete refs and exit 0 (no network) — for tests
    --timeout per-ref resolver timeout in seconds (default 30)
    --scope   which ref set to check (default: deploy — see the ⛔ note on
              `--list` compatibility next to _SCOPES below)
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - pyyaml is a hard dep in CI / pre-commit
    print("check_image_refs_resolve: PyYAML not installed", file=sys.stderr)
    sys.exit(2)

# Globs of the deployment sources humans hand-edit image refs into.
# ⛔ `values*.yaml`, not `values.yaml` (#1302). A `-f values-tier2.yaml` overlay is
# a DOCUMENTED deployment profile (helm/da-portal/README.md), and it can pin an
# image the base never mentions — which this extractor, and therefore the nightly
# CVE scan's matrix-drift guard, would never have seen. Two sibling gates over the
# same concern already read `values*` (check_iac_helm.py, check_image_pin_capability.py);
# this one was the odd one out, and da-portal's overlays had drifted two months
# behind the base pin underneath that asymmetry.
SOURCE_GLOBS = ("helm/*/values*.yaml", "k8s/**/*.yaml", "k8s/**/*.yml")

# Images BUILT LOCALLY by a chart's own Dockerfile and NOT published to a public
# registry (the deployer builds/loads them, or pushes to their own registry). They
# cannot be resolved against a public registry, so the resolve check would
# false-fail on them — skip by repository name. (federation-gateway audit-sidecar:
# helm/federation-gateway/audit-sidecar/Dockerfile, values repository has no host.)
#
# ⛔ SCOPE: this skip is about RESOLVING A REF, and it says nothing about CVE
# coverage. Reading it as "this image is not our problem" is exactly how
# federation-audit-sidecar shipped unscanned for months (#1337): the reasoning
# below is sound here and was silently inherited by a question it never
# answered. Both skipped classes are covered elsewhere — first-party by
# release.yaml, and the chart-shipped locally-built ones by the self-built
# `scan` matrix in nightly-image-scan.yaml (pinned to the Dockerfile inventory
# by test_selfbuilt_matrix_covers_every_dockerfile).
LOCAL_BUILT_IMAGES = {"federation-audit-sidecar"}

# First-party images live in our own registry namespace: their currency is the
# release pipeline's job, and resolving them needs ghcr auth (would false-fail an
# anonymous CI check). L1-B targets the PUBLIC third-party refs (the #897 typo
# class), so skip our own namespace here.
#
# ⛔ "the release pipeline's job" is TRUE OF FIVE IMAGES, not of the prefix.
# `ghcr.io/vencil/vector-projection-gate` matches this prefix and is published by
# nothing at all (#1337; an anonymous manifest inspect is `denied` while the five
# real ones resolve) — so for that one this skip was hiding an unpublished image
# behind a rule written for published ones. It is now covered by the self-built
# `scan` matrix instead. Same scope note as LOCAL_BUILT_IMAGES above: skipping a
# ref RESOLUTION check is not a statement about CVE coverage.
SKIP_REPO_PREFIXES = ("ghcr.io/vencil/",)

# ── The CUSTOMER-DELIVERED scan face (#1337 follow-up) ───────────────────────
# `da-tools init` writes third-party image refs into files the CUSTOMER runs:
# the GitLab apply stage (which carries `environment: name: production` plus
# cluster-write credentials) and the git-sync patch applied into their cluster.
# Those refs live in `scripts/**`, so SOURCE_GLOBS above cannot see them and
# neither can Renovate (all three of its customManagers key on `@sha256:`,
# which these deliberately do not carry). They were in NO automated view of the
# registry at all.
#
# ⛔ A SEPARATE SCOPE, not a widened SOURCE_GLOBS, and the reason is mechanical
# rather than stylistic: the nightly `scan-thirdparty` matrix is pinned to
# `--list`'s output by SET EQUALITY (test_thirdparty_matrix_equals_deployed_refs).
# Folding these four into the default output would therefore FORCE them into the
# production supply-chain scan face — a face whose whole contract is "images we
# deploy", digest-pinned and Renovate-bumped. They are neither. So `--list` with
# the default scope must stay byte-for-byte what it was; the delivered set gets
# its own scope, its own workflow step, and its own nightly bucket.
#
# ⛔ NO `all`. An earlier draft offered one and nothing ever called it — not the
# workflow (which deliberately runs two named steps so a reader can tell WHICH
# scope broke from the step name alone), not a test, not a Makefile target. An
# accepted-but-unexercised option is a third code path that only a future reader
# runs, first time, in the dark; the repo's standing rule is to delete
# speculative surface rather than carry it. If a combined run is ever genuinely
# wanted, two invocations already give it, with better failure attribution.
_SCOPES = ("deploy", "delivered")

# ⛔ IMPORTED, never transcribed. A hand-copied table here would be a fourth
# spelling of the same four refs (init_project.py, the nightly matrix, this, and
# whatever the next reader adds) — and the copy that goes stale silently is
# always the one nobody runs. The drift guard compares the nightly matrix
# against THIS import, so a ref is spelled out in exactly two places:
# init_project.py (the owner) and the nightly matrix (bound to it by that
# guard). ⛔ That count is a property to MAINTAIN, not a fact to assume — the
# drift guard's own positive samples were briefly a third copy, labelled
# "verbatim", until a coordinated bump proved they could go stale in silence.
# They now import from here instead. Anything that needs these refs imports.
# Same shape as generate_platform_data.py importing this file for the deploy SSOT.
DELIVERED_PIN_SOURCE = ("scripts", "tools", "ops", "init_project.py")


def delivered_refs(root: Path) -> set[str]:
    """Third-party refs `da-tools init` hands to a customer.

    Fail-CLOSED in both directions: a load failure raises (rather than degrading
    to "nothing to check", which reads exactly like a clean run), and an empty
    pin table is an error rather than a silent no-op.

    ⛔ EACH SOURCE IS CHECKED SEPARATELY, and that is not tidiness. The two
    sources are unioned, so a check on the UNION is unreachable as long as
    either one is non-empty: an earlier draft tested `if not refs` after
    unconditionally adding GIT_SYNC_IMAGE, which made the emptiness guard dead
    code — emptying `_GITLAB_APPLY_IMAGES` entirely still exited 0 and reported
    one clean ref. A guard over a union can only catch the case where EVERY
    source failed at once, which is the least likely one.
    """
    import importlib.util

    path = root.joinpath(*DELIVERED_PIN_SOURCE)
    spec = importlib.util.spec_from_file_location("_da_init_project", path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"check_image_refs_resolve: cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    # ⛔ The pin source is a CLI module, so executing it is not free: it does four
    # `sys.path.insert(0, …)` off its own `__file__` and never undoes them. That
    # is harmless for the CI gate (one-shot process) and NOT harmless in-process.
    # Measured: load a COPY of the source from a tmp dir and the process's
    # sys.path gains four tmp entries AT THE FRONT — a later `import
    # init_project` then resolves to that copy, and the copy is exactly the
    # MUTATED one a test wrote (empty pin table). The suite does not hit this
    # today only because the half-empty test's last line reloads the real file,
    # re-inserting the real dirs in front; that line exists to prove this
    # function is not raising unconditionally, so the containment is accidental
    # and would vanish in any reordering. Snapshot/restore instead of relying on
    # it, and drop the sys.modules entry so a failed exec leaves nothing
    # half-initialised behind.
    saved_path = list(sys.path)
    sys.modules["_da_init_project"] = mod
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.path[:] = saved_path
        sys.modules.pop("_da_init_project", None)

    # Member-level emptiness, not just container-level: `{""}` is a truthy set,
    # so a single blanked pin would sail past a `not apply_refs` test and reach
    # the registry as an unparseable ref. Downstream does catch it (the resolver
    # fails and the gate exits 1), so this is not a fail-open — it is the
    # difference between "the pin table is EMPTY: <name>" and a resolver error
    # about an empty string.
    table = getattr(mod, "_GITLAB_APPLY_IMAGES", {})
    # ⚠️ Count non-empty VALUES, not the deduped set: two entries may legitimately
    # pin the same image, and comparing `len(set)` against the entry count would
    # red the gate for that alone.
    non_empty = [ref for _var, ref in table.values()
                 if isinstance(ref, str) and ref.strip()]
    apply_refs = {ref.strip() for ref in non_empty}
    git_sync = getattr(mod, "GIT_SYNC_IMAGE", "")
    git_sync = git_sync.strip() if isinstance(git_sync, str) else ""
    empty = [name for name, value in
             (("_GITLAB_APPLY_IMAGES", apply_refs), ("GIT_SYNC_IMAGE", git_sync))
             if not value]
    if table and len(non_empty) < len(table):
        empty.append(
            f"_GITLAB_APPLY_IMAGES declares {len(table)} entries but only "
            f"{len(non_empty)} carry a non-empty ref")
    if empty:
        raise SystemExit(
            f"check_image_refs_resolve: the customer-delivered pin table in {path} "
            f"is missing or EMPTY: {', '.join(empty)} — refusing to report a clean "
            "scope over a table that resolved to nothing.")
    return apply_refs | {git_sync}


def _repo_of(ref: str) -> str:
    """The repository portion of a ref (strip @digest then :tag)."""
    return ref.split("@", 1)[0].rsplit(":", 1)[0]


def _resolvable(ref: str) -> str:
    """A form the resolver can parse. skopeo/docker reject a ref carrying BOTH a
    `:tag` AND an `@digest` (fatal "Error parsing reference"); the digest is
    authoritative, so resolve `<repo>@<digest>` and drop the informational tag.
    Tag-only refs pass through unchanged. (#902 L2 pins as `repo:tag@digest` —
    readable tag + immutable digest, which Kubernetes accepts but skopeo won't.)"""
    if "@" in ref:
        return f"{_repo_of(ref)}@{ref.split('@', 1)[1]}"
    return ref


def _is_concrete(ref: str) -> bool:
    """A ref we can actually resolve: has a tag, isn't a Helm template."""
    if "{{" in ref or "}}" in ref:
        return False
    # Need a tag (or digest) after the final path segment's colon.
    last = ref.rsplit("/", 1)[-1]
    return ":" in last or "@" in last


def _refs_from_node(node) -> set[str]:
    """Recursively collect concrete image refs from a parsed YAML node."""
    found: set[str] = set()

    def walk(n):
        if isinstance(n, dict):
            # Shape A: {repository, tag[, registry]} image block.
            repo = n.get("repository")
            tag = n.get("tag")
            if isinstance(repo, str) and isinstance(tag, str) and tag.strip():
                registry = n.get("registry")
                ref = f"{registry}/{repo}" if isinstance(registry, str) and registry else repo
                digest = n.get("digest")
                ref = f"{ref}:{tag}@{digest}" if isinstance(digest, str) and digest else f"{ref}:{tag}"
                if _is_concrete(ref):
                    found.add(ref)
            # Shape B: `image:` as a single "repo:tag" string (e.g. mariadb.image,
            # raw k8s container image).
            img = n.get("image")
            if isinstance(img, str) and _is_concrete(img):
                found.add(img.strip())
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)

    walk(node)
    return found


def discover_refs(root: Path) -> set[str]:
    refs: set[str] = set()
    for pattern in SOURCE_GLOBS:
        for path in sorted(root.glob(pattern)):
            try:
                docs = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
            except yaml.YAMLError as exc:
                print(f"::warning:: skipping unparseable {path}: {exc}", file=sys.stderr)
                continue
            for doc in docs:
                if doc is not None:
                    refs |= _refs_from_node(doc)
    # Skip locally-built (never published) + first-party (needs ghcr auth; release's
    # job) refs — an anonymous resolve would false-fail them. L1-B = public third-party.
    def _keep(r: str) -> bool:
        repo = _repo_of(r)
        return repo not in LOCAL_BUILT_IMAGES and not repo.startswith(SKIP_REPO_PREFIXES)

    return {r for r in refs if _keep(r)}


def _resolver():
    """Return (cmd_builder, name) for the available resolver, or (None, None)."""
    if shutil.which("skopeo"):
        return (lambda ref: ["skopeo", "inspect", "--no-tags", f"docker://{ref}"], "skopeo")
    if shutil.which("docker"):
        return (lambda ref: ["docker", "manifest", "inspect", ref], "docker")
    return (None, None)


def _resolve_once(cmd: list[str], timeout: int) -> tuple[bool, str]:
    """Run one resolver command; return (resolved, last-line-of-reason)."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return (False, f"timeout after {timeout}s")
    if proc.returncode == 0:
        return (True, "")
    lines = (proc.stderr or proc.stdout or "non-zero exit").strip().splitlines()
    return (False, lines[-1] if lines else "non-zero exit")


def main() -> int:
    ap = argparse.ArgumentParser(description="Verify chart/manifest image refs resolve in their registry.")
    ap.add_argument("--root", default=".", help="repo root to scan (default: cwd)")
    ap.add_argument("--list", action="store_true", help="print discovered concrete refs and exit (no network)")
    ap.add_argument("--timeout", type=int, default=30, help="per-ref resolver timeout (s)")
    ap.add_argument("--scope", choices=_SCOPES, default="deploy",
                    help="which ref set to check: deploy (helm values + k8s manifests, "
                         "the default) or delivered (what `da-tools init` writes into a "
                         "customer repo). There is no combined value on purpose — run "
                         "it twice, so a failure says which scope broke.")
    args = ap.parse_args()

    root = Path(args.root)
    refs = sorted(discover_refs(root) if args.scope == "deploy" else delivered_refs(root))

    if args.list:
        for ref in refs:
            print(ref)
        return 0

    if not refs:
        print("check_image_refs_resolve: no concrete image refs found — nothing to check.")
        return 0

    build_cmd, name = _resolver()
    if build_cmd is None:
        print("::warning:: neither skopeo nor docker available — SKIPPING image-ref "
              "resolution (install skopeo in CI to enforce). Refs that WOULD be checked:")
        for ref in refs:
            print(f"  - {ref}")
        return 0

    print(f"Resolving {len(refs)} concrete image ref(s) [scope={args.scope}] via {name}...")
    failed: list[tuple[str, str]] = []
    for ref in refs:
        resolvable = _resolvable(ref)  # `repo:tag@digest` → `repo@digest` for the resolver
        ok, reason = _resolve_once(build_cmd(resolvable), args.timeout)
        if not ok:
            # One retry absorbs a transient registry blip before failing the gate.
            ok, reason = _resolve_once(build_cmd(resolvable), args.timeout)
        if ok:
            print(f"  ok       {ref}")
        else:
            failed.append((ref, reason))
            print(f"  FAIL     {ref}  ({reason})")

    if failed:
        print(f"\n::error:: {len(failed)} image ref(s) do NOT resolve in their registry "
              f"(the #897 class — typo'd / yanked tag):", file=sys.stderr)
        for ref, reason in failed:
            print(f"  - {ref}: {reason}", file=sys.stderr)
        return 1

    print(f"\nAll {len(refs)} image refs resolve.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
