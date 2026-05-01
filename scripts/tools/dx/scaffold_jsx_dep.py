#!/usr/bin/env python3
"""scaffold_jsx_dep.py — generate a tenant-manager-style JSX dep file
with proper front-matter + window.__X self-registration boilerplate,
and auto-update the orchestrator's `dependencies: [...]` list +
`const X = window.__X;` import block.

Why this tool exists
====================

PR-2d (#153) decomposed `tenant-manager.jsx` into a multi-file layout
under `docs/interactive/tools/tenant-manager/{fixtures,utils,hooks,
components,views}/`. The pattern uses jsx-loader's front-matter
`dependencies: [...]` + `window.__X = X;` self-registration because
indirect eval `(0, eval)(code)` doesn't leak `const`/`let` declarations
to global scope (S#69).

Manually creating a new dep file requires four touch-points that are
EASY to miss:

  1. Front-matter scaffold (title + purpose + register-on-window block)
  2. The actual symbol definition (function / const / etc.)
  3. The `window.__<Name> = <Name>;` self-registration at file tail
  4. The orchestrator's `dependencies: [...]` array entry (multi-line,
     YAML-list-inside-front-matter — easy to mis-format)
  5. The orchestrator's `const <Name> = window.__<Name>;` import block

Forgetting #4 means the dep never loads. Forgetting #5 means the
orchestrator gets ReferenceError at runtime. Forgetting #3 means the
orchestrator's window pickup returns undefined. None of these are
caught by lint — only by the e2e Smoke Tests (slow / coarse).

This tool eliminates the footgun by doing all five steps mechanically.

Usage
=====

    # Scaffold a hook + auto-update tenant-manager.jsx
    python3 scripts/tools/dx/scaffold_jsx_dep.py \\
        --kind hook --name useFooBar --parent tenant-manager

    # Scaffold a component
    python3 scripts/tools/dx/scaffold_jsx_dep.py \\
        --kind component --name FooBar --parent tenant-manager

    # Scaffold a fixture with multiple symbols
    python3 scripts/tools/dx/scaffold_jsx_dep.py \\
        --kind fixture --name demo-bars --parent tenant-manager \\
        --symbols DEMO_BARS,DEMO_BAR_GROUPS

    # Dry-run (preview only, no writes)
    python3 scripts/tools/dx/scaffold_jsx_dep.py \\
        --kind component --name Foo --parent tenant-manager --dry-run

Kinds
=====

  fixture     → <parent>/fixtures/<name>.js   (data: const objects)
  util        → <parent>/utils/<name>.js      (pure helper functions)
  hook        → <parent>/hooks/<name>.js      (custom React hook)
  component   → <parent>/components/<name>.jsx (React function component)
  view        → <parent>/views/<name>.jsx     (early-return view, e.g. LoadingView)

Idempotency
===========

  - Refuses to overwrite an existing dep file (use --force to override).
  - Skips orchestrator updates if the dep path is already in the
    `dependencies: [...]` list.
  - Skips orchestrator updates if `const <Name> = window.__<Name>;`
    is already in the import block.

Refs
====

  - S#69 (PR #156): indirect-eval const-leak archaeology
  - S#70 (PR #158): pre-merge self-review + defensive imports
  - Issue #153: PR-2d Phase 1 + Phase 2 file decomposition
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent

# Map kind → (subdir, file extension, template function name).
# Order is significant for help text presentation.
KINDS = {
    "fixture": ("fixtures", ".js"),
    "util": ("utils", ".js"),
    "hook": ("hooks", ".js"),
    "component": ("components", ".jsx"),
    "view": ("views", ".jsx"),
}


@dataclass
class ScaffoldPaths:
    """Resolved paths for a scaffold operation."""
    parent_orchestrator: Path  # docs/interactive/tools/<parent>.jsx
    parent_dir: Path           # docs/interactive/tools/<parent>/
    dep_file: Path             # docs/interactive/tools/<parent>/<subdir>/<name>.<ext>
    dep_relpath: str           # <parent>/<subdir>/<name>.<ext> — the string used in deps[]
    primary_symbol: str        # the main exported symbol name (matches `--name` for non-fixtures)


def derive_paths(kind: str, name: str, parent: str) -> ScaffoldPaths:
    """Derive all paths from CLI args.

    `name` is used as both the filename (kebab-case allowed for fixture/util)
    and the primary exported symbol name. For fixture / util kinds the
    filename can be kebab-case but the symbol is derived by uppercasing
    (fixture → SCREAMING_SNAKE) or camelCasing (util → camelCase).

    For hook / component / view, `name` MUST be a valid JS identifier
    that's also a valid React hook/component name (camelCase starting
    with `use` for hooks; PascalCase for components/views).
    """
    if kind not in KINDS:
        raise ValueError(f"Unknown kind: {kind!r}. Choices: {list(KINDS.keys())}")
    subdir, ext = KINDS[kind]

    # Validate naming convention per kind.
    if kind == "hook" and not name.startswith("use"):
        raise ValueError(
            f"Hook name must start with 'use' (React Rules of Hooks). Got: {name!r}"
        )
    if kind in ("component", "view") and not (name and name[0].isupper()):
        raise ValueError(
            f"{kind.capitalize()} name must be PascalCase (React convention). Got: {name!r}"
        )

    tools_dir = PROJECT_ROOT / "docs" / "interactive" / "tools"
    parent_orch = tools_dir / f"{parent}.jsx"
    parent_dir = tools_dir / parent
    dep_file = parent_dir / subdir / f"{name}{ext}"
    dep_relpath = f"{parent}/{subdir}/{name}{ext}"

    return ScaffoldPaths(
        parent_orchestrator=parent_orch,
        parent_dir=parent_dir,
        dep_file=dep_file,
        dep_relpath=dep_relpath,
        primary_symbol=name,
    )


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

_TEMPLATE_HEADER = '''---
title: "{parent_title} — {name}"
purpose: |
  [TODO: describe what this {kind} provides / its responsibility]

  Generated by `scripts/tools/dx/scaffold_jsx_dep.py` (PR #TBD).
  See S#69 (#156) / S#70 (#158) for the indirect-eval `const`-leak
  rationale that motivates the `window.__X = X;` self-registration
  pattern this file uses.
---

'''


def _template_fixture(name: str, parent_title: str, symbols: List[str]) -> str:
    """Multi-symbol fixture template (e.g. DEMO_TENANTS + DEMO_GROUPS)."""
    body_decls = "\n\n".join(
        f"const {sym} = {{\n  // TODO: define {sym}\n}};"
        for sym in symbols
    )
    body_window = "\n".join(f"window.__{sym} = {sym};" for sym in symbols)
    return (
        _TEMPLATE_HEADER.format(parent_title=parent_title, name=name, kind="fixture")
        + body_decls
        + "\n\n// Register on window for orchestrator pickup.\n"
        + body_window
        + "\n"
    )


def _template_util(name: str, parent_title: str, symbols: List[str]) -> str:
    """Pure-function utility template."""
    body_decls = "\n\n".join(
        f"function {sym}(/* TODO: params */) {{\n  // TODO: implement\n}}"
        for sym in symbols
    )
    body_window = "\n".join(f"window.__{sym} = {sym};" for sym in symbols)
    return (
        _TEMPLATE_HEADER.format(parent_title=parent_title, name=name, kind="util")
        + body_decls
        + "\n\n// Register on window for orchestrator pickup.\n"
        + body_window
        + "\n"
    )


def _template_hook(name: str, parent_title: str) -> str:
    """Custom React hook template."""
    return (
        _TEMPLATE_HEADER.format(parent_title=parent_title, name=name, kind="hook")
        + "const { useState, useEffect } = React;\n"
        "\n"
        "// Defensive explicit imports (per S#70 self-review): make any\n"
        "// orchestrator-shared globals deterministic at lookup time vs\n"
        "// relying on Babel-standalone's implicit scope leak. Uncomment\n"
        "// + extend as needed.\n"
        "// const styles = window.__styles;\n"
        "\n"
        f"function {name}() {{\n"
        "  // TODO: declare state, effects, and return values.\n"
        "  // Add params if the hook needs callbacks/values from the\n"
        "  // orchestrator (e.g. `function " + name + "({ setApiNotification, t })`).\n"
        "}\n"
        "\n"
        "// Register on window for orchestrator pickup.\n"
        f"window.__{name} = {name};\n"
    )


def _template_component(name: str, parent_title: str, *, kind: str = "component") -> str:
    """Function-component template (.jsx)."""
    return (
        _TEMPLATE_HEADER.format(parent_title=parent_title, name=name, kind=kind)
        + "// const { useState } = React;  // uncomment if needed\n"
        "\n"
        "// Defensive explicit imports (per S#70 self-review): make any\n"
        "// orchestrator-shared globals deterministic at lookup time.\n"
        "// const styles = window.__styles;\n"
        "\n"
        f"function {name}(props) {{\n"
        "  // TODO: destructure props + render. Return null for empty states.\n"
        "  return null;\n"
        "}\n"
        "\n"
        "// Register on window for orchestrator pickup.\n"
        f"window.__{name} = {name};\n"
    )


def _template_view(name: str, parent_title: str) -> str:
    """Early-return view template (e.g. LoadingView, ErrorView)."""
    return _template_component(name, parent_title, kind="view")


def render_template(
    kind: str,
    name: str,
    parent: str,
    symbols: Optional[List[str]] = None,
) -> str:
    """Render the file contents for a new dep file.

    `symbols` only meaningful for fixture / util kinds; defaults to
    [name] (single symbol matching the filename) when not provided.
    """
    parent_title = parent.replace("-", " ").title()
    syms = symbols or [name]

    if kind == "fixture":
        return _template_fixture(name, parent_title, syms)
    if kind == "util":
        return _template_util(name, parent_title, syms)
    if kind == "hook":
        return _template_hook(name, parent_title)
    if kind == "component":
        return _template_component(name, parent_title)
    if kind == "view":
        return _template_view(name, parent_title)
    raise ValueError(f"Unknown kind: {kind!r}")


# ---------------------------------------------------------------------------
# Orchestrator updates — find + insert into existing front-matter and
# `const X = window.__X;` import block. Idempotent.
# ---------------------------------------------------------------------------

# Match the dependencies array in front-matter. Captures everything between
# `[` and `]`. The regex is multi-line aware via `[\s\S]*?` (lazy).
_RE_DEPS_ARRAY = re.compile(
    r"(dependencies:\s*\[)([\s\S]*?)(\])",
    re.MULTILINE,
)

# Match the `const X = window.__X;` import block. We anchor on the comment
# marker that PR-2d added so we can append in the right place.
_RE_IMPORT_BLOCK_END = re.compile(
    r"(const\s+\w+\s*=\s*window\.__\w+;\s*\n)+",
)


def update_orchestrator_deps(
    orchestrator_text: str,
    dep_relpath: str,
) -> tuple[str, bool]:
    """Append `dep_relpath` to the dependencies array. Returns
    (new_text, changed). `changed` is False if the path was already
    present (idempotent).
    """
    m = _RE_DEPS_ARRAY.search(orchestrator_text)
    if not m:
        raise RuntimeError(
            "Orchestrator has no `dependencies: [...]` block in front-matter. "
            "Add one manually first (or extend this script to scaffold it)."
        )
    before, body, close = m.group(1), m.group(2), m.group(3)

    # Idempotency: skip if path already listed.
    if dep_relpath in body:
        return orchestrator_text, False

    # Determine indentation from existing entries (default to 2 spaces).
    indent = "  "
    existing_lines = [ln for ln in body.split("\n") if ln.strip()]
    if existing_lines:
        # Use the indentation of the first non-empty line.
        first = existing_lines[0]
        indent = first[: len(first) - len(first.lstrip())]

    # Append a new entry with a leading comma after the previous entry.
    # The body usually looks like:
    #   \n  "a.js",\n  "b.js"\n
    # We need to:
    #   - Add a comma to the last existing entry if it doesn't have one
    #   - Insert the new entry on its own line before the closing `]`
    body = body.rstrip()
    # Strip any trailing newlines/whitespace before the closing bracket.
    if body and not body.endswith(","):
        # Need a comma after the previous last item.
        body += ","
    body += f"\n{indent}\"{dep_relpath}\"\n"

    new_text = orchestrator_text[: m.start()] + before + body + close + orchestrator_text[m.end():]
    return new_text, True


def update_orchestrator_imports(
    orchestrator_text: str,
    symbol: str,
) -> tuple[str, bool]:
    """Append `const <symbol> = window.__<symbol>;` to the import block.
    Returns (new_text, changed). Idempotent on re-run.
    """
    new_line = f"const {symbol} = window.__{symbol};"
    if new_line in orchestrator_text:
        return orchestrator_text, False

    # Find the LAST occurrence of `const X = window.__X;` to anchor the
    # insertion. We append after the last existing line of the block.
    matches = list(_RE_IMPORT_BLOCK_END.finditer(orchestrator_text))
    if not matches:
        raise RuntimeError(
            "Orchestrator has no `const X = window.__X;` import block. "
            "Add one manually first (or extend this script to scaffold it)."
        )
    last = matches[-1]
    insert_at = last.end()
    new_text = orchestrator_text[:insert_at] + new_line + "\n" + orchestrator_text[insert_at:]
    return new_text, True


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Scaffold a tenant-manager-style JSX dep file.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  scaffold_jsx_dep.py --kind hook --name useFoo --parent tenant-manager\n"
            "  scaffold_jsx_dep.py --kind component --name Foo --parent tenant-manager\n"
            "  scaffold_jsx_dep.py --kind fixture --name demo-bars --parent tenant-manager \\\n"
            "      --symbols DEMO_BARS,DEMO_BAR_GROUPS\n"
        ),
    )
    parser.add_argument("--kind", required=True, choices=list(KINDS.keys()))
    parser.add_argument("--name", required=True, help="Symbol name (matches filename)")
    parser.add_argument(
        "--parent",
        required=True,
        help="Orchestrator name without .jsx (e.g. 'tenant-manager')",
    )
    parser.add_argument(
        "--symbols",
        help="(fixture/util only) Comma-separated symbol list. Defaults to [--name].",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing dep file (default: refuse)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print actions without writing files",
    )
    args = parser.parse_args(argv)

    # Resolve paths + validate naming.
    try:
        paths = derive_paths(args.kind, args.name, args.parent)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    # Validate orchestrator exists.
    if not paths.parent_orchestrator.exists():
        print(
            f"error: orchestrator not found: {paths.parent_orchestrator}\n"
            f"hint: parent should be the .jsx filename (without .jsx) of an "
            f"existing tool with a `dependencies: [...]` front-matter block.",
            file=sys.stderr,
        )
        return 2

    # Validate dep file doesn't already exist.
    if paths.dep_file.exists() and not args.force:
        print(
            f"error: dep file already exists: {paths.dep_file}\n"
            f"hint: pass --force to overwrite.",
            file=sys.stderr,
        )
        return 2

    # Resolve symbols list.
    symbols: Optional[List[str]] = None
    if args.symbols:
        if args.kind not in ("fixture", "util"):
            print(
                f"error: --symbols only meaningful for fixture/util (got {args.kind!r})",
                file=sys.stderr,
            )
            return 2
        symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]

    # Render the new file.
    new_file_content = render_template(args.kind, args.name, args.parent, symbols)

    # Compute orchestrator updates.
    orch_text = paths.parent_orchestrator.read_text(encoding="utf-8")
    try:
        orch_text_after_deps, deps_changed = update_orchestrator_deps(
            orch_text, paths.dep_relpath
        )
        # The import-block update appends ONE symbol entry per scaffolded
        # file. For multi-symbol fixtures, we add the FIRST symbol; the
        # caller is expected to manually add the rest if they want them
        # all imported into the orchestrator. (For most fixtures only the
        # first is referenced — e.g. DEMO_TENANTS but not DEMO_GROUPS in
        # the orchestrator's case.)
        primary_for_import = symbols[0] if symbols else paths.primary_symbol
        orch_final, imports_changed = update_orchestrator_imports(
            orch_text_after_deps, primary_for_import
        )
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    # Report + write.
    rel_orch = paths.parent_orchestrator.relative_to(PROJECT_ROOT)
    rel_dep = paths.dep_file.relative_to(PROJECT_ROOT)
    if args.dry_run:
        print(f"[dry-run] would create: {rel_dep} ({len(new_file_content)} bytes)")
        print(f"[dry-run] would update: {rel_orch}")
        print(f"  - dependencies[] += {paths.dep_relpath!r} (changed={deps_changed})")
        print(f"  - import block += const {primary_for_import} = window.__{primary_for_import}; (changed={imports_changed})")
        if symbols and len(symbols) > 1:
            print(
                f"  NOTE: --symbols listed {len(symbols)} symbols; only the FIRST "
                f"({symbols[0]}) was added to the orchestrator's import block. "
                f"Add the others manually if needed."
            )
        return 0

    paths.dep_file.parent.mkdir(parents=True, exist_ok=True)
    paths.dep_file.write_text(new_file_content, encoding="utf-8")
    paths.parent_orchestrator.write_text(orch_final, encoding="utf-8")
    print(f"created: {rel_dep}")
    print(f"updated: {rel_orch}")
    print(f"  - dependencies[] += {paths.dep_relpath!r} (changed={deps_changed})")
    print(f"  - import block += const {primary_for_import} = window.__{primary_for_import}; (changed={imports_changed})")
    if symbols and len(symbols) > 1:
        print(
            f"  NOTE: --symbols listed {len(symbols)} symbols; only the FIRST "
            f"({symbols[0]}) was added to the orchestrator's import block. "
            f"Add the others manually if the orchestrator references them by name."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
