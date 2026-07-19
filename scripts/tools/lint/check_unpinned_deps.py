#!/usr/bin/env python3
"""check_unpinned_deps.py — block unpinned dependency-acquisition in CI/build.

Root cause chronicle
---------------------
`pip install <pkg>` / `go install <mod>@latest` with no version tracks
*latest* on every CI run. For behavior-affecting tools (test runner,
property-based generator, coverage, schema validation, the swag OpenAPI
generator) a new upstream release can silently drift test outcomes,
rename a health check, or flip a coverage gate — the "local green, CI
first-run red" version-drift class. It first bit us as the schemathesis
`filter_too_much` seed-flake (#1158): schemathesis was installed
unpinned, and even after pinning it the flake source (hypothesis) still
floated underneath. A sweep then found the same pattern across ~24 CI
install sites (pytest / hypothesis / mkdocs / swag / benchstat / ...).

Root solve = pin every CI/build/dev-container dependency to an SSOT
(`requirements/ci-constraints.txt` for pip via `-c`; explicit `@version`
for `go install`) so an upgrade is an explicit, reviewable PR — and this
gate so a NEW unpinned install can't silently reintroduce the drift.

What counts as pinned (NOT a violation)
  pip : the command carries `-r`/`--requirement` or `-c`/`--constraint`
        (a requirements/constraints file pins every package), OR every
        named package carries `==`/`@`, OR it only upgrades pip itself
        (`--upgrade pip`).
  go  : `go install <mod>@<version>` where <version> is a real tag or
        pseudo-version — only `@latest`/`@master`/`@main`/`@HEAD` flag.
  npm : the installed package carries an explicit `@<version>` (a leading
        `@scope/` is a scope, not a version). `npm ci` / `npx` / bare
        `npm install` (package.json + lockfile) are fine.

Scope: .github/workflows/**, Makefile, .devcontainer/devcontainer.json,
and Dockerfiles. Manual/dev shell scripts under scripts/** are out of
scope for now (they carry hint/NOTE text that reads like installs, and
their few real installs are low-risk manual tools) — a documented v1
boundary, not a silent gap. See BASELINE_ALLOWLIST for the handful of
currently-accepted exceptions.

Usage
-----
  python3 scripts/tools/lint/check_unpinned_deps.py [--ci]

Exit codes (per scripts/tools/_lib_exitcodes.py):
  0  no unpinned install found (or advisory run without --ci)
  1  at least one unpinned install found (--ci)
  2  caller error (repo layout missing)
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import List, NamedTuple

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_THIS_DIR, ".."))
try:
    from _lib_compat import try_utf8_stdout  # noqa: E402
except Exception:  # pragma: no cover - compat shim optional
    def try_utf8_stdout() -> None:  # type: ignore
        pass
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR  # noqa: E402

_SKIP_DIRS = {".git", "node_modules", ".claude", "site", "__pycache__",
              ".mypy_cache", ".pytest_cache", "vendor"}

# Currently-accepted exceptions, matched by (repo-relative file, substring).
# Content-based so it survives line moves. Keep this list SHORT and each
# entry justified — it is the baseline, not a dumping ground.
class _Alw(NamedTuple):
    file: str
    contains: str
    reason: str


BASELINE_ALLOWLIST: List[_Alw] = [
    _Alw("Makefile",
         'benchstat@latest"',
         "hint text inside an echo, not an install (the real benchstat "
         "install in bench-gate-release.yaml is pinned)."),
    _Alw(".github/workflows/commitlint.yaml",
         "npm install --save-dev @commitlint/cli",
         "dev-tool global without a lockfile; pinning npm globals is a "
         "documented #1158 follow-up."),
    _Alw(".github/workflows/docs-ci.yaml",
         "npm install -g @mermaid-js/mermaid-cli",
         "dev-tool global without a lockfile; pinning npm globals is a "
         "documented #1158 follow-up."),
]

_PIP_RE = re.compile(r"\b(?:pip[0-9]?|python[0-9]?\s+-m\s+pip)\s+install\b(?P<rest>.*)")
_GO_RE = re.compile(r"\bgo\s+install\s+(?P<mod>\S+?)@(?P<ver>latest|master|main|HEAD)\b")
_NPM_RE = re.compile(r"\bnpm\s+(?:install|i|add)\b(?P<rest>.*)")
_SHELL_BREAK = re.compile(r"(&&|\|\||;|\|)")
# Segment splitter used to isolate EACH command on a chained line, so a
# second `&& pip install bar` after a pinned first install can't hide.
_SEGMENT_SPLIT = re.compile(r"&&|\|\||;|\|")


class Finding(NamedTuple):
    file: str
    lineno: int
    kind: str
    detail: str
    line: str


def _repo_root() -> Path:
    p = Path(_THIS_DIR).resolve()
    for parent in [p, *p.parents]:
        if (parent / ".git").exists():
            return parent
    return p.parents[2]


def _target_files(root: Path) -> List[Path]:
    out: List[Path] = []
    wf = root / ".github" / "workflows"
    if wf.is_dir():
        out += sorted(wf.glob("*.yml")) + sorted(wf.glob("*.yaml"))
    for extra in ("Makefile", ".devcontainer/devcontainer.json"):
        p = root / extra
        if p.is_file():
            out.append(p)
    # Dockerfiles anywhere in the tree (skip vendored / worktree dirs).
    for cur, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for fn in files:
            if fn == "Dockerfile" or fn.endswith(".Dockerfile"):
                out.append(Path(cur) / fn)
    return out


def _first_command(rest: str) -> str:
    """The segment up to the next shell operator, minus a trailing #comment.

    A trailing unquoted shell comment (`pip install foo==1.0  # pin for X`)
    must not have its `# pin for X` tokenized as packages — that would
    false-flag a correctly pinned install. Split on whitespace-then-`#`, so
    a `#egg=…`/`#fragment` with no preceding space (a VCS URL) is preserved.
    """
    seg = _SHELL_BREAK.split(rest, maxsplit=1)[0]
    return re.split(r"\s#", seg, maxsplit=1)[0]


def _pip_unpinned(rest: str) -> List[str]:
    """Return the unpinned package tokens in a `pip install <rest>` command."""
    seg = _first_command(rest)
    toks = seg.split()
    lowered = {t.split("=", 1)[0] for t in toks}
    if lowered & {"-r", "--requirement", "-c", "--constraint"}:
        return []  # a requirements/constraints file pins everything installed
    upgrading = bool({"-U", "--upgrade"} & set(toks))
    unpinned: List[str] = []
    for t in toks:
        if t.startswith("-") or "/" in t or t in ("\\",):
            continue
        name = t.strip("'\"")
        if not name:
            continue
        if name == "pip" and upgrading:
            continue  # `pip install --upgrade pip`
        if "==" in name or "@" in name:
            continue
        unpinned.append(name)
    return unpinned


def _npm_unpinned(rest: str) -> List[str]:
    seg = _first_command(rest).strip()
    toks = seg.split()
    # bare `npm install` / `npm ci` (no positional package) → uses lockfile.
    pkgs = []
    for t in toks:
        if t.startswith("-"):
            continue
        pkgs.append(t)
    unpinned = []
    for p in pkgs:
        body = p[1:] if p.startswith("@") else p  # drop leading @scope marker
        if "@" in body:
            continue  # carries an explicit @version
        unpinned.append(p)
    return unpinned


def _logical_lines(text: str):
    """Yield (lineno, logical_line), joining trailing-backslash continuations.

    `pip install requests \\`⏎`  -c requirements/ci-constraints.txt` is ONE
    shell command split across two physical lines; scanning them separately
    both misses packages on the continuation AND false-flags a correctly
    pinned install whose `-c`/`-r` lives on the next line. Join first.
    lineno is the FIRST physical line of the logical line.
    """
    raw = text.splitlines()
    i = 0
    while i < len(raw):
        start = i + 1
        buf = raw[i]
        while buf.rstrip().endswith("\\") and i + 1 < len(raw):
            buf = buf.rstrip()[:-1] + " " + raw[i + 1].strip()
            i += 1
        yield start, buf
        i += 1


def scan_file(path: Path, root: Path) -> List[Finding]:
    rel = path.relative_to(root).as_posix()
    findings: List[Finding] = []
    text = path.read_text(encoding="utf-8", errors="replace")
    for lineno, logical in _logical_lines(text):
        stripped = logical.strip()
        # Whole-line comment — incl. Makefile recipe comments that carry the
        # `@` silence prefix (`@# ...`), whose body is documentation only.
        comment_body = stripped[1:].lstrip() if stripped.startswith("@") else stripped
        if comment_body.startswith("#"):
            continue
        if any(a.file == rel and a.contains in logical for a in BASELINE_ALLOWLIST):
            continue
        # Scan EACH `&&`/`;`/`|`-separated command, so a chained second
        # `pip install bar` can't hide behind a pinned first one.
        for seg in _SEGMENT_SPLIT.split(logical):
            m = _PIP_RE.search(seg)
            if m:
                bad = _pip_unpinned(m.group("rest"))
                if bad:
                    findings.append(Finding(rel, lineno, "pip",
                                            f"unpinned package(s): {', '.join(bad)}", stripped))
            gm = _GO_RE.search(seg)
            if gm:
                findings.append(Finding(rel, lineno, "go",
                                        f"`go install {gm.group('mod')}@{gm.group('ver')}`", stripped))
            nm = _NPM_RE.search(seg)
            if nm:
                bad = _npm_unpinned(nm.group("rest"))
                if bad:
                    findings.append(Finding(rel, lineno, "npm",
                                            f"unpinned package(s): {', '.join(bad)}", stripped))
    return findings


def scan_repo(root: Path) -> List[Finding]:
    findings: List[Finding] = []
    for path in _target_files(root):
        findings.extend(scan_file(path, root))
    return findings


def main() -> int:
    try_utf8_stdout()
    parser = argparse.ArgumentParser(
        description="Block unpinned pip/go/npm dependency installs in "
        "CI/build files (#1158). Pin via requirements/ci-constraints.txt "
        "(pip -c) or an explicit @version (go install).")
    parser.add_argument("--root", default=str(_repo_root()),
                        help="Repo root to scan.")
    parser.add_argument("--ci", action="store_true", help="Exit 1 on any finding.")
    args = parser.parse_args()

    root = Path(args.root)
    if not (root / ".github").is_dir() and not (root / "Makefile").is_file():
        print(f"ERROR: does not look like the repo root: {root}", file=sys.stderr)
        return EXIT_CALLER_ERROR

    findings = scan_repo(root)
    if findings:
        print("❌ unpinned dependency install(s) found "
              "(pin via requirements/ci-constraints.txt or an explicit @version):")
        for f in findings:
            print(f"  - {f.file}:{f.lineno} [{f.kind}] {f.detail}")
            print(f"      {f.line}")
    else:
        print("✅ no unpinned pip/go/npm installs in CI/build files.")

    if findings and args.ci:
        return EXIT_VIOLATION
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
