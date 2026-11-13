#!/usr/bin/env python3
"""check_chart_version_bump.py — a chart whose packaged content changed must raise `version` (#1269).

A chart version is what an advisory's `patched_versions`, a customer's
`helm upgrade --version` and an OCI tag all name. If two different packages
carry the same version, none of those mean anything: "patched in 0.4.0" no
longer says which 0.4.0. Nothing checked it — a PR could change a template and
leave `version:` alone, and the next `helm push` would publish different bytes
under a version that already existed (or that an advisory already cites).

What is compared
----------------
The PACKAGES, not the repo diff. For every chart the diff touches, both sides —
the base commit and the working tree — are run through `helm package`, and the
archives are compared file by file (sha256 of each member's content). So:

* an edit that does not ship needs no bump: a `.helmignore`d file, a comment in
  Chart.yaml (helm re-serialises Chart.yaml, so its comments never reach the
  archive — measured with helm v4.3.0);
* a deleted template, a changed default in values.yaml, a new file in
  templates/ — anything that changes what a `helm pull` receives — does.

Archive BYTES are not compared: the tarball records each file's mtime, which
differs between two checkouts of the same commit.

The version is the one helm itself read: it names the archive
`<name>-<version>.tgz`. Chart.yaml is never parsed here, so this cannot
disagree with helm about what the version is (ADR-036 §12).

Rule: content differs ⇒ HEAD's version must be higher than the base's (semver
precedence). Charts are paired by their `name` (what a version identifies), not
their directory, so moving a chart to a new directory does not turn it into a
"new chart" that may keep its version. A new name and a removed name pass; an
unchanged package passes.

Scope: charts with no `helm push` site — the git-only charts, installed from a
checkout (the push sites come from
`check_chart_ship_surface.discover_pushed_charts`, the derivation
`check_chart_pull_surface` uses for GIT_ONLY). A chart a release line pushes is
listed and skipped: its version is set at the release wrap (`bump_docs.py`) and
must equal the tag, so a per-PR bump would fight that model (tenant-api's
appVersion must equal its version, which must be an image tag the release
builds).
⛔ That skip is NOT a claim that those versions are unique. They are not held:
the release playbook's re-tag SOP moves a tag to a new commit under the same
version, `helm push` in release.yaml does not check whether the version is
already published (an OCI tag is overwritten), and `make chart-push` pushes the
working tree under Chart.yaml's version with no tag at all. Between releases,
main also carries the last released version over newer content. Closing that
needs a publish-time check, not this one (#1269).

Base
----
`--base` when given. Otherwise the merge base of `$LINT_DIFF_BASE` /
`origin/$GITHUB_BASE_REF` / `origin/main` with HEAD — the repo's one resolver
(`_lint_helpers.resolve_diff_base_ref`). When that merge base IS HEAD (a branch
with no commit of its own yet), the working tree is compared with HEAD.
CI on a push to main passes the push's `before` commit in
`$CHART_VERSION_BUMP_BASE` (read like `--base`; this tool's own name, so no
other diff-aware hook in the same step changes base):
branch protection does not require a PR to be up to date with main, so two PRs
that each raised a chart to the same version can both merge, and only main's
run sees them together. `before`, not HEAD^1: with rebase-merge allowed, HEAD^1
is the previous commit of the same PR.

Exit codes
    0  every changed package carries a higher version
    1  violation
    2  could not run (no helm, base unresolvable, helm package failed,
       unparsable version). Never a silent pass.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import os
import re
import subprocess  # nosec B404 — fixed argv, no shell, git only
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

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
from _lint_helpers import DiffBaseMissingError, resolve_diff_base_ref  # noqa: E402
from check_chart_package_contents import CallerError, package  # noqa: E402
from check_chart_ship_surface import discover_pushed_charts  # noqa: E402

CHARTS_DIR = "helm"
BASE_ENV = "CHART_VERSION_BUMP_BASE"
_SEMVER = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?(?:\+[0-9A-Za-z.-]+)?$"
)


def _git(repo: Path, *args: str, binary: bool = False) -> subprocess.CompletedProcess:
    try:
        if binary:
            proc = subprocess.run(  # nosec B603 B607 — fixed argv, no shell
                ["git", *args], cwd=str(repo), capture_output=True, timeout=120)
        else:
            proc = subprocess.run(  # nosec B603 B607 — fixed argv, no shell
                ["git", *args], cwd=str(repo), capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        raise CallerError(f"git {' '.join(args)} failed to run: {exc}") from exc
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace") if binary else proc.stderr
        raise CallerError(f"git {' '.join(args)} exited {proc.returncode}: {err.strip()}")
    return proc


def semver_key(version: str) -> Tuple:
    """Sort key with semver precedence (pre-release < release; build ignored)."""
    m = _SEMVER.match(version)
    if not m:
        raise CallerError(f"chart version {version!r} is not semver; helm requires it")
    major, minor, patch, pre = m.groups()
    if pre is None:
        pre_key: Tuple = (1,)
    else:
        pre_key = (0, *[(0, int(p), "") if p.isdigit() else (1, 0, p) for p in pre.split(".")])
    return (int(major), int(minor), int(patch), pre_key)


def resolve_base(repo: Path, explicit: Optional[str]) -> Tuple[str, str]:
    """Return (commit, label) of what the working tree is compared with."""
    if not explicit:
        env = os.environ.get(BASE_ENV, "").strip()
        # GitHub sends forty zeros as `before` when a push creates the branch.
        if env and env.strip("0"):
            explicit = env
    if explicit:
        commit = _git(repo, "rev-parse", "--verify", f"{explicit}^{{commit}}").stdout.strip()
        return commit, explicit
    try:
        ref, commit = resolve_diff_base_ref()
    except DiffBaseMissingError as exc:
        raise CallerError(str(exc)) from exc
    return commit, f"{ref} (merge base {commit[:12]})"


def changed_charts(repo: Path, base: str) -> List[str]:
    """Chart directory names (`helm/<name>`) with any change, deletion or
    untracked file between `base` and the working tree."""
    names: Set[str] = set()
    diff = _git(repo, "diff", "--name-only", "--no-renames", base, "--", CHARTS_DIR).stdout
    untracked = _git(repo, "ls-files", "--others", "--exclude-standard", "--", CHARTS_DIR).stdout
    for line in (diff + untracked).splitlines():
        parts = line.strip().split("/")
        if len(parts) >= 3 and parts[0] == CHARTS_DIR:
            names.add(f"{CHARTS_DIR}/{parts[1]}")
    return sorted(names)


def _extract_base_chart(repo: Path, base: str, chart: str, dest: Path) -> Optional[Path]:
    """Write the chart as it is at `base` under dest; None if it has no Chart.yaml there."""
    present = _git(repo, "ls-tree", "--name-only", base, f"{chart}/Chart.yaml").stdout.strip()
    if not present:
        return None
    blob = _git(repo, "archive", "--format=tar", base, "--", chart, binary=True).stdout
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:", encoding="utf-8") as tar:
        tar.extractall(dest, filter="data")
    return dest / chart


def package_digest(chart_dir: Path, workdir: Path) -> Tuple[str, str, Dict[str, str]]:
    """(name, version, {member path without the chart-name prefix: sha256}) —
    name and version as helm read them (it names the archive after both)."""
    out = workdir / "out"
    out.mkdir(parents=True)
    tgz = package(chart_dir, out)
    files: Dict[str, str] = {}
    with tarfile.open(tgz, "r:gz", encoding="utf-8") as tar:
        names = []
        for m in tar.getmembers():
            if not m.isfile():
                continue
            names.append(m.name)
            data = tar.extractfile(m).read()  # type: ignore[union-attr]
            files[m.name.split("/", 1)[1] if "/" in m.name else m.name] = hashlib.sha256(data).hexdigest()
    prefixes = {n.split("/", 1)[0] for n in names}
    if len(prefixes) != 1:
        raise CallerError(f"{tgz.name}: expected one top-level directory, got {sorted(prefixes)}")
    name = prefixes.pop()
    stem = tgz.name[:-len(".tgz")]
    if not stem.startswith(name + "-"):
        raise CallerError(f"{tgz.name}: archive name does not start with the chart name {name!r}")
    return name, stem[len(name) + 1:], files


Package = Tuple[str, str, Dict[str, str]]  # (dir, version, files)


def _packages(repo: Path, base: str, charts: List[str], scratch: Path
              ) -> Tuple[Dict[str, Package], Dict[str, Package]]:
    """name -> package, for the changed chart dirs at base and in the working tree."""
    head: Dict[str, Package] = {}
    old: Dict[str, Package] = {}
    for i, chart in enumerate(charts):
        if (repo / chart / "Chart.yaml").is_file():
            name, version, files = package_digest(repo / chart, scratch / f"head-{i}")
            if name in head:
                raise CallerError(f"two charts are named {name!r}: {head[name][0]} and {chart}")
            head[name] = (chart, version, files)
        base_dir = _extract_base_chart(repo, base, chart, scratch / f"base-tree-{i}")
        if base_dir is not None:
            name, version, files = package_digest(base_dir, scratch / f"base-{i}")
            old[name] = (chart, version, files)
    return head, old


def check(repo: Path, base: str, charts: List[str], scratch: Path,
          pushed: Set[str]) -> Tuple[List[str], List[str]]:
    """(violations, notes) for the changed chart dirs, paired by chart name."""
    head, old = _packages(repo, base, charts, scratch)
    violations: List[str] = []
    notes: List[str] = []
    for name in sorted(head):
        chart, head_version, head_files = head[name]
        if name in pushed:
            notes.append(f"{chart}: skipped — a release line pushes {name!r}; its version is "
                         f"set at the release wrap, and nothing here checks it (#1269)")
            continue
        if name not in old:
            continue  # a new name: any version is its first
        base_chart, base_version, base_files = old[name]
        if base_files == head_files or semver_key(head_version) > semver_key(base_version):
            continue
        changed = sorted(
            {k for k in base_files.keys() | head_files.keys() if base_files.get(k) != head_files.get(k)})
        shown = ", ".join(changed[:8]) + (f" (+{len(changed) - 8} more)" if len(changed) > 8 else "")
        verb = "still" if head_version == base_version else "went DOWN to"
        moved = f" (was {base_chart})" if base_chart != chart else ""
        violations.append(
            f"{chart}{moved}: the package changed ({shown}) but version {verb} {head_version} "
            f"(base: {base_version}). Raise `version:` in {chart}/Chart.yaml and add a "
            f"history line: the same version must never name two different packages.")
    return violations, notes


def main(argv: Optional[List[str]] = None) -> int:
    try_utf8_stdout()
    parser = argparse.ArgumentParser(
        description=i18n_text(
            "chart 打包後的內容（helm package 的產出，逐檔比對）若與 base 不同，"
            "Chart.yaml 的 version 必須比 base 高。比的是打包內容，不是 repo diff。",
            "A chart whose packaged content (the `helm package` output, compared "
            "file by file) differs from the base must carry a higher `version` "
            "than the base. Packages are compared, not the repo diff.",
        ),
    )
    parser.add_argument(
        "--base",
        help=i18n_text(
            "比對的 commit；省略時取 $LINT_DIFF_BASE／origin/$GITHUB_BASE_REF／"
            "origin/main 與 HEAD 的 merge base",
            "commit to compare with; omitted, the merge base of $LINT_DIFF_BASE / "
            "origin/$GITHUB_BASE_REF / origin/main with HEAD",
        ),
    )
    parser.add_argument("--repo", default=str(Path(__file__).resolve().parents[3]),
                        help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    repo = Path(args.repo)

    violations: List[str] = []
    notes: List[str] = []
    try:
        base, label = resolve_base(repo, args.base)
        charts = changed_charts(repo, base)
        pushed = set(discover_pushed_charts(repo)) if charts else set()
        with tempfile.TemporaryDirectory(prefix="chart-version-bump-") as tmp:
            violations, notes = check(repo, base, charts, Path(tmp), pushed)
    except CallerError as exc:
        print(f"❌ chart version bump check could not run: {exc}", file=sys.stderr)
        return EXIT_CALLER_ERROR

    for n in notes:
        print(f"   · {n}")
    if violations:
        print(f"❌ chart version bump (#1269), compared with {label}:", file=sys.stderr)
        for v in violations:
            print(f"   - {v}", file=sys.stderr)
        return EXIT_VIOLATION
    print(f"✓ chart version bump: {len(charts)} changed chart(s) checked against {label}")
    return EXIT_OK


if __name__ == "__main__":
    import _lib_exitcodes
    _lib_exitcodes.run(main)
