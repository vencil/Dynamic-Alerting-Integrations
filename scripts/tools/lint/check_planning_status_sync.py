#!/usr/bin/env python3
"""check_planning_status_sync.py — CI-time PR-trailer ↔ frontmatter sync gate.

Implements [ADR-019](docs/adr/019-planning-ssot.md) Layer 3 (Active CI Sync Check)
+ [#379](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/379) chunk 2b
deliverable.

What it does
------------
1. Reads commit trailers (`Resolves`, `Closes`, `Fixes`, `Fix`) from every commit in
   the PR range (`<base>..HEAD`) **using git's native trailer parser**:
   `git log --format='%(trailers:key=Resolves,valueonly=true,unfold)' <base>..HEAD`.
   This handles RFC-2822 trailer semantics for free — case-insensitive key match,
   blank-line-required separation, multi-line values, mixed order. (See [#454 day-2
   review](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/454) for
   why regex on commit body alone misses these.)

2. For each trailer pointing at an in-scope id (`TRK-NNN` / `ADR-NNN` / `S#NNN`):
   - Locate the matching planning entry via
     `generate_planning_index.discover_all()` (same 4-source discovery that powers
     `make planning-index`).
   - Verify the entry's frontmatter `status:` is `done`.
   - Verify the entry's `pr_ref:` is set to the **current PR number** (when
     provided via ``--pr-number``).

3. Per [ADR-019 Layer 3](docs/adr/019-planning-ssot.md#三層設計) the default
   behaviour is **soft-warn**: print findings to stderr, exit 0 so CI shows a
   yellow neutral check rather than a red block. `--strict` upgrades to hard-fail
   (exit 1) — wire that on once contributor adoption is high enough that false
   positives are negligible.

CLI
---
``check_planning_status_sync.py``
    [--base <ref>] [--pr-number <N>] [--strict] [--ci] [--json]

CI gotcha
---------
The `actions/checkout@v4` step MUST set ``fetch-depth: 0`` so `<base>..HEAD`
resolves; otherwise `git log` fatals with "unknown revision" and the lint
silently skips. The companion workflow at `.github/workflows/planning-status-sync.yaml`
hard-codes this.

Exit codes
----------
- 0  no findings, or findings present in soft-warn mode
- 1  findings present in ``--strict`` / ``--ci`` mode
- 2  setup error (missing git, missing base ref, missing pyyaml, etc.)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

# Reuse the discovery layer from chunk 2a — keeps the entry-locating semantics
# identical to `make planning-index` so a passing PR's index renders correctly.
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts" / "dx"))
try:
    from generate_planning_index import discover_all, PlanningEntry  # noqa: E402
except ImportError as e:
    print(f"FATAL: cannot import generate_planning_index ({e})", file=sys.stderr)
    sys.exit(2)

# Make stdout tolerate non-ASCII on Windows shells.
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

# Per ADR-019 §Implementation gotcha: `\b` word-boundary anchored + only the
# three live namespaces. (Legacy `TD-NN` / `HA-NN` / `REG-NN` are intentionally
# excluded — they have to be translated to `TRK-NNN` via planning-id-mapping.md
# before a Resolves/Closes/Fixes trailer is meaningful.)
ID_RE = re.compile(r"^(?P<id>TRK-\d+[a-z]?|ADR-\d+|S#\d+)$")

# Trailer keys understood by GitHub's "magic close" + Conventional Commits.
TRAILER_KEYS = ("Resolves", "Closes", "Fixes", "Fix")


@dataclass(frozen=True)
class TrailerHit:
    verb: str       # the trailer key as found (case preserved for display)
    id: str         # canonical ID, e.g. "TRK-228"
    commit_sha: str  # short sha — for blaming the offending commit


@dataclass(frozen=True)
class ValidationIssue:
    id: str
    kind: str    # "missing-entry" | "status-not-done" | "pr-ref-mismatch" | "pr-ref-missing"
    detail: str  # human message


class CheckError(RuntimeError):
    """Setup error that should produce exit 2."""


# ---------------------------------------------------------------------------
# Trailer extraction — git native parser
# ---------------------------------------------------------------------------
def _run_git(args: List[str], *, cwd: Path) -> str:
    """Run ``git`` and return stdout. Raises CheckError on non-zero exit."""
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
        )
    except FileNotFoundError as e:
        raise CheckError(f"git not on PATH: {e}") from e
    except subprocess.TimeoutExpired as e:
        raise CheckError(f"git command timed out: {e}") from e
    if proc.returncode != 0:
        raise CheckError(
            f"git {' '.join(args)} failed (exit {proc.returncode}):\n{proc.stderr.strip()}"
        )
    return proc.stdout


def extract_trailers(base: str, *, repo_root: Path = REPO_ROOT) -> List[TrailerHit]:
    """Pull every Resolves/Closes/Fixes/Fix trailer from ``<base>..HEAD``.

    Uses ``git log --format`` with ``%(trailers:key=...)`` interpolation so we
    inherit git's case-insensitive trailer-key match + blank-line-required
    body/trailer separation. Each emitted record carries the short SHA so
    callers can blame the originating commit.
    """
    range_spec = f"{base}..HEAD"
    # Confirm the base ref exists; otherwise git log silently emits nothing
    # which we'd mis-interpret as "no trailers".
    try:
        _run_git(["rev-parse", "--verify", base], cwd=repo_root)
    except CheckError as e:
        raise CheckError(
            f"base ref '{base}' not found in repo. "
            "If running in GitHub Actions, ensure actions/checkout@v4 has "
            "fetch-depth: 0."
        ) from e

    hits: List[TrailerHit] = []
    for key in TRAILER_KEYS:
        # Format: each record = "<short_sha>\t<trailer_value>\n", one trailer per
        # line. `valueonly=true` strips the key from the line; `unfold=true`
        # folds wrapped values.
        fmt = f"--format=%h%x09%(trailers:key={key},valueonly=true,unfold=true)"
        raw = _run_git(["log", fmt, range_spec], cwd=repo_root)
        # `%h` always produces a sha; the trailer value is empty for commits
        # without that key, leading to lines like "abc1234\t".
        for line in raw.splitlines():
            if "\t" not in line:
                continue
            sha, value = line.split("\t", 1)
            value = value.strip()
            if not value:
                continue
            # A single commit may carry multiple trailers of the same key, which
            # git emits as multiple records — each line already represents one.
            for token in value.split():
                m = ID_RE.match(token.rstrip(",;"))
                if not m:
                    continue
                hits.append(TrailerHit(verb=key, id=m.group("id"), commit_sha=sha))
    # Dedup — same (id, sha) pair can appear if two trailer keys point at it.
    seen: set[Tuple[str, str]] = set()
    unique: List[TrailerHit] = []
    for h in hits:
        key = (h.id, h.commit_sha)
        if key in seen:
            continue
        seen.add(key)
        unique.append(h)
    return unique


# ---------------------------------------------------------------------------
# Entry lookup + validation
# ---------------------------------------------------------------------------
def find_entry_by_id(target_id: str, entries: Iterable[PlanningEntry]) -> Optional[PlanningEntry]:
    """Return the first entry whose id matches. None if not found."""
    for e in entries:
        if e.id == target_id:
            return e
    return None


def validate_sync(
    hits: List[TrailerHit],
    entries: List[PlanningEntry],
    *,
    pr_number: Optional[str] = None,
) -> List[ValidationIssue]:
    """Apply the three sync rules per ADR-019 Layer 3."""
    issues: List[ValidationIssue] = []
    for h in hits:
        entry = find_entry_by_id(h.id, entries)
        if entry is None:
            issues.append(
                ValidationIssue(
                    id=h.id,
                    kind="missing-entry",
                    detail=(
                        f"trailer `{h.verb} {h.id}` (commit {h.commit_sha}) but no "
                        f"matching planning entry found in repo. "
                        "Add frontmatter (`id:` + `tracking_kind:` + `status:`) to the "
                        "backlog file so the entry is discoverable."
                    ),
                )
            )
            continue
        if entry.status != "done":
            issues.append(
                ValidationIssue(
                    id=h.id,
                    kind="status-not-done",
                    detail=(
                        f"trailer `{h.verb} {h.id}` (commit {h.commit_sha}) but "
                        f"entry in {entry.source_path} has status `{entry.status}`. "
                        "Flip it to `done` in this PR."
                    ),
                )
            )
        if pr_number is not None and entry.pr_ref != pr_number:
            kind = "pr-ref-missing" if not entry.pr_ref else "pr-ref-mismatch"
            issues.append(
                ValidationIssue(
                    id=h.id,
                    kind=kind,
                    detail=(
                        f"trailer `{h.verb} {h.id}` (commit {h.commit_sha}) but "
                        f"entry in {entry.source_path} has pr_ref `{entry.pr_ref or '(empty)'}` "
                        f"≠ current PR `{pr_number}`. Fill the entry's `pr_ref:` field."
                    ),
                )
            )
    return issues


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _format_human(hits: List[TrailerHit], issues: List[ValidationIssue]) -> str:
    if not hits:
        return "OK no Resolves/Closes/Fixes trailers found in PR range — nothing to validate.\n"
    if not issues:
        return f"OK all {len(hits)} trailer reference(s) align with frontmatter (status: done + pr_ref).\n"
    lines = [
        f"Found {len(issues)} planning-status sync issue(s) across {len(hits)} trailer reference(s):",
        "",
    ]
    for issue in issues:
        lines.append(f"  [{issue.kind}] {issue.id}: {issue.detail}")
    lines.append("")
    lines.append(
        "Default behaviour is **soft-warn** (exit 0). "
        "Run with `--strict` to fail CI on these issues."
    )
    return "\n".join(lines) + "\n"


def _format_json(hits: List[TrailerHit], issues: List[ValidationIssue]) -> str:
    payload = {
        "trailers": [
            {"verb": h.verb, "id": h.id, "commit": h.commit_sha} for h in hits
        ],
        "issues": [
            {"id": i.id, "kind": i.kind, "detail": i.detail} for i in issues
        ],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def _emit_gha_warnings(issues: List[ValidationIssue]) -> None:
    """Emit `::warning::` workflow-command lines so issues render as inline
    annotations on the GitHub Actions PR check UI (not just buried in step log).

    Without these, soft-warn mode (exit 0) is invisible — the check shows
    green and the warnings only surface if a reviewer clicks into the step
    output. The GHA workflow-command syntax is plaintext + a magic prefix;
    runs as a no-op outside GHA (the runner is what interprets it).

    Newlines + commas need URL-escaping per
    https://docs.github.com/en/actions/using-workflows/workflow-commands-for-github-actions
    """
    for issue in issues:
        # Single-line %xx-escape for newlines / commas / colons that would
        # otherwise terminate the workflow command.
        msg = (
            issue.detail
            .replace("%", "%25")
            .replace("\r", "%0D")
            .replace("\n", "%0A")
        )
        # `title=` shows up as the annotation header in the PR Checks tab.
        print(f"::warning title=planning-sync ({issue.kind})::{issue.id}: {msg}")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Validate PR trailers ↔ planning frontmatter sync (ADR-019 Layer 3).",
    )
    ap.add_argument(
        "--base",
        default=os.environ.get("PLANNING_SYNC_BASE", "origin/main"),
        help="base ref for the PR diff (default: origin/main; env PLANNING_SYNC_BASE)",
    )
    ap.add_argument(
        "--pr-number",
        default=os.environ.get("PLANNING_SYNC_PR_NUMBER"),
        help="current PR number (skip pr_ref check if omitted; env PLANNING_SYNC_PR_NUMBER)",
    )
    ap.add_argument(
        "--strict",
        action="store_true",
        help="exit 1 on any sync issue (default: exit 0 with warnings — soft-warn)",
    )
    ap.add_argument(
        "--ci",
        action="store_true",
        help="alias for --strict (kept for symmetry with peer lints)",
    )
    ap.add_argument(
        "--json",
        action="store_true",
        help="emit machine-readable JSON report instead of human prose",
    )
    args = ap.parse_args()

    try:
        hits = extract_trailers(args.base)
    except CheckError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    entries = discover_all()
    issues = validate_sync(hits, entries, pr_number=args.pr_number)

    report = _format_json(hits, issues) if args.json else _format_human(hits, issues)
    sink = sys.stderr if issues else sys.stdout
    sink.write(report)

    # When running inside GitHub Actions, additionally emit `::warning::`
    # workflow-command lines so each issue surfaces as a UI annotation on the
    # PR Checks tab — soft-warn is otherwise invisible (the check is green and
    # the stderr message is buried in the step log).
    if issues and os.environ.get("GITHUB_ACTIONS") == "true":
        _emit_gha_warnings(issues)

    if issues and (args.strict or args.ci):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
