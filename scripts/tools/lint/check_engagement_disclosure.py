#!/usr/bin/env python3
"""Engagement-disclosure gate — block "active engagement" assertions in the PUBLIC repo.

WHY (dev-rules.md §E): this repo and its issues are PUBLIC, and a public write is
IRREVERSIBLE (indexed / forked / archived; post-hoc redaction is unreliable). Naming a
source platform is fine — what leaks is asserting that a SPECIFIC engagement is
IN FLIGHT. Combined with the product mix already visible in the repo, that conjunction
can be k=1 in a small market (k-anonymity).

WHAT THIS IS NOT: a keyword denylist. Measured on this repo, 10+ mentions of the source
platform are entirely benign (log-sink examples, secret-token allowlists, generic
multi-region scenarios, "e.g. Splunk" as a class example). Blocking the word would produce
almost all false positives and drown the real signal — and the inverse held too: ranked by
keyword count, the highest-hit files were the LEAST sensitive. So this gate fires only on
the narrow conjunction "<source platform> ... <in-flight marker>" on one line.

It is a BACKSTOP. The primary control is the pre-publication human check in dev-rules §E
(the tuple rule is semantic and cannot be linted).

Exit codes (scripts/tools/_lib_exitcodes.py): 0 clean / 1 violation / 2 caller error.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR  # noqa: E402
from _lib_validation import i18n_text  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]

# Text surfaces that are published (docs site, repo browse, release notes).
# ⚠️ These MUST cover everything the pre-commit `files:` pattern triggers on —
# a file that fires the hook but is not scanned is the worst shape of silent
# failure for an irreversible-publication guard. `README*.md` (not `README.md`)
# because the English README is the more externally-consumed surface, and
# `ya?ml` because `.yml` files do exist under tests/.
SCAN_GLOBS = (
    "docs/**/*.md",
    "CHANGELOG.md",
    "README*.md",
    "rule-packs/**/*.yaml",
    "rule-packs/**/*.yml",
    "tests/**/*.yaml",
    "tests/**/*.yml",
    "tests/**/*.py",
    "scripts/**/*.py",
)

# A proprietary scheduled-search source platform being migrated away from.
_PLATFORM = re.compile(r"splunk|scheduled-search", re.IGNORECASE)

# An assertion that such a migration is CURRENTLY UNDERWAY for us.
# NOTE the English/Chinese symmetry: every ZH in-flight marker needs an EN
# counterpart or the gate silently under-covers the English surfaces.
_IN_FLIGHT = re.compile(
    r"active\s+[^.\n]{0,40}?migration"     # "active ... migration" (spaces allowed)
    r"|migration\s+target"                  # "migration target(s)"
    r"|migration[^.\n]{0,20}?in\s+progress"  # "migration ... in progress"
    r"|migration[^.\n]{0,20}?under\s?way"    # "migration is underway"
    r"|migrating\s+(?:from|off)"             # "migrating from/off <platform>"
    r"|遷移目標|遷移前置|進行中|遷移中"
    r"|dual-run\s+soak|雙活\s*soak",
    re.IGNORECASE,
)

# Inline code spans are stripped before the opt-out marker is looked for, so a
# doc that *quotes* the marker (e.g. this file's own policy page showing
# `<!-- deid-ok: reason -->` as an example) cannot accidentally exempt itself.
_CODE_SPAN_RE = re.compile(r"`[^`]*`")

# Explicit inline opt-out for lines that must SHOW the anti-pattern (policy docs, tests).
# A marker beats a per-file allowlist: it is line-scoped, greppable, and reviewable in the
# diff that introduces it. Prefer rewording to capability framing over adding a marker.
#
# ⚠️ The marker must be COMMENT-ANCHORED AND carry a rationale (`<!-- deid-ok: why -->`
# or `# deid-ok: why`). A bare substring test was fail-open: prose that merely *mentions*
# the marker (e.g. a CHANGELOG bullet describing this gate) silently exempted itself.
DEID_OK_RE = re.compile(r"(?:<!--|#)\s*deid-ok:\s*(?!-->)\S")

# The gate must not flag its own pattern definitions / help text.
SELF_PATH = Path(__file__).resolve()


def scan(root: Path) -> list[tuple[str, int, str]]:
    findings: list[tuple[str, int, str]] = []
    seen: set[Path] = set()
    for pattern in SCAN_GLOBS:
        for path in root.glob(pattern):
            if not path.is_file() or path in seen:
                continue
            seen.add(path)
            if path.resolve() == SELF_PATH:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for lineno, line in enumerate(text.splitlines(), 1):
                if not (_PLATFORM.search(line) and _IN_FLIGHT.search(line)):
                    continue
                if DEID_OK_RE.search(_CODE_SPAN_RE.sub("", line)):
                    continue
                # POSIX-normalised at the source so the whole API (scan / --json /
                # stderr) is platform-identical — local Windows output must match CI.
                rel = path.relative_to(root).as_posix()
                findings.append((rel, lineno, line.strip()[:160]))
    return sorted(findings)


def main() -> int:
    # Bilingual --help is a repo-wide behavioural contract
    # (tests/shared/test_bilingual_help_contract.py): the help text must actually
    # switch on DA_LANG, not merely contain some CJK.
    parser = argparse.ArgumentParser(
        description=i18n_text(
            "阻擋公開 repo 中「進行中 engagement」的斷言（dev-rules §E）",
            "Block in-flight engagement assertions in the public repo (dev-rules §E)"))
    parser.add_argument("--json", action="store_true", dest="json_output",
                        help=i18n_text(
                            "stdout 輸出機器可讀 JSON（人類訊息一律走 stderr）",
                            "emit machine-readable JSON on stdout (human messages go to stderr)"))
    args = parser.parse_args()

    if not REPO_ROOT.is_dir():
        print(f"ERROR: repo root not found: {REPO_ROOT}", file=sys.stderr)
        return EXIT_CALLER_ERROR

    # Deliberately repo-wide, never narrowed to changed files. A published
    # violation is irreversible, so the question this gate answers is "is the
    # repo clean right now", not "did THIS commit add one" — an existing
    # violation must keep blocking until it is actually fixed. (An earlier
    # revision narrowed by `args.paths`; with `pass_filenames: false` that
    # branch was unreachable, and its path comparison silently dropped every
    # finding when given absolute paths — a fail-open waiting to be wired up.)
    findings = scan(REPO_ROOT)

    # Report POSIX paths on every platform so CI output and local output match.
    rows = [(f.replace("\\", "/"), n, t) for f, n, t in findings]
    if args.json_output:
        print(json.dumps({
            "tool": "check-engagement-disclosure",
            "violations": [{"file": f, "line": n, "text": t} for f, n, t in rows],
            "count": len(rows),
        }, ensure_ascii=False, indent=2))
    else:
        for f, n, t in rows:
            print(f"{f}:{n}: {t}", file=sys.stderr)

    if findings:
        print(i18n_text(
            f"\n{len(findings)} 筆 engagement-disclosure 違規 — dev-rules.md §E。\n"
            "這是「合取」gate：點名平台沒問題，斷言遷移正在進行中才是問題。\n"
            "改寫為能力敘述，例如：\n"
            "  'an active <X>->VM migration target'  ->  'a cross-engine migration reference pack'",
            f"\n{len(findings)} engagement-disclosure violation(s) — dev-rules.md §E.\n"
            "This is the CONJUNCTION gate: naming the platform is fine, asserting an\n"
            "in-flight migration is not. Reword to capability framing, e.g.\n"
            "  'an active <X>->VM migration target'  ->  'a cross-engine migration reference pack'"),
            file=sys.stderr)
        return EXIT_VIOLATION
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
