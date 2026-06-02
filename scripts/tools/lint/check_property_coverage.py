#!/usr/bin/env python3
"""check_property_coverage.py — Property-pilot coverage drift detector.

Gap 2 of the testing-quality memory roadmap. The property + mutation
pilot landed at 31 functions across 12 modules (see PRs #329-#333),
but with no enforcement, future-added pure helpers wouldn't get
flagged for property tests. This lint is the ratchet.

What this validates
-------------------

For each module declared in `tests/shared/property-coverage.yaml`:

  1. **Every top-level function in the module is triaged.** Either
     listed under `covered:` (with a property test in
     test_property_tools.py + a mutation entry in _mutation_pilot.py)
     OR listed under `excluded:` (with a one-line reason).

     Functions in the source that are in NEITHER list cause the lint
     to fail — forcing the author to consciously decide.

  2. **Every `covered:` claim is real.** The function actually exists
     in the source AND is referenced by name in
     `tests/shared/test_property_tools.py`. (Catches typos and stale
     manifest entries.)

  3. **Every `excluded:` reason is non-empty.** Prevents drive-by
     `excluded: foo:` exclusions without justification.

  4. **No orphan manifest entries.** Functions listed but missing
     from the source file → manifest is stale.

Usage
-----

    python3 scripts/tools/lint/check_property_coverage.py
    python3 scripts/tools/lint/check_property_coverage.py --ci
    python3 scripts/tools/lint/check_property_coverage.py --json

Exit codes
----------

  0  manifest in sync with source + tests
  1  drift detected (functions need triage / manifest stale)
  2  configuration error (manifest missing, source unparseable, etc.)

Why a separate manifest, not inline test scanning
-------------------------------------------------

Could be tempting to just scan `test_property_tools.py` for
`Test{FuncName}Properties` class names and infer coverage. Two
reasons against:

  - **`excluded:` with reasons** is the load-bearing part. Inferring
    coverage doesn't capture WHY a function isn't tested (I/O-bound,
    orchestrator, argparse helper). The reasons are documentation.
  - **The manifest forces a triage decision** at the moment a new
    function is added. Implicit inference would silently miss
    additions until a quarterly audit.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, ".."))  # Repo subdir layout
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_MANIFEST = REPO_ROOT / "tests" / "shared" / "property-coverage.yaml"
DEFAULT_TEST_FILE = REPO_ROOT / "tests" / "shared" / "test_property_tools.py"


def _safe_relative(path: Path, base: Path) -> str:
    """Best-effort `path.relative_to(base).as_posix()`; falls back to
    the absolute string if the path is outside the base. Used in
    diagnostic messages where we'd rather not crash on
    monkeypatch-driven tests that pass an unrelated tmp_path."""
    try:
        return path.relative_to(base).as_posix()
    except ValueError:
        return path.as_posix()


@dataclass
class Issue:
    """One drift finding."""
    module: str
    severity: str  # "error" or "warn"
    kind: str  # missing-triage / stale-covered / stale-excluded / no-reason / no-test-ref
    detail: str

    def format(self) -> str:
        return f"  [{self.kind}] {self.module}: {self.detail}"


def _module_top_level_functions(source_path: Path) -> list[str]:
    """Return names of top-level `def` functions AND class methods in *source_path*.

    Top-level functions are returned as bare names (`foo`).
    Class methods are returned as `ClassName.method_name`.

    Skips:
      - dunder methods (`__init__`, `__str__`, etc.) — they're protocol
        glue, not value computations worth a property test
      - typing-stub bodies (anywhere)

    Includes:
      - public functions (no leading underscore)
      - single-underscore-prefixed helpers (load-bearing in this codebase
        per scripts/tools convention; many `_audience_str` / `_parse_front_matter`
        ARE in pilot scope)
      - class methods (e.g., `GoBinaryDispatcher._resolve_binary`)
    """
    try:
        tree = ast.parse(source_path.read_text(encoding="utf-8"),
                         filename=str(source_path))
    except (OSError, SyntaxError) as e:
        raise ValueError(f"cannot parse {source_path}: {e}") from e

    names: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("__"):
                continue
            names.append(node.name)
        elif isinstance(node, ast.ClassDef):
            for item in node.body:
                if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if item.name.startswith("__"):
                    continue
                names.append(f"{node.name}.{item.name}")
    return names


def _test_file_function_names(test_path: Path) -> set[str]:
    """Names referenced in test_property_tools.py.

    We use a generous heuristic: any identifier that appears either
    as a `mod.<name>(`, `mod.<name>` attribute access, or simply as
    a bare word in the file is considered "referenced".

    The strict alternative — looking for `Test{Pascal(name)}Properties`
    — is brittle to test class naming conventions and would
    false-fail on parameterised cases (TestParseDurationSecondsProperties
    parametrises over multiple aspects of parse_duration_seconds).
    """
    if not test_path.is_file():
        return set()
    text = test_path.read_text(encoding="utf-8")
    # Crude word-boundary scan for any identifier
    import re
    return set(re.findall(r"\b[a-zA-Z_][a-zA-Z0-9_]*\b", text))


def validate_manifest(
    manifest: dict,
    repo_root: Path,
    test_path: Path,
) -> list[Issue]:
    """Run all checks. Returns list of Issues (empty = clean)."""
    issues: list[Issue] = []
    modules = manifest.get("modules") or {}
    if not isinstance(modules, dict):
        issues.append(Issue(
            module="<root>", severity="error", kind="bad-manifest",
            detail=f"`modules:` must be a mapping, got {type(modules).__name__}",
        ))
        return issues

    referenced_names = _test_file_function_names(test_path)

    for rel_path, scope in sorted(modules.items()):
        source_path = repo_root / rel_path
        if not source_path.is_file():
            issues.append(Issue(
                module=rel_path, severity="error", kind="missing-source",
                detail=f"manifest lists module {rel_path!r} but the file doesn't exist",
            ))
            continue

        if not isinstance(scope, dict):
            issues.append(Issue(
                module=rel_path, severity="error", kind="bad-scope",
                detail=f"module entry must be a mapping, got {type(scope).__name__}",
            ))
            continue

        covered = scope.get("covered") or []
        excluded = scope.get("excluded") or {}
        if not isinstance(covered, list):
            issues.append(Issue(
                module=rel_path, severity="error", kind="bad-covered",
                detail="`covered:` must be a list of function names",
            ))
            continue
        if not isinstance(excluded, dict):
            issues.append(Issue(
                module=rel_path, severity="error", kind="bad-excluded",
                detail="`excluded:` must be a mapping of function-name → reason",
            ))
            continue

        # Source AST scan
        try:
            source_funcs = set(_module_top_level_functions(source_path))
        except ValueError as e:
            issues.append(Issue(
                module=rel_path, severity="error", kind="parse-error",
                detail=str(e),
            ))
            continue

        covered_set = set(covered)
        excluded_set = set(excluded.keys())
        manifest_set = covered_set | excluded_set

        # 1. Functions in source but not in manifest
        untriaged = source_funcs - manifest_set
        for name in sorted(untriaged):
            issues.append(Issue(
                module=rel_path, severity="error", kind="missing-triage",
                detail=(
                    f"function {name!r} exists in {rel_path} but is not "
                    "listed in property-coverage.yaml. Either add a "
                    "property test (and list under `covered:`) OR list "
                    "under `excluded:` with a one-line reason."
                ),
            ))

        # 2. Manifest entries with no source backing (stale)
        stale = manifest_set - source_funcs
        for name in sorted(stale):
            issues.append(Issue(
                module=rel_path, severity="error", kind="stale-manifest",
                detail=(
                    f"manifest lists {name!r} but it doesn't exist in "
                    f"{rel_path}. Remove the entry — it's stale."
                ),
            ))

        # 3. Covered claims must have a test reference. For
        # `Class.method` entries we accept either the dotted form OR
        # the bare method name in the test source (tests typically call
        # `instance.method(...)`, which surfaces as bare `method` in
        # the regex word scan).
        for name in sorted(covered_set):
            if name not in source_funcs:
                continue  # already flagged as stale above
            bare_name = name.split(".", 1)[1] if "." in name else name
            if name not in referenced_names and bare_name not in referenced_names:
                issues.append(Issue(
                    module=rel_path, severity="error", kind="no-test-ref",
                    detail=(
                        f"function {name!r} is listed under `covered:` "
                        "but no reference found in "
                        f"{_safe_relative(test_path, repo_root)}. "
                        "Add a property test."
                    ),
                ))

        # 4. Excluded entries must have a non-empty reason
        for name, reason in sorted(excluded.items()):
            if not isinstance(reason, str) or not reason.strip():
                issues.append(Issue(
                    module=rel_path, severity="error", kind="no-reason",
                    detail=(
                        f"function {name!r} is listed under `excluded:` "
                        "with empty / non-string reason. Add one-line "
                        "justification (I/O-bound / orchestrator / "
                        "argparse helper / etc.)."
                    ),
                ))

    return issues


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate property-pilot coverage manifest vs source + tests.",
    )
    parser.add_argument(
        "--manifest", default=str(DEFAULT_MANIFEST),
        help="Manifest YAML path (default: tests/shared/property-coverage.yaml)",
    )
    parser.add_argument(
        "--test-file", default=str(DEFAULT_TEST_FILE),
        help="Property-test file path (default: tests/shared/test_property_tools.py)",
    )
    parser.add_argument(
        "--ci", action="store_true",
        help="CI mode (terse output; default behavior is to exit 1 on findings).",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Emit JSON instead of text.",
    )
    args = parser.parse_args(argv)

    manifest_path = Path(args.manifest)
    if not manifest_path.is_file():
        print(f"ERROR: manifest not found: {manifest_path}", file=sys.stderr)
        return EXIT_CALLER_ERROR

    try:
        import yaml
    except ImportError:
        print("ERROR: pyyaml not installed", file=sys.stderr)
        return EXIT_CALLER_ERROR

    try:
        with open(manifest_path, encoding="utf-8") as f:
            manifest = yaml.safe_load(f) or {}
    except yaml.YAMLError as e:
        print(f"ERROR: cannot parse {manifest_path}: {e}", file=sys.stderr)
        return EXIT_CALLER_ERROR

    test_path = Path(args.test_file)
    issues = validate_manifest(manifest, REPO_ROOT, test_path)

    if args.json:
        import dataclasses
        print(json.dumps({
            "check": "property-coverage",
            "manifest": _safe_relative(manifest_path, REPO_ROOT),
            "modules_scanned": len(manifest.get("modules") or {}),
            "issues": [dataclasses.asdict(i) for i in issues],
            "summary": {"errors": len(issues)},
        }, indent=2, ensure_ascii=False))
    else:
        if not issues:
            count = len(manifest.get("modules") or {})
            if not args.ci:
                print(
                    f"property-coverage.yaml: {count} modules in scope, "
                    "all functions triaged, all covered claims backed by tests."
                )
            return EXIT_OK

        print(
            f"property-coverage drift detected: {len(issues)} finding(s)",
            file=sys.stderr,
        )
        for issue in issues:
            print(issue.format(), file=sys.stderr)
        print(
            "\nTo fix: edit tests/shared/property-coverage.yaml to triage "
            "each function (covered: + property test, OR excluded: with reason).",
            file=sys.stderr,
        )

    return EXIT_VIOLATION if issues else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
