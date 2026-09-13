#!/usr/bin/env python3
"""Measure how much prose agents ship in CHANGELOG entries and PR bodies, and whether PR bodies carry evidence blocks.

The first line above is deliberately one sentence: generate_tool_map.py publishes
only a docstring's FIRST LINE into docs/internal/tool-map{,.en}.md.

    python3 scripts/tools/dx/agent_output_metrics.py changelog
    python3 scripts/tools/dx/agent_output_metrics.py changelog --section v2.9.0 --cap 1000 --json
    python3 scripts/tools/dx/agent_output_metrics.py pr-bodies --limit 25

WHAT THIS IS FOR
================
The agent-harness plan (2026-09-13) changes how agents are asked to write:
evidence blocks instead of narrative, a per-entry cap on CHANGELOG prose, a
four-section PR template. Every one of those changes needs a BEFORE number
and the same measurement run again AFTER, or "it helped" is a feeling. This
tool is that measurement. It is not a gate: exit 0 whenever it measured
something, and the cap is reported, never enforced (the enforcing lint is a
separate change in generate_changelog.py --lint).

METRICS
=======
``changelog``
    An ENTRY is one column-0 ``- `` bullet inside one ``## [<section>]`` block,
    plus every indented continuation line under it (nested bullets, wrapped
    prose, indented fences). A blank line does not close an entry; the next
    column-0 line that is not a bullet does (a ``### `` heading, an HTML
    comment, a column-0 fence). Reported: entry count, character length at
    median / p75 / p90 / max (nearest-rank percentiles over ``len(str)``,
    i.e. code points, not bytes -- CJK-safe), how many entries exceed
    ``--cap``, and the ``--top`` longest entries with their line numbers.

``pr-bodies``
    ``gh pr list --state <state> --limit <n> --json number,body``. Reported:
    PR count, body length distribution (same percentiles), and how many
    bodies contain an EVIDENCE FENCE: a fenced code block whose first
    non-blank line starts with ``$ `` (a command the author claims to have
    run). That shape is the contract the plan asks agents to follow; before
    the contract exists the count is the baseline.

WHAT IT CANNOT TELL YOU
=======================
Length is a proxy for prose, not a measure of it: a 3,000-character entry
may be a legitimate table. Read the ``longest`` list before drawing a
conclusion. An evidence fence proves the SHAPE, not that the command ran.

EXIT CODES (scripts/tools/_lib_exitcodes.py)
============================================
  0  measured (a metric that is merely "bad" is still exit 0)
  2  cannot do the job: no subcommand, CHANGELOG path or section missing,
     ``gh`` missing / failing / returning something that is not JSON
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

_HEADING_RE = re.compile(r"^## \[?(?P<name>[^\]\s]+)\]?")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_EVIDENCE_FIRST_LINE_RE = re.compile(r"^\s*\$ \S")


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
        if m and m.group("name") == section:
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
    Continuation = lines starting with two spaces or a tab (non-blank). Blank
    lines are skipped without closing the entry. Any other column-0 line
    closes it. Lines inside a column-0 fenced block (three backticks or
    ``~~~`` at column 0) are neither bullets nor continuation: a ``- `` there
    is code, not an entry.
    """
    cur_no: Optional[int] = None
    cur: List[str] = []
    in_fence = False
    for offset, line in enumerate(block):
        if line.startswith("```") or line.startswith("~~~"):
            in_fence = not in_fence
            if cur_no is not None:
                yield cur_no, "\n".join(cur)
            cur_no, cur = None, []
            continue
        if in_fence:
            continue
        if line.startswith("- "):
            if cur_no is not None:
                yield cur_no, "\n".join(cur)
            cur_no = first_line_no + offset
            cur = [line]
        elif not line.strip():
            continue
        elif cur_no is not None and (line.startswith("  ") or line.startswith("\t")):
            cur.append(line)
        else:
            if cur_no is not None:
                yield cur_no, "\n".join(cur)
            cur_no, cur = None, []
    if cur_no is not None:
        yield cur_no, "\n".join(cur)


def nearest_rank(sorted_values: Sequence[int], p: float) -> int:
    """Nearest-rank percentile: the value at ceil(p * n), 1-based."""
    if not sorted_values:
        return 0
    k = max(1, math.ceil(p * len(sorted_values)))
    return sorted_values[k - 1]


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
    longest = sorted(entries, key=lambda e: len(e[1]), reverse=True)[:top]
    return {
        "status": "ok",
        "metric": "changelog",
        "section": section,
        "entries": len(entries),
        "chars": length_stats(lengths),
        "cap": cap,
        "over_cap": sum(1 for n in lengths if n > cap),
        "longest": [
            {"line": no, "chars": len(body), "head": body.splitlines()[0][:80]}
            for no, body in longest
        ],
    }


# ============================================================
# pr-bodies
# ============================================================


def has_evidence_fence(body: str) -> bool:
    """True when some fenced block's first non-blank line starts with ``$ ``."""
    in_fence = False
    awaiting_first = False
    for line in body.splitlines():
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            awaiting_first = in_fence
            continue
        if in_fence and awaiting_first and line.strip():
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
    print(f"changelog [{r['section']}]: {r['entries']} entries; chars median "
          f"{c['median']} p75 {c['p75']} p90 {c['p90']} max {c['max']}; "
          f"over cap {r['cap']}: {r['over_cap']}")
    for e in r["longest"]:
        print(f"  L{e['line']:<6} {e['chars']:>6}  {e['head']}")


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
    parser.add_argument("--json", action="store_true",
                        help="emit one JSON document on stdout and nothing else")
    sub = parser.add_subparsers(dest="metric")

    ch = sub.add_parser("changelog", help="entry length distribution for one CHANGELOG section")
    ch.add_argument("--path", default=DEFAULT_CHANGELOG, help="CHANGELOG file (default: CHANGELOG.md)")
    ch.add_argument("--section", default=DEFAULT_SECTION,
                    help="section name as written in the '## [...]' heading (default: Unreleased)")
    ch.add_argument("--cap", type=int, default=DEFAULT_CAP,
                    help="report how many entries exceed this many characters (default: 1000)")
    ch.add_argument("--top", type=int, default=DEFAULT_TOP,
                    help="how many longest entries to list (default: 5)")

    pr = sub.add_parser("pr-bodies", help="PR body length distribution and evidence-fence count via gh")
    pr.add_argument("--limit", type=int, default=DEFAULT_PR_LIMIT, help="how many PRs (default: 25)")
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
        return _fail(f"gh pr list failed (rc {exc.returncode}): {(exc.stderr or '').strip()[-300:]}")
    try:
        prs = json.loads(raw)
    except json.JSONDecodeError as exc:
        return _fail(f"gh pr list did not return JSON: {exc}")
    if not isinstance(prs, list):
        return _fail("gh pr list returned JSON that is not a list")
    _emit(measure_pr_bodies(prs, args.state, args.limit), args.json)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
