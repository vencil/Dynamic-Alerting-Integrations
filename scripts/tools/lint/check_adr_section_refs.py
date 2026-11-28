#!/usr/bin/env python3
"""check_adr_section_refs.py — an ADR section reference must name a section that exists.

ADR-027 §9 item 8. Code, docs and config cite design decisions as
``ADR-NNN §<section>``. An ADR is rewritten or renumbered and the citation
keeps pointing at a section that is gone — or, worse, at a different section
that now carries the number. Nothing breaks, so nothing notices: the reader
is sent to the wrong rationale.

What is checked, in every git-tracked text file:

1. ``ADR-NNN §<section>`` — and the sections chained after it
   (``ADR-036 §2, §3``, ``ADR-027 §2 / §7``, also joined by an
   ideographic comma or a CJK conjunction) — must match a heading of that
   ADR (``docs/adr/NNN-*.md``, the Chinese and the English file together):
     - a number (``§7``, ``§2.3``) matches a heading numbered so
       (``### 7. …``);
     - a code (``§D3``) matches a heading that starts with it (``### D3 …``);
     - a name (``§Consequences``, or a Chinese heading's name) matches a heading whose
       text, its leading number, code and symbols removed, starts with the
       name — or the other way round, since a name written in running CJK
       text has no word boundary after it — or holds it (three characters or
       more: ``§fail-open`` for "gateway fail-open").
   ``ADR-NNN`` naming no ADR file is a finding too.

   Two more shapes are the same reference and are checked the same way:
     - a link: ``[ADR-020](../adr/020-tenant-federation.md) §Gateway`` —
       the link target between ``ADR-NNN`` and ``§`` is skipped — or the
       section as the link text: ``ADR-NNN [§section](../adr/NNN-….md)``;
     - a line break: ``ADR-NNN`` (or its link) ends a line and the next line,
       its comment prefix (``#``, ``//``, ``--``, ``*``, ``;``, ``>``)
       removed, opens with ``§<section>`` — a code comment or a paragraph
       wrapped there; sections chained after it on that next line are
       checked too. The finding is reported on the line with ``ADR-NNN``.
       (A markdown heading right after such a line, ``## §X``, reads as a
       continuation too; the repository has none.)
2. ``ADR-027`` followed by a section code of the withdrawn 2026-07 draft
   (``D2-B``, ``LD-6 P4b``, ``MED-8``, …) is a finding whatever follows: those
   codes name sections the rewritten ADR does not have. The same holds for
   ``ADR-024`` and the codes of its rollout and the draft it replaced
   (``OQ-6``, ``PR3b``, ``AC-7``, ``Phase 1``, ``Pass-3``, ``§3 Reef 1``).
   A section number carrying a letter (``§2b``) is one token: it names no
   heading unless a heading is numbered so.
3. What a reference names inside its section (#4293), the section being the
   one its last § names — in either language's file: a section named in one
   language stands for the section at the same place in the other file:
     - a quoted name right after it (``ADR-036 §2 "Go is the only writer"``, also in
       “…” or 「…」) must appear in that section — its heading or its text,
       markup and spaces ignored: a quote is a section's name or a phrase of
       it, and a renamed one is the stale reference. A quote opened at the
       end of a line and closed on the next (its comment prefix removed) is
       one quote;
     - an item number right after it (``ADR-027 §9 item 8``, ``§2 第 3 項``)
       must be one of the numbered list items at the start of a line in the
       section (its subsections included).

Not checked: a section cited without its ADR (``§7`` alone, ``see §3
below``) — inside an ADR that is its own; elsewhere it cannot be resolved.
Nor a break inside a reference (``ADR-024 §`` with the section on the next
line, or a chain ``§3、`` continued there), a link whose text does not end
with ``ADR-NNN`` (``[ADR-024 X](…) §3``), or a link target holding a space,
a title or a ``)``; none of these is written in the repository today. A YAML
double-quoted string continued by a backslash at the end of the line is not
joined either: it occurs only in the generated ConfigMap copies of
the rule packs, whose source files are checked.

Scope: ``git ls-files`` text files, minus ``SKIP_PATHS`` (each with its
reason). Heading text is read outside fenced code blocks.

Usage:  python3 scripts/tools/lint/check_adr_section_refs.py [--ci] [--json]
Exit:   0 clean | 1 findings (with --ci) | 2 cannot run (no git, no docs/adr)

Lint class: (a) enumerated SOT — the headings of docs/adr/ are the set; a
reference outside it is the finding. No allowlist and no bypass tag: a stale
reference is fixed by pointing it at the section that holds the decision now.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, ".."))  # Repo subdir layout
from _lib_exitcodes import EXIT_CALLER_ERROR, EXIT_OK, EXIT_VIOLATION, Report  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]
ADR_DIR = Path("docs/adr")

# Paths not scanned, each with its reason. Keep it short: a stale reference
# anywhere else is fixed, not exempted.
SKIP_PATHS: dict[str, str] = {
    "CHANGELOG.md": "released notes record what a release said at the time; they are not rewritten",
    "docs/CHANGELOG.md": "a symlink to CHANGELOG.md, for the docs site",
    "tests/lint/test_check_adr_section_refs.py": "this lint's own fixtures: references made up to fail",
}
SKIP_SUFFIXES = (".golden", ".png", ".jpg", ".svg", ".ico", ".gz", ".woff", ".woff2", ".pdf")

# A section token: a number, a code (letters then a digit: D3, S6b-2), or a
# name running to the next space or punctuation.
_TOKEN = r"(?:\d+(?:\.\d+)*[a-z]?(?![A-Za-z])|[A-Za-z]{1,4}\d[\w-]*|[^\s,;:()（）、。，；：/\\\[\]{}\"'`*|<>]+)"
# ADR-NNN, then an optional 's, or the target of a markdown link whose text
# it is (``[ADR-020](../adr/020-….md)``), an optional space, then the first
# §section, which may open a link's text (``[§section](…)``).
_LINK_TARGET = r"(?:'s|\]\([^)\s]*\))?"
_REF = re.compile(r"ADR-(\d{3})" + _LINK_TARGET + r"\s?\[?§\s?(" + _TOKEN + r")")
# A line that ends with ADR-NNN (or its link), and the comment prefix of the
# line after it: the reference may continue there (see the module docstring).
_REF_AT_EOL = re.compile(r"ADR-(\d{3})" + _LINK_TARGET + r"\s*$")
_COMMENT_PREFIX = re.compile(r"^\s*(?:#+|//+|--+|\*+|;+|>+)?\s*")
# Further sections chained after a reference: "、§3", ", §3", " / §3", " and §3".
_CHAIN = re.compile(r"\s*(?:[、,，/]|\band\b|與|和|及)\s*§\s?(" + _TOKEN + r")")

# ADR-027's withdrawn draft codes (the "舊引用對照" table the rewrite
# retired): after "ADR-027" with only separators between, a finding.
_OLD_027 = re.compile(
    r"ADR-027(?:'s)?[\s/:：·,，(（-]*"
    r"(D1|D2-A|D2-B|D4|G4|O1|L7|Phase-1|MED-7|MED-8|LD-6(?:\s?P\d+[a-z]?)?)(?![\w-])"
)

# ADR-024's retired codes: the rollout batches and acceptance items of the
# draft it replaced, none of them a section of the ADR as written now.
_OLD_024 = re.compile(
    r"ADR-024(?:'s)?[\s/:：·,，(（-]*"
    r"(OQ-\d+|PR-?\d+[a-z]?|AC-\d+|Phase\s?\d+|Pass-\d+|§\s?3\s+Reef(?:\s?\d+)?)(?![\w-])"
)

# What a reference names inside its section (#4293): a quoted name, or an
# item number, right after its last section token.
_QUOTED = re.compile(r'[,，]?\s+["“「]([^"”」\n]{2,80})["”」]')
# A quote opened at the end of a line, and its rest on the next line.
_QUOTE_OPEN = re.compile(r'[,，]?\s+["“「]([^"”」\n]{1,80})$')
_QUOTE_REST = re.compile(r'([^"”」\n]{0,80})["”」]')
_ITEM = re.compile(r"\s+(?:item\s?(\d+)|第\s?(\d+)\s?[項项条條點点])")
_LIST_ITEM = re.compile(r"^(\d+)\.\s")
_SPACES = re.compile(r"\s+")

_HEADING = re.compile(r"^(#{2,6})\s+(.*?)\s*#*\s*$")
_LEAD_NUMBER = re.compile(r"^(\d+(?:\.\d+)*)[.)、：:]?(?:\s+|$)")
_LEAD_CODE = re.compile(r"^([A-Za-z]{1,4}\d[\w-]*)[.)：:]?(?:\s+|$)")
_MARKUP = re.compile(r"[*_`~]|\[([^\]]*)\]\([^)]*\)")


@dataclass(frozen=True)
class Heading:
    number: str  # "7", "2.3", or ""
    code: str  # "D3" (lower-cased), or ""
    name: str  # the rest, lower-cased, markup removed


@dataclass(frozen=True)
class Section:
    heading: Heading
    items: frozenset[int]  # the numbers of its numbered list items
    text: str  # heading and body, markup and spaces removed, lower-cased
    file: str = ""  # the ADR file it is in
    pos: int = 0  # its place among that file's headings


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    adr: str
    section: str
    reason: str


# Leading symbols a heading may open with (⚠️, ⛔, ✅ and the like): not part
# of its name.
_LEAD_SYMBOLS = re.compile(r"^[^\w(（\[]+")


def _clean(text: str) -> str:
    return _LEAD_SYMBOLS.sub("", _MARKUP.sub(lambda m: m.group(1) or "", text).strip())


def parse_heading(text: str) -> Heading:
    """Split a heading into its leading number, leading code and name."""
    text = _clean(text)
    number = code = ""
    m = _LEAD_NUMBER.match(text)
    if m:
        number, text = m.group(1), text[m.end():]
    else:
        m = _LEAD_CODE.match(text)
        if m:
            code, text = m.group(1).lower(), text[m.end():]
    return Heading(number, code, text.strip().lower())


def read_headings(path: Path) -> list[Heading]:
    """The level-2+ headings of a markdown file, outside fenced code blocks."""
    out: list[Heading] = []
    fence = ""
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.lstrip()
        if stripped.startswith(("```", "~~~")):
            marker = stripped[:3]
            fence = "" if fence == marker else (fence or marker)
            continue
        if fence:
            continue
        m = _HEADING.match(line)
        if m:
            out.append(parse_heading(m.group(2)))
    return out


def _flat(text: str) -> str:
    return _SPACES.sub("", _clean(text)).lower()


def read_sections(path: Path) -> list[Section]:
    """Every level-2+ section of a markdown file, outside fenced code
    blocks: its heading, the numbers of its top-level numbered items, and its
    text up to the next heading of the same or a higher level (subsections
    included)."""
    lines = path.read_text(encoding="utf-8").splitlines()
    heads: list[tuple[int, int, str]] = []  # line index, level, heading text
    fence = ""
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith(("```", "~~~")):
            marker = stripped[:3]
            fence = "" if fence == marker else (fence or marker)
            continue
        if fence:
            continue
        m = _HEADING.match(line)
        if m:
            heads.append((i, len(m.group(1)), m.group(2)))
    out: list[Section] = []
    for k, (i, level, text) in enumerate(heads):
        end = next((j for j, lv, _ in heads[k + 1:] if lv <= level), len(lines))
        body = lines[i + 1:end]
        items = frozenset(int(m.group(1)) for b in body if (m := _LIST_ITEM.match(b)))
        out.append(Section(parse_heading(text), items, _flat(" ".join([text, *body])), path.name, k))
    return out


def load_sections(root: Path, tracked: set[str] | None = None) -> dict[str, list[Section]]:
    """ADR number → the sections of its files (every language)."""
    out: dict[str, list[Section]] = {}
    for p in sorted((root / ADR_DIR).glob("[0-9][0-9][0-9]-*.md")):
        if tracked is not None and (ADR_DIR / p.name).as_posix() not in tracked:
            continue
        out.setdefault(p.name[:3], []).extend(read_sections(p))
    return out


def load_adrs(root: Path, tracked: set[str] | None = None) -> dict[str, list[Heading]]:
    """ADR number → the headings of its files (every language). With tracked,
    only the ADR files git tracks count: an ADR left unstaged must not make a
    reference pass here that fails on a clean checkout in CI."""
    adrs: dict[str, list[Heading]] = {}
    for p in sorted((root / ADR_DIR).glob("[0-9][0-9][0-9]-*.md")):
        if tracked is not None and (ADR_DIR / p.name).as_posix() not in tracked:
            continue
        adrs.setdefault(p.name[:3], []).extend(read_headings(p))
    return adrs


def section_exists(headings: list[Heading], token: str) -> bool:
    """Whether token names one of headings (see the module docstring)."""
    token = token.rstrip(".")
    if re.fullmatch(r"\d+(?:\.\d+)*", token):
        return any(h.number == token for h in headings)
    t = token.lower()
    for h in headings:
        if h.code and h.code == t:
            return True
        name = h.name
        if not name:
            continue
        if name.startswith(t) or (len(name) >= 2 and t.startswith(name)):
            return True
        # A name inside the heading ("§fail-open" for "Adjacent gap:
        # gateway fail-open, accepted"): three characters or more, so a
        # stray letter does not match.
        if len(t) >= 3 and t in name:
            return True
    return False


@dataclass(frozen=True)
class Reference:
    adr: str
    sections: list[str]
    quoted: str | None = None  # a quoted name after the last section
    item: int | None = None  # an item number after the last section
    quote_open: str | None = None  # a quote opened at the end of the line


def _sections_after(m: re.Match[str], text: str) -> Reference:
    """The section a _REF match names, those chained after it, and a quoted
    name or an item number right after the last one."""
    tokens = [m.group(2)]
    pos = m.end()
    while True:
        c = _CHAIN.match(text, pos)
        if not c:
            break
        tokens.append(c.group(1))
        pos = c.end()
    quoted = item = quote_open = None
    if q := _QUOTED.match(text, pos):
        quoted = q.group(1)
    elif n := _ITEM.match(text, pos):
        item = int(n.group(1) or n.group(2))
    elif o := _QUOTE_OPEN.match(text, pos):
        quote_open = o.group(1)
    return Reference(m.group(1), tokens, quoted, item, quote_open)


def _close_quote(ref: Reference, next_line: str | None) -> Reference:
    """ref with a quote it opened at the end of its line closed on the
    next line, when the next line closes one."""
    if ref.quote_open is None or next_line is None:
        return ref
    rest = next_line[_COMMENT_PREFIX.match(next_line).end():]
    m = _QUOTE_REST.match(rest)
    if not m:
        return ref
    return Reference(ref.adr, ref.sections, f"{ref.quote_open} {m.group(1)}", ref.item)


def _references(line: str, next_line: str | None) -> list[Reference]:
    """Every reference one line makes, one broken onto the next line
    included."""
    refs = [_close_quote(_sections_after(m, line), next_line) for m in _REF.finditer(line)]
    eol = _REF_AT_EOL.search(line)
    if eol and next_line is not None:
        rest = next_line[_COMMENT_PREFIX.match(next_line).end():]
        joined = f"ADR-{eol.group(1)} {rest}"
        m = _REF.match(joined)  # matches only when § (or [§) opens rest
        if m:
            refs.append(_sections_after(m, joined))
    return refs


def _inside_section(sections: list[Section], ref: Reference) -> str | None:
    """Why what ref names inside its last section is not there, or None."""
    if ref.quoted is None and ref.item is None:
        return None
    tok = ref.sections[-1]
    named = [s for s in sections if section_exists([s.heading], tok)]
    # A section named in one language only: the section at the same place
    # in a file where the token names nothing is the same section.
    places = {s.pos for s in named}
    files = {s.file for s in named}
    held = named + [s for s in sections if s.file not in files and s.pos in places]
    if ref.quoted is not None:
        want = _flat(ref.quoted)
        if not any(want in s.text for s in held):
            return f'the quoted name "{ref.quoted}" is not in §{tok}'
    elif not any(ref.item in s.items for s in held):
        return f"§{tok} has no item {ref.item}"
    return None


def scan_text(path: str, text: str, adrs: dict[str, list[Heading]],
              sections: dict[str, list[Section]] | None = None) -> list[Finding]:
    """Every finding in one file's text. Without sections, what a reference
    names inside its section is not checked."""
    out: list[Finding] = []
    lines = text.splitlines()
    for lineno, line in enumerate(lines, 1):
        for m in _OLD_027.finditer(line):
            out.append(Finding(path, lineno, "027", m.group(1),
                               "a section code of the withdrawn draft; cite the section that holds it now"))
        for m in _OLD_024.finditer(line):
            out.append(Finding(path, lineno, "024", m.group(1),
                               "a code of the rollout or the replaced draft; cite the section that holds it now"))
        next_line = lines[lineno] if lineno < len(lines) else None
        for ref in _references(line, next_line):
            headings = adrs.get(ref.adr)
            missing = False
            for tok in ref.sections:
                if headings is None:
                    out.append(Finding(path, lineno, ref.adr, tok, "no such ADR under docs/adr/"))
                    missing = True
                elif not section_exists(headings, tok):
                    out.append(Finding(path, lineno, ref.adr, tok, "no heading of the ADR has this number or name"))
                    missing = True
            if not missing and sections is not None:
                why = _inside_section(sections.get(ref.adr, []), ref)
                if why:
                    out.append(Finding(path, lineno, ref.adr, ref.sections[-1], why))
    return out


def tracked_files(root: Path) -> list[str]:
    res = subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True, timeout=60, check=True)
    return [p for p in res.stdout.decode("utf-8").split("\0") if p]


def find_findings(root: Path | None = None) -> list[Finding]:
    base = root if root is not None else REPO_ROOT
    files = tracked_files(base)
    adrs = load_adrs(base, set(files))
    sections = load_sections(base, set(files))
    if not adrs:
        raise FileNotFoundError(f"no tracked ADR under {base / ADR_DIR}")
    out: list[Finding] = []
    for rel in files:
        if rel in SKIP_PATHS or rel.endswith(SKIP_SUFFIXES):
            continue
        try:
            text = (base / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue  # binary, or a submodule / link git tracks
        if "ADR-" not in text:
            continue
        out.extend(scan_text(rel, text, adrs, sections))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ci", action="store_true", help="exit 1 on findings")
    ap.add_argument("--json", action="store_true", help="print findings as JSON")
    args = ap.parse_args()
    try:
        findings = find_findings()
    except (OSError, subprocess.SubprocessError) as e:
        print(f"check_adr_section_refs: cannot run: {e}", file=sys.stderr)
        return EXIT_CALLER_ERROR
    status = EXIT_VIOLATION if findings and args.ci else EXIT_OK
    if args.json:
        return Report([asdict(f) for f in findings], status)  # run() encodes it
    if not findings:
        print("OK every ADR section reference names a section that exists.")
    else:
        print(f"❌ {len(findings)} ADR section reference(s) name no section of the ADR (ADR-027 §9 item 8):")
        for f in findings:
            print(f"  {f.path}:{f.line}: ADR-{f.adr} {f.section} — {f.reason}")
    return status


if __name__ == "__main__":
    import _lib_exitcodes
    _lib_exitcodes.run(main)
