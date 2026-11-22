#!/usr/bin/env python3
"""check_doc_quotes.py — a declared quote in docs/ must match its source line for line (#1558).

A fenced block in docs/**/*.md that declares itself an excerpt of a file in
the repo,

    ```yaml {.quote src="k8s/03-monitoring/deployment-prometheus.yaml"}
    ```text {.quote src="scripts/tools/metric-dictionary.yaml" anchor="mysql_global_status_threads_connected:"}

is held to that file. Opt-in: a block without `.quote` is not looked at, and
this guard claims nothing about undeclared quotes, inline code spans, code
comments or CHANGELOG entries.

The rules (one place: `match_quote`):

* An **elision line** is a whole line that, once its indentation and an
  optional comment leader (`#`, `//`, `--`, `;`) are off, starts with `...`
  or `…`. The rest of the line is a free note (`# ... the other Rule Packs
  likewise`). An elision inside a line (`foo … bar`) is not one: that line
  does not match, and the block is red.
* The block is cut at its elision lines into **segments**. Each segment must
  EQUAL a run of consecutive whole lines of the source. Two normalisations
  only: each side's common indentation is removed (a doc may lift a nested
  excerpt to the left margin), and trailing whitespace is dropped. No
  Unicode folding, no punctuation or whitespace squeezing: `→` written `->`,
  a full-width `；` written `;`, a clause cut off the end of a line are what
  this guard exists to catch.
* Segments appear in the source in order and do not overlap; between two
  segments the source skips at least one line (an elision that elides
  nothing misleads as much as one that is not marked). A leading or trailing
  elision line is optional (a quote is a part of its source), but when
  written it must stand for at least one line of the source too.
* Prose saying "excerpt" does not mark an elision: where the gap is must be
  marked in the block.
* `anchor`, when given, is a literal that occurs exactly once in the source
  (0 or 2+ times is red); the first segment is searched from the anchor's
  line on. Without an anchor the search starts at the top. No line numbers,
  no regex.
* `src` is a path relative to the repository root, of a regular file inside
  it, outside the directories the repository ignores (`UNTRACKED_DIRS`).
* The `.quote` vocabulary is closed: `.quote`, `src` (mandatory), `anchor`;
  any other attribute is an error. It is read by the docs' YAML inventory's
  `parse_declaration` (the one reading of fence attributes), and on a YAML
  block it excludes every consumer attribute of md-yaml-fence-check.
* A page `foo.md` and its translation `foo.en.md` carry the same number of
  `.quote` blocks with the same multiset of `src` (a quote updated in one
  language and not the other is the drift this pairing catches).

Which code blocks exist is the site's renderer's answer, read through
scripts/tools/_lib_doc_yaml_inventory.py (`Renderer`, `locate_blocks`): no
fence regex of its own (#3814).

What it does not do, on purpose: it runs no tool, no subprocess and no git,
and opens no network connection; it reads files only. "Is `src` tracked by
git" is therefore approximated without git: the file exists, is a regular
file whose real path is inside the repository, and no directory on its path
is one the repository ignores (`UNTRACKED_DIRS`, mirrored from .gitignore's
directory entries). A single ignored FILE elsewhere (a `_scratch.json`) is
not recognised locally; CI checks out tracked files only, so a quote of an
untracked file is red there (`src` does not exist). Editing a source file
does not run this hook locally (its `files:` covers docs/ and the guard);
ci.yml's Lint job runs it on every PR.

Exit codes: 0 every declared quote matches; 1 a violation; 2 the site's
renderer is not installed or mkdocs.yml cannot be read (never a skip).

Usage:
  python3 scripts/tools/lint/check_doc_quotes.py [--repo-root .] [--report]
"""
from __future__ import annotations

import argparse
import difflib
import os
import re
import sys
import textwrap
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_THIS_DIR, ".."))
from _lib_exitcodes import EXIT_CALLER_ERROR, EXIT_OK, EXIT_VIOLATION  # noqa: E402
import _lib_doc_yaml_inventory as inventory  # noqa: E402  (the renderer and the fence vocabulary)

#: An elision line: indentation, an optional comment leader, then `...` / `…`.
ELISION_RE = re.compile(r"^\s*(?:(?:#|//|--|;)\s*)?(?:\.\.\.|…)")

#: Directories the repository ignores (.gitignore's directory entries), as
#: repo-relative prefixes; `ANYWHERE_DIRS` match at any depth. A `src` under
#: one is not a tracked file.
UNTRACKED_DIRS = (
    ".git/", ".build/", ".vibe/", "site/", "tmp/", "dev/", ".schemathesis/",
    "docs/assets/dist/", "docs/rule-packs/", "docs/internal/design-reviews/",
    "tests/_test_output/", "tests/_test_multidb_output/", "_ci_logs/",
)
ANYWHERE_DIRS = frozenset({
    "node_modules", "__pycache__", ".pytest_cache", ".ruff_cache", ".hypothesis",
    ".benchmarks", "htmlcov", ".vite", ".idea",
})

#: An inline code span in the rendered HTML: a `<code` outside a `<pre>`.
_PRE_RE = re.compile(r"<pre\b.*?</pre>", re.S)


@dataclass
class Quote:
    path: str                 # the doc, repo-relative
    line: int                 # 1-based line of the opening fence
    body: "list[str]"         # the block's lines
    src: "str | None" = None
    anchor: "str | None" = None
    errors: "list[str]" = field(default_factory=list)


def is_elision(line: str) -> bool:
    return bool(ELISION_RE.match(line))


def _norm(lines: "list[str]") -> "list[str]":
    """Trailing whitespace off, then the common indentation."""
    return textwrap.dedent("\n".join(line.rstrip() for line in lines)).split("\n")


def split_segments(body: "list[str]"):
    """``(segments, leading, trailing, problem)``: *segments* are
    ``(offset, lines)`` with *offset* the index in *body* of the segment's
    first line; *leading* / *trailing*: the block starts / ends with an
    elision line. *problem* names a block that cannot be matched (nothing
    but elisions, two elision lines with nothing between)."""
    cuts = [i for i, line in enumerate(body) if is_elision(line)]
    leading = bool(cuts) and cuts[0] == 0
    trailing = bool(cuts) and cuts[-1] == len(body) - 1
    segments: "list[tuple[int, list[str]]]" = []
    start = 0
    for c in cuts + [len(body)]:
        segments.append((start, body[start:c]))
        start = c + 1
    if leading:
        segments = segments[1:]
    if trailing and segments:
        segments = segments[:-1]
    problem = None
    if not segments or all(not seg for _, seg in segments):
        problem = "the block has no line to compare (only elision lines, or empty)"
    else:
        for off, seg in segments:
            if not seg:
                problem = (f"two elision lines with nothing between (block line {off}): "
                           "write one")
                break
    return segments, leading, trailing, problem


def _positions(want: "list[str]", src: "list[str]", lo: int) -> "list[int]":
    """Every i >= lo where src[i:i+len(want)], normalised, equals *want*."""
    k = len(want)
    first = want[0].strip()
    return [i for i in range(lo, len(src) - k + 1)
            if src[i].strip() == first and _norm(src[i:i + k]) == want]


def match_quote(body: "list[str]", src: "list[str]", start: int = 0):
    """Hold a quote's *body* to its source lines *src*, searching from
    *start* (the anchor's line). Returns None when it matches, else
    ``(segment index, body offset, message)`` for the first segment that
    could not be placed (the deepest one any placement reached)."""
    segments, leading, trailing, problem = split_segments(body)
    if problem:
        return (0, 0, problem)
    wants = [_norm(seg) for _, seg in segments]
    deepest = [0, start]   # (segment index, the lowest line it was searched from)

    def place(idx: int, lo: int) -> bool:
        if idx == len(wants):
            return True
        if idx > deepest[0]:
            deepest[:] = [idx, lo]
        for i in _positions(wants[idx], src, lo):
            if idx == 0 and leading and i == 0:
                continue   # a leading elision line must stand for a line
            end = i + len(wants[idx])
            if idx == len(wants) - 1:
                if trailing and end >= len(src):
                    continue   # a trailing elision line must stand for a line
                return True
            if place(idx + 1, end + 1):   # +1: an elision skips at least one line
                return True
        return False

    if place(0, start):
        return None
    idx, lo = deepest
    off, seg = segments[idx]
    return (idx, off, _explain(wants[idx], src, lo, idx, leading, trailing, len(wants)))


def _explain(want, src, lo, idx, leading, trailing, n) -> str:
    """Why segment *idx* (normalised *want*) has no place at or after source
    line *lo*: its longest prefix found in the source and the first line that
    differs, or the source line closest to its first line."""
    k = len(want)
    best, best_i = 0, None
    for i in range(lo, len(src)):
        window = _norm(src[i:i + k])
        run = 0
        while run < min(len(window), k) and window[run] == want[run]:
            run += 1
        if run > best:
            best, best_i = run, i
    after = f"at or after source line {lo + 1}"
    if best_i is not None and best == k:
        # the whole segment is in the source, but not where the rules allow
        why = ("an elision line must stand for at least one source line"
               if (idx == 0 and leading) or (idx == n - 1 and trailing)
               else "segments must follow each other in order with at least one source "
                    "line skipped between them")
        return (f"segment {idx + 1} equals source lines {best_i + 1}-{best_i + k}, but {why}")
    if best_i is not None:
        got = _norm(src[best_i:best_i + k])
        theirs = got[best] if best < len(got) else "<end of file>"
        return (f"segment {idx + 1} is not a run of source lines {after}; closest: its first "
                f"{best} line(s) match source lines {best_i + 1}-{best_i + best}, then segment "
                f"line {best + 1} {want[best]!r} != source line {best_i + best + 1} "
                f"{theirs!r}")
    target = want[0].strip()
    scored = [(difflib.SequenceMatcher(None, target, line.strip()).ratio(), j)
              for j, line in enumerate(src)]
    ratio, j = max(scored) if scored else (0.0, -1)
    if j < 0:
        return f"segment {idx + 1}: the source is empty"
    return (f"segment {idx + 1} is not a run of source lines {after}: its first line "
            f"{want[0]!r} is in no source line; the closest is source line {j + 1} "
            f"{src[j].strip()!r}")


def resolve_src(root: Path, src: str) -> "tuple[Path | None, str | None]":
    """(path, None) for a `src` that names a tracked-looking regular file in
    the repository, else (None, why)."""
    if src.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", src) or "\\" in src:
        return None, f"src={src!r} must be a path relative to the repository root, with '/'"
    parts = src.split("/")
    if any(p in ("", ".", "..") for p in parts):
        return None, f"src={src!r} must be a plain repo path (no '.', '..' or empty part)"
    rel = "/".join(parts)
    for d in UNTRACKED_DIRS:
        if (rel + "/").startswith(d):
            return None, f"src={src!r} is under {d}, which the repository ignores (no tracked file)"
    if ANYWHERE_DIRS & set(parts[:-1]):
        return None, f"src={src!r} is under a directory the repository ignores"
    path = root / rel
    if not path.is_file():
        return None, f"src={src!r} does not exist (or is not a regular file)"
    real = Path(os.path.realpath(path))
    try:
        real.relative_to(Path(os.path.realpath(root)))
    except ValueError:
        return None, f"src={src!r} resolves outside the repository ({real})"
    return path, None


def read_lines(path: Path) -> "list[str]":
    """The source's lines, split on '\\n' only; a final newline adds no line."""
    text = path.read_bytes().decode("utf-8")
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def collect(root: Path, renderer: "inventory.Renderer"):
    """(quotes, file errors, stats) for every docs/**/*.md under *root*."""
    quotes: "list[Quote]" = []
    errors: "list[tuple[str, int, str]]" = []
    stats = Counter()
    for rel, content in inventory.iter_markdown(root):
        lines = content.split("\n")
        blocks = renderer.code_blocks(content)
        stats["files"] += 1
        stats["fenced"] += len(blocks)
        stats["inline"] += _PRE_RE.sub("", renderer.html).count("<code")
        tab = renderer.md.tab_length
        for block, i in inventory.locate_blocks(rel, lines, blocks, tab_length=tab,
                                                errors=errors,
                                                select=inventory.is_quote_fence,
                                                kind=".quote"):
            n = block.source.count("\n") + 1
            opening = lines[i - 1].expandtabs(tab)
            prefix = inventory._CONTAINER_PREFIX_RE.match(opening).group(0)
            body = block.source.split("\n") if ">" in prefix else lines[i:i + n]
            q = Quote(rel, i, body, src=block.attrs.get("src"))
            decl = inventory.parse_declaration(block, inventory._info_text(opening[len(prefix):]))
            if decl.errors:
                q.errors.extend(decl.errors)
            elif decl.quote is None:   # pragma: no cover — is_quote_fence picked it
                q.errors.append("not a .quote fence")
            else:
                q.src, q.anchor = decl.quote
            quotes.append(q)
    stats["quotes"] = len(quotes)
    return quotes, errors, stats


def judge(root: Path, q: Quote) -> None:
    """Fill *q*.errors: its source, its anchor, its segments."""
    if q.errors:
        return
    path, why = resolve_src(root, q.src)
    if why:
        q.errors.append(why)
        return
    try:
        src = read_lines(path)
    except UnicodeDecodeError as e:
        q.errors.append(f"src={q.src!r} is not UTF-8 text ({e})")
        return
    start = 0
    if q.anchor is not None:
        text = "\n".join(src)
        count = text.count(q.anchor)
        if count != 1:
            q.errors.append(f"anchor={q.anchor!r} occurs {count} time(s) in {q.src}; it must "
                            "occur exactly once")
            return
        start = text[:text.index(q.anchor)].count("\n")
    miss = match_quote(q.body, src, start)
    if miss is not None:
        _, off, message = miss
        q.errors.append(f"(block line {q.line + 1 + off}) {message}")


def pair_errors(quotes: "list[Quote]", docs: "set[str]") -> "list[tuple[str, int, str]]":
    """`foo.md` and `foo.en.md`: the same number of `.quote` blocks and the
    same multiset of `src` (#1558)."""
    by: "dict[str, list[Quote]]" = {}
    for q in quotes:
        by.setdefault(q.path, []).append(q)
    out = []
    for en in sorted(d for d in docs if d.endswith(".en.md")):
        zh = en[:-len(".en.md")] + ".md"
        if zh not in docs:
            continue
        a = Counter(q.src for q in by.get(zh, []))
        b = Counter(q.src for q in by.get(en, []))
        if a != b:
            line = (by.get(zh) or by.get(en))[0].line
            out.append((zh, line, (
                f"{zh} has {sum(a.values())} .quote block(s) {sorted(a.elements())} and "
                f"{en} has {sum(b.values())} {sorted(b.elements())}: a page and its "
                "translation quote the same sources the same number of times")))
    return out


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(
        description="A ```<lang> {.quote src=...} block in docs/ must match its source (#1558).")
    parser.add_argument("--repo-root", default=".", help="repository root (default: .)")
    parser.add_argument("--report", action="store_true",
                        help="list every declared quote with PASS/FAIL")
    args = parser.parse_args(argv)
    root = Path(args.repo_root).resolve()
    if not (root / "docs").is_dir():
        print(f"ERROR: no docs/ under {root}", file=sys.stderr)
        return EXIT_CALLER_ERROR
    try:
        renderer = inventory.Renderer(root)
        quotes, errors, stats = collect(root, renderer)
    except inventory.RendererError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return EXIT_CALLER_ERROR
    for q in quotes:
        judge(root, q)
        errors.extend((q.path, q.line, f"src={q.src!r}: {e}") for e in q.errors)
    docs = {rel for rel, _ in inventory.iter_markdown(root)}
    errors.extend(pair_errors(quotes, docs))
    if args.report:
        for q in quotes:
            print(f"{'FAIL' if q.errors else 'PASS'}  {q.path}:{q.line}  src={q.src}"
                  + (f"  anchor={q.anchor!r}" if q.anchor else ""))
    for path, line, msg in sorted(errors):
        print(f"{path}:{line}: {msg}")
    print(f"{stats['files']} file(s), {stats['fenced']} fenced block(s), "
          f"{stats['quotes']} declared .quote block(s), {len(errors)} error(s); "
          f"{stats['inline']} inline code span(s) and "
          f"{stats['fenced'] - stats['quotes']} undeclared block(s) not checked")
    return EXIT_VIOLATION if errors else EXIT_OK


if __name__ == "__main__":
    import _lib_exitcodes
    _lib_exitcodes.run(main)
