#!/usr/bin/env python3
"""check_env_bool_parsers.py — ADR-034 mechanical enforcement (narrow).

Forbids hand-rolled "truthy string -> bool" parsers in Go production code.

Why this exists
---------------
ADR-034 says a legal value must not double as the fallback for an
unrecognized one, when that value decides whether a CHECK RUNS. Its second
case study is `envBool` (#1599 / #1624): a 6-line switch that accepted
"true"/"1"/"yes"/"on" and returned false for everything else, used as the
default for five `flag.Bool` switches — three of which gate authorization or
audit. A typo there was byte-identical to a deliberate opt-out.

The fix was to hand the string to `strconv.ParseBool`, the very parser
`flag.Bool` uses. This lint keeps that door shut: writing another such
parser is what reopens the class.

⚠️ Scope is deliberately narrow, and that is a documented weakness, not an
oversight. It matches two single-line shapes:

  (B) a `case` arm listing two or more boolean-ish string literals, e.g.
      `case "true", "1", "yes", "on":` — a switch arm shaped like that is a
      truthy parser and essentially nothing else. A legitimate enum switch
      (`case "pr", "pr-github":`) does not match, because its literals are
      not boolean-ish.

  (C) a line that reads an env var AND compares it to a boolean-ish literal,
      e.g. `if strings.ToLower(os.Getenv(k)) == "true"`.

It does NOT catch a parser split across lines in a shape neither rule sees,
nor one written in Python, nor one that spells its literals differently. It
is a tripwire for the known shape, not proof that no hand-rolled parser
exists. ADR-034 §"機械執行機制：補上了一個窄的 tripwire，不是完整保證" states
this limit.

`_test.go` files are excluded: test helpers legitimately compare env vars to
string literals for subprocess re-exec sentinels (see
`envbool_test.go`'s `ENVBOOL_CRASHER == "1"`), which is process plumbing,
not configuration parsing.

Baseline at introduction: ZERO matches in the repo — #1624 removed the only
one. So this lint starts as a pure regression tripwire, and its self-test
carries an anti-noop witness (a synthetic violation MUST be flagged), because
a shape gate that silently finds nothing to check is worse than no gate.

Lint class: (b) negative pattern (docs/internal/lint-policy.md) — diff-only
scan, auto-stage, hard block, PR-body bypass.

Usage:   python3 scripts/tools/lint/check_env_bool_parsers.py [--ci] [FILES...]
Exit:    0 clean | 1 findings (with --ci) | 2 caller error
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, ".."))  # Repo subdir layout
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR  # noqa: E402
from _lint_helpers import (  # noqa: E402
    get_diff_added_lines,
    parse_bypass_tag,
    resolve_diff_base,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
LINT_NAME = "env-bool-parsers"

# Literals a hand-rolled boolean parser spells its accepted inputs with.
# `strconv.ParseBool` accepts 1/t/T/TRUE/true/True/0/f/F/FALSE/false/False;
# the extras below ("yes"/"on"/...) are exactly what a hand-rolled parser adds
# and what makes it diverge from the flag package.
BOOLISH = frozenset(
    {
        "true", "false", "1", "0", "t", "f", "y", "n",
        "yes", "no", "on", "off", "enabled", "disabled",
    }
)

_CASE_RE = re.compile(r'^\s*case\s+(?P<lits>"(?:[^"\\]|\\.)*"(?:\s*,\s*"(?:[^"\\]|\\.)*")*)\s*:')
_LITERAL_RE = re.compile(r'"((?:[^"\\]|\\.)*)"')
_GETENV_RE = re.compile(r"\bGetenv\s*\(")
_CMP_RE = re.compile(r'[=!]=\s*"((?:[^"\\]|\\.)*)"')


def line_violation(line: str) -> str | None:
    """Return a human-readable reason if ``line`` is a hand-rolled bool parse.

    Pure and single-line by design: the diff-only scan mandated for (b) class
    lints (lint-policy.md §3) hands over added lines, not whole files, so a
    rule that needed surrounding context could not run at all.
    """
    stripped = line.split("//", 1)[0]

    m = _CASE_RE.match(stripped)
    if m:
        lits = [x.lower() for x in _LITERAL_RE.findall(m.group("lits"))]
        boolish = [x for x in lits if x in BOOLISH]
        if len(boolish) >= 2:
            return (
                f"switch arm over boolean-ish literals {boolish} — this is a "
                f"hand-rolled truthy parser"
            )

    if _GETENV_RE.search(stripped):
        for lit in _CMP_RE.findall(stripped):
            if lit.lower() in BOOLISH:
                return (
                    f'env var compared directly to the boolean-ish literal '
                    f'"{lit}" — parse it with strconv.ParseBool instead'
                )

    return None


def is_in_scope(rel_path: str) -> bool:
    """Go production code only; see the module docstring on `_test.go`."""
    return rel_path.endswith(".go") and not rel_path.endswith("_test.go")


def find_violations(files: list[str], base: str) -> list[tuple[str, int, str, str]]:
    """Return (relpath, line_no, line, reason) for each added offending line."""
    out: list[tuple[str, int, str, str]] = []
    for rel in files:
        if not is_in_scope(rel):
            continue
        for line_no, content in get_diff_added_lines(Path(rel), base):
            reason = line_violation(content)
            if reason:
                out.append((rel, line_no, content.rstrip(), reason))
    return out


def _read_pr_body(pr_body_file: str | None) -> str | None:
    if pr_body_file:
        try:
            return Path(pr_body_file).read_text(encoding="utf-8")
        except OSError as e:
            print(f"WARN: cannot read --pr-body-file {pr_body_file}: {e}", file=sys.stderr)
    return os.environ.get("PR_BODY") or None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("files", nargs="*", help="Files to scan (pre-commit passes these).")
    ap.add_argument("--ci", action="store_true", help="Exit 1 on findings.")
    ap.add_argument("--pr-body-file", default=None, help="File holding the PR body (bypass tag).")
    args = ap.parse_args()

    if not args.files:
        return EXIT_OK

    try:
        base = resolve_diff_base()
    except Exception as e:  # pragma: no cover - defensive
        print(f"FATAL: cannot resolve diff base: {e}", file=sys.stderr)
        return EXIT_CALLER_ERROR

    findings = find_violations(args.files, base)
    if not findings:
        return EXIT_OK

    print("❌ Hand-rolled env-var boolean parser(s) found (ADR-034):", file=sys.stderr)
    for rel, line_no, content, reason in findings:
        print(f"   {rel}:{line_no}: {reason}", file=sys.stderr)
        print(f"       {content.strip()}", file=sys.stderr)
    print(
        "\n   Use strconv.ParseBool — the same parser flag.Bool uses — and make an\n"
        "   unparseable value fatal instead of silently falling back to false.\n"
        "   An unset value may keep the flag's own default; see envBool in\n"
        "   components/tenant-api/cmd/server/main.go for the reference shape.\n"
        "   Rationale: docs/adr/034-legal-value-as-fallback.md\n",
        file=sys.stderr,
    )

    bypass_reason = parse_bypass_tag(_read_pr_body(args.pr_body_file), LINT_NAME)
    if bypass_reason:
        print(f"⚠️  BYPASSED via PR body: {bypass_reason}", file=sys.stderr)
        print("   Reviewer must confirm the bypass is justified.", file=sys.stderr)
        return EXIT_OK

    print(
        f"   If this is a legitimate exception, add to the PR body:\n"
        f"\n    bypass-lint: {LINT_NAME}\n    reason: <why this is not a config bool parse>\n",
        file=sys.stderr,
    )
    return EXIT_VIOLATION if args.ci else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
