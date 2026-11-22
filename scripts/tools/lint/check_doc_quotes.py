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

* An **elision line** is a line that, once its indentation and trailing
  whitespace are off, is exactly one of `ELISION_MARKERS`: `# ...`,
  `// ...`, `-- ...`, `; ...`, or the same with `…`. Nothing else is one: no
  note after the marker (a note is unchecked text inside a quote — it goes in
  the prose), and no bare `...` / `…` (a JS spread, a Python stub, YAML's
  document end are source lines, not elisions). An elision inside a line
  (`foo … bar`) is not one either: that line does not match, and the block
  is red. A block with an elision line whose source holds a line that is
  itself a marker is red (`ambiguous elision marker`): the guard cannot tell
  the quoted line from the elision, so it does not guess.
* The block is cut at its elision lines into **segments**; each segment is
  a run of consecutive source lines. Equality is per character after
  trailing whitespace is dropped, with ONE indentation offset: there is a
  single whitespace prefix P such that every non-blank block line d is
  matched by the source line P + d (no tab expansion), and a blank block
  line by a blank source line. P is set where the first segment is placed
  (the source line's indentation minus the block line's, which must be a
  suffix of it), and holds for the whole block: a doc may lift an excerpt
  to the left margin, but not re-nest one line under another, turn a tab
  into spaces or move a recipe to column 0. No Unicode folding, no
  punctuation or whitespace squeezing: `→` written `->`, a full-width `；`
  written `;`, a clause cut off the end of a line are what this guard
  exists to catch.
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
* `src` is a path relative to the repository root, of a regular file, with
  no symlink on its path (the file itself or a directory above it), and not
  a Markdown page (`MARKDOWN_SUFFIXES`, any case): a page that repeats another page verbatim uses
  pymdownx.snippets, and a quote of a page — the doc itself, its
  translation — would vouch for nothing.
* The `.quote` vocabulary is closed: `.quote`, `src` (mandatory), `anchor`;
  any other attribute is an error. It is read by the docs' YAML inventory's
  `parse_declaration` (the one reading of fence attributes), and on a YAML
  block it excludes every consumer attribute of md-yaml-fence-check.
* A declaration that misses the vocabulary is an error, not a block
  silently skipped (`look_alikes`). Caught: a block the site renders whose
  attributes (as the renderer reads them; the highlighter's `title=` is not
  one) hold a class equal to `quote` in another case (`{.Quote}`), or a key
  starting with `src` (`{src2=…}`, `{source=…}`) without `.quote`; `src=` /
  `anchor=` without `.quote` are the vocabulary's own errors. A fence
  opening line the site does NOT render as a block (no braces: ```` ```yaml
  .quote src=… ````; a leftover in an HTML comment) when its info, with
  quoted values removed, holds `.quote` (any case) or a `src…=` key. Not
  caught: a misspelt class or key that shares neither (`{.qoute}`,
  `{from=…}`) — no edit distance is guessed — and a look-alike line inside
  another rendered block's body, which is that block's content.
* A page `foo.md` and its translation `foo.en.md` carry the same number of
  `.quote` blocks with the same multiset of `src` (a quote updated in one
  language and not the other is the drift this pairing catches).

Placement is greedy, and that is exact: once P is fixed, whether a segment
equals the source at a position does not depend on where the other segments
are, and the only constraints between segments are their order and the
one-line minimum gap; placing each segment at its earliest allowed position
leaves every later segment the most room, so if any placement exists the
greedy one is found. Of the first segment's positions only the earliest one
for each distinct P is tried (a later position with the same P only takes
room away). Cost: O(source lines x block lines) per distinct P.

Which code blocks exist is the site's renderer's answer, read through
scripts/tools/_lib_doc_yaml_inventory.py (`Renderer`, `locate_blocks`): no
fence regex of its own (#3814). A block's lines are the file's lines with
the opening fence's container prefix (list indentation, `> `) removed.

What it does not do, on purpose: it runs no tool, no subprocess and no git,
and opens no network connection; it reads files only. Whether `src` is
tracked by git is therefore not judged locally: CI checks out tracked files
only, so a quote of an untracked file is red there (`src` does not exist)
— loud, not silent. Editing a source file does not run this hook locally
(its `files:` covers docs/ and the guard); ci.yml's Lint job runs it on
every PR.

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
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_THIS_DIR, ".."))
from _lib_exitcodes import EXIT_CALLER_ERROR, EXIT_OK, EXIT_VIOLATION  # noqa: E402
import _lib_doc_yaml_inventory as inventory  # noqa: E402  (the renderer and the fence vocabulary)

#: The whole elision lines, once indentation and trailing whitespace are off.
ELISION_MARKERS = frozenset(f"{lead} {dots}" for lead in ("#", "//", "--", ";")
                            for dots in ("...", "…"))

#: An inline code span in the rendered HTML: a `<code` outside a `<pre>`.
_PRE_RE = re.compile(r"<pre\b.*?</pre>", re.S)
#: A fence opening, once the container prefix is off: its info text after it.
_FENCE_OPEN_RE = re.compile(r"^(?:`{3,}|~{3,})(?P<info>.*)$")
#: In an unrendered fence's info, quoted values removed: `.quote` or a `src…=` key.
_QUOTED_VALUE_RE = re.compile(r"\"[^\"]*\"|'[^']*'")
_LOOKS_LIKE_QUOTE_RE = re.compile(r"\.quote\b|\bsrc[\w-]*\s*=", re.I)
#: A `src` naming one of these is a page, not a source file (any case).
MARKDOWN_SUFFIXES = (".md", ".markdown", ".mdx")


@dataclass
class Quote:
    path: str                 # the doc, repo-relative
    line: int                 # 1-based line of the opening fence
    body: "list[str]"         # the block's lines, container prefix off
    src: "str | None" = None
    anchor: "str | None" = None
    errors: "list[str]" = field(default_factory=list)


def is_elision(line: str) -> bool:
    return line.strip() in ELISION_MARKERS


def _indent(line: str) -> str:
    return line[:len(line) - len(line.lstrip(" \t"))]


def split_segments(body: "list[str]"):
    """``(segments, leading, trailing, problem)``: *segments* are
    ``(offset, lines)`` with *offset* the index in *body* of the segment's
    first line, lines with trailing whitespace off; *leading* / *trailing*:
    the block starts / ends with an elision line. *problem* is
    ``(offset, message)`` for a block that cannot be matched."""
    body = [line.rstrip() for line in body]
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
    if not segments or all(not any(seg) for _, seg in segments):
        problem = (0, "the block has no line to compare (only elision lines or blank lines)")
    else:
        for off, seg in segments:
            if not seg:
                problem = (off - 1, "two elision lines with nothing between: write one")
                break
            if not any(seg):
                problem = (off, "a segment of blank lines only: the elision lines around it "
                                "say nothing about where it is")
                break
    return segments, leading, trailing, problem


def _fits(seg: "list[str]", src: "list[str]", i: int, prefix: str) -> bool:
    if i + len(seg) > len(src):
        return False
    for d, s in zip(seg, src[i:i + len(seg)]):
        if (s != "") if d == "" else (s != prefix + d):
            return False
    return True


def _prefix_at(seg: "list[str]", src: "list[str]", i: int) -> "str | None":
    """The P the first segment placed at *i* implies, or None."""
    j = next(k for k, d in enumerate(seg) if d)
    if i + j >= len(src):
        return None
    d, s = seg[j], src[i + j]
    di, si = _indent(d), _indent(s)
    if not si.endswith(di):
        return None
    return si[:len(si) - len(di)]


def match_quote(body: "list[str]", src: "list[str]", start: int = 0):
    """Hold a quote's *body* to its source lines *src* (trailing whitespace
    already off), searching from *start* (the anchor's line). Returns None
    when it matches, else ``(body offset, message)`` for the first block line
    that could not be placed."""
    segments, leading, trailing, problem = split_segments(body)
    if problem:
        return problem
    if leading or trailing or len(segments) > 1:
        marker = next((n for n, s in enumerate(src) if is_elision(s)), None)
        if marker is not None:
            return (0, f"ambiguous elision marker: source line {marker + 1} is itself an "
                       f"elision marker ({src[marker].strip()!r}); this file can only be "
                       "quoted without elisions")
    first = segments[0][1]
    tried: "set[str]" = set()
    best = (0, start, None)   # (segments placed, the lowest line the next one was searched from, P)
    for i in range(start, len(src)):
        if leading and i == 0:
            continue   # a leading elision line must stand for a line
        prefix = _prefix_at(first, src, i)
        if prefix is None or prefix in tried or not _fits(first, src, i, prefix):
            continue
        tried.add(prefix)
        end, placed = i + len(first), 1
        for _, seg in segments[1:]:
            lo = end + 1   # an elision skips at least one line
            hit = next((k for k in range(lo, len(src)) if _fits(seg, src, k, prefix)), None)
            if hit is None:
                break
            end, placed = hit + len(seg), placed + 1
        else:
            if not (trailing and end >= len(src)):
                return None
        if placed > best[0] or best[2] is None:
            best = (placed, end + 1, prefix)
    placed, lo, prefix = best
    if placed == len(segments):
        return (segments[-1][0] + len(segments[-1][1]),
                f"the trailing elision line stands for no line: segment {placed} ends at the "
                "end of the source")
    if placed == 0 and leading and start == 0:
        p0 = _prefix_at(first, src, 0)
        if p0 is not None and _fits(first, src, 0, p0):
            return (segments[0][0] - 1, "the leading elision line stands for no line: "
                                        "segment 1 starts at source line 1")
    off, seg = segments[placed]
    return (off, _explain(seg, src, lo if placed else start, placed, prefix))


def _explain(seg, src, lo, idx, prefix) -> str:
    """Why segment *idx* has no place at or after source line *lo*: its
    longest prefix found there (with P when known) and the first line that
    differs, or the source line closest to its first line."""
    k = len(seg)
    best, best_i = 0, None
    for i in range(lo, len(src)):
        p = prefix if prefix is not None else _prefix_at(seg, src, i)
        if p is None:
            continue
        run = 0
        while run < k and i + run < len(src) and _fits(seg[run:run + 1], src, i + run, p):
            run += 1
        if run > best:
            best, best_i = run, i
    after = f"at or after source line {lo + 1}"
    shown = "" if prefix is None else f" with the indentation offset {prefix!r} set by segment 1"
    if best_i is not None and best == k:
        return (f"segment {idx + 1} equals source lines {best_i + 1}-{best_i + k}, but segments "
                "must follow each other in order with at least one source line skipped "
                "between them")
    if best_i is not None:
        theirs = src[best_i + best] if best_i + best < len(src) else "<end of file>"
        return (f"segment {idx + 1} is not a run of source lines {after}{shown}; closest: its "
                f"first {best} line(s) match source lines {best_i + 1}-{best_i + best}, then "
                f"segment line {best + 1} {seg[best]!r} != source line {best_i + best + 1} "
                f"{theirs!r}")
    target = next(d for d in seg if d)
    for j in range(lo, len(src)):
        if src[j].strip() == target.strip():
            return (f"segment {idx + 1} is not a run of source lines {after}{shown}: its "
                    f"line {target!r} is source line {j + 1} {src[j]!r} with a different "
                    "indentation (one offset holds for the whole block)")
    scored = [(difflib.SequenceMatcher(None, target.strip(), line.strip()).ratio(), j)
              for j, line in enumerate(src)]
    if not scored:
        return f"segment {idx + 1}: the source is empty"
    _, j = max(scored)
    return (f"segment {idx + 1} is not a run of source lines {after}{shown}: its line "
            f"{target!r} is in no source line; the closest is source line {j + 1} "
            f"{src[j].strip()!r}")


def resolve_src(root: Path, src: str) -> "tuple[Path | None, str | None]":
    """(path, None) for a `src` that names a regular, non-Markdown file in
    the repository with no symlink on its path, else (None, why)."""
    if src.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", src) or "\\" in src:
        return None, "it must be a path relative to the repository root, with '/'"
    parts = src.split("/")
    if any(p in ("", ".", "..") for p in parts):
        return None, "it must be a plain repo path (no '.', '..' or empty part)"
    if parts[-1].lower().endswith(MARKDOWN_SUFFIXES):
        return None, ("it is a Markdown page: a page repeated verbatim is embedded "
                      "with pymdownx.snippets (--8<--), not quoted")
    for k in range(1, len(parts) + 1):
        if (root.joinpath(*parts[:k])).is_symlink():
            return None, f"{'/'.join(parts[:k])} is a symlink (not allowed on the path)"
    path = root.joinpath(*parts)
    if not path.is_file():
        return None, "it does not exist (or is not a regular file)"
    return path, None


def read_lines(path: Path) -> "list[str]":
    """The source's lines, split on '\\n' only, trailing whitespace off; a
    final newline adds no line."""
    text = path.read_bytes().decode("utf-8")
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return [line.rstrip() for line in lines]


def _strip_container(lines: "list[str]", i: int, n: int, prefix: str, tab: int = 4):
    """The block's *n* lines from line *i*, the opening fence's container
    *prefix* off, or (None, line) for a line that does not carry it. The
    prefix is compared with tabs expanded (a fence indented with a tab, its
    lines with spaces, is one list item); what is left is kept as written."""
    target = prefix.expandtabs(tab)
    out = []
    for k in range(i, i + n):
        line = lines[k]
        if line.strip() == "" or line.expandtabs(tab).rstrip() == target.rstrip():
            out.append("")
            continue
        j = 0
        while j < len(line) and len(line[:j].expandtabs(tab)) < len(target):
            j += 1
        if line[:j].expandtabs(tab) != target:
            return None, k
        out.append(line[j:])
    return out, None


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
        openings: "set[int]" = set()
        for block, i in inventory.locate_blocks(rel, lines, blocks, tab_length=tab,
                                                errors=errors,
                                                select=inventory.is_quote_fence,
                                                kind=".quote"):
            openings.add(i - 1)
            n = block.source.count("\n") + 1
            opening = lines[i - 1]
            prefix = inventory._CONTAINER_PREFIX_RE.match(opening).group(0)
            body, bad = _strip_container(lines, i, n, prefix, tab)
            q = Quote(rel, i, body or [], src=block.attrs.get("src"))
            decl = inventory.parse_declaration(
                block, inventory._info_text(opening.expandtabs(tab)[len(prefix.expandtabs(tab)):]))
            if bad is not None:
                q.errors.append(f"line {bad + 1} does not carry the fence's container "
                                f"prefix {prefix!r}")
            if decl.errors:
                q.errors.extend(decl.errors)
            elif decl.quote is None:   # pragma: no cover — is_quote_fence picked it
                q.errors.append("not a .quote fence")
            else:
                q.src, q.anchor = decl.quote
            quotes.append(q)
        errors.extend(look_alikes(rel, lines, blocks, openings, tab))
    stats["quotes"] = len(quotes)
    return quotes, errors, stats


def _misdeclared(block: "inventory.CodeBlock") -> bool:
    """A rendered block with no `.quote` vocabulary whose attributes look
    like a quote declaration: a class `quote` in another case, or a key
    starting with `src`."""
    if inventory.is_quote_fence(block):
        return False
    names = [c.lower() for c in block.classes] + [k.lstrip(".").lower() for k in block.attrs]
    return "quote" in names or any(k.lower().startswith("src") for k in block.attrs)


def look_alikes(rel, lines, blocks, openings, tab):
    """Errors for declarations that miss the vocabulary (module docstring)."""
    out = []
    for block, i in inventory.locate_blocks(rel, lines, blocks, tab_length=tab, errors=out,
                                            select=_misdeclared, kind="look-alike"):
        out.append((rel, i, (
            f"this block's attributes {block.classes + sorted(block.attrs)} look like a quote "
            "declaration but are none: write ```<lang> {.quote src=\"<repo path>\"} "
            "(lower-case .quote, src= and anchor= only)")))
    rendered: "set[int]" = set(openings)
    for block, i in inventory.locate_blocks(rel, lines, blocks, tab_length=tab, errors=[],
                                            select=lambda b: True, kind="code"):
        rendered.update(range(i - 1, i + block.source.count("\n") + 1))
    for k, line in enumerate(lines):
        if k in rendered:
            continue
        m = _FENCE_OPEN_RE.match(inventory._CONTAINER_PREFIX_RE.sub("", line, count=1))
        if m and _LOOKS_LIKE_QUOTE_RE.search(_QUOTED_VALUE_RE.sub("", m.group("info"))):
            out.append((rel, k + 1, (
                f"this fence's info {m.group('info').strip()!r} looks like a quote declaration, "
                "but the site renders no code block from it: write "
                "```<lang> {.quote src=\"<repo path>\"} (braces, lower-case .quote, src= and "
                "anchor= only)")))
    return out


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
        q.errors.append(f"it is not UTF-8 text ({e})")
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
        off, message = miss
        q.errors.append(f"(line {q.line + 1 + off}) {message}")


def pair_errors(quotes: "list[Quote]", docs: "set[str]") -> "list[tuple[str, int, str]]":
    """`foo.md` and `foo.en.md`: the same number of `.quote` blocks and the
    same multiset of `src` (#1558). Reported on the Chinese page, at its
    first `.quote` (line 1 when it has none); the English lines are named."""
    by: "dict[str, list[Quote]]" = {}
    for q in quotes:
        by.setdefault(q.path, []).append(q)
    out = []
    for en in sorted(d for d in docs if d.endswith(".en.md")):
        zh = en[:-len(".en.md")] + ".md"
        if zh not in docs:
            continue
        zq, eq = by.get(zh, []), by.get(en, [])
        a, b = Counter(q.src for q in zq), Counter(q.src for q in eq)
        if a != b:
            out.append((zh, zq[0].line if zq else 1, (
                f"{zh} has {sum(a.values())} .quote block(s) {sorted(a.elements())} and "
                f"{en} has {sum(b.values())} {sorted(b.elements())} (at line(s) "
                f"{', '.join(str(q.line) for q in eq) or 'none'}): a page and its "
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
