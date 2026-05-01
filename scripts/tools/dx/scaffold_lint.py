#!/usr/bin/env python3
"""scaffold_lint.py — generate a new pre-commit lint script from template.

Why this exists
---------------
v2.8.0 shipped 4 lint scripts in one cycle (PR #154 / #162 / #166 /
#169), each hand-rolled with the same boilerplate:

  1. argparse with ``--ci`` flag (and optional ``--strict-X`` for
     granular activation per PR #162 pattern)
  2. ``_compute_exit_code(*, ci, ..., n_violations) -> int`` pure
     helper for the severity matrix (testable without disk / IO)
  3. ``Finding`` dataclass with ``path/line/col/snippet`` fields and
     ``render()`` method that handles paths-outside-PROJECT_ROOT
     (PR #166 amend caught this as a real bug)
  4. ``scan_source(path, source) -> list[Finding]`` pure scanner
  5. ``_iter_target_files()`` / ``_resolve_paths()`` for file discovery
  6. ``main(argv)`` argparse + scan + print + exit
  7. Per-line ignore comment with **3-line lookback** (consistent
     with PR #166 ``# subprocess-timeout: ignore`` /
     PR #169 ``<!-- enforcement-claim: ignore -->`` /
     PR #170 ``<!-- marketing-language: ignore -->``)
  8. Test scaffold: ``TestComputeExitCode`` truth table +
     ``TestMain`` integration + ``TestLive*`` dogfood gate
  9. Pre-commit hook entry stub with FATAL-on-finds severity (per
     PR #169 / #170 ship-strict-from-day-1 pattern when codebase
     post-rewrite violation count is 0)

**Scaffold-before-third-instance rule** says 4 hand-rolls is past
time (rule fires at 3rd instance). This tool codifies the
boilerplate; the next lint takes ~15 min instead of 1-2 hr.

Five kinds (matching common shape of past lints)
------------------------------------------------

- ``ast`` — Python AST walker (PR #166 subprocess-timeout shape):
  scans ``scripts/`` / ``tests/`` for code patterns
- ``text`` — keyword / regex line-by-line (PR #170 marketing-language
  shape): scans ``docs/`` / ``README``
- ``yaml`` — YAML structural validation: scans ``rule-packs/`` etc
- ``meta`` — cross-file consistency (PR #169 dev-rules-enforcement
  shape): scans one doc against another config
- ``freshness`` — front-matter / age guard: scans docs by date

Per-kind templates only differ in the ``scan_source`` placeholder +
default ``files:`` regex + suppression specifics (text supports
fenced-code-block detection; ast / yaml don't).

Usage
-----

::

    # Generate
    python3 scripts/tools/dx/scaffold_lint.py \\
        --name foo_bar \\
        --kind text \\
        --description "Detect foo_bar pattern in docs" \\
        --files '^docs/.*\\.md$'

    # Or via Make wrapper
    make lint-extract NAME=foo_bar KIND=text \\
        DESCRIPTION="..." FILES='^docs/.*\\.md$'

    # Dry-run
    python3 scripts/tools/dx/scaffold_lint.py \\
        --name foo --kind text --description "..." \\
        --files '...' --dry-run

    # Force overwrite existing
    python3 scripts/tools/dx/scaffold_lint.py \\
        --name foo --kind text --description "..." \\
        --files '...' --force

Idempotent on re-run: skips files that already exist unless
``--force`` is passed; pre-commit hook entry insertion is a no-op
if the hook id is already present.

What it does NOT do
-------------------
- Does not write the actual scan logic — that's the lint author's
  job. The template has a ``# TODO`` placeholder and a small
  example pattern to start from.
- Does not run the new lint to dogfood it. Author should run
  ``pytest tests/lint/test_check_<name>.py`` after filling in
  ``scan_source``.
- Does not auto-update ``tool-map.md`` — that's done by the
  existing pre-commit ``tool-map-check`` hook on next commit.

References (the 4 lints this scaffold abstracts):
- PR #154 ``lint_jsx_babel.py`` linecount soft/hard cap
- PR #162 ``lint_jsx_babel.py`` granular ``--strict`` split
- PR #166 ``check_subprocess_timeout.py`` AST class
- PR #169 ``check_dev_rules_enforcement.py`` meta class

A 5th candidate (``check_marketing_language.py``, PR #170 closed
2026-05-01) was abandoned: keyword lists are inherently incomplete
for natural-language nuance ("卓越條件" technical vs "卓越" marketing
can't be lex'd), and reviewer judgment is more reliable than
heuristic + ignore markers. The pattern still informs the ``text``
kind template (fenced-code suppression / per-line ignore lookback),
just not the specific hook.
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parents[3]
LINT_DIR = PROJECT_ROOT / "scripts" / "tools" / "lint"
TESTS_DIR = PROJECT_ROOT / "tests" / "lint"
PRECOMMIT_CONFIG = PROJECT_ROOT / ".pre-commit-config.yaml"

VALID_KINDS = ("ast", "text", "yaml", "meta", "freshness")

# Identifier shape for new lint name: snake_case, must start with letter,
# only [a-z0-9_]. Maps to file `check_<name>.py` and hook id `<name>-check`
# (with `_` → `-` for the hook id).
_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*[a-z0-9]$")

# Hook id derived from name: replace `_` with `-`, append `-check`.
_HOOK_SUFFIX = "-check"


@dataclass
class ScaffoldPaths:
    """Resolved paths for a scaffolded lint."""

    name: str
    kind: str
    script: Path
    test: Path
    hook_id: str
    ignore_marker: str


def is_valid_lint_name(name: str) -> bool:
    """True if name is a valid Python module identifier matching our convention."""
    if not name:
        return False
    if not _NAME_PATTERN.match(name):
        return False
    # Reserved by Python or our codebase.
    if name in {"check", "lint", "test", "init", "main"}:
        return False
    return True


def derive_paths(name: str, kind: str) -> ScaffoldPaths:
    """Resolve target paths for the new lint + test."""
    if not is_valid_lint_name(name):
        raise ValueError(
            f"invalid lint name {name!r}: must be snake_case ASCII "
            f"matching {_NAME_PATTERN.pattern}"
        )
    if kind not in VALID_KINDS:
        raise ValueError(
            f"invalid kind {kind!r}: must be one of {VALID_KINDS}"
        )

    script = LINT_DIR / f"check_{name}.py"
    test = TESTS_DIR / f"test_check_{name}.py"
    hook_id = f"{name.replace('_', '-')}{_HOOK_SUFFIX}"
    # Ignore comment marker per kind: text uses HTML comment for docs;
    # ast / meta use Python comment; yaml uses YAML comment.
    if kind == "text":
        ignore_marker = f"<!-- {name.replace('_', '-')}: ignore -->"
    elif kind == "yaml":
        ignore_marker = f"# {name.replace('_', '-')}: ignore"
    else:  # ast / meta / freshness — Python or generic comment style
        ignore_marker = f"# {name.replace('_', '-')}: ignore"

    return ScaffoldPaths(
        name=name,
        kind=kind,
        script=script,
        test=test,
        hook_id=hook_id,
        ignore_marker=ignore_marker,
    )


# ---------------------------------------------------------------------------
# Template renderers
# ---------------------------------------------------------------------------

_SHARED_HEADER = '''#!/usr/bin/env python3
"""check_{name}.py — {description}.

[TODO: expand docstring with]
- Why this exists (motivating prior incident or rule)
- What it flags (precise contract)
- Severity model (when does --ci fail?)
- Per-line ignore: ``{ignore_marker}``
- References to related PRs / playbook sections

Generated by ``scripts/tools/dx/scaffold_lint.py`` ({kind} kind).
See https://github.com/vencil/Dynamic-Alerting-Integrations/pull/171
for the scaffold tool's design rationale.
"""
from __future__ import annotations

import argparse
{kind_imports}
import sys
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]

# Per-line ignore marker — append to the line containing the call OR
# up to 3 lines above (covers multi-line rationale comment blocks).
_IGNORE_MARKER = "{ignore_marker_inner}"
_IGNORE_LOOKBACK_LINES = 3


@dataclass
class {finding_class}:
    """A single finding from this lint."""

    path: Path
    line: int
    col: int
    snippet: str

    def render(self) -> str:
        # Default: render absolute paths relative to PROJECT_ROOT for
        # readable output. If path is OUTSIDE PROJECT_ROOT (e.g. tmp_path
        # used in tests, or absolute CLI args), fall back to the absolute
        # string — `relative_to` would raise ValueError otherwise.
        # (Robustness lesson from PR #166 amend: tmp_path fixtures.)
        if self.path.is_absolute():
            try:
                rel = self.path.relative_to(PROJECT_ROOT)
            except ValueError:
                rel = self.path
        else:
            rel = self.path
        return f"{{rel}}:{{self.line}}:{{self.col}} {{self.snippet[:120]}}"


def _line_has_ignore(source_lines: list[str], line_no: int) -> bool:
    """True if line_no OR up to 3 lines above contain the ignore marker."""
    for offset in range(_IGNORE_LOOKBACK_LINES + 1):
        candidate = line_no - offset
        if 1 <= candidate <= len(source_lines):
            if _IGNORE_MARKER in source_lines[candidate - 1]:
                return True
    return False


'''

_AST_SCAN = '''def scan_source(path: Path, source: str) -> list[{finding_class}]:
    """Walk a Python source string and return all findings.

    Robust to syntax errors (returns empty list rather than crashing —
    the lint should not block commits because some other file has a
    parse error; that's caught by other lints).
    """
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return []

    source_lines = source.splitlines()
    findings: list[{finding_class}] = []

    for node in ast.walk(tree):
        # TODO: replace this stub with the real detection rule.
        # Example pattern (from PR #166 subprocess-timeout):
        #
        #   if not isinstance(node, ast.Call):
        #       continue
        #   if not _matches_target_pattern(node):
        #       continue
        #   if _has_required_kwarg(node):
        #       continue
        #   if _line_has_ignore(source_lines, node.lineno):
        #       continue
        #   findings.append({finding_class}(
        #       path=path,
        #       line=node.lineno,
        #       col=node.col_offset + 1,
        #       snippet=source_lines[node.lineno - 1].strip(),
        #   ))
        del node  # placeholder

    return findings

'''

_TEXT_SCAN = '''def _build_fenced_block_set(lines: list[str]) -> set[int]:
    """Return set of 1-based line numbers that are inside ``` fences.

    Doc-lint utility: keyword matches inside fenced code blocks are
    almost always illustrative ("don't write `bad_keyword` like this")
    and should be suppressed automatically.
    """
    fenced: set[int] = set()
    in_fence = False
    for idx, line in enumerate(lines, start=1):
        stripped = line.lstrip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            fenced.add(idx)
            continue
        if in_fence:
            fenced.add(idx)
    return fenced


def _is_within_code_span(line: str, col: int) -> bool:
    """True if column ``col`` (1-based) is inside backticks on this line."""
    pre = line[: col - 1]
    return pre.count("`") % 2 == 1


# TODO: define your detection patterns here. Examples:
#   _BANNED_KEYWORDS = ("foo", "bar")
#   _PATTERN_RE = re.compile(r"...")


def scan_source(path: Path, source: str) -> list[{finding_class}]:
    """Walk a text file and return all findings.

    Skips fenced code blocks + inline backticks + lines with the
    ignore marker (per shared `_line_has_ignore`).
    """
    findings: list[{finding_class}] = []
    lines = source.splitlines()
    fenced = _build_fenced_block_set(lines)

    for idx, line in enumerate(lines, start=1):
        if idx in fenced:
            continue
        if _line_has_ignore(lines, idx):
            continue
        # TODO: replace with real detection.
        # Example (from PR #170 marketing-language):
        #   for kw in _BANNED_KEYWORDS:
        #       pos = line.lower().find(kw.lower())
        #       while pos >= 0:
        #           col = pos + 1
        #           if _is_within_code_span(line, col):
        #               pos = line.lower().find(kw.lower(), pos + 1)
        #               continue
        #           findings.append({finding_class}(
        #               path=path, line=idx, col=col,
        #               snippet=line.strip(),
        #           ))
        #           pos = line.lower().find(kw.lower(), pos + 1)
        del line  # placeholder

    return findings

'''

_YAML_SCAN = '''def scan_source(path: Path, source: str) -> list[{finding_class}]:
    """Parse YAML + return all findings.

    Robust to YAML parse errors (returns empty list with one synthetic
    finding noting the parse failure).
    """
    findings: list[{finding_class}] = []
    try:
        data = yaml.safe_load(source)
    except yaml.YAMLError as exc:
        findings.append({finding_class}(
            path=path,
            line=getattr(exc, "problem_mark", None).line + 1
                if getattr(exc, "problem_mark", None)
                else 0,
            col=0,
            snippet=f"YAML parse error: {{exc}}",
        ))
        return findings

    if data is None:
        return findings

    # TODO: walk the parsed structure. Example:
    #   if isinstance(data, dict):
    #       for key, value in data.items():
    #           if not _is_valid_key(key):
    #               findings.append({finding_class}(
    #                   path=path, line=0, col=0,
    #                   snippet=f"invalid key: {{key}}",
    #               ))
    return findings

'''

_META_SCAN = '''def scan_source(
    path: Path,
    source: str,
    *,
    known_set: set[str] | None = None,
) -> list[{finding_class}]:
    """Cross-file consistency scanner.

    Walks ``source`` line-by-line for claims that should resolve
    against ``known_set`` (loaded from another file by the caller —
    e.g. PR #169 loads hook ids from .pre-commit-config.yaml).

    Trigger context (must appear on same line as candidate claim)
    keeps false-positive rate near zero. Update ``_TRIGGERS`` for
    your specific rule.
    """
    if known_set is None:
        known_set = set()

    # TODO: replace these with your trigger phrases.
    triggers_re = re.compile(r"|".join([
        # "pre-commit hook",
        # "must satisfy",
        # ...
    ]) or r"$.^")  # never matches if list is empty

    # TODO: replace with the inline-code identifier shape your claim uses.
    candidate_re = re.compile(r"`([a-z][a-z0-9_-]+)`", re.IGNORECASE)

    findings: list[{finding_class}] = []
    lines = source.splitlines()

    for idx, line in enumerate(lines, start=1):
        if _line_has_ignore(lines, idx):
            continue
        if not triggers_re.search(line):
            continue
        for m in candidate_re.finditer(line):
            claim = m.group(1)
            if claim in known_set:
                continue
            findings.append({finding_class}(
                path=path,
                line=idx,
                col=m.start() + 1,
                snippet=line.strip(),
            ))

    return findings

'''

_FRESHNESS_SCAN = '''def scan_source(path: Path, source: str) -> list[{finding_class}]:
    """Front-matter freshness / age scanner.

    Reads YAML front-matter (between leading ``---`` markers) and
    flags entries based on date / version fields per the rule.
    """
    if not source.startswith("---\\n"):
        return []  # No front-matter — skip silently.

    end = source.find("\\n---\\n", 4)
    if end < 0:
        return []
    fm_text = source[4:end]
    try:
        front_matter = yaml.safe_load(fm_text)
    except yaml.YAMLError:
        return []
    if not isinstance(front_matter, dict):
        return []

    findings: list[{finding_class}] = []
    # TODO: implement freshness check. Example pattern:
    #   age_days = (today - parsed_date).days
    #   if age_days > MAX_DAYS:
    #       findings.append({finding_class}(
    #           path=path, line=1, col=0,
    #           snippet=f"front-matter date {{front_matter.get('date')}} > {{MAX_DAYS}}d old",
    #       ))
    return findings

'''

_SHARED_FOOTER = '''def _resolve_target_paths(args: argparse.Namespace) -> list[Path]:
    """Resolve --paths args or fall back to default scan."""
    if args.paths:
        return [
            Path(p) if Path(p).is_absolute() else PROJECT_ROOT / p
            for p in args.paths
        ]
    # TODO: replace with your default file discovery.
    # Example:
    #   return sorted((PROJECT_ROOT / "scripts").rglob("*.py"))
    return []


def _compute_exit_code(*, ci: bool, n_findings: int) -> int:
    """Pure helper for severity routing.

    Severity matrix:

    | --ci  | n_findings | exit |
    |-------|------------|------|
    | False | *          | 0    |
    | True  | 0          | 0    |
    | True  | >0         | 1    |

    For granular --strict-X flags (per PR #162 pattern), extend this
    helper with additional bool parameters. See PR #166's
    ``check_subprocess_timeout.py`` for the granular shape.
    """
    if not ci:
        return 0
    return 1 if n_findings > 0 else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="{description}",
    )
    parser.add_argument(
        "paths",
        nargs="*",
        help="Files or directories to scan. See _resolve_target_paths.",
    )
    parser.add_argument(
        "--ci",
        action="store_true",
        help="Exit 1 on findings (default: report only).",
    )
    args = parser.parse_args(argv)

    paths = _resolve_target_paths(args)
    if not paths:
        if args.ci:
            print("✓ no files matched scan target")
        return 0

    all_findings: list[{finding_class}] = []
    for path in paths:
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            print(f"⚠ cannot read {{path}}: {{exc}}", file=sys.stderr)
            continue
        all_findings.extend(scan_source(path, source))

    if not all_findings:
        if args.ci:
            print(f"✓ no findings across {{len(paths)}} file(s)")
        return 0

    print(
        f"✗ {{len(all_findings)}} finding(s) in "
        f"{{len({{f.path for f in all_findings}})}} file(s):",
        file=sys.stderr,
    )
    for f in all_findings:
        print(f"  {{f.render()}}", file=sys.stderr)

    return _compute_exit_code(ci=args.ci, n_findings=len(all_findings))


if __name__ == "__main__":
    raise SystemExit(main())
'''


_KIND_IMPORTS = {
    "ast": "import ast",
    "text": "import re",
    "yaml": "import yaml",
    "meta": "import re\nimport yaml",
    "freshness": "import yaml",
}

_KIND_SCAN = {
    "ast": _AST_SCAN,
    "text": _TEXT_SCAN,
    "yaml": _YAML_SCAN,
    "meta": _META_SCAN,
    "freshness": _FRESHNESS_SCAN,
}


def render_script(paths: ScaffoldPaths, description: str) -> str:
    """Compose the full Python source for a new lint."""
    finding_class = (
        "".join(part.capitalize() for part in paths.name.split("_"))
        + "Finding"
    )
    header = _SHARED_HEADER.format(
        name=paths.name,
        description=description,
        kind=paths.kind,
        ignore_marker=paths.ignore_marker,
        # The marker as embedded inside Python string literal must escape
        # any character that would close the string (none here, but be
        # defensive). The marker text itself is taken from ScaffoldPaths.
        ignore_marker_inner=paths.ignore_marker.replace("\"", "\\\""),
        finding_class=finding_class,
        kind_imports=_KIND_IMPORTS[paths.kind],
    )
    scan = _KIND_SCAN[paths.kind].format(finding_class=finding_class)
    footer = _SHARED_FOOTER.format(
        description=description,
        finding_class=finding_class,
    )
    return header + scan + footer


# ---------------------------------------------------------------------------
# Test scaffold
# ---------------------------------------------------------------------------

_TEST_TEMPLATE = '''"""Tests for check_{name}.py — {description}.

Pinned contracts
----------------
1. **Detection**: TODO describe what the lint flags + which positive
   cases are covered.
2. **Suppression**: TODO ignore-marker behavior covered.
3. **Severity matrix** (`_compute_exit_code`):
   - !ci, * → exit 0
   - ci, 0 findings → exit 0
   - ci, >0 findings → exit 1
4. **Robustness**: TODO syntax-error / weird-input survival.
5. **Live dogfood** (`TestLiveRepo`): scans the actual repo and
   confirms zero findings before merge — gates broken state from
   landing if --ci is fatal.

Generated by ``scripts/tools/dx/scaffold_lint.py``. See PR #171.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_TOOLS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "scripts", "tools", "lint"
)
sys.path.insert(0, _TOOLS_DIR)

import check_{name} as lint  # noqa: E402


def _scan(source: str, fake_path: str = "fake.txt"):
    return lint.scan_source(Path(fake_path), source)


# ---------------------------------------------------------------------------
# Detection — TODO: add positive cases
# ---------------------------------------------------------------------------
class TestDetection:
    def test_placeholder_no_findings_in_empty_source(self):
        assert _scan("") == []

    # TODO: add @pytest.mark.parametrize for positive detection cases.


# ---------------------------------------------------------------------------
# Suppression — ignore comment behavior (3-line lookback)
# ---------------------------------------------------------------------------
class TestSuppression:
    def test_ignore_on_same_line_suppresses(self):
        # TODO: replace with a positive-detection case + ignore on same line.
        # src = "<offending content>  {ignore_marker}\\n"
        # assert _scan(src) == []
        pass

    def test_ignore_within_3_line_lookback_suppresses(self):
        # TODO: ignore marker 1-3 lines above offense suppresses.
        pass

    def test_ignore_outside_3_line_lookback_does_not_suppress(self):
        # TODO: marker 4+ lines above offense should NOT suppress
        # (keeps ignore radius tight).
        pass


# ---------------------------------------------------------------------------
# Severity matrix — _compute_exit_code truth table
# ---------------------------------------------------------------------------
class TestComputeExitCode:
    @pytest.mark.parametrize("n", [0, 1, 5])
    def test_no_ci_always_exit_0(self, n):
        assert lint._compute_exit_code(ci=False, n_findings=n) == 0

    def test_ci_zero_findings_exit_0(self):
        assert lint._compute_exit_code(ci=True, n_findings=0) == 0

    @pytest.mark.parametrize("n", [1, 5, 100])
    def test_ci_with_findings_exit_1(self, n):
        assert lint._compute_exit_code(ci=True, n_findings=n) == 1


# ---------------------------------------------------------------------------
# main() integration — argparse + exit code wiring
# ---------------------------------------------------------------------------
class TestMain:
    @pytest.mark.timeout(15)
    def test_main_clean_exit_0(self, tmp_path, capsys, monkeypatch):
        # TODO: write a clean fixture file and assert main() returns 0.
        clean = tmp_path / "clean.txt"
        clean.write_text("# placeholder clean file\\n", encoding="utf-8")
        rc = lint.main(["--ci", str(clean)])
        assert rc == 0

    @pytest.mark.timeout(15)
    def test_main_dirty_under_ci_exit_1(self, tmp_path, capsys, monkeypatch):
        # TODO: write a dirty fixture that triggers detection;
        # assert main(['--ci', ...]) returns 1.
        pass


# ---------------------------------------------------------------------------
# Live dogfood — actual repo must pass
# ---------------------------------------------------------------------------
class TestLiveRepo:
    """Run the lint against the actual repo. If this PR's edits don't
    fully eliminate findings, this test fails — preventing PR from
    landing in a broken state.
    """

    @pytest.mark.timeout(30)
    def test_live_repo_has_no_findings(self):
        # TODO: enumerate real targets (pattern from existing lints):
        #   candidates = sorted((lint.PROJECT_ROOT / "<dir>").rglob("*.<ext>"))
        candidates: list[Path] = []
        if not candidates:
            pytest.skip("No targets resolved; fill TODO before merge")
        all_findings = []
        for path in candidates:
            try:
                source = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            all_findings.extend(lint.scan_source(path, source))

        assert all_findings == [], (
            f"Live repo has {{len(all_findings)}} finding(s):\\n"
            + "\\n".join(f"  - {{f.render()}}" for f in all_findings[:10])
        )
'''


def render_test(paths: ScaffoldPaths, description: str) -> str:
    """Compose full pytest source for a new lint."""
    return _TEST_TEMPLATE.format(
        name=paths.name,
        description=description,
        ignore_marker=paths.ignore_marker.replace("\\", "\\\\"),
    )


# ---------------------------------------------------------------------------
# Pre-commit hook entry insertion
# ---------------------------------------------------------------------------

_HOOK_ENTRY_TEMPLATE = '''
      - id: {hook_id}
        name: {description}
        entry: python3 -X utf8 scripts/tools/lint/check_{name}.py --ci
        language: python
        pass_filenames: false
        files: {files_pattern}
        # Auto-stage, FATAL on findings (default for new lints when
        # post-rewrite violation count is 0; mirrors PR #169 / #170).
        # If existing codebase has violations, add granular `--strict-{name}`
        # flag and ship warn-only first (PR #166 pattern).
        # Per-line escape via `{ignore_marker}` for legitimate
        # exceptions / illustrative quotes.

'''


def render_hook_entry(
    paths: ScaffoldPaths,
    description: str,
    files_pattern: str,
) -> str:
    return _HOOK_ENTRY_TEMPLATE.format(
        hook_id=paths.hook_id,
        name=paths.name,
        description=description,
        files_pattern=files_pattern,
        ignore_marker=paths.ignore_marker,
    )


def insert_hook_entry(
    config_text: str,
    hook_entry: str,
    hook_id: str,
) -> tuple[str, bool]:
    """Insert hook entry into .pre-commit-config.yaml before the marker.

    The insertion point is at the END of the first ``hooks:`` block.
    Idempotent: if ``hook_id`` already appears, returns (config_text,
    False) unchanged.

    Returns (new_text, changed).
    """
    if f"id: {hook_id}\n" in config_text:
        return config_text, False
    if f"id: {hook_id}\r\n" in config_text:
        return config_text, False

    # Find the LAST `      - id:` line in the file (last hook entry).
    # We append after the last hook to preserve existing order.
    pattern = re.compile(r"^      - id: ([\w\-]+)$", re.MULTILINE)
    matches = list(pattern.finditer(config_text))
    if not matches:
        # No hooks yet — append at end of file.
        return config_text + hook_entry, True

    # Find the end of the last hook (next blank line or EOF).
    last_match = matches[-1]
    after_last_id = last_match.end()
    # Walk forward to find next blank line (gives us end of hook block).
    rest = config_text[after_last_id:]
    blank_match = re.search(r"\n\n", rest)
    if blank_match:
        insert_at = after_last_id + blank_match.end() - 1  # before second \n
    else:
        insert_at = len(config_text)

    new_text = (
        config_text[:insert_at]
        + hook_entry.rstrip("\n")
        + "\n"
        + config_text[insert_at:]
    )
    return new_text, True


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate a new pre-commit lint script from template."
    )
    parser.add_argument(
        "--name",
        required=True,
        help="snake_case lint name (without 'check_' prefix). E.g. 'foo_bar'.",
    )
    parser.add_argument(
        "--kind",
        required=True,
        choices=VALID_KINDS,
        help=f"Lint shape: {VALID_KINDS}",
    )
    parser.add_argument(
        "--description",
        required=True,
        help="One-line description for docstring + hook name.",
    )
    parser.add_argument(
        "--files",
        default="",
        help="Pre-commit hook 'files:' regex (e.g. '^docs/.*\\.md$'). "
        "Empty = scaffolds with placeholder, author must fill in.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print intended actions, don't write files.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing files.",
    )
    parser.add_argument(
        "--no-hook",
        action="store_true",
        help="Skip pre-commit hook entry insertion (useful for scaffolds "
        "you intend to wire manually).",
    )
    args = parser.parse_args(argv)

    try:
        paths = derive_paths(args.name, args.kind)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    files_pattern = args.files or '"TODO_FILES_PATTERN"  # author must fill in'

    actions: list[tuple[str, Path, str]] = []  # (action, target, content)

    # Script
    if paths.script.exists() and not args.force:
        actions.append(("skip-exists", paths.script, ""))
    else:
        actions.append((
            "write",
            paths.script,
            render_script(paths, args.description),
        ))

    # Test
    if paths.test.exists() and not args.force:
        actions.append(("skip-exists", paths.test, ""))
    else:
        actions.append((
            "write",
            paths.test,
            render_test(paths, args.description),
        ))

    # Pre-commit hook entry
    if not args.no_hook:
        if PRECOMMIT_CONFIG.exists():
            current = PRECOMMIT_CONFIG.read_text(encoding="utf-8")
            new_text, changed = insert_hook_entry(
                current,
                render_hook_entry(paths, args.description, files_pattern),
                paths.hook_id,
            )
            if changed:
                actions.append(("hook-insert", PRECOMMIT_CONFIG, new_text))
            else:
                actions.append(("hook-skip-exists", PRECOMMIT_CONFIG, ""))

    # Apply / report
    print(f"Scaffolding lint: {paths.name} (kind={paths.kind})")
    print(f"  hook id: {paths.hook_id}")
    print(f"  ignore marker: {paths.ignore_marker}")
    for action, target, content in actions:
        rel = target.relative_to(PROJECT_ROOT) if target.is_absolute() else target
        if action == "skip-exists":
            print(f"  ⊘ skip (exists, use --force): {rel}")
            continue
        if action == "hook-skip-exists":
            print(f"  ⊘ skip (hook id already present): {rel}")
            continue
        if args.dry_run:
            print(f"  + would {action}: {rel} ({len(content)} bytes)")
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        print(f"  ✓ {action}: {rel}")

    if args.dry_run:
        print("\n(dry-run — no files written)")
    else:
        print(
            "\nNext steps:\n"
            f"  1. Fill in TODOs in {paths.script.relative_to(PROJECT_ROOT)}\n"
            f"  2. Fill in TODOs in {paths.test.relative_to(PROJECT_ROOT)}\n"
            f"  3. Run: pytest {paths.test.relative_to(PROJECT_ROOT)} -v\n"
            f"  4. Run: pre-commit run {paths.hook_id} --all-files\n"
            "  5. Replace `TODO_FILES_PATTERN` placeholder in "
            ".pre-commit-config.yaml if you didn't pass --files"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
