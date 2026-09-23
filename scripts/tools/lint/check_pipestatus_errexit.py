#!/usr/bin/env python3
"""PIPESTATUS read in a file that never relaxes errexit (TRK-382 / #1845).

Under ``set -e`` + ``set -o pipefail`` a non-zero pipeline terminates the
script on the pipeline line, so a later ``${PIPESTATUS[...]}`` read is
unreachable: the guard looks present and never runs. Both historical
instances (TRK-376 in a workflow, TRK-381 in ``bench_wrapper.sh``) were
found by human review; shellcheck has no opinion on the shape.

WHAT THIS CHECKS — a file-level text predicate, deliberately NOT a
reachability analysis (three versions of that were refuted in #1820; see
#1845 "已被打死的方向"). A file is *strict* when its non-comment lines:

- turn errexit on (``-<flags with e>`` or ``-o errexit``), and
- turn pipefail on (``-<flags ending in o> pipefail``), and
- never relax either (``+<flags with e>``, ``+o errexit``, ``+o pipefail``).

⛔ The two sides are read with deliberately different widths, because they
fail in opposite directions. A wrong "arms errexit/pipefail" makes a file
strict ⇒ a loud red with an exemption exit. A wrong "relaxes" clears the
whole file ⇒ a silent miss. So every doubt is resolved toward loud:

- **Arming is read wide**: any token after the first ``set`` word on the
  logical line counts, whatever the command is (``echo set foo -e`` and
  ``set -- -e`` arm it too — over-reports, not misses). A logical line
  joins physical lines ending in ``\\``; tokens split on whitespace and
  ``;&|()`` and lose surrounding quotes (``set -euo "pipefail"``).
- **Relaxing is read narrow**: only a line that *starts* with ``set``;
  its arguments end at ``;`` ``&`` ``|`` ``#`` and option parsing ends at
  ``--`` / ``-`` (``set -- +e`` sets a positional parameter, not a flag).
  A relaxing ``set`` anywhere else (``foo; set +e``, ``if set +e``) is not
  seen — an over-report, not a miss.

Neither side models bash beyond this; there is no attempt to decide which
``set`` a word really belongs to (two regex versions of that were refuted
by blind review).

Every non-comment line of a strict file that reads ``PIPESTATUS`` is a
violation unless the same line carries an inline exemption::

    rc=${PIPESTATUS[0]}  # pipestatus-ok: <why this read is reachable>

The predicate is wider than the real defect: a read that is locally
protected (inside ``if`` / ``||`` / after a trailing ``&``) is flagged too.
The exemption is the exit for an author who can see why the coarse rule is
wrong here. ⛔ Its reason text cannot be verified — ``# pipestatus-ok: ok``
passes. That residual is accepted, not solved (#1443).

Known boundaries (each pinned by a test in
``tests/lint/test_check_pipestatus_errexit.py``):

- Only whole-line comments (``^\\s*#``) are masked. A trailing comment
  that mentions ``PIPESTATUS`` or an arming flag counts as code (loud).
- A line starting with ``set +e`` inside a heredoc or a multi-line string
  is read as a real relaxation and clears the file (silent).
- Relaxation is file-level: one ``set +e`` anywhere clears every read in
  the file, including reads outside the relaxed region.
- Flags set outside the file text are invisible: shebang arguments,
  ``bash -e script.sh``, and GitHub Actions ``shell: bash`` (which implies
  ``-eo pipefail``). The Actions default shell (no ``shell:`` key) is
  ``bash -e {0}`` — no pipefail — so it is not strict.

Population: ``git ls-files`` under ``--root``, filtered to ``*.sh`` and
``.github/workflows/*.y[a]ml``. The report prints the population counts on
every run; they are not copied into any doc.

Exit codes: 0 ok (or violations without ``--ci``) / 1 violation (``--ci``)
/ 2 cannot measure — not a git repo, empty population, unreadable file.

Usage:
    python scripts/tools/lint/check_pipestatus_errexit.py          # report
    python scripts/tools/lint/check_pipestatus_errexit.py --ci     # exit 1
    python scripts/tools/lint/check_pipestatus_errexit.py --json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_THIS_DIR, ".."))
try:
    from _lib_compat import try_utf8_stdout  # noqa: E402
except Exception:  # pragma: no cover
    def try_utf8_stdout() -> None:  # type: ignore
        pass
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR  # noqa: E402

_COMMENT_LINE = re.compile(r"^\s*#")
_SET_WORD = re.compile(r"(?<![\w$.-])set(?![\w-])")
_LINE_START_SET = re.compile(r"^\s*set(?:\s+|$)")
_ARG_END = re.compile(r"[;&|#]")
_WIDE_SPLIT = re.compile(r"[\s;&|()]+")
_READ = re.compile(r"PIPESTATUS")
_EXEMPT = re.compile(r"#\s*pipestatus-ok:(.*)$")
_POPULATION = re.compile(r"(?:\.sh|^\.github/workflows/[^/]+\.ya?ml)$")


class MeasureError(Exception):
    """The population could not be measured — maps to rc 2, never to rc 0."""


@dataclass
class FileResult:
    path: str
    strict: bool
    violations: List[Tuple[int, str]] = field(default_factory=list)
    exempted: List[int] = field(default_factory=list)


def _flags(tok: str, sigil: str) -> str:
    """Letters of a ``-xyz`` / ``+xyz`` option cluster, or "" if it is not one.

    Plain string checks, not a regex: a backtracking pattern here was
    quadratic on one long near-matching token.
    """
    body = tok[1:]
    if len(tok) > 1 and tok[0] == sigil and body.isascii() and body.isalpha():
        return body
    return ""


def _logical_lines(code: List[Tuple[int, str]]) -> List[str]:
    """Join physical lines that end in a backslash continuation."""
    out: List[str] = []
    buf = ""
    for _, ln in code:
        if ln.endswith("\\"):
            buf += ln[:-1] + " "
            continue
        out.append(buf + ln)
        buf = ""
    if buf:
        out.append(buf)
    return out


def _arms(line: str) -> Tuple[bool, bool]:
    """Wide side: (errexit, pipefail) armed by any token after the first ``set`` word."""
    m = _SET_WORD.search(line)
    if not m:
        return False, False
    toks = [t.strip("'\"") for t in _WIDE_SPLIT.split(line[m.end():])]
    toks = [t for t in toks if t]
    errexit = pipefail = False
    for i, tok in enumerate(toks):
        nxt = toks[i + 1] if i + 1 < len(toks) else ""
        flags = _flags(tok, "-")
        if "e" in flags or (tok == "-o" and nxt == "errexit"):
            errexit = True
        if flags.endswith("o") and nxt == "pipefail":
            pipefail = True
    return errexit, pipefail


def _relaxes(line: str) -> bool:
    """Narrow side: only a line-start ``set``, only its own options."""
    m = _LINE_START_SET.match(line)
    if not m:
        return False
    rest = line[m.end():]
    end = _ARG_END.search(rest)
    toks = (rest[:end.start()] if end else rest).split()
    for i, tok in enumerate(toks):
        if tok in ("--", "-"):
            return False
        if "e" in _flags(tok, "+"):
            return True
        if tok == "+o" and i + 1 < len(toks) and toks[i + 1] in ("errexit", "pipefail"):
            return True
    return False


def scan_text(path: str, text: str) -> FileResult:
    """Pure core: apply the file-level predicate to one file's text."""
    code = [(i, ln) for i, ln in enumerate(text.splitlines(), 1)
            if not _COMMENT_LINE.match(ln)]
    armed = [_arms(ln) for ln in _logical_lines(code)]
    strict = (any(e for e, _ in armed) and any(p for _, p in armed)
              and not any(_relaxes(ln) for _, ln in code))
    result = FileResult(path=path, strict=strict)
    if not strict:
        return result
    for lineno, ln in code:
        if not _READ.search(ln):
            continue
        m = _EXEMPT.search(ln)
        if m and m.group(1).strip():
            result.exempted.append(lineno)
        elif m:
            result.violations.append((lineno, "exemption has an empty reason"))
        else:
            result.violations.append((lineno, "PIPESTATUS read in a strict file"))
    return result


def enumerate_population(root: Path) -> List[str]:
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z"],
            capture_output=True, check=False, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MeasureError(f"git ls-files failed: {exc}") from exc
    if out.returncode != 0:
        raise MeasureError(
            f"git ls-files rc={out.returncode} under {root} (not a git repo?): "
            f"{out.stderr.decode('utf-8', 'replace').strip()}")
    files = [p for p in out.stdout.decode("utf-8", "replace").split("\0")
             if p and _POPULATION.search(p)]
    if not files:
        raise MeasureError(
            f"population is empty under {root}: no tracked *.sh or "
            f".github/workflows/*.y[a]ml — nothing was measured")
    return sorted(files)


def scan_repo(root: Path) -> List[FileResult]:
    results = []
    for rel in enumerate_population(root):
        try:
            text = (root / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise MeasureError(f"cannot read {rel}: {exc}") from exc
        results.append(scan_text(rel, text))
    return results


def _repo_root() -> Path:
    p = Path(_THIS_DIR).resolve()
    for parent in [p, *p.parents]:
        if (parent / ".git").exists():
            return parent
    return p.parent.parent.parent


def main(argv: Optional[List[str]] = None) -> int:
    try_utf8_stdout()
    parser = argparse.ArgumentParser(
        description="PIPESTATUS read in a file that never relaxes errexit (#1845)")
    parser.add_argument("--ci", action="store_true", help="exit 1 on violation")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--root", type=Path, default=None,
                        help="repository root to scan (default: this repo)")
    args = parser.parse_args(argv)

    root = (args.root or _repo_root()).resolve()
    try:
        results = scan_repo(root)
    except MeasureError as exc:
        print(f"[pipestatus-errexit] ⛔ cannot measure: {exc}", file=sys.stderr)
        return EXIT_CALLER_ERROR

    strict = [r for r in results if r.strict]
    bad = [r for r in results if r.violations]
    n_viol = sum(len(r.violations) for r in results)
    n_exempt = sum(len(r.exempted) for r in results)

    if args.json:
        print(json.dumps({
            "population": len(results),
            "strict_files": len(strict),
            "violations": [{"path": r.path, "line": ln, "reason": why}
                           for r in bad for ln, why in r.violations],
            "exempted": [{"path": r.path, "line": ln}
                         for r in results for ln in r.exempted],
        }, ensure_ascii=False, indent=2))
    else:
        print(f"[pipestatus-errexit] population={len(results)} "
              f"strict_files={len(strict)} violations={n_viol} exempted={n_exempt}")
        for r in bad:
            for ln, why in r.violations:
                print(f"  ❌ {r.path}:{ln}: {why}")
        if bad:
            print("  This file turns on errexit + pipefail and never relaxes them, so a\n"
                  "  non-zero pipeline exits before the PIPESTATUS read runs. Either wrap\n"
                  "  the pipeline in `set +e` … `set -e`, or, if the read is reachable\n"
                  "  here, add `# pipestatus-ok: <why>` on that line.", file=sys.stderr)

    if n_viol and args.ci:
        return EXIT_VIOLATION
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
