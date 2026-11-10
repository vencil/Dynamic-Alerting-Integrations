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
2. ``ADR-027`` followed by a section code of the withdrawn 2026-07 draft
   (``D2-B``, ``LD-6 P4b``, ``MED-8``, …) is a finding whatever follows: those
   codes name sections the rewritten ADR does not have.

Not checked: a section cited without its ADR (``§7`` alone, ``see §3
below``) — inside an ADR that is its own; elsewhere it cannot be resolved.

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
    "tests/lint/test_check_adr_section_refs.py": "this lint's own fixtures: references made up to fail",
}
SKIP_SUFFIXES = (".golden", ".png", ".jpg", ".svg", ".ico", ".gz", ".woff", ".woff2", ".pdf")

# A section token: a number, a code (letters then a digit: D3, S6b-2), or a
# name running to the next space or punctuation.
_TOKEN = r"(?:\d+(?:\.\d+)*|[A-Za-z]{1,4}\d[\w-]*|[^\s,;:()（）、。，；：/\\\[\]{}\"'`*|<>]+)"
# ADR-NNN, an optional 's or space, then the first §section.
_REF = re.compile(r"ADR-(\d{3})(?:'s)?\s?§\s?(" + _TOKEN + r")")
# Further sections chained after a reference: "、§3", ", §3", " / §3", " and §3".
_CHAIN = re.compile(r"\s*(?:[、,，/]|\band\b|與|和|及)\s*§\s?(" + _TOKEN + r")")

# ADR-027's withdrawn draft codes (the "舊引用對照" table the rewrite
# retired): after "ADR-027" with only separators between, a finding.
_OLD_027 = re.compile(
    r"ADR-027(?:'s)?[\s/:：·,，(（-]*"
    r"(D1|D2-A|D2-B|D4|G4|O1|L7|Phase-1|MED-7|MED-8|LD-6(?:\s?P\d+[a-z]?)?)(?![\w-])"
)

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


def load_adrs(root: Path) -> dict[str, list[Heading]]:
    """ADR number → the headings of its files (every language)."""
    adrs: dict[str, list[Heading]] = {}
    for p in sorted((root / ADR_DIR).glob("[0-9][0-9][0-9]-*.md")):
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


def scan_text(path: str, text: str, adrs: dict[str, list[Heading]]) -> list[Finding]:
    """Every finding in one file's text."""
    out: list[Finding] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for m in _OLD_027.finditer(line):
            out.append(Finding(path, lineno, "027", m.group(1),
                               "a section code of the withdrawn draft; cite the section that holds it now"))
        for m in _REF.finditer(line):
            adr, tokens = m.group(1), [m.group(2)]
            pos = m.end()
            while True:
                c = _CHAIN.match(line, pos)
                if not c:
                    break
                tokens.append(c.group(1))
                pos = c.end()
            headings = adrs.get(adr)
            for tok in tokens:
                if headings is None:
                    out.append(Finding(path, lineno, adr, tok, "no such ADR under docs/adr/"))
                elif not section_exists(headings, tok):
                    out.append(Finding(path, lineno, adr, tok, "no heading of the ADR has this number or name"))
    return out


def tracked_files(root: Path) -> list[str]:
    res = subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True, timeout=60, check=True)
    return [p for p in res.stdout.decode("utf-8").split("\0") if p]


def find_findings(root: Path | None = None) -> list[Finding]:
    base = root if root is not None else REPO_ROOT
    adrs = load_adrs(base)
    if not adrs:
        raise FileNotFoundError(f"no ADR under {base / ADR_DIR}")
    out: list[Finding] = []
    for rel in tracked_files(base):
        if rel in SKIP_PATHS or rel.endswith(SKIP_SUFFIXES):
            continue
        try:
            text = (base / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue  # binary, or a submodule / link git tracks
        if "ADR-" not in text:
            continue
        out.extend(scan_text(rel, text, adrs))
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
