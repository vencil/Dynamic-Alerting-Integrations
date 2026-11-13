#!/usr/bin/env python3
"""release_publish.py — the only way release.yaml writes to a registry or a GitHub Release (#3848).

The immutability check used to be a step of its own, with each publishing step
gated by an `if:` on its output. A gate beside the operation can be stepped
around (`|| true` on the check, a line continuation the guard does not read, a
promotion that writes one tag more than the check asked about), so the check
moved INTO the operation: each subcommand decides and then publishes, in one
process, and release.yaml calls nothing else that publishes
(tests/ops/test_release_immutable_wiring.py).

    image --image <registry>/<repo> --digest <candidate> --revision <commit>
          --tag <tag> [--tag latest] [--sign]
        Every tag except `latest` is checked (check_release_immutable.judge_image).
        All absent → (with --sign) `cosign sign` the candidate digest, then
        `docker buildx imagetools create` one tag at a time, `latest` FIRST and
        the versioned tags LAST, each read back with `imagetools inspect`.
        Versioned last: a run that stops half way leaves the version absent, so
        a re-run publishes it (and moves `latest` again) — the other order
        would leave the version present and `latest` behind for good. All
        already published from <commit> → nothing is written, `latest` is not
        touched. Mixed, or published from anything else → violation.
        $GITHUB_OUTPUT gets `publish=` and `digest=` (the digest the version
        tag names after this run: the candidate, or the one already published).

    chart --tgz <packaged.tgz> --registry oci://<registry>/<path>
        check_release_immutable.judge_chart, then `helm push` only when absent,
        then the same judgement again: the registry must now hold exactly
        this package (a read-back, not helm's exit status alone).

    assets --tag <git tag> --title <title> FILE...
        The GitHub Release's assets are never replaced (no `--clobber`): a
        re-run rebuilds archives whose bytes differ (mtimes), so a replaced
        asset would no longer match what was downloaded and checksummed.
        Release absent → created (`--generate-notes`). Of FILE's basenames:
        all present with `state: uploaded` → nothing uploaded; none present →
        all uploaded; some, or any not in state `uploaded` → violation
        (delete those with `gh release delete-asset`, then re-run).

Credentials for the registry read: $REGISTRY_USERNAME / $REGISTRY_PASSWORD
(see check_release_immutable). docker / helm / cosign use their own login.

Exit codes
    0  published, or nothing to publish
    1  violation — the version is published with other content, or a
       Release holds part of the asset set
    2  could not decide or could not publish (registry or gh gave no usable
       answer, a command failed, a read-back did not match). Never a silent pass.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess  # nosec B404 — fixed argv, no shell
import sys
from pathlib import Path
from typing import List, Optional

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, ".."))  # Repo subdir layout
from _lib_exitcodes import (  # noqa: E402
    EXIT_OK,
    EXIT_VIOLATION,
    EXIT_CALLER_ERROR,
)
from _lib_compat import try_utf8_stdout  # noqa: E402
from _lib_validation import i18n_text  # noqa: E402
import _lib_engine  # noqa: E402
import check_release_immutable as immutable  # noqa: E402

CallerError, Violation = immutable.CallerError, immutable.Violation
NoAnswer = immutable.registry.NoAnswer

#: Tags that move by design; never checked, written before the versioned ones.
MUTABLE_TAGS = ("latest",)
_TAG = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,127}")
_IMAGE = re.compile(r"[a-z0-9.-]+(?::[0-9]+)?(?:/[a-z0-9._-]+)+")
_RELEASE_LIST_LIMIT = 1000

#: The subprocess seam (tests replace it).
RUN = subprocess.run


def _run(argv: List[str], timeout: float) -> str:
    """Run *argv*; its stdout. Anything but rc 0 is a CallerError."""
    try:
        proc = RUN(argv, capture_output=True, text=True, encoding="utf-8",  # nosec B603
                   errors="replace", timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        raise CallerError(f"{' '.join(argv[:3])} failed to run: {exc}") from exc
    if proc.returncode != 0:
        raise CallerError(f"{' '.join(argv[:3])} exited {proc.returncode}: "
                          f"{(proc.stderr or '').strip()[-2000:]}")
    return proc.stdout or ""


# ── image ───────────────────────────────────────────────────────────────────


def publish_image(image: str, digest: str, revision: str, tags: List[str], sign: bool,
                  timeout: float) -> immutable.Decision:
    if not _IMAGE.fullmatch(image):
        raise CallerError(f"--image {image!r} is not <registry>/<repository> (no tag, no digest)")
    bad = [t for t in tags if not _TAG.fullmatch(t)]
    if bad or len(set(tags)) != len(tags):
        raise CallerError(f"--tag values must be distinct tags: {tags}")
    versioned = [t for t in tags if t not in MUTABLE_TAGS]
    if not versioned:
        raise CallerError("at least one --tag other than `latest` is required: "
                          "it is the version this run publishes")
    decisions = [immutable.decide_image(f"{image}:{t}", revision, digest, timeout)
                 for t in versioned]
    for d in decisions:
        print(f"   {'publish' if d.publish else 'skip'}: {d.message}")
    if all(not d.publish for d in decisions):
        digests = {d.digest for d in decisions}
        if len(digests) != 1:
            raise Violation(f"{image}: {', '.join(versioned)} are published on different "
                            f"digests {sorted(digests)}")
        return immutable.Decision(False, f"{image}: already published from {revision}; "
                                         f"nothing written, `latest` not moved", digests.pop())
    if any(not d.publish for d in decisions):
        raise Violation(f"{image}: some of {versioned} are already published and some are "
                        f"not — a version is published whole or not at all; ship a new version")
    if sign:
        print(_run(["cosign", "sign", "--yes", f"{image}@{digest}"], timeout=600), end="")
    ordered = [t for t in tags if t in MUTABLE_TAGS] + versioned
    for tag in ordered:
        print(_run(["docker", "buildx", "imagetools", "create", "--tag", f"{image}:{tag}",
                    f"{image}@{digest}"], timeout=600), end="")
        got = _run(["docker", "buildx", "imagetools", "inspect", f"{image}:{tag}",
                    "--format", "{{.Manifest.Digest}}"], timeout=120).strip()
        if got != digest:
            raise CallerError(f"{image}:{tag} reads back as {got!r}, not {digest}")
        print(f"✓ {image}:{tag} -> {digest}")
    return immutable.Decision(True, f"{image}: published {', '.join(ordered)} at {digest}", digest)


# ── chart ───────────────────────────────────────────────────────────────────


def _helm_push(tgz: str, oci_registry: str) -> str:
    """`helm push`, through _lib_engine (helm is a registered engine)."""
    located = _lib_engine.locate("helm")
    if isinstance(located, _lib_engine.NoVerdict):
        raise CallerError(located.message())
    result = _lib_engine.run(located, "push", ["push", tgz, oci_registry], timeout=600)
    if isinstance(result, _lib_engine.NoVerdict):
        raise CallerError(result.message())
    return result.data


#: The seam the tests replace.
HELM_PUSH = _helm_push


def publish_chart(tgz: str, oci_registry: str, timeout: float) -> immutable.Decision:
    decision = immutable.decide_chart(tgz, oci_registry, timeout)
    if not decision.publish:
        return decision
    print(HELM_PUSH(tgz, oci_registry), end="")
    # Read back: the registry must now hold exactly this package.
    after = immutable.decide_chart(tgz, oci_registry, timeout)
    if after.publish:
        raise CallerError(f"helm push exited 0 but the registry still has no {tgz}")
    print(f"✓ read back: {after.message}")
    return decision


# ── GitHub Release assets ───────────────────────────────────────────────────


def _json(text: str, what: str):
    try:
        return json.loads(text)
    except (ValueError, RecursionError) as exc:
        raise CallerError(f"{what} did not answer JSON: {exc}") from exc


def publish_assets(tag: str, title: str, files: List[str]) -> immutable.Decision:
    paths = [Path(f) for f in files]
    missing = [str(p) for p in paths if not p.is_file()]
    if missing:
        raise CallerError(f"not files: {missing}")
    names = [p.name for p in paths]
    if len(set(names)) != len(names):
        raise CallerError(f"two files share a name: {sorted(n for n in names if names.count(n) > 1)}")
    listed = _json(_run(["gh", "release", "list", "--limit", str(_RELEASE_LIST_LIMIT),
                         "--json", "tagName"], timeout=120), "gh release list")
    if not isinstance(listed, list) or len(listed) >= _RELEASE_LIST_LIMIT:
        raise CallerError(f"gh release list answered {type(listed).__name__} of "
                          f"{len(listed) if isinstance(listed, list) else '?'} — cannot tell "
                          f"whether {tag} exists")
    if any(isinstance(r, dict) and r.get("tagName") == tag for r in listed):
        view = _json(_run(["gh", "release", "view", tag, "--json", "assets"], timeout=120),
                     "gh release view")
        assets = view.get("assets") if isinstance(view, dict) else None
        if not isinstance(assets, list):
            raise CallerError(f"gh release view {tag} has no assets list")
        # Only an asset whose upload completed counts; `state` is in gh's
        # asset JSON (verified on this repo's releases: "uploaded"). One still
        # "starter" (an interrupted upload) leaves the set partial.
        present = {a.get("name") for a in assets if isinstance(a, dict)}
        stale = sorted(a.get("name") for a in assets if isinstance(a, dict)
                       and a.get("name") in names and a.get("state") != "uploaded")
        if stale:
            raise Violation(f"Release {tag}: {stale} are not fully uploaded (state is not "
                            f"`uploaded`) — delete them (gh release delete-asset {tag} <name>), "
                            f"then re-run.")
    else:
        print(_run(["gh", "release", "create", tag, "--title", title, "--generate-notes"],
                   timeout=120), end="")
        present = set()
    have = sorted(set(names) & present)
    if len(have) == len(names):
        return immutable.Decision(False, f"Release {tag} already has all {len(names)} assets; "
                                         f"none replaced")
    if have:
        lacking = sorted(set(names) - present)
        raise Violation(
            f"Release {tag} holds {len(have)} of the {len(names)} assets (present: {have}; "
            f"missing: {lacking}). Assets are never replaced, and a partial set cannot be "
            f"completed from a rebuild whose bytes differ: delete the present ones "
            f"(gh release delete-asset {tag} <name>), then re-run.")
    print(_run(["gh", "release", "upload", tag, *[str(p) for p in paths]], timeout=1800), end="")
    return immutable.Decision(True, f"Release {tag}: uploaded {len(names)} assets")


# ── CLI ─────────────────────────────────────────────────────────────────────


def main(argv: Optional[List[str]] = None) -> int:
    try_utf8_stdout()
    parser = argparse.ArgumentParser(description=i18n_text(
        "release.yaml 唯一的發布入口：先確認版號不可變，再發布映像、chart 或 Release 資產。",
        "release.yaml's only way to publish: check the version is immutable, then "
        "publish the image, the chart or the Release assets."))
    parser.add_argument("--timeout", type=float, default=30.0,
                        help=i18n_text("每個 registry 請求的逾時秒數", "seconds per registry request"))
    sub = parser.add_subparsers(dest="command", required=True)
    image = sub.add_parser("image")
    image.add_argument("--image", required=True)
    image.add_argument("--digest", required=True)
    image.add_argument("--revision", required=True)
    image.add_argument("--tag", action="append", required=True)
    image.add_argument("--sign", action="store_true")
    chart = sub.add_parser("chart")
    chart.add_argument("--tgz", required=True)
    chart.add_argument("--registry", required=True)
    assets = sub.add_parser("assets")
    assets.add_argument("--tag", required=True)
    assets.add_argument("--title", required=True)
    assets.add_argument("files", nargs="+")
    args = parser.parse_args(argv)
    try:
        if args.command == "image":
            if not immutable._DIGEST.fullmatch(args.digest):
                raise CallerError(f"--digest {args.digest!r} is not sha256:<hex>")
            decision = publish_image(args.image, args.digest, args.revision, args.tag,
                                     args.sign, args.timeout)
            outputs = {"digest": decision.digest}
        elif args.command == "chart":
            decision, outputs = publish_chart(args.tgz, args.registry, args.timeout), {}
        else:
            decision, outputs = publish_assets(args.tag, args.title, args.files), {}
    except CallerError as exc:
        print(f"❌ release publish could not run: {exc}", file=sys.stderr)
        return EXIT_CALLER_ERROR
    except NoAnswer as exc:
        print(f"❌ release publish got no answer from the registry: {exc}", file=sys.stderr)
        print("   This is 'could not ask', not 'not published' — nothing was published.",
              file=sys.stderr)
        return EXIT_CALLER_ERROR
    except Violation as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return EXIT_VIOLATION
    immutable.write_output(publish="true" if decision.publish else "false", **outputs)
    print(f"{'✅ published' if decision.publish else '⏭️  skipped'}: {decision.message}")
    return EXIT_OK


if __name__ == "__main__":
    import _lib_exitcodes
    _lib_exitcodes.run(main)
