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
precedence). A new chart, a removed chart and an unchanged package pass.

Scope: charts with no `helm push` site — the git-only charts, installed from a
checkout, whose version nothing else keeps unique (the push sites come from
`check_chart_ship_surface.discover_pushed_charts`, the same derivation
`check_chart_pull_surface` uses for GIT_ONLY). A chart a release line pushes is
listed and skipped, on purpose: its version is set at the release wrap
(`bump_docs.py`), the release job refuses a Chart.yaml version that differs
from the tag, and a git tag cannot be pushed twice — so a published version
already names exactly one package. Requiring a bump in every PR there would
fight that model (tenant-api's appVersion must equal its version, which must be
an image tag the release builds). What this leaves open for those charts: main,
between releases, carries the last released version over newer content.

Base
----
The merge base of `$LINT_DIFF_BASE` / `origin/$GITHUB_BASE_REF` / `origin/main`
with HEAD — the repo's one resolver (`_lint_helpers.resolve_diff_base_ref`).
When HEAD *is* that base (CI on a push to main), it compares with HEAD^1
instead: branch protection does not require a PR to be up to date with main, so
two PRs that each raised a chart to the same new version — each green against
the main it forked from — can both merge, and only main's own run sees the
result.

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
    """Return (commit, label) of what HEAD is compared with."""
    if explicit:
        commit = _git(repo, "rev-parse", "--verify", f"{explicit}^{{commit}}").stdout.strip()
        return commit, explicit
    try:
        ref, commit = resolve_diff_base_ref()
    except DiffBaseMissingError as exc:
        raise CallerError(str(exc)) from exc
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    if commit == head:
        parent = _git(repo, "rev-parse", "--verify", "HEAD^1^{commit}").stdout.strip()
        return parent, f"HEAD^1 ({parent[:12]}; HEAD is {ref} itself)"
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


def check_chart(repo: Path, base: str, chart: str, scratch: Path,
                pushed: Set[str]) -> Tuple[Optional[str], Optional[str]]:
    """(violation, note) for one chart; either may be None."""
    head_dir = repo / chart
    if not (head_dir / "Chart.yaml").is_file():
        return None, None  # removed (or not a chart): nothing ships under its name
    name, head_version, head_files = package_digest(head_dir, scratch / "head")
    if name in pushed:
        return None, (f"{chart}: skipped — a release line pushes {name!r}, so its tag "
                      f"keeps each published version unique")
    base_dir = _extract_base_chart(repo, base, chart, scratch / "base-tree")
    if base_dir is None:
        return None, None  # new chart: any version is its first
    _, base_version, base_files = package_digest(base_dir, scratch / "base")
    if base_files == head_files:
        return None, None
    if semver_key(head_version) > semver_key(base_version):
        return None, None
    changed = sorted(
        {k for k in base_files.keys() | head_files.keys() if base_files.get(k) != head_files.get(k)})
    shown = ", ".join(changed[:8]) + (f" (+{len(changed) - 8} more)" if len(changed) > 8 else "")
    verb = "still" if head_version == base_version else "went DOWN to"
    return (f"{chart}: the package changed ({shown}) but version {verb} {head_version} "
            f"(base: {base_version}). Raise `version:` in {chart}/Chart.yaml and add a "
            f"history line: the same version must never name two different packages."), None


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
            "origin/main 與 HEAD 的 merge base，HEAD 就是該 base 時改比 HEAD^1",
            "commit to compare with; omitted, the merge base of $LINT_DIFF_BASE / "
            "origin/$GITHUB_BASE_REF / origin/main with HEAD, or HEAD^1 when HEAD "
            "is that base",
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
        for chart in charts:
            with tempfile.TemporaryDirectory(prefix="chart-version-bump-") as tmp:
                v, note = check_chart(repo, base, chart, Path(tmp), pushed)
            if v:
                violations.append(v)
            if note:
                notes.append(note)
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
