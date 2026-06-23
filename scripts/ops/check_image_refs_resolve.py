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

Usage:
  check_image_refs_resolve.py [--root DIR] [--list] [--timeout SECS]
    --root    repo root to scan (default: cwd)
    --list    print the discovered concrete refs and exit 0 (no network) — for tests
    --timeout per-ref resolver timeout in seconds (default 30)
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
SOURCE_GLOBS = ("helm/*/values.yaml", "k8s/**/*.yaml", "k8s/**/*.yml")

# Images BUILT LOCALLY by a chart's own Dockerfile and NOT published to a public
# registry (the deployer builds/loads them, or pushes to their own registry). They
# cannot be resolved against a public registry, so the resolve check would
# false-fail on them — skip by repository name. (federation-gateway audit-sidecar:
# helm/federation-gateway/audit-sidecar/Dockerfile, values repository has no host.)
LOCAL_BUILT_IMAGES = {"federation-audit-sidecar"}

# First-party images live in our own registry namespace: their currency is the
# release pipeline's job, and resolving them needs ghcr auth (would false-fail an
# anonymous CI check). L1-B targets the PUBLIC third-party refs (the #897 typo
# class), so skip our own namespace here.
SKIP_REPO_PREFIXES = ("ghcr.io/vencil/",)


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
    args = ap.parse_args()

    refs = sorted(discover_refs(Path(args.root)))

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

    print(f"Resolving {len(refs)} concrete image ref(s) via {name}...")
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
