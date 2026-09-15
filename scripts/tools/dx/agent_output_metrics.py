#!/usr/bin/env python3
"""Measure CHANGELOG entry lengths and PR-body evidence blocks; a ruler, not a gate.

    agent_output_metrics.py changelog [--path F] [--section S] [--cap N] [--top N] [--json]
    agent_output_metrics.py pr-bodies [--limit N] [--state S] [--json]

Definitions (the contract a later cap must reuse, not re-derive):

* ENTRY: a column-0 ``- `` bullet inside one ``## [section]`` block plus its
  indented continuation lines (two spaces or a tab). Blank lines inside it
  count (trailing ones dropped); any other line closes it (``### ``, comment,
  column-0 fence, column-0 text). Lines inside a column-0 fence are neither
  bullets nor continuation.
* ``chars``: ``len(str)`` of the raw entry text (code points; prefix,
  indentation and newlines included). ``prose_chars``: the same minus table
  rows (``|``) and lines inside an INDENTED fence.
* EVIDENCE FENCE: a fenced block whose first non-blank line starts with ``$ ``.
  Fences pair the CommonMark way: opener may carry an info string (no backtick
  in a backtick fence's), closer is the same character, not shorter, and
  nothing else on the line.
* Percentiles are nearest-rank.

Caveats: length is a proxy for prose; an evidence fence proves shape, not that
the command ran; column-0 text after a bullet belongs to no entry, so a cap on
this definition must also reject such text or it can be walked around.

Exit codes (scripts/tools/_lib_exitcodes.py): 0 measured; 2 caller/env error
(no subcommand, negative counts, missing file or section, gh missing/failing
or not returning a JSON list of PR objects).
"""
from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import sys
from pathlib import Path
from typing import Iterator, List, Optional, Sequence, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent))
from _lib_exitcodes import EXIT_CALLER_ERROR, EXIT_OK  # noqa: E402

DEFAULT_CHANGELOG = "CHANGELOG.md"
DEFAULT_SECTION = "Unreleased"
DEFAULT_CAP = 1000
DEFAULT_TOP = 5
DEFAULT_PR_LIMIT = 25
GH_TIMEOUT_S = 120

# `## [Name With Spaces] — suffix` or `## bare-name suffix`: the bracketed
# form may contain spaces, the bare form is the first whitespace-free token.
_HEADING_RE = re.compile(r"^## (?:\[(?P<bracketed>[^\]]+)\]|(?P<bare>\S+))")
# A fence line = optional indent, a run of 3+ backticks or tildes, the rest.
_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})(.*)$")
_INDENTED_FENCE_RE = re.compile(r"^\s+(`{3,}|~{3,})(.*)$")
_COLUMN0_FENCE_RE = re.compile(r"^(`{3,}|~{3,})(.*)$")
_TABLE_ROW_RE = re.compile(r"^\s*\|")
_EVIDENCE_FIRST_LINE_RE = re.compile(r"^\s*\$ \S")


def fence_step(fence: Optional[str], marker: str, rest: str) -> Tuple[Optional[str], bool]:
    """CommonMark fence pairing on one candidate line (``marker`` + ``rest``).

    Opening: any info string is allowed except that a backtick fence's info
    string may not contain a backtick. Closing: only the same character, at
    least as long as the opener, followed by nothing but whitespace. Any other
    line is content. Returns (new_fence, toggled)."""
    if fence is None:
        if marker[0] == "`" and "`" in rest:
            return None, False
        return marker, True
    if marker[0] == fence[0] and len(marker) >= len(fence) and not rest.strip():
        return None, True
    return fence, False


def non_negative_int(value: str) -> int:
    n = int(value)
    if n < 0:
        raise argparse.ArgumentTypeError(f"must be >= 0, got {n}")
    return n


# ============================================================
# changelog
# ============================================================


def section_lines(text: str, section: str) -> Optional[Tuple[int, List[str]]]:
    """Return (1-based line number of the heading, lines of the block) for
    ``## [<section>]`` or ``## <section>``; None when the heading is absent.

    The block runs to the next ``## `` heading (exclusive). ``### `` headings
    stay inside the block.
    """
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        m = _HEADING_RE.match(line)
        if m and (m.group("bracketed") or m.group("bare")) == section:
            start = i
            break
    if start is None:
        return None
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if lines[j].startswith("## "):
            end = j
            break
    return start + 1, lines[start + 1:end]


def iter_entries(block: Sequence[str], first_line_no: int) -> Iterator[Tuple[int, str]]:
    """Yield (1-based line number, entry text) for every top-level bullet.

    ``first_line_no`` is the line number of ``block[0]`` in the source file.
    Continuation = non-blank lines starting with two spaces or a tab. Blank
    lines inside an entry are kept (trailing ones dropped). Any other line that is not
    a bullet (column-0 text, a ``### `` heading, a comment, a one-space
    indent) closes it. Lines inside a column-0 fenced block (three backticks
    or ``~~~`` at column 0, closed by the SAME marker) are neither bullets
    nor continuation: a ``- `` there is code, not an entry. Entry text is
    the raw lines joined by newlines, so the ``- `` prefix, indentation and
    newlines all count toward its length.
    """
    cur_no: Optional[int] = None
    cur: List[str] = []
    fence: Optional[str] = None   # the marker that opened the current fence
    for offset, line in enumerate(block):
        m = _COLUMN0_FENCE_RE.match(line)
        if m:
            fence, toggled = fence_step(fence, m.group(1), m.group(2))
            if toggled:
                if cur_no is not None:
                    yield cur_no, _strip_trailing_blank(cur)
                cur_no, cur = None, []
                continue
        if fence is not None:
            continue
        if line.startswith("- "):
            if cur_no is not None:
                yield cur_no, _strip_trailing_blank(cur)
            cur_no = first_line_no + offset
            cur = [line]
        elif not line.strip():
            if cur_no is not None:
                cur.append("")
        elif cur_no is not None and (line.startswith("  ") or line.startswith("\t")):
            cur.append(line)
        else:
            if cur_no is not None:
                yield cur_no, _strip_trailing_blank(cur)
            cur_no, cur = None, []
    if cur_no is not None:
        yield cur_no, _strip_trailing_blank(cur)


def _strip_trailing_blank(lines: List[str]) -> str:
    """Join an entry's lines; blank lines INSIDE it count, trailing ones do not."""
    end = len(lines)
    while end > 1 and not lines[end - 1].strip():
        end -= 1
    return "\n".join(lines[:end])


def nearest_rank(sorted_values: Sequence[int], p: float) -> int:
    """Nearest-rank percentile: the value at ceil(p * n), 1-based."""
    if not sorted_values:
        return 0
    k = max(1, math.ceil(p * len(sorted_values)))
    return sorted_values[k - 1]


def prose_len(entry: str) -> int:
    """Length of an entry with its evidence lines removed.

    Evidence = lines inside an indented fenced block (the fence lines
    themselves included) and table rows (first non-blank char ``|``).
    Everything else, including the bullet line, is prose.
    """
    kept: List[str] = []
    fence: Optional[str] = None
    for line in entry.splitlines():
        m = _INDENTED_FENCE_RE.match(line)
        if m:
            fence, toggled = fence_step(fence, m.group(1), m.group(2))
            if toggled:
                continue
        if fence is not None or _TABLE_ROW_RE.match(line):
            continue
        kept.append(line)
    return len("\n".join(kept))


def length_stats(values: Sequence[int]) -> dict:
    s = sorted(values)
    return {
        "median": nearest_rank(s, 0.5),
        "p75": nearest_rank(s, 0.75),
        "p90": nearest_rank(s, 0.9),
        "max": s[-1] if s else 0,
    }


def measure_changelog(text: str, section: str, cap: int, top: int) -> Optional[dict]:
    found = section_lines(text, section)
    if found is None:
        return None
    heading_no, block = found
    entries = list(iter_entries(block, heading_no + 1))
    lengths = [len(body) for _, body in entries]
    prose = [prose_len(body) for _, body in entries]
    longest = sorted(entries, key=lambda e: len(e[1]), reverse=True)[:max(top, 0)]
    return {
        "status": "ok",
        "metric": "changelog",
        "section": section,
        "entries": len(entries),
        "chars": length_stats(lengths),
        "prose_chars": length_stats(prose),
        "cap": cap,
        "over_cap": sum(1 for n in lengths if n > cap),
        "over_cap_prose": sum(1 for n in prose if n > cap),
        "longest": [
            {"line": no, "chars": len(body), "prose_chars": prose_len(body),
             "head": body.splitlines()[0][:80]}
            for no, body in longest
        ],
    }


# ============================================================
# pr-bodies
# ============================================================


def has_evidence_fence(body: str) -> bool:
    """True when some fenced block's first non-blank line starts with ``$ ``.

    A fence is closed only by the marker that opened it, so a ``~~~`` line
    inside a backtick block is content, not a closer.
    """
    fence: Optional[str] = None
    awaiting_first = False
    for line in body.splitlines():
        m = _FENCE_RE.match(line)
        if m:
            fence, toggled = fence_step(fence, m.group(1), m.group(2))
            if toggled:
                awaiting_first = fence is not None
                continue
        if fence is not None and awaiting_first and line.strip():
            if _EVIDENCE_FIRST_LINE_RE.match(line):
                return True
            awaiting_first = False
    return False


def _run_gh(state: str, limit: int) -> str:
    """Return gh's stdout. Raises FileNotFoundError / CalledProcessError /
    subprocess.TimeoutExpired; the caller maps them to exit 2."""
    proc = subprocess.run(
        ["gh", "pr", "list", "--state", state, "--limit", str(limit),
         "--json", "number,body"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=GH_TIMEOUT_S, check=True,
    )
    return proc.stdout


def pr_shape_error(prs: object) -> Optional[str]:
    """Why ``prs`` is not what ``gh pr list --json number,body`` returns, or None."""
    if not isinstance(prs, list):
        return "gh pr list returned JSON that is not a list"
    for i, p in enumerate(prs):
        if not isinstance(p, dict) or type(p.get("number")) is not int:
            return f"gh pr list element {i} is not an object with an integer 'number'"
        if p.get("body") is not None and not isinstance(p["body"], str):
            return f"gh pr list element {i} has a non-string 'body'"
    return None


def measure_pr_bodies(prs: Sequence[dict], state: str, limit: int) -> dict:
    lengths = [len(p.get("body") or "") for p in prs]
    with_fence = [p["number"] for p in prs if has_evidence_fence(p.get("body") or "")]
    return {
        "status": "ok",
        "metric": "pr-bodies",
        "state": state,
        "limit": limit,
        "prs": len(prs),
        "chars": length_stats(lengths),
        "with_evidence_fence": len(with_fence),
        "evidence_fence_prs": sorted(with_fence),
        "evidence_fence_rule": "fenced block whose first non-blank line starts with '$ '",
    }


# ============================================================
# CLI
# ============================================================


def _print_changelog(r: dict) -> None:
    c = r["chars"]
    p = r["prose_chars"]
    print(f"changelog [{r['section']}]: {r['entries']} entries; chars median "
          f"{c['median']} p75 {c['p75']} p90 {c['p90']} max {c['max']}; "
          f"over cap {r['cap']}: {r['over_cap']}")
    print(f"  prose only (no indented fences / table rows): median {p['median']} "
          f"p75 {p['p75']} p90 {p['p90']} max {p['max']}; over cap: {r['over_cap_prose']}")
    for e in r["longest"]:
        print(f"  L{e['line']:<6} {e['chars']:>6} ({e['prose_chars']:>6} prose)  {e['head']}")


def _print_pr_bodies(r: dict) -> None:
    c = r["chars"]
    print(f"pr-bodies [{r['state']}, last {r['limit']}]: {r['prs']} PRs; chars median "
          f"{c['median']} p75 {c['p75']} p90 {c['p90']} max {c['max']}; "
          f"with evidence fence: {r['with_evidence_fence']}")
    if r["evidence_fence_prs"]:
        print("  evidence fence in: " + ", ".join(f"#{n}" for n in r["evidence_fence_prs"]))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent_output_metrics.py",
        description=("Measure agent prose volume in CHANGELOG entries and PR bodies, "
                     "and how many PR bodies carry an evidence fence. Not a gate."),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    # `--json` is accepted both before and after the subcommand: the parent
    # parser owns it, and each subparser inherits it through `parents=`.
    json_flag = argparse.ArgumentParser(add_help=False)
    json_flag.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                           help="emit one JSON document on stdout and nothing else")
    parser.add_argument("--json", action="store_true",
                        help="emit one JSON document on stdout and nothing else")
    sub = parser.add_subparsers(dest="metric")

    ch = sub.add_parser("changelog", parents=[json_flag],
                        help="entry length distribution for one CHANGELOG section")
    ch.add_argument("--path", default=DEFAULT_CHANGELOG, help="CHANGELOG file (default: CHANGELOG.md)")
    ch.add_argument("--section", default=DEFAULT_SECTION,
                    help="section name as written in the '## [...]' heading (default: Unreleased)")
    ch.add_argument("--cap", type=non_negative_int, default=DEFAULT_CAP,
                    help="report how many entries exceed this many characters (default: 1000)")
    ch.add_argument("--top", type=non_negative_int, default=DEFAULT_TOP,
                    help="how many longest entries to list (default: 5)")

    pr = sub.add_parser("pr-bodies", parents=[json_flag],
                        help="PR body length distribution and evidence-fence count via gh")
    pr.add_argument("--limit", type=non_negative_int, default=DEFAULT_PR_LIMIT,
                    help="how many PRs (default: 25)")
    pr.add_argument("--state", default="merged", choices=("merged", "open", "closed", "all"),
                    help="gh pr list --state (default: merged)")
    return parser


def _emit(result: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, ensure_ascii=False))
    elif result["metric"] == "changelog":
        _print_changelog(result)
    else:
        _print_pr_bodies(result)


def _fail(msg: str) -> int:
    print(f"ERROR: {msg}", file=sys.stderr)
    return EXIT_CALLER_ERROR


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.metric:
        parser.print_usage(sys.stderr)
        return _fail("a subcommand is required: changelog | pr-bodies")

    if args.metric == "changelog":
        path = Path(args.path)
        if not path.is_file():
            return _fail(f"CHANGELOG not found: {path}")
        try:
            text = path.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError as exc:
            return _fail(f"{path} is not UTF-8: {exc}")
        result = measure_changelog(text, args.section, args.cap, args.top)
        if result is None:
            return _fail(f"section '## [{args.section}]' not found in {path}")
        result["path"] = str(path)
        _emit(result, args.json)
        return EXIT_OK

    try:
        raw = _run_gh(args.state, args.limit)
    except FileNotFoundError:
        return _fail("gh not found on PATH")
    except subprocess.TimeoutExpired:
        return _fail(f"gh pr list timed out after {GH_TIMEOUT_S}s")
    except subprocess.CalledProcessError as exc:
        # gh puts the real reason on its FIRST non-empty stderr line and
        # follows it with pages of usage text; report the reason.
        first = next((ln for ln in (exc.stderr or "").splitlines() if ln.strip()), "")
        return _fail(f"gh pr list failed (rc {exc.returncode}): {first.strip()[:300]}")
    try:
        prs = json.loads(raw)
    except json.JSONDecodeError as exc:
        return _fail(f"gh pr list did not return JSON: {exc}")
    shape = pr_shape_error(prs)
    if shape:
        return _fail(shape)
    _emit(measure_pr_bodies(prs, args.state, args.limit), args.json)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
