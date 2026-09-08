#!/usr/bin/env python3
"""Every chart we TELL customers to pull must be a chart we actually push.

#1352 case A is what the gap looks like: `components/da-portal/README.md`
carried a `helm install … oci://…/charts/da-portal` line, `bump_docs.py` kept
its version string fresh at every release wrap, and `release-portal` published
a Docker image and nothing else. Packaging is not publishing, and a version
string a bumper keeps fresh is not evidence that the artifact behind it exists.

Both halves are derived from the repo, so no registry round-trip is needed and
this runs in pre-commit like any other lint:

  * the PULL side — every `oci://<registry>/<owner>/charts/<name>` spelled out
    in a tracked file;
  * the PUSH side — `discover_pushed_charts()`, the `helm push` call sites in
    the Makefile and the workflows.

A name on the pull side with no push site is the violation. The reverse is
NOT: a chart can be published without any document teaching it, and demanding
docs for it would be this gate inventing a policy nobody agreed to.

Scanned surface: `git ls-files`, minus EXCLUDED_PREFIXES / EXCLUDED_FILES
below — build output generated from sources that ARE scanned, test fixtures,
and the changelogs, which record what was true then rather than instructing a
reader now.

⛔ `.github/workflows/release.yaml` is deliberately NOT excluded. Its header
lists the artifacts a release produces, and #1352 case B is that list naming a
chart no tag has ever been cut for. A gate that skipped the file would have
missed half its own ticket.

Exit codes:
  0 = every chart the repo tells customers to pull has a `helm push`
  1 = at least one does not
  2 = the check could not be RUN (unreadable file, unparseable push site)
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Set, Tuple

import yaml

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
from check_chart_ship_surface import (  # noqa: E402
    CallerError,
    discover_pushed_charts,
)

REPO_ROOT = Path(__file__).resolve().parents[3]

EXCLUDED_PREFIXES: Tuple[str, ...] = (
    "site/",
    "docs/assets/dist/",
    "tests/",
)
EXCLUDED_FILES: Tuple[str, ...] = (
    "CHANGELOG.md",
    "CHANGELOG-archive.md",
)


def registry_and_owner(repo_root: Path = REPO_ROOT) -> Tuple[str, str]:
    """(registry, owner) from release.yaml's top-level `env:`.

    Derived rather than hard-coded, so renaming either one cannot leave this
    gate matching a literal nothing spells any more.

    ⛔ Parsed as YAML, not with a line regex. `REGISTRY: "ghcr.io"` is the same
    document as `REGISTRY: ghcr.io`, and a regex that keeps the quotes builds a
    pattern nothing matches — the gate then passes because it looked in the
    wrong place, which is the failure this whole ticket is about.
    """
    wf = repo_root / ".github" / "workflows" / "release.yaml"
    try:
        doc = yaml.safe_load(wf.read_text(encoding="utf-8"))
    except OSError as exc:
        raise CallerError(f"cannot read {wf}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise CallerError(f"cannot parse {wf}: {exc}") from exc
    env = doc.get("env") if isinstance(doc, dict) else None
    found = {k: env.get(k) for k in ("REGISTRY", "IMAGE_OWNER")} if isinstance(env, dict) else {}
    unusable = sorted(k for k, v in found.items() if not isinstance(v, str) or not v.strip())
    if len(found) < 2 or unusable:
        raise CallerError(
            f"{wf.relative_to(repo_root).as_posix()} has no usable top-level "
            f"REGISTRY / IMAGE_OWNER under `env:`, so this gate cannot tell "
            f"which OCI references are ours; it will not guess"
        )
    return found["REGISTRY"].strip(), found["IMAGE_OWNER"].strip()


def _tracked_files(repo_root: Path = REPO_ROOT) -> List[str]:
    """Repo-relative paths of tracked files, minus the derived/historical ones."""
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_root), "ls-files", "-z"],
            capture_output=True, check=True, timeout=120,
        ).stdout.decode("utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError) as exc:
        raise CallerError(f"cannot list tracked files: {exc}") from exc
    return [
        rel for rel in out.split("\0")
        if rel
        and not rel.startswith(EXCLUDED_PREFIXES)
        and rel not in EXCLUDED_FILES
    ]


def taught_charts(repo_root: Path = REPO_ROOT) -> Dict[str, List[str]]:
    """Chart name -> the places that tell a reader to pull it over OCI."""
    registry, owner = registry_and_owner(repo_root)
    # Assembled rather than written out, so this module's own source does not
    # contain a matchable reference and does not need to exclude itself.
    pattern = re.compile(
        r"oci://" + re.escape(f"{registry}/{owner}") + r"/charts/"
        r"(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)"
    )
    taught: Dict[str, List[str]] = {}
    for rel in _tracked_files(repo_root):
        path = repo_root / rel
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, ValueError):
            continue          # binary asset — carries no guidance
        except OSError as exc:
            raise CallerError(f"cannot read {rel}: {exc}") from exc
        for n, line in enumerate(text.splitlines(), 1):
            for m in pattern.finditer(line):
                taught.setdefault(m.group("name"), []).append(f"{rel}:{n}")
    return taught


def check(repo_root: Path = REPO_ROOT) -> List[str]:
    """Return the list of violations. Empty list == pass."""
    taught = taught_charts(repo_root)
    pushed: Set[str] = set(discover_pushed_charts(repo_root))
    violations: List[str] = []
    for name in sorted(set(taught) - pushed):
        where = taught[name]
        shown = ", ".join(where[:4]) + (f" (+{len(where) - 4} more)"
                                        if len(where) > 4 else "")
        violations.append(
            f"{name}: the repo tells customers to pull "
            f"oci://<registry>/<owner>/charts/{name} at {shown}, but no "
            f"`helm push` anywhere publishes a chart by that name. A reader "
            f"following those instructions gets a 403 from a package that "
            f"does not exist. Either publish it (add the package/push steps "
            f"to its release job, the way #1352 did for da-portal) or stop "
            f"teaching it."
        )
    return violations


def main(argv: List[str] | None = None) -> int:
    # argparse with no flags — the repo-wide CLI contract: `--help` exits 0,
    # unknown flags exit 2 (argparse's default). try_utf8_stdout() first
    # because CJK in --help raises UnicodeEncodeError on a legacy Windows
    # console (cp950/cp936) before argparse ever prints.
    try_utf8_stdout()
    parser = argparse.ArgumentParser(
        description=i18n_text(
            "斷言每個「文件教客戶用 oci:// 拉取」的 chart，都真的有對應的 "
            "helm push；兩邊都從 repo 靜態推導，不需要打 registry。",
            "Assert that every chart the repo tells customers to pull over "
            "oci:// really has a matching `helm push`. Both sides are derived "
            "from the repo, so no registry round-trip is needed.",
        ),
    )
    parser.parse_args(argv)

    try:
        violations = check()
        pushed = sorted(discover_pushed_charts())
    except CallerError as exc:
        print(f"❌ chart pull surface check could not run: {exc}", file=sys.stderr)
        return EXIT_CALLER_ERROR

    if violations:
        # stderr, matching check_chart_ship_surface.py: a lint failure that
        # prints to stdout is invisible wherever a runner keeps only stderr.
        print("❌ chart pull surface (#1352):", file=sys.stderr)
        for v in violations:
            print(f"   - {v}", file=sys.stderr)
        return EXIT_VIOLATION

    print(f"✓ every chart taught over OCI is pushed: {', '.join(pushed)}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
