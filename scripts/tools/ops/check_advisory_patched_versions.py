#!/usr/bin/env python3
"""check_advisory_patched_versions.py — every advisory's `patched_versions` names a version that exists (#1269).

A security advisory's affected-package row has two version fields: the
vulnerable range and the patched versions. GitHub validates neither for
ecosystem `other` (every advisory this repo has), so a row can name a version
that was never published, or stay empty after the fix shipped — and the
advisory then tells a reader nothing they can act on. Nothing checked either.

What it checks, per row of every advisory (draft and published):

* the package name says WHICH version space the row is in — the part in
  parentheses is one of
      (container image: ghcr.io/<owner>/<image>)          registry tags
      (Helm chart: oci://ghcr.io/<owner>/charts/<chart>)  registry tags
      (Helm chart, git-only: helm/<dir>)                  every `version:` that
                                                          <dir>/Chart.yaml has
                                                          had in HEAD's history
      (platform release: git tag v*)                      git tags v<version>
  A name in no such form is a violation: the space is the question, and a
  free-text name cannot answer it.
* every row has a readable vulnerable range;
* a non-empty `patched_versions` lists concrete versions (comma-separated, no
  operators), each one EXISTS in that space (a leading `v` is ignored on both
  sides: image tags carry it, chart versions do not) and lies OUTSIDE the
  vulnerable range — naming the last affected version is the likeliest slip;
* an empty `patched_versions` is a violation once the space holds a version
  OUTSIDE the vulnerable range — a fixed version has been published and the
  row still does not name it. While none exists, it is listed, not failed.
  Pre-releases (`2.9.9-rc1`) never count as that fix.

A git-only chart "has" every version its Chart.yaml carried in any commit of
HEAD's history — that is what a reader can check out. A chart whose directory
moved loses the versions it had under the old path (the history is not
followed across the move).

Advisory states: `draft`, `triage` and `published` are checked; `closed` and
`withdrawn` make no claim and are skipped; any other value (or none) is rc 2,
not a silent skip.

⚠️ Timing — what this does NOT do. It runs at `make pre-tag`, before the tag is
pushed, so the version a release is about to create does not exist yet and
cannot be named. The flow it supports: tag → release publishes the fixed
version → fill `patched_versions` → publish the advisory. If that last fill is
forgotten, the NEXT pre-tag (any line) fails here. It does not fail the
release that carries the fix itself.

It runs whether or not `ADVISORY_ACK=1` is set: that acknowledges "drafts exist
and are not published now", which says nothing about whether a row is right.

Exit codes
    0  every row resolves (unfilled rows with no fixed version listed)
    1  violation
    2  could not ask (no gh, API refused, registry unreachable, shallow clone
       for a git-only row, unparsable range). Never a silent pass.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess  # nosec B404 — fixed argv, no shell
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple

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

REPO_ROOT = Path(__file__).resolve().parents[3]

_SEMVER = re.compile(
    r"^v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?(?:\+[0-9A-Za-z.-]+)?$"
)
_PACKAGE = re.compile(
    r"^(?P<artifact>.+?) \((?P<kind>container image|Helm chart|Helm chart, git-only|"
    r"platform release): (?P<where>[^()]+)\)$"
)
_CONSTRAINT = re.compile(r"^(<=|>=|<|>|=)\s*(\S+)$")


class CallerError(Exception):
    """The check could not be RUN — never reported as a pass."""


def semver_key(version: str) -> Tuple:
    m = _SEMVER.match(version)
    if not m:
        raise ValueError(version)
    major, minor, patch, pre = m.groups()
    pre_key: Tuple = (1,) if pre is None else (
        0, *[(0, int(p), "") if p.isdigit() else (1, 0, p) for p in pre.split(".")])
    return (int(major), int(minor), int(patch), pre_key)


def norm(version: str) -> str:
    return version[1:] if version.startswith("v") else version


def in_range(version: str, vulnerable: str) -> bool:
    """Is `version` inside a GHSA vulnerable range ("< 1.2.3", ">= 1.0.0, < 2.0.0", "= 0.3.1")?"""
    key = semver_key(version)
    for part in vulnerable.split(","):
        part = part.strip()
        m = _CONSTRAINT.match(part)
        if not m:
            raise CallerError(f"cannot read the vulnerable range {vulnerable!r} (part {part!r})")
        op, bound = m.groups()
        try:
            b = semver_key(bound)
        except ValueError as exc:
            raise CallerError(f"vulnerable range {vulnerable!r}: {bound!r} is not semver") from exc
        ok = {"<": key < b, "<=": key <= b, ">": key > b, ">=": key >= b, "=": key == b}[op]
        if not ok:
            return False
    return True


def fixed_versions(known: Set[str], vulnerable: str) -> List[str]:
    """Versions that came AFTER the vulnerable ones, oldest first. Outside the
    range is not enough: for `= 0.3.1`, 0.1.0 is outside it too, and it is no
    fix. Newer than every affected version we know of — or, when none of them
    is in the space, newer than every bound the range names."""
    versions = sorted(known, key=semver_key)
    affected = [v for v in versions if in_range(v, vulnerable)]
    if affected:
        floor, strict = semver_key(affected[-1]), True
    else:
        # The bound itself can be the fix: `< 2.9.8` with 2.9.8 published.
        bounds = [semver_key(m.group(2)) for p in vulnerable.split(",")
                  if (m := _CONSTRAINT.match(p.strip()))]
        floor, strict = max(bounds), False
    return [v for v in versions
            if (semver_key(v) > floor if strict else semver_key(v) >= floor)
            and not in_range(v, vulnerable)
            and "-" not in v]  # a pre-release is not the fix a reader installs


@dataclass(frozen=True)
class Space:
    kind: str
    where: str

    @property
    def label(self) -> str:
        return f"{self.kind}: {self.where}"


def parse_package(name: str) -> Optional[Space]:
    m = _PACKAGE.match(name.strip())
    if not m:
        return None
    kind, where = m.group("kind"), m.group("where").strip()
    if kind == "platform release" and where != "git tag v*":
        return None
    if kind == "Helm chart, git-only" and not re.fullmatch(r"helm/[A-Za-z0-9._-]+", where):
        return None
    return Space(kind, where)


# ── version spaces ──────────────────────────────────────────────────────────


def _git(repo: Path, *args: str) -> str:
    try:
        proc = subprocess.run(  # nosec B603 B607 — fixed argv, no shell
            ["git", *args], cwd=str(repo), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        raise CallerError(f"git {' '.join(args)} failed to run: {exc}") from exc
    if proc.returncode != 0:
        raise CallerError(f"git {' '.join(args)} exited {proc.returncode}: {proc.stderr.strip()}")
    return proc.stdout


def git_chart_versions(repo: Path, chart_dir: str) -> Set[str]:
    """Every `version:` <chart_dir>/Chart.yaml has had in HEAD's history. Read
    the way release.yaml reads it (`grep '^version:'`)."""
    if _git(repo, "rev-parse", "--is-shallow-repository").strip() == "true":
        raise CallerError(
            f"{chart_dir}: this clone is shallow, so the versions its Chart.yaml has had "
            "cannot all be seen — `git fetch --unshallow` (a missing version here would be "
            "'not looked at', not 'never existed')")
    out: Set[str] = set()
    path = f"{chart_dir}/Chart.yaml"
    for sha in _git(repo, "log", "--format=%H", "--", path).split():
        try:
            text = _git(repo, "show", f"{sha}:{path}")
        except CallerError:
            continue  # the commit that deleted it
        m = re.search(r"^version:\s*[\"']?([^\"'\s#]+)", text, re.M)
        if m:
            out.add(m.group(1))
    if not out:
        raise CallerError(f"{path} has no `version:` anywhere in HEAD's history")
    return out


def git_platform_tags(repo: Path) -> Set[str]:
    return {t[1:] for t in _git(repo, "tag", "-l", "v*").split() if _SEMVER.match(t)}


def ghcr_tags(where: str, opener: Callable = urllib.request.urlopen) -> Set[str]:
    """Tags of a public ghcr.io repository, via the anonymous pull token."""
    ref = where[len("oci://"):] if where.startswith("oci://") else where
    host, _, repo = ref.partition("/")
    if host != "ghcr.io" or not repo:
        raise CallerError(f"{where!r}: only ghcr.io references are supported")
    try:
        with opener(f"https://ghcr.io/token?scope=repository:{urllib.parse.quote(repo)}:pull",
                    timeout=30) as resp:
            token = json.load(resp)["token"]
        tags: Set[str] = set()
        url: Optional[str] = f"https://ghcr.io/v2/{repo}/tags/list?n=1000"
        while url:
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
            with opener(req, timeout=30) as resp:
                tags.update(json.load(resp).get("tags") or [])
                link = resp.headers.get("Link", "")
            m = re.search(r"<([^>]+)>;\s*rel=\"next\"", link)
            url = urllib.parse.urljoin("https://ghcr.io", m.group(1)) if m else None
    except (urllib.error.URLError, OSError, KeyError, ValueError) as exc:
        raise CallerError(f"cannot list the tags of {where}: {exc}") from exc
    return tags


class Spaces:
    """Version space per Space, each fetched once."""

    def __init__(self, repo: Path, registry: Callable[[str], Set[str]] = ghcr_tags):
        self.repo, self.registry, self._cache = repo, registry, {}

    def versions(self, space: Space) -> Set[str]:
        if space not in self._cache:
            if space.kind in ("container image", "Helm chart"):
                raw = self.registry(space.where)
            elif space.kind == "Helm chart, git-only":
                raw = git_chart_versions(self.repo, space.where)
            else:
                raw = git_platform_tags(self.repo)
            self._cache[space] = {norm(v) for v in raw if _SEMVER.match(v)}
        return self._cache[space]


# ── the check ───────────────────────────────────────────────────────────────


def check_row(ghsa: str, row: dict, spaces: Spaces) -> Tuple[Optional[str], Optional[str]]:
    """(violation, note) for one affected-package row."""
    name = ((row.get("package") or {}).get("name") or "").strip()
    where = f"{ghsa} [{name or '<no package name>'}]"
    space = parse_package(name)
    if space is None:
        return (f"{where}: the package name does not say which version space the row is in — "
                f"end it with one of `(container image: ghcr.io/…)`, `(Helm chart: oci://…)`, "
                f"`(Helm chart, git-only: helm/<dir>)`, `(platform release: git tag v*)`"), None
    vulnerable = (row.get("vulnerable_version_range") or "").strip()
    patched = (row.get("patched_versions") or "").strip()
    if not vulnerable:
        return f"{where}: no vulnerable version range — the row says nothing a reader can check", None
    in_range("0.0.0", vulnerable)  # parse it now: an unreadable range is rc 2 either way
    known = spaces.versions(space)
    if patched:
        missing, bad = [], []
        for v in (p.strip() for p in patched.split(",")):
            if not _SEMVER.match(v):
                bad.append(v)
            elif norm(v) not in known:
                missing.append(v)
        if bad:
            return (f"{where}: patched_versions {patched!r} must list concrete versions "
                    f"(no operators): {bad}"), None
        if missing:
            return (f"{where}: patched {missing} does not exist in {space.label} — a reader "
                    f"cannot install it. Fix the version, or publish it first"), None
        affected = [v for v in (p.strip() for p in patched.split(",")) if in_range(v, vulnerable)]
        if affected:
            return (f"{where}: patched {affected} is inside the vulnerable range {vulnerable!r} "
                    f"— that version is affected, not fixed"), None
        return None, None
    fixed = fixed_versions(known, vulnerable)
    if fixed:
        return (f"{where}: patched_versions is empty, but {space.label} already has versions "
                f"newer than everything in {vulnerable!r} (earliest: {fixed[0]}). Fill it with "
                f"the first one that carries the fix"), None
    return None, f"{where}: not yet patched — no version outside {vulnerable!r} exists in {space.label}"


_MAX_PAGES = 100


def _split_response(raw: str) -> Tuple[Dict[str, str], str]:
    """`gh api -i` output → (lower-cased headers, body)."""
    head, sep, body = raw.replace("\r\n", "\n").partition("\n\n")
    if not sep:
        raise CallerError("gh api -i returned no header/body separator")
    headers = {}
    for line in head.splitlines()[1:]:
        k, _, v = line.partition(":")
        headers[k.strip().lower()] = v.strip()
    return headers, body


def fetch_advisories(runner: Callable = subprocess.run) -> List[dict]:
    """All advisories, draft and published. This endpoint pages by CURSOR (the
    `after=` in the Link header) and ignores `page=` (measured: page=1 and
    page=2 return the same item). The cursor is re-sent on the repos/ path:
    `gh api --paginate` follows the Link as given, a repositories/{id}/… URL,
    which some GitHub proxies refuse."""
    out: List[dict] = []
    query = "per_page=100"
    for _ in range(_MAX_PAGES):
        try:
            proc = runner(  # nosec B603 B607 — fixed argv, no shell
                ["gh", "api", "-i", f"repos/{{owner}}/{{repo}}/security-advisories?{query}"],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
        except (OSError, subprocess.SubprocessError) as exc:
            raise CallerError(f"gh api failed to run: {exc}") from exc
        if proc.returncode != 0:
            raise CallerError(f"gh api exited {proc.returncode}: {(proc.stderr or '').strip()}")
        headers, body = _split_response(proc.stdout)
        try:
            batch = json.loads(body)
        except ValueError as exc:
            raise CallerError(f"gh api returned non-JSON: {exc}") from exc
        if not isinstance(batch, list):
            raise CallerError(f"gh api returned {type(batch).__name__}, not a list")
        out.extend(batch)
        m = re.search(r"<[^>]*[?&](after=[^&>]+)[^>]*>;\s*rel=\"next\"", headers.get("link", ""))
        if not m:
            return out
        query = f"per_page=100&{m.group(1)}"
    raise CallerError(f"more than {_MAX_PAGES} pages of advisories — refusing to guess the rest")


def run(advisories: List[dict], spaces: Spaces) -> Tuple[List[str], List[str]]:
    violations, notes = [], []
    for adv in advisories:
        ghsa = adv.get("ghsa_id") or "<no ghsa_id>"
        state = adv.get("state")
        if state in ("closed", "withdrawn"):
            continue  # they make no claim
        if state not in ("draft", "published", "triage"):
            raise CallerError(f"{ghsa}: unknown advisory state {state!r}")
        rows = adv.get("vulnerabilities") or []
        if not rows:
            violations.append(f"{ghsa}: no affected-package rows at all")
        for row in rows:
            v, n = check_row(ghsa, row, spaces)
            if v:
                violations.append(v)
            if n:
                notes.append(n)
    return violations, notes


def main(argv: Optional[List[str]] = None) -> int:
    try_utf8_stdout()
    parser = argparse.ArgumentParser(
        description=i18n_text(
            "檢查每份 security advisory（draft 與已發布）每一列的 patched_versions："
            "有填就必須在該列的版號空間（registry tag、git-only chart 歷史版號、平台 git tag）"
            "真的存在；沒填而空間裡已有不在受影響範圍內的版本，就是忘了回填。",
            "Check every security advisory row (draft and published): a filled "
            "patched_versions must exist in the row's version space (registry tags, "
            "a git-only chart's historical versions, platform git tags); an empty one "
            "is a violation once a version outside the vulnerable range exists.",
        ),
    )
    parser.add_argument(
        "--advisories-json",
        help=i18n_text("讀這個檔（GitHub API 的 advisory 陣列）而不呼叫 gh",
                       "read this file (a GitHub API advisory array) instead of calling gh"),
    )
    args = parser.parse_args(argv)
    try:
        if args.advisories_json:
            advisories = json.loads(Path(args.advisories_json).read_text(encoding="utf-8"))
        else:
            advisories = fetch_advisories()
        violations, notes = run(advisories, Spaces(REPO_ROOT))
    except (CallerError, OSError, ValueError) as exc:
        print(f"❌ advisory patched-versions check could not run: {exc}", file=sys.stderr)
        print("   This is 'could not ask', not 'nothing wrong'.", file=sys.stderr)
        return EXIT_CALLER_ERROR
    for n in notes:
        print(f"   · {n}")
    if violations:
        print("❌ advisory patched versions (#1269):", file=sys.stderr)
        for v in violations:
            print(f"   - {v}", file=sys.stderr)
        print("   Edit the advisory (Security tab → the advisory → Edit); this check reads "
              "it live.", file=sys.stderr)
        return EXIT_VIOLATION
    print(f"✅ advisory patched versions: {len(advisories)} advisories checked")
    return EXIT_OK


if __name__ == "__main__":
    import _lib_exitcodes
    _lib_exitcodes.run(main)
