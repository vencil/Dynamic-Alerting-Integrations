#!/usr/bin/env python3
"""check_orphan_lint.py — orphan / dead-lint detector (#717; supersedes #456 residue).

Why this exists
---------------
``scripts/tools/dx/generate_tool_map.py`` globs ``scripts/tools/lint/check_*.py``,
so a newly-added lint is guaranteed to be *recorded* into ``tool-map.{md,en.md}``
(the ``tool-map-check`` hard gate). But being documented is not being *run*: if a
new ``check_*.py`` is wired into no runner, it is a **dead lint** — it exists, it is
documented, and it never executes. #141 Track A had to *revive a dead exporter check
by hand*; dead-lint is a real, human-caught failure mode. This gate makes the
detection mechanical.

``check_structure.py`` already enforces script *placement* (ops/dx/lint buckets); it
does NOT check *registration*. This lint closes that residual gap.

What counts as "live"
---------------------
A ``check_*.py`` is **live** when its filename appears as an invocation in ANY
repo-internal runner (the "被任一 runner 引用" reference graph from #717's scope):

  - ``.pre-commit-config.yaml``        — an ``entry:`` hook
  - ``scripts/tools/validate_all.py``  — the ``TOOLS`` registry, **but only
    the rows some automatic caller actually selects** (see below)
  - ``Makefile``                       — a recipe target
  - ``.github/workflows/*.{yml,yaml}`` — a ``run:`` step
  - any sibling script under ``scripts/`` *outside* ``scripts/tools/lint/``
    (e.g. ``dx/pr_preflight.py`` runs ``check_pr_scope_drift``,
    ``dx/bump_playbook_versions.py`` runs ``check_playbook_freshness``,
    ``dx/scan_component_health.py`` runs ``check_tool_registry_jsx_parity``)

Design note — broader than the two runners the #717 body names
--------------------------------------------------------------
The issue body names only pre-commit + ``validate_all`` as runners, but its Scope
section says "只查『可執行 lint 是否被任一 runner 引用』的 repo-internal 引用圖".
Honouring the literal two-runner list would false-flag 8 checks that legitimately
run via Makefile / CI / a sibling script, forcing a static allowlist of 8 entries
that are not actually orphans — a fail-open design (if such a check were later
*removed* from CI, the allowlist would keep hiding it). The reference-graph model is
self-maintaining instead: a check is live iff some runner still invokes it, and a
later removal re-surfaces it. So this gate scans the full runner graph and the
allowlist stays reserved for *genuinely* runner-less checks.

Registry membership is not execution (#1492)
-------------------------------------------
``validate_all.py``'s ``TOOLS`` registry used to count as a runner by itself.
It is not one: every automatic caller of ``validate_all.py`` (``make
lint-docs``, ``docs-ci.yaml``) passes ``--only <list>`` and runs only those
rows. Measured on #1492: 33 registry rows, two ``--only`` lists, and ONE row
(``frontmatter_versions``) that no caller selected and no other runner
invoked — yet this gate said "all wired", and #1480 was misled by that green.
So the registry is now read for REACHABILITY: a row counts as wired iff some
automatic caller selects its key with ``--only`` (backslash continuations
joined, comment lines ignored) or some caller runs ``validate_all.py`` with
no ``--only`` at all. ``validate_all.py`` itself is no longer in the corpus —
its row strings would otherwise rescue every registered lint by name.

Why ``scripts/tools/lint/`` is excluded from the referencer set
---------------------------------------------------------------
Lint files routinely mention *other* checks' filenames in their docstrings
(cross-references, design notes). Counting those prose mentions as "references"
would mask a dead lint behind a comment. The real cross-script invokers all live in
``scripts/tools/dx/`` / ``ops/``, so excluding ``lint/`` loses no genuine wiring.

Allowlist
---------
A ``check_*.py`` that is intentionally standalone (manual-only, invoked by a human
on demand, wired to no runner) may be listed in ``ALLOWLIST`` with a one-line
justification. Helper modules are ``_``-prefixed (``_lib*.py`` / ``_lint_helpers.py``)
and never match the ``check_*.py`` glob, so they need no allowlist entry. The
allowlist is currently empty: every executable lint is wired to a runner.

Known limitation (accepted, like check_lint_toolchain_fit.py): detection is a
word-boundary filename match against the runner corpus (see ``_is_referenced`` —
not a bare substring, so ``disable_check_foo.py`` / a ``.pyc`` path no longer masks
``check_foo.py``). It is still a tripwire, not an airtight proof: a runner that
builds the script name dynamically from pieces would slip through, and a
commented-out invocation of the *exact* filename inside a *runner* file (not a lint
file, which we already exclude) would still read as live. Both are rare; revisit if
they ever happen in practice rather than reaching for an AST analyzer now.

Scope discipline (#717)
-----------------------
Repo-internal reference-graph only. This does NOT reimplement #456's staged-diff
heuristic (high false-positive; the wiring-triple body is already covered by
``tool-map-check`` / ``build-completeness-check`` / ``cli-coverage-check`` /
``doc-datools-cmds`` / ``check_changelog_no_tbd``). We also trust that the runners
themselves are live — a lint wired only into a runner that is itself never invoked
("second-order orphan") is out of scope; in practice every runner here (pre-commit,
the validate_all Drift-Detection CI job, Makefile targets, workflows) is reachable.

Usage:
    python3 scripts/tools/lint/check_orphan_lint.py        # report mode
    python3 scripts/tools/lint/check_orphan_lint.py --ci   # exit 1 on orphans

Exit codes (see scripts/tools/_lib_exitcodes.py):
    0 = no orphan lints
    1 = orphan / dead lint(s) found (--ci mode)
    2 = caller/environment error (e.g. lint dir missing)
"""
from __future__ import annotations

import argparse
import ast
import os
import re
import sys
from pathlib import Path

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)                     # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, ".."))  # repo subdir layout
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

# ── Config ──────────────────────────────────────────────────────────

# check_*.py files that are intentionally standalone (manual-only / human-invoked,
# wired to no runner). Each entry: basename -> one-line justification. Keep empty
# unless a check genuinely has no runner home; prefer wiring a check into a runner
# over allowlisting it.
ALLOWLIST: dict[str, str] = {
    # "check_example_manual.py": "manual-only forensic tool, run on demand (#NNN)",
}

# Directories to never descend into when scanning scripts/ for sibling-script
# referencers. Tree-walking under scripts/ must skip local virtualenvs and vendored
# trees: a dev's scripts/.venv/ holds thousands of third-party .py files that would
# explode the corpus (slow / OOM) and could even false-rescue a dead lint by an
# incidental filename collision in dependency source.
_SKIP_DIRS = frozenset({
    "__pycache__", ".venv", "venv", ".tox", ".nox",
    "node_modules", ".git", ".mypy_cache", ".pytest_cache",
})

# ── Reference-graph builders ────────────────────────────────────────

def find_check_lints(lint_dir: Path) -> list[str]:
    """Return sorted basenames of executable lints (check_*.py) in lint_dir."""
    return sorted(p.name for p in lint_dir.glob("check_*.py"))


def gather_referencers(project_root: Path, lint_dir: Path) -> list[Path]:
    """Collect every repo-internal runner file that may invoke a check_*.py.

    Excludes scripts/tools/lint/ itself (lint files cross-reference each other in
    prose; those mentions are not invocations — see module docstring).
    """
    referencers: list[Path] = []

    for rel in (".pre-commit-config.yaml", "Makefile"):
        p = project_root / rel
        if p.exists():
            referencers.append(p)

    workflows = project_root / ".github" / "workflows"
    if workflows.is_dir():
        referencers.extend(sorted(workflows.glob("*.yml")))
        referencers.extend(sorted(workflows.glob("*.yaml")))

    # GitLab CI is a first-class runner too (the repo carries GitLab lineage,
    # e.g. .gitlab/ci/config-diff.gitlab-ci.yml). Include it so a lint wired
    # only into GitLab CI is not false-flagged as orphan.
    root_gitlab_ci = project_root / ".gitlab-ci.yml"
    if root_gitlab_ci.exists():
        referencers.append(root_gitlab_ci)
    gitlab_dir = project_root / ".gitlab"
    if gitlab_dir.is_dir():
        referencers.extend(sorted(gitlab_dir.glob("**/*.yml")))
        referencers.extend(sorted(gitlab_dir.glob("**/*.yaml")))

    scripts_dir = project_root / "scripts"
    if scripts_dir.is_dir():
        for pattern in ("**/*.py", "**/*.sh"):
            for p in scripts_dir.glob(pattern):
                if _SKIP_DIRS.intersection(p.parts):  # skip venv / vendored trees
                    continue
                if p.parent == lint_dir:  # lint files are not referencers
                    continue
                if p.name == "validate_all.py":  # the registry is not a runner (#1492)
                    continue
                referencers.append(p)

    return referencers


# An INVOCATION of validate_all.py: a `python`/`python3` (optionally `-X utf8`)
# prefix, or a `scripts/tools/` path — a docstring's usage line, a YAML job
# `name:`, or a comment saying "(validate_all.py," is prose, not a call.
_VALIDATE_ALL_CALL = re.compile(
    r"(?:python3?\s+(?:-X\s+utf8\s+)?(?:\./)?(?:scripts/tools/)?"
    r"|(?:\./)?scripts/tools/)validate_all\.py(?![\w./])"
    # the rest of the line, PLUS following lines that are option
    # continuations (a YAML `>-`/`|` block lists one flag per line)
    r"([^\n]*(?:\n[ \t]+-[^\n]*)*)")
_ONLY_ARG = re.compile(r"--only[\s=]+[\"']?([\w,-]+)")
_SKIP_ARG = re.compile(r"--skip[\s=]+[\"']?([\w,-]+)")


def _executable_text(text: str, suffix: str = "") -> str:
    """Runner text reduced to what would EXECUTE.

    ``#`` comment lines and trailing ``  # …`` comments are dropped, a
    Python file's string literals (docstrings carry usage lines such as
    ``python3 scripts/tools/validate_all.py --ci``) are blanked via AST, and
    backslash line continuations are joined — a Makefile recipe spreads
    ``--only`` over two lines, and a comment or usage line that mentions
    ``validate_all.py`` is not a call (blind review found both shapes
    flipping the gate fail-open).
    """
    if suffix == ".py":
        try:
            tree = ast.parse(text)
            lines = text.splitlines(keepends=True)
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    for i in range(node.lineno - 1, node.end_lineno):
                        lines[i] = "\n"
            text = "".join(lines)
        except SyntaxError:
            pass
    kept = []
    for ln in text.splitlines():
        if ln.lstrip().startswith("#"):
            continue
        kept.append(re.sub(r"\s+#.*$", "", ln))
    return re.sub(r"\\\n\s*", " ", "\n".join(kept))


def registry_entries(project_root: Path) -> dict[str, str]:
    """``validate_all.TOOLS`` as {key: check basename}, read via AST so a
    registry that does not import (or is absent) yields {} instead of a crash."""
    va = project_root / "scripts" / "tools" / "validate_all.py"
    if not va.is_file():
        return {}
    try:
        tree = ast.parse(va.read_text(encoding="utf-8", errors="ignore"))
    except SyntaxError:
        return {}
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "TOOLS" for t in node.targets)
                and isinstance(node.value, (ast.List, ast.Tuple))):
            continue
        for elt in node.value.elts:
            if isinstance(elt, ast.Tuple) and len(elt.elts) >= 2:
                try:
                    key, rel = ast.literal_eval(elt.elts[0]), ast.literal_eval(elt.elts[1])
                except ValueError:
                    continue
                if isinstance(key, str) and isinstance(rel, str):
                    out[key] = Path(rel).name
    return out


def registry_reachable(project_root: Path, referencers: list[Path]) -> set[str]:
    """Basenames of registry rows some automatic caller really runs.

    A caller is any referencer line invoking ``validate_all.py``: with
    ``--only k1,k2`` it reaches exactly those keys; with no ``--only`` (and not
    ``--list`` / ``--help``) it reaches every row. Membership alone reaches
    nothing — that is the #1492 defect.
    """
    entries = registry_entries(project_root)
    if not entries:
        return set()
    selected: set[str] = set()
    bare_minus: list[set[str]] = []   # one entry per bare caller: its --skip set
    for f in referencers:
        try:
            text = _executable_text(f.read_text(encoding="utf-8", errors="ignore"),
                                    f.suffix)
        except OSError:
            continue
        for m in _VALIDATE_ALL_CALL.finditer(text):
            args = " ".join(m.group(1).split())
            only = _ONLY_ARG.search(args)
            if only:
                selected.update(k for k in only.group(1).split(",") if k)
            elif "--only" in args or "--list" in args or "--help" in args:
                continue          # unparsable/quoted --only, or not a run: reaches nothing
            elif not args.strip():
                continue          # no flags at all is not a known caller shape: fail closed
            else:
                skip = _SKIP_ARG.search(args)
                bare_minus.append(set(skip.group(1).split(",")) if skip else set())
    keys = selected & set(entries)
    for skipped in bare_minus:
        keys |= set(entries) - skipped
    return {entries[k] for k in keys}


def read_corpus(referencers: list[Path]) -> str:
    """Concatenate the text of every referencer file (errors ignored)."""
    chunks: list[str] = []
    for f in referencers:
        try:
            chunks.append(f.read_text(encoding="utf-8", errors="ignore"))
        except OSError:
            continue
    return "\n".join(chunks)


def _is_referenced(name: str, corpus: str) -> bool:
    """True iff `name` appears as a standalone filename token in `corpus`.

    Word-boundary match, NOT a bare substring: the char before the name must not
    be a filename char and the char after `.py` must not be alphanumeric. This
    stops a longer filename from masking a shorter one —  e.g. a runner invoking
    `disable_check_foo.py` / `old_check_foo.py` must NOT count as referencing the
    distinct `check_foo.py`, and a `.pyc` bytecode path must not count either.
    (It does NOT defang a literal prose mention of the *exact* filename inside a
    runner file; `lint/` — the usual cross-reference source — is already excluded,
    and commented-out invocations in Makefile/CI are rare.)
    """
    pattern = rf"(?<![A-Za-z0-9_-]){re.escape(name)}(?![A-Za-z0-9_])"
    return re.search(pattern, corpus) is not None


def find_orphans(
    check_lints: list[str],
    corpus: str,
    allowlist: dict[str, str] | None = None,
    reachable: set[str] | frozenset[str] = frozenset(),
) -> list[str]:
    """Return check_*.py basenames neither referenced in the runner corpus nor
    reachable through a selected ``validate_all`` registry row (#1492).

    Allowlisted basenames are never reported.
    """
    allow = set(allowlist or {})
    orphans = []
    for name in check_lints:
        if name in allow:
            continue
        if name in reachable:
            continue
        if not _is_referenced(name, corpus):
            orphans.append(name)
    return orphans


# ── Main ────────────────────────────────────────────────────────────

def main() -> int:
    """CLI entry point: orphan / dead-lint detector."""
    parser = argparse.ArgumentParser(
        description="Detect lint scripts wired into no runner (dead lints).",
    )
    parser.add_argument(
        "--ci", action="store_true",
        help="Exit with code 1 on orphans (for CI/pre-commit)",
    )
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parents[3]
    lint_dir = project_root / "scripts" / "tools" / "lint"
    if not lint_dir.is_dir():
        print(f"✗ lint dir not found: {lint_dir}", file=sys.stderr)
        return EXIT_CALLER_ERROR

    check_lints = find_check_lints(lint_dir)
    referencers = gather_referencers(project_root, lint_dir)
    corpus = read_corpus(referencers)
    reachable = registry_reachable(project_root, referencers)
    entries = registry_entries(project_root)
    orphans = find_orphans(check_lints, corpus, ALLOWLIST, reachable)

    if not orphans:
        allow_note = f" ({len(ALLOWLIST)} allowlisted)" if ALLOWLIST else ""
        print(f"✓ All {len(check_lints)} executable lints are wired to a "
              f"runner{allow_note}.")
        return EXIT_OK

    print("✗ Orphan / dead lint(s) — wired into no runner:")
    registered = {v: k for k, v in entries.items()}
    for name in orphans:
        if name in registered:
            print(f"  DEAD  scripts/tools/lint/{name}  (in validate_all TOOLS as "
                  f"'{registered[name]}', but no automatic caller selects it "
                  f"with --only — registry membership is not execution, #1492)")
        else:
            print(f"  DEAD  scripts/tools/lint/{name}")
    print()
    print("Fix: wire each into a runner — a .pre-commit-config.yaml entry:, "
          "a validate_all.py TOOLS row that an automatic --only list SELECTS, "
          "a Makefile recipe, a CI workflow run: step, or invoke it from a "
          "dx/ops sibling script. If it is")
    print("genuinely manual-only, add it to ALLOWLIST in this file with a "
          "justification.")

    return EXIT_VIOLATION if args.ci else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
