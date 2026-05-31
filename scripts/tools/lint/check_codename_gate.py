#!/usr/bin/env python3
"""check_codename_gate.py — Layer 2 glossary-driven codename gate (#469).

Layer 1 (``check_codename_leak.py``, #462/#468) hard-codes a catalog of known
codename regexes. That list is inherently *lagging*: every new codename family
coined in v2.9.0+ leaks silently until a human notices and adds a regex —
whack-a-mole. This is the *self-healing* Layer 2.

The SSOT moves into ``docs/glossary.md`` ("## 內部代號 — 禁止用於對外文件" /
"## Explicitly Internal — Do Not Use in Customer Docs"). Registering a new
codename family is one table row there — no code change. The glossary-PR diff
becomes the human gatekeeping moment.

Two complementary detectors run over the same customer-facing scope as Layer 1:

  1. Internal-pattern scan (the TEETH, deterministic, zero-FP): every codename
     template registered in the glossary is compiled to a regex and matched
     directly. A hit is a confirmed leak → counted as a hard violation.
     This is the maintainable successor to Layer 1's hard-coded PATTERNS.

  2. Shape scan (the DISCOVERY, soak/FP-prone): broad regexes catch anything
     that merely *looks like* a codename (hyphen-tags, two-word capitalised
     phrases, Phase/version suffixes). Each shape token is then classified:
       - matches an Internal template   → already counted in (1)
       - listed in glossary Approved     → OK (pass)
       - matches a built-in safe pattern → OK (ADR-NNN, CVE-, SHA-, UTF-, …)
       - otherwise                       → UNREGISTERED → "register first"

Glossary registries are parsed straight from ``docs/glossary.md``:
  - Approved  = every ``**Term**`` entry in the alphabetical dictionary
                (+ parenthetical abbreviations), case-insensitive.
  - Internal  = the codename templates in the Internal section's table.

Template syntax (readers can ignore; the lint compiles it):
  ``{N}`` → a run of digits      ``{X}``  → a single ASCII letter
  ``{x}`` → a single lowercase   ``{AE}`` → a single uppercase A–E
All other characters are literal.

Rollout (per #469 step 3): ship as a non-blocking warn-mode manual hook first
(``stages: [manual]``, no ``--strict``), soak 1–2 weeks to seed the glossary
and measure the unregistered-token rate, then promote to ``--strict`` + auto
stage once that rate is low.

Usage:
    # Full report (default scope, full-file scan — the soak/measurement mode)
    python3 scripts/tools/lint/check_codename_gate.py

    # CI gate: exit 1 on confirmed internal leaks only (unregistered = warn)
    python3 scripts/tools/lint/check_codename_gate.py --ci

    # CI gate, fully promoted: unregistered tokens also fail
    python3 scripts/tools/lint/check_codename_gate.py --ci --strict

    # Wider exploratory scope (T1 + T2 docs)
    python3 scripts/tools/lint/check_codename_gate.py --scope full

Bypass (per lint-policy.md §4): add to PR body
    bypass-lint: codename-gate
    reason: <≥30 words explaining why this is legitimate>

Exit codes:
    0  no hard violations (or bypass matched with audit-trail warning)
    1  hard violation(s) found (with --ci): internal leak, or — with --strict —
       any unregistered shape token
    2  glossary registries empty — glossary.md missing or section renamed
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

# Make stdout tolerate non-ASCII on Windows shells (cp950, cp1252).
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

sys.path.insert(0, str(Path(__file__).parent))
from _lint_helpers import parse_bypass_tag  # noqa: E402

# Reuse Layer 1's scope + hygiene infrastructure verbatim so the two gates can
# never drift apart on *which* files count as customer-facing.
from check_codename_leak import (  # noqa: E402
    DEFAULT_SCAN_PATHS,
    FULL_SCAN_PATHS,
    _is_code_comment,
    iter_files,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
GLOSSARY_PATH = REPO_ROOT / "docs" / "glossary.md"
# Both glossary editions legitimately enumerate the codenames (they ARE the
# catalog) — never scan them for leaks.
GLOSSARY_SELF = {
    (REPO_ROOT / "docs" / "glossary.md").resolve(),
    (REPO_ROOT / "docs" / "glossary.en.md").resolve(),
}

# Heading text (ZH) that opens the Internal-codename table. Matched on the
# CJK title; the EN edition (glossary.en.md) is the bilingual mirror and is not
# parsed — glossary.md is the SSOT (S#101 ZH-primary policy).
INTERNAL_SECTION_RE = re.compile(r"^##\s+內部代號")

# Leading determiners/articles that, as the FIRST word of a two-word-cap shape
# token, mark it as sentence prose ("The Migration", "An Operator"), not a
# codename. Deterministic stopword skip — NOT statistical NLP (#469 explicitly
# rejects non-deterministic corpus/TF-IDF approaches). Single-char "A" never
# matches the shape (needs a lowercase tail), so it is not listed.
_LEADING_DETERMINERS = frozenset({
    "the", "an", "this", "that", "these", "those",
    "its", "our", "your", "their", "his", "her",
})

# ── Shape detectors (broad on purpose; see #469) ──────────────────────────
# These find tokens that merely *look like* a codename. Classification happens
# afterwards against the glossary registries.
SHAPE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # Hyphen-tag: TRK-301, ADR-024, DEC-B, B-1, CVE-2024-1234, K8s-Master, …
    ("hyphen-tag", re.compile(r"(?<![A-Za-z0-9])[A-Z][A-Za-z]{0,4}-[A-Za-z0-9]+\b")),
    # Two-word capitalised: Migration Toolkit, Tenant Manager, Track A, …
    ("two-word-cap", re.compile(r"\b[A-Z][a-z]+\s+[A-Z]+[a-z0-9]*\b")),
    # NOTE: a generic "Phase <token>" shape is intentionally NOT a discovery
    # detector. Layer 1 established that "Phase 1 / Phase A" is legitimate
    # playbook prose (hundreds of FPs); only the dotted internal form
    # "Phase .a/.b/.c" is a codename, caught directly by the `Phase .{x}`
    # Internal template — not via shape discovery.
    # Version-suffix codename: v2.8.0-final / v2.0.0-preview …
    ("version-suffix", re.compile(r"\bv\d+\.\d+\.\d+-[A-Za-z0-9]+\b")),
]

# Token-level safe patterns: a shape match that is a well-known public
# identifier, not a codename. Token-level (not whole-line) so a real codename
# sharing a line with "SHA-256" is still caught.
SAFE_TOKEN_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"^ADR-\d+$"),          # public ADR refs (docs/adr/ is published)
    re.compile(r"^TRK-\d+$"),          # public tracking namespace (ADR-019); cited openly in ADRs
    re.compile(r"^CVE-\d", re.I),
    re.compile(r"^CWE-\d", re.I),
    re.compile(r"^GHSA-", re.I),
    re.compile(r"^RFC-?\d", re.I),
    re.compile(r"^ISO-?\d", re.I),
    re.compile(r"^SHA-(1|256|512)$", re.I),
    re.compile(r"^MD-?5$", re.I),
    re.compile(r"^UTF-(8|16|32)$", re.I),
    re.compile(r"^TLS-", re.I),
    re.compile(r"^HTTP", re.I),
    re.compile(r"^X-[A-Za-z0-9-]+$"),  # HTTP header fragments (X-Forwarded, X-Request, X-B3, X-Amzn…)
    re.compile(r"^v\d+\.\d+\.\d+$"),   # plain semver (no codename suffix)
]

SKIP_EXT = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".pdf", ".zip"}


# ── Template compiler ─────────────────────────────────────────────────────
_TEMPLATE_TOKEN_RE = re.compile(r"(\{N\}|\{X\}|\{x\}|\{AE\})")
_TEMPLATE_MAP = {
    "{N}": r"\d+",
    "{X}": r"[A-Za-z]",
    "{x}": r"[a-z]",
    "{AE}": r"[A-E]",
}


def compile_template(template: str) -> re.Pattern[str]:
    """Compile a glossary codename template (e.g. ``TRK-{N}``) to a regex.

    Placeholders map per ``_TEMPLATE_MAP``; every other character is literal.
    Anchored on both ends with a left look-behind (so ``AB-1`` does not match
    ``{AE}-{N}``) and a trailing ``\\b``.
    """
    parts = _TEMPLATE_TOKEN_RE.split(template)
    body = "".join(_TEMPLATE_MAP.get(p, re.escape(p)) for p in parts if p)
    return re.compile(r"(?<![A-Za-z0-9])" + body + r"\b")


# ── Glossary parsing ──────────────────────────────────────────────────────
def load_glossary(path: Path = GLOSSARY_PATH, internal_section_re=INTERNAL_SECTION_RE):
    """Parse glossary.md → (approved_terms:set[str], internal:list[(tmpl,regex)]).

    approved_terms are lower-cased. internal templates come from the Internal
    section's markdown table (first column, backtick-wrapped). ``path`` defaults
    to the ZH SSOT; ``internal_section_re`` lets callers (e.g. the ZH↔EN parity
    test) point the parser at the EN edition's heading.
    """
    approved: set[str] = set()
    internal: list[tuple[str, re.Pattern[str]]] = []
    if not path.exists():
        return approved, internal

    content = path.read_text(encoding="utf-8", errors="replace")
    lines = content.splitlines()

    # Approved: every **Term** bold entry + parentheticals (matches the
    # convention check_glossary_coverage.py already relies on).
    term_re = re.compile(r"^\*\*(.+?)\*\*")
    for line in lines:
        m = term_re.match(line)
        if not m:
            continue
        raw = m.group(1).strip()
        approved.add(raw.lower())
        # ZH terms use FULLWIDTH parens （）; English terms use ASCII (). Handle
        # both so e.g. "Tenant Manager（租戶管理介面）" registers the clean
        # "tenant manager" token, not just the paren-suffixed string.
        paren = re.search(r"[(（]([^)）]+)[)）]", raw)
        if paren:
            approved.add(paren.group(1).strip().lower())
        main = re.sub(r"\s*[(（][^)）]*[)）]\s*", "", raw).strip()
        if main:
            approved.add(main.lower())

    # Internal: scan rows of the Internal-section table; the first backtick
    # span on each table row is the codename template.
    in_section = False
    cell_re = re.compile(r"`([^`]+)`")
    for line in lines:
        if line.startswith("## "):
            in_section = bool(internal_section_re.match(line))
            continue
        if not in_section:
            continue
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        # Skip the header row and the |---|---| separator.
        if set(stripped) <= set("|-: "):
            continue
        if "代號模式" in stripped or "Codename pattern" in stripped:
            continue
        first_cell = stripped.split("|")[1] if "|" in stripped else ""
        cm = cell_re.search(first_cell)
        if cm:
            tmpl = cm.group(1).strip()
            internal.append((tmpl, compile_template(tmpl)))

    return approved, internal


# ── Classification ────────────────────────────────────────────────────────
def _is_safe_token(token: str) -> bool:
    return any(p.match(token) for p in SAFE_TOKEN_PATTERNS)


def _approved_match(token: str, approved: set[str]) -> bool:
    """Case-insensitive approved-set lookup with a simple English plural fold.

    The glossary registers singular product terms ("Rule Pack", "Recording
    Rule"); docs use the plural just as often ("Rule Packs"). Folding a single
    trailing 's' lets the plural match without a second glossary row and keeps
    the warn-mode soak signal from drowning in trivial plural variants.
    """
    low = token.lower()
    if low in approved:
        return True
    if low.endswith("s") and low[:-1] in approved:
        return True
    return False


def scan_line(
    line: str,
    internal: list[tuple[str, re.Pattern[str]]],
    approved: set[str],
) -> tuple[list[tuple[str, str]], list[str]]:
    """Return (internal_hits, unregistered_tokens) for one line.

    internal_hits: list of (template, matched_text) — confirmed leaks.
    unregistered_tokens: shape tokens that are neither internal, approved, nor
    a built-in safe identifier — candidates that should be registered.
    """
    internal_hits: list[tuple[str, str]] = []
    internal_spans: list[str] = []
    for tmpl, rx in internal:
        for m in rx.finditer(line):
            internal_hits.append((tmpl, m.group(0)))
            internal_spans.append(m.group(0))

    internal_span_set = set(internal_spans)
    unregistered: list[str] = []
    for label, rx in SHAPE_PATTERNS:
        for m in rx.finditer(line):
            token = m.group(0).strip()
            # Exact-match de-dup only: a shape token equal to an internal hit
            # is already reported as a leak, don't double-count it. Do NOT use
            # substring containment here — that wrongly suppresses a distinct
            # adjacent token (e.g. internal `DEC-B` hiding shape `DEC-Beta`),
            # which is exactly the family-extension case discovery must surface.
            if token in internal_span_set:
                continue
            if label == "two-word-cap":
                words = token.split(" ", 1)
                # Drop tokens led by a determiner ("The Migration") — sentence
                # prose, not a codename. Deterministic, not NLP.
                if words[0].lower() in _LEADING_DETERMINERS:
                    continue
                # Drop "<Word> <single-letter>" enumeration labels (Tier A,
                # Option B, Path A) — doc structure, not codenames. Registered
                # single-letter codenames (Track A) are unaffected: the
                # internal-pattern scan catches them before this point. Tradeoff:
                # a brand-new, not-yet-registered single-letter codename family
                # is invisible to *discovery* until registered — acceptable for
                # this warn-mode layer (the hard teeth are the internal scan).
                if len(words) == 2 and len(words[1]) == 1 and words[1].isalpha():
                    continue
            if _approved_match(token, approved):
                continue
            if _is_safe_token(token):
                continue
            unregistered.append(token)
    return internal_hits, unregistered


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.strip().splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--ci", action="store_true", help="Exit non-zero on hard violations")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Promote unregistered shape tokens from warning to hard violation",
    )
    parser.add_argument(
        "--scope",
        choices=["default", "full"],
        default="default",
        help="default: Layer-1 customer-facing scope. full: also helm/ + tests/.",
    )
    parser.add_argument("--pr-body-file", default=None, help="PR body file for bypass tag check")
    parser.add_argument("--verbose", action="store_true", help="List every finding's file:line")
    args = parser.parse_args()

    approved, internal = load_glossary()
    if not internal:
        print(
            "ERROR: no internal codename templates parsed from docs/glossary.md.\n"
            "  Expected a '## 內部代號 — 禁止用於對外文件' section with a template table.",
            file=sys.stderr,
        )
        return 2

    scan_paths = FULL_SCAN_PATHS if args.scope == "full" else DEFAULT_SCAN_PATHS
    files = iter_files(scan_paths)

    internal_findings: list[tuple[str, int, str, str, str]] = []
    unregistered_counts: dict[str, int] = {}
    unregistered_findings: list[tuple[str, int, str, str]] = []

    for fp in files:
        if fp.suffix.lower() in SKIP_EXT:
            continue
        # Don't scan the glossary editions — they legitimately list codenames.
        if fp.resolve() in GLOSSARY_SELF:
            continue
        try:
            text = fp.read_text(encoding="utf-8")
        except (UnicodeDecodeError, PermissionError):
            continue
        suffix = fp.suffix.lower()
        rel = fp.relative_to(REPO_ROOT).as_posix()
        for i, line in enumerate(text.splitlines(), 1):
            if _is_code_comment(line, suffix):
                continue
            hits, unreg = scan_line(line, internal, approved)
            for tmpl, match in hits:
                internal_findings.append((rel, i, tmpl, match, line.rstrip()[:120]))
            for token in unreg:
                unregistered_counts[token] = unregistered_counts.get(token, 0) + 1
                unregistered_findings.append((rel, i, token, line.rstrip()[:120]))

    # Bypass check (lint-policy.md §4)
    pr_body = None
    if args.pr_body_file:
        try:
            pr_body = Path(args.pr_body_file).read_text(encoding="utf-8")
        except (FileNotFoundError, PermissionError):
            pass
    pr_body = pr_body or os.environ.get("PR_BODY")
    bypass_reason = parse_bypass_tag(pr_body, "codename-gate")

    # ── Report ────────────────────────────────────────────────────────────
    print("=" * 68)
    print("CODENAME GATE (Layer 2, glossary-driven) — #469")
    print("=" * 68)
    print(f"Scanned files:          {len(files)} (scope={args.scope})")
    print(f"Glossary approved terms: {len(approved)}")
    print(f"Glossary internal tmpls: {len(internal)}")
    print(f"Internal leaks:          {len(internal_findings)}  (hard)")
    print(
        f"Unregistered tokens:     {len(unregistered_findings)} hits / "
        f"{len(unregistered_counts)} distinct  "
        f"({'hard' if args.strict else 'warn'})"
    )
    print()

    if internal_findings:
        print("INTERNAL CODENAME LEAKS (registered as off-limits in glossary):")
        for rel, line_no, tmpl, match, snippet in internal_findings:
            print(f"  ✗ {rel}:{line_no}: '{match}' matches [{tmpl}] — {snippet}")
        print()

    if unregistered_counts:
        print(f"UNREGISTERED SHAPE TOKENS (top 30 of {len(unregistered_counts)} distinct):")
        for token, count in sorted(unregistered_counts.items(), key=lambda x: -x[1])[:30]:
            print(f"  ? `{token}` — {count} hit(s)")
        if args.verbose:
            print("  --- detail ---")
            for rel, line_no, token, snippet in unregistered_findings:
                print(f"      {rel}:{line_no}: `{token}` — {snippet}")
        print(
            "\n  Each token must be registered in docs/glossary.md:\n"
            "    customer-facing term → add a **Term** entry in the A–Z dictionary;\n"
            "    internal codename    → add a row to the '## 內部代號' table."
        )
        print()

    hard = len(internal_findings) + (len(unregistered_findings) if args.strict else 0)

    if hard == 0:
        print("OK no codename-gate violations.")
        return 0

    if bypass_reason:
        print(
            f"⚠️  BYPASSED via PR body: {bypass_reason}\n"
            f"   {hard} hard finding(s) above are author-acknowledged intentional."
        )
        return 0

    print(
        f"FAIL {hard} hard violation(s).\n"
        "  Internal codenames belong in CHANGELOG.md / docs/internal/** only.\n"
        "  Register new proper nouns in docs/glossary.md (see section header).\n"
        "  Intentional? add to PR body:\n"
        "    bypass-lint: codename-gate\n"
        "    reason: <≥30 words>\n"
        "  See docs/internal/lint-policy.md §4."
    )
    return 1 if args.ci else 0


if __name__ == "__main__":
    sys.exit(main())
