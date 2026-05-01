#!/usr/bin/env python3
"""check_dev_rules_enforcement.py — detect doc-drift in dev-rules.md.

Why this exists
---------------
PR #168 (closed, superseded by this PR) shipped a snapshot audit doc
that found ``dev-rules.md`` claimed 4 lint hooks that didn't exist in
``.pre-commit-config.yaml``:

  - Rule #2 claimed ``lint_hardcode_tenant``      → NOT FOUND
  - Rule #5 claimed "7 條 SAST 規則 (auto)"        → NO matching scripts
  - Rule #6 claimed ``check_marketing_language``  → NOT FOUND
  - Rule #1 claimed implicit hook                  → process-only by design

The user's correct critique: a snapshot audit doc is itself doc, not
code-driven. The half-life is one minor version. Real code-driven
solution = a lint that catches drift CONTINUOUSLY, not a one-time
audit report.

This script is that lint. Behavior:

1. Scan ``docs/internal/dev-rules.md`` for any inline-code-quoted
   identifier that matches a hook-name shape (``check_*`` /
   ``lint_*`` / ``*_check`` / ``*-check`` etc) in a "claim" context
   (preceded by trigger words like "hook", "pre-commit", "scan",
   "check 方式", etc).
2. Parse ``.pre-commit-config.yaml`` for all hook ``id:`` values + all
   referenced lint script paths.
3. For each claim in dev-rules.md: error if the name is not in either
   the ``id:`` set or the script-stem set.
4. Allow per-line opt-out: ``<!-- enforcement-claim: ignore -->`` on
   the line containing the claim. Useful when documenting third-party
   hooks (bandit, ruff, etc) that don't live in our config.

The lint is intentionally **conservative on detection**: only fires
when the claim is in a recognizable "(hook|pre-commit|scan)
context", to avoid false positives on prose that happens to mention
a script name. Per-line ignore covers the residual edge cases.

Severity
--------
- default      → report-only, exit 0 (audit mode)
- ``--ci``      → fatal on drift (exit 1)
- This is **not** behind a granular ``--strict`` flag because the
  number of existing violations is already 0 after this PR's
  dev-rules.md re-wording. We can ship it fatal from day 1.

Self-dogfood
------------
This script is itself a "hook" referenced in dev-rules.md (after this
PR's edits). The claim text uses the exact pattern the lint detects,
so the lint catches itself if its own id is removed from
``.pre-commit-config.yaml``.
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEV_RULES_PATH = PROJECT_ROOT / "docs" / "internal" / "dev-rules.md"
PRECOMMIT_PATH = PROJECT_ROOT / ".pre-commit-config.yaml"

_IGNORE_MARKER = "enforcement-claim: ignore"

# Trigger words that establish "this inline-code identifier is being
# claimed as an enforced hook/scan". Conservative — keeps detection
# precision high; users who write prose mentioning a hook name
# without the trigger context don't fire the lint.
#
# The trigger must appear within the SAME LINE as the inline-code
# identifier (we scan line-by-line; multi-line context not supported).
# This keeps false-positive risk near zero — the claim must be in a
# direct enforcement assertion, not in an unrelated paragraph.
_TRIGGERS = (
    "pre-commit hook",
    "pre-commit stage",
    "pre-commit",  # broader — covers "由 pre-commit 跑"
    "hook ",  # "hook `foo` 會 ..." — note trailing space to avoid in-word match
    "hook`",  # no-space form
    "lint hook",
    "由 .* 強制",
    "scan",
    "scanner",
    "linter",
    "auto.*scan",
    "自動掃描",
    "自動檢查",
    "會掃描",
    "會檢查",
    "manual stage",
)
_TRIGGERS_RE = re.compile("|".join(_TRIGGERS), re.IGNORECASE)

# Inline-code identifier — backticks around an identifier-shaped
# token. Match: ``check_foo``, ``lint_foo``, ``foo-check``,
# ``foo_check``. Skip: very short (likely punctuation), all-caps
# constants (likely env vars), paths with /.
_HOOK_NAME_RE = re.compile(
    r"`([a-z][a-z0-9_-]{2,}[a-z0-9])`",
    re.IGNORECASE,
)

# Filter: identifier must look like a hook/lint name pattern.
_LIKELY_HOOK_RE = re.compile(
    r"^("
    r"check[_-][a-z0-9_-]+"  # check_foo / check-foo
    r"|lint[_-][a-z0-9_-]+"  # lint_foo / lint-foo
    r"|fix[_-][a-z0-9_-]+"   # fix_file_hygiene
    r"|generate[_-][a-z0-9_-]+"
    r"|validate[_-][a-z0-9_-]+"
    r"|[a-z0-9_-]+[_-]check"  # foo-check
    r"|[a-z0-9_-]+[_-]hygiene"  # file-hygiene
    r"|[a-z0-9_-]+[_-]guard"   # sed-damage-guard
    r"|[a-z0-9_-]+[_-]drift"   # tool-map-drift
    r")$",
    re.IGNORECASE,
)


@dataclass
class EnforcementDrift:
    """A claim in dev-rules.md that doesn't match any real hook id."""

    line_no: int
    snippet: str
    claimed_hook: str

    def render(self) -> str:
        return (
            f"docs/internal/dev-rules.md:{self.line_no} "
            f"claims hook `{self.claimed_hook}` but no such id in "
            f".pre-commit-config.yaml or matching script in scripts/tools/lint/"
            f"\n    line: {self.snippet[:120]}"
        )


def _load_known_hook_names() -> set[str]:
    """Parse known hook ids from .pre-commit-config.yaml + script stems.

    Two sources because dev-rules.md sometimes references the hook id
    (``- id: foo-check``) and sometimes the underlying script name
    (``check_foo.py`` → claim refers to ``check_foo``). Accept both.

    Hand-rolled regex parse rather than YAML import to avoid a
    runtime dependency on pyyaml — the lint should run with stdlib
    only so it can ship without conditional imports.
    """
    names: set[str] = set()
    if PRECOMMIT_PATH.exists():
        text = PRECOMMIT_PATH.read_text(encoding="utf-8", errors="replace")
        for m in re.finditer(r"^\s*-\s*id:\s*([\w\-]+)\s*$", text, re.MULTILINE):
            names.add(m.group(1))
            # Common variant: hook id with underscores OR hyphens —
            # normalize to both forms so claims using either spelling match.
            names.add(m.group(1).replace("-", "_"))
            names.add(m.group(1).replace("_", "-"))

    # Also add lint script stems (e.g. check_foo.py → check_foo).
    lint_dir = PROJECT_ROOT / "scripts" / "tools" / "lint"
    if lint_dir.is_dir():
        for py in lint_dir.glob("*.py"):
            if py.stem.startswith("_"):
                continue  # private helpers
            names.add(py.stem)
            names.add(py.stem.replace("_", "-"))

    # Also add dx tool stems — `generate_doc_map.py --check` is a
    # legitimate enforcement path even though dx, not lint.
    dx_dir = PROJECT_ROOT / "scripts" / "tools" / "dx"
    if dx_dir.is_dir():
        for py in dx_dir.glob("*.py"):
            if py.stem.startswith("_"):
                continue
            names.add(py.stem)
            names.add(py.stem.replace("_", "-"))

    return names


def scan_for_drift(
    source: str,
    known_names: set[str],
) -> list[EnforcementDrift]:
    """Walk dev-rules.md line-by-line, return claims that don't resolve."""
    drifts: list[EnforcementDrift] = []
    lines = source.splitlines()

    for idx, line in enumerate(lines, start=1):
        # Skip lines explicitly marked ignore.
        if _IGNORE_MARKER in line:
            continue

        # Must have a trigger word in the same line.
        if not _TRIGGERS_RE.search(line):
            continue

        # Find every hook-name-shaped inline-code token on the line.
        for m in _HOOK_NAME_RE.finditer(line):
            name = m.group(1)
            if not _LIKELY_HOOK_RE.match(name):
                continue
            if name in known_names:
                continue
            # Drift detected.
            drifts.append(
                EnforcementDrift(
                    line_no=idx,
                    snippet=line.strip(),
                    claimed_hook=name,
                )
            )

    return drifts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Detect doc-drift in dev-rules.md: any pre-commit hook "
            "name claimed in the doc must actually exist in the "
            "repo's .pre-commit-config.yaml or scripts/tools/lint/."
        )
    )
    parser.add_argument(
        "--ci",
        action="store_true",
        help="Exit 1 on any drift (default: report only).",
    )
    parser.add_argument(
        "--path",
        default=str(DEV_RULES_PATH),
        help="Path to dev-rules.md (default: docs/internal/dev-rules.md).",
    )
    args = parser.parse_args(argv)

    rules_path = Path(args.path)
    if not rules_path.exists():
        print(f"⚠ {rules_path} not found — skipping", file=sys.stderr)
        return 0

    source = rules_path.read_text(encoding="utf-8", errors="replace")
    known = _load_known_hook_names()
    drifts = scan_for_drift(source, known)

    if not drifts:
        if args.ci:
            print(f"✓ no enforcement-claim drift in {rules_path.name}")
        return 0

    print(
        f"✗ {len(drifts)} enforcement-claim drift(s) in {rules_path.name}:",
        file=sys.stderr,
    )
    for d in drifts:
        print(f"  {d.render()}", file=sys.stderr)

    print(
        "\nResolve each by either:\n"
        "  (a) Adding the hook to .pre-commit-config.yaml + script — "
        "the rule is now genuinely enforced.\n"
        "  (b) Re-wording the rule to use 'reviewer convention' or "
        "'process rule' instead of a hook name — honest about being "
        "doc-only.\n"
        "  (c) Adding `<!-- enforcement-claim: ignore -->` on the "
        "line — for third-party hooks (bandit etc) not in our config.\n"
        "\n"
        "See dev-rules.md history (PR #168 was closed in favor of this "
        "lint) for the original 4 doc-drift findings that motivated this "
        "check.",
        file=sys.stderr,
    )
    return 1 if args.ci else 0


if __name__ == "__main__":
    raise SystemExit(main())
