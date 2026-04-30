#!/usr/bin/env python3
"""Commit-scope doc drift gate (L1 pre-commit hook + validate_all integration).

Root cause:
  PR #147 (issue #127) commit was rejected by commitlint because the author
  wrote `fix(threshold-exporter):` while the SOT (`.commitlintrc.yaml`
  `scope-enum`) only accepts `exporter`. The author had checked
  `docs/internal/commit-convention.md` first — which listed only 7 of the
  31 enforced scopes and didn't call out the verbose-vs-short-name
  pitfall. Doc drift directly caused the misstep.

This hook keeps the pitfall fix from regressing:

  * **Source of truth**: `.commitlintrc.yaml` → `rules.scope-enum.[2]`
    (the list of allowed scopes).
  * **Doc surface**: `docs/internal/commit-convention.md` § "Scope" —
    each scope mentioned as a bold list-item (`- **<scope>**:`).
  * **Drift classes**:
      - **Type A (HARD FAIL)**: doc lists a scope NOT in SOT. Any commit
        the author writes following that doc gets rejected by commitlint.
      - **Type B (SOFT WARN)**: SOT has a scope doc doesn't mention.
        By design the doc is a "most-used subset" pointing at the SOT
        for the full list — scopes can exist without being highlighted.
        Reported but doesn't fail the hook.

Why list-items only:
  Bold body text like `**Common pitfall**:` would otherwise be parsed
  as a scope. Limiting to lines starting with `- ` (markdown list-item
  bullet) catches every legitimate scope definition while filtering
  callout / emphasis bolds that happen to be inside the section.

Exit codes:
  0  No drift, or only Type B (warn-only).
  1  At least one Type A drift — doc lists illegal scope.
  2  Caller error (missing SOT / doc, malformed yaml).

CLI:
    python3 scripts/tools/lint/check_commit_scope_doc.py            # report mode
    python3 scripts/tools/lint/check_commit_scope_doc.py --ci       # exit 1 on Type A
    python3 scripts/tools/lint/check_commit_scope_doc.py --json     # machine-readable
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_SOT = REPO_ROOT / ".commitlintrc.yaml"
DEFAULT_DOC = REPO_ROOT / "docs" / "internal" / "commit-convention.md"

SCOPE_HEADING = re.compile(r"^### Scope\b")
NEXT_SECTION_HEADING = re.compile(r"^###?\s")
LIST_ITEM = re.compile(r"^\s*-\s")
# Allow `+` in scope names so compound scopes like `dx+e2e` / `lint+tooling`
# can be extracted when the doc lists them. (`\w` = [A-Za-z0-9_], adding
# `-` for `rule-packs` / `phase-a` and `+` for compound scopes.)
BOLD_TOKEN = re.compile(r"\*\*([\w+-]+)\*\*")


def extract_doc_scopes(doc_path: Path) -> set[str]:
    """Return the set of scope names mentioned as bold list-items in §Scope.

    Walks the file, switches into "in-scope-section" after seeing the
    `### Scope` heading, switches back out at the next `###`/`##`
    heading. Only `- **<scope>**:` style mentions count; bolds inside
    callouts / paragraphs are ignored.
    """
    if not doc_path.exists():
        raise FileNotFoundError(f"doc not found: {doc_path}")
    in_scope_section = False
    scopes: set[str] = set()
    for line in doc_path.read_text(encoding="utf-8").splitlines():
        if SCOPE_HEADING.match(line):
            in_scope_section = True
            continue
        if in_scope_section and NEXT_SECTION_HEADING.match(line):
            break
        if not in_scope_section:
            continue
        if not LIST_ITEM.match(line):
            continue
        for match in BOLD_TOKEN.finditer(line):
            scopes.add(match.group(1))
    return scopes


def extract_sot_scopes(yaml_path: Path) -> set[str]:
    """Parse `.commitlintrc.yaml` and return rules.scope-enum.[2] as a set.

    commitlint's scope-enum rule format is
        scope-enum:
          - 2          # severity
          - always     # condition
          - [scope1, scope2, ...]
    so we want the list at index 2. yaml.safe_load handles the dash-list
    expansion for us.
    """
    if not yaml_path.exists():
        raise FileNotFoundError(f"SOT not found: {yaml_path}")
    try:
        import yaml as yaml_mod
    except ImportError as exc:
        raise RuntimeError(
            "PyYAML is required (pip install pyyaml). "
            "Repo CI installs this via setup."
        ) from exc
    with open(yaml_path, encoding="utf-8") as f:
        cfg = yaml_mod.safe_load(f)
    if not isinstance(cfg, dict):
        raise ValueError(f"{yaml_path} root is not a mapping")
    rules = cfg.get("rules") or {}
    scope_enum = rules.get("scope-enum") or []
    if len(scope_enum) < 3 or not isinstance(scope_enum[2], list):
        raise ValueError(
            f"{yaml_path}: rules.scope-enum.[2] is missing or not a list"
        )
    return {str(s) for s in scope_enum[2]}


def compute_drift(
    doc_scopes: set[str], sot_scopes: set[str]
) -> tuple[set[str], set[str]]:
    """Return (illegal_in_doc, unmentioned_in_doc) tuples."""
    illegal = doc_scopes - sot_scopes
    unmentioned = sot_scopes - doc_scopes
    return illegal, unmentioned


def render_report(
    doc_scopes: set[str],
    sot_scopes: set[str],
    illegal: set[str],
    unmentioned: set[str],
    *,
    ci_mode: bool,
) -> str:
    lines: list[str] = []
    lines.append("=" * 60)
    lines.append("Commit Scope Drift Check")
    lines.append("=" * 60)
    lines.append(f"  SOT (.commitlintrc.yaml): {len(sot_scopes)} scopes")
    lines.append(f"  Doc (commit-convention):  {len(doc_scopes)} mentioned")
    lines.append("")
    if illegal:
        lines.append(f"❌ Type A drift — {len(illegal)} scope(s) in doc but NOT in SOT:")
        for s in sorted(illegal):
            lines.append(f"     - **{s}** (commitlint will REJECT any commit using this scope)")
        lines.append("")
        lines.append(
            "  Fix: either add the scope to .commitlintrc.yaml `scope-enum`, "
            "or remove the bold mention from commit-convention.md §Scope."
        )
        lines.append("")
    if unmentioned:
        verdict = "ℹ️ " if not illegal else "  "
        lines.append(
            f"{verdict}Type B (warn-only) — {len(unmentioned)} scope(s) in SOT but not "
            f"mentioned in doc's §Scope (by design — doc is a curated subset):"
        )
        # Truncate to first 10 to avoid spam
        sorted_un = sorted(unmentioned)
        preview = sorted_un[:10]
        for s in preview:
            lines.append(f"     - {s}")
        if len(sorted_un) > 10:
            lines.append(f"     ... and {len(sorted_un) - 10} more")
        lines.append("")
    if not illegal and not unmentioned:
        lines.append("✅ No drift — doc and SOT agree.")
        lines.append("")
    if illegal:
        if ci_mode:
            lines.append("Result: FAIL (Type A drift, --ci)")
        else:
            lines.append("Result: FAIL (Type A drift)")
    else:
        lines.append("Result: PASS")
    lines.append("=" * 60)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check that commit-convention.md §Scope doesn't drift "
        "from .commitlintrc.yaml SOT."
    )
    parser.add_argument(
        "--sot",
        type=Path,
        default=DEFAULT_SOT,
        help=f"Path to commitlint config yaml (default: {DEFAULT_SOT})",
    )
    parser.add_argument(
        "--doc",
        type=Path,
        default=DEFAULT_DOC,
        help=f"Path to commit-convention doc (default: {DEFAULT_DOC})",
    )
    parser.add_argument(
        "--ci",
        action="store_true",
        help="CI-mode banner (currently same exit-code behavior as default; "
             "Type A drift always returns 1, Type B only is 0). Reserved for "
             "future soft-mode toggling.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Machine-readable JSON output instead of human report.",
    )
    args = parser.parse_args(argv)

    try:
        doc_scopes = extract_doc_scopes(args.doc)
        sot_scopes = extract_sot_scopes(args.sot)
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 2

    illegal, unmentioned = compute_drift(doc_scopes, sot_scopes)

    if args.json_output:
        payload = {
            "sot_count": len(sot_scopes),
            "doc_count": len(doc_scopes),
            "illegal": sorted(illegal),
            "unmentioned": sorted(unmentioned),
            "drift_a_count": len(illegal),
            "drift_b_count": len(unmentioned),
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(render_report(doc_scopes, sot_scopes, illegal, unmentioned, ci_mode=args.ci))

    if illegal:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
