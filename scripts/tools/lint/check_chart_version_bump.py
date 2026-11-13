#!/usr/bin/env python3
"""check_chart_version_bump.py — a git-only chart's version must name one package (#1269).

A chart version is what an advisory's `patched_versions`, a customer's
`helm upgrade --version` and an OCI tag all name. If two different packages
carry the same version, none of those mean anything: "patched in 0.4.0" no
longer says which 0.4.0. Nothing checked it — a PR (Renovate's image bumps
included) could change a chart and leave `version:` alone.

The invariant, checked statelessly on every run for every git-only chart:
the package the working tree would produce equals the package at the commit
that FIRST carried its current version. Equivalently:

* version unchanged from the base → the content must equal what it was when
  that version was introduced (found by walking the base's history of
  Chart.yaml, renames followed);
* version changed → it must be higher than the base's (semver precedence).

Being stateless matters on main: CI on a push to main may be superseded by a
later push and never finish, so "compare with the previous push" would leave
a merged change unchecked. Comparing with the version's first commit does not
depend on which runs finished. It also means a rebase-merged PR whose LATER
commit changes a chart its earlier commit bumped is red on main: that version
then names two packages on main's history. Squash merges (the repo's
practice) never hit this.

What is compared
----------------
PACKAGES, not the repo diff: both sides go through `helm package`, and the
archives are compared file by file (sha256 of each member). An edit that does
not ship needs no bump (a `.helmignore`d file; a Chart.yaml comment — helm
re-serialises Chart.yaml). The working-tree side packages only what git sees
(tracked and untracked-not-ignored files), so a `__pycache__/` a local test
run left behind does not count; a clean checkout would not ship it.
The version is the one helm read (`<name>-<version>.tgz`); the history walk
reads `^version:` the way release.yaml does.

Scope: charts with no `helm push` site (git-only; push sites from
`check_chart_ship_surface.discover_pushed_charts`). A chart a release line
pushes is listed and skipped: its version is set at the release wrap
(`bump_docs.py`), so a per-PR bump would fight that model.
Those versions are kept unique at publish time instead: release.yaml pushes
charts only through `release_publish.py chart`, which refuses a version
already published with other content before it runs `helm push` (#3848).

Base
----
`--base` when given, else the merge base of `$LINT_DIFF_BASE` /
`origin/$GITHUB_BASE_REF` / `origin/main` with HEAD. On main itself that is
HEAD, and every chart is checked against its version's first commit.
A shallow clone that cuts the history before a version's first commit is
rc 2 — "not looked at" is not "unchanged".

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
    """Return (commit, label) of the base."""
    if explicit:
        commit = _git(repo, "rev-parse", "--verify", f"{explicit}^{{commit}}").stdout.strip()
        return commit, explicit
    try:
        ref, commit = resolve_diff_base_ref()
    except DiffBaseMissingError as exc:
        raise CallerError(str(exc)) from exc
    return commit, f"{ref} (merge base {commit[:12]})"


_VERSION_LINE = re.compile(r"^version:\s*[\"']?([^\"'\s#]+)", re.M)
_NAME_LINE = re.compile(r"^name:\s*[\"']?([^\"'\s#]+)", re.M)


def _blob(repo: Path, rev: str, path: str) -> Optional[str]:
    proc = subprocess.run(  # nosec B603 B607 — fixed argv, no shell
        ["git", "show", f"{rev}:{path}"], cwd=str(repo), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=60)
    return proc.stdout if proc.returncode == 0 else None


def base_charts(repo: Path, base: str) -> Dict[str, str]:
    """chart name -> its directory at `base` (name read from Chart.yaml's
    `name:` line; used only to pair a moved chart with its old directory)."""
    out: Dict[str, str] = {}
    for line in _git(repo, "ls-tree", "-r", "--name-only", base, "--", CHARTS_DIR).stdout.splitlines():
        parts = line.split("/")
        if len(parts) == 3 and parts[2] == "Chart.yaml":
            m = _NAME_LINE.search(_blob(repo, base, line) or "")
            if m:
                out[m.group(1)] = f"{parts[0]}/{parts[1]}"
    return out


def introduced_at(repo: Path, base: str, chart: str, version: str) -> Tuple[str, str]:
    """(commit, chart dir at that commit) that first carried `version`, walking
    `base`'s history of <chart>/Chart.yaml newest-first, renames followed."""
    log = _git(repo, "log", "--follow", "--name-only", "--format=%x00%H", base, "--",
               f"{chart}/Chart.yaml").stdout
    intro: Optional[Tuple[str, str]] = None
    oldest = None
    for entry in log.split("\x00")[1:]:
        lines = [x for x in entry.splitlines() if x.strip()]
        if len(lines) < 2:
            continue
        sha, path = lines[0], lines[-1]
        oldest = sha
        text = _blob(repo, sha, path)
        m = _VERSION_LINE.search(text or "")
        if not m or m.group(1) != version:
            return intro if intro else (sha, path.rsplit("/", 1)[0])
        intro = (sha, path.rsplit("/", 1)[0])
    if intro is None:
        raise CallerError(f"{chart}: no commit on {base[:12]} carries version {version}")
    # The walk ran out of history without seeing another version.
    if _git(repo, "rev-parse", "--is-shallow-repository").stdout.strip() == "true":
        parents = _git(repo, "rev-list", "--parents", "-n", "1", oldest).stdout.split()
        if len(parents) == 1:
            raise CallerError(
                f"{chart}: this clone is shallow and its history ends at {oldest[:12]}, still "
                f"carrying {version} — where that version was introduced cannot be seen. "
                f"Deepen it (`git fetch --deepen=1000 origin` or --unshallow).")
    return intro


def _git_visible_copy(repo: Path, chart: str, dest: Path) -> Path:
    """Copy what a clean checkout of this tree would hold for `chart`: tracked
    and untracked-not-ignored files that exist."""
    listed = _git(repo, "ls-files", "-co", "--exclude-standard", "--", chart).stdout.splitlines()
    for rel in listed:
        src = repo / rel
        if src.is_file():
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(src.read_bytes())
    return dest / chart


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


def check(repo: Path, base: str, scratch: Path, pushed: Set[str]) -> Tuple[List[str], List[str], int]:
    """(violations, notes, charts checked) over every chart in the working tree."""
    violations: List[str] = []
    notes: List[str] = []
    at_base = base_charts(repo, base)
    checked = 0
    for i, chart_yaml in enumerate(sorted((repo / CHARTS_DIR).glob("*/Chart.yaml"))):
        chart = f"{CHARTS_DIR}/{chart_yaml.parent.name}"
        work = scratch / f"c{i}"
        head_dir = _git_visible_copy(repo, chart, work / "tree")
        if not (head_dir / "Chart.yaml").is_file():
            continue  # untracked and ignored: not part of this tree
        name, version, files = package_digest(head_dir, work / "head")
        if name in pushed:
            notes.append(f"{chart}: skipped — a release line pushes {name!r}; its version is "
                         f"set at the release wrap, and nothing here checks it (#1269)")
            continue
        if name not in at_base:
            continue  # a new chart: any version is its first
        checked += 1
        base_dir = at_base[name]
        base_version_m = _VERSION_LINE.search(_blob(repo, base, f"{base_dir}/Chart.yaml") or "")
        base_version = base_version_m.group(1) if base_version_m else ""
        if version != base_version:
            if semver_key(version) <= semver_key(base_version):
                violations.append(f"{chart}: version went DOWN to {version} (base: {base_version}).")
            continue
        sha, intro_dir = introduced_at(repo, base, base_dir, version)
        old = _extract_base_chart(repo, sha, intro_dir, work / "intro-tree")
        if old is None:
            raise CallerError(f"{intro_dir}: no Chart.yaml at {sha[:12]}")
        _, _, old_files = package_digest(old, work / "intro")
        if old_files == files:
            continue
        changed = sorted({k for k in old_files.keys() | files.keys() if old_files.get(k) != files.get(k)})
        shown = ", ".join(changed[:8]) + (f" (+{len(changed) - 8} more)" if len(changed) > 8 else "")
        violations.append(
            f"{chart}: the package changed ({shown}) since {sha[:12]} introduced version "
            f"{version}, and the version is still {version}. Raise `version:` in "
            f"{chart}/Chart.yaml and add a history line: the same version must never name "
            f"two different packages.")
    return violations, notes, checked


def main(argv: Optional[List[str]] = None) -> int:
    try_utf8_stdout()
    parser = argparse.ArgumentParser(
        description=i18n_text(
            "只從 git 取用的 chart：版號沒變時，打包內容（helm package 產出，逐檔比對）"
            "必須與該版號第一次出現的 commit 相同；版號變了則必須比 base 高。",
            "Git-only charts: with the version unchanged, the packaged content (the "
            "`helm package` output, file by file) must equal the commit that first "
            "carried that version; a changed version must be higher than the base's.",
        ),
    )
    parser.add_argument(
        "--base",
        help=i18n_text(
            "base；省略時取 $LINT_DIFF_BASE／origin/$GITHUB_BASE_REF／"
            "origin/main 與 HEAD 的 merge base",
            "the base; omitted, the merge base of $LINT_DIFF_BASE / "
            "origin/$GITHUB_BASE_REF / origin/main with HEAD",
        ),
    )
    parser.add_argument("--repo", default=str(Path(__file__).resolve().parents[3]),
                        help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    repo = Path(args.repo)

    try:
        base, label = resolve_base(repo, args.base)
        pushed = set(discover_pushed_charts(repo))
        with tempfile.TemporaryDirectory(prefix="chart-version-bump-") as tmp:
            violations, notes, checked = check(repo, base, Path(tmp), pushed)
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
    print(f"✓ chart version bump: {checked} git-only chart(s) checked against their version's "
          f"first commit (base {label})")
    return EXIT_OK


if __name__ == "__main__":
    import _lib_exitcodes
    _lib_exitcodes.run(main)
