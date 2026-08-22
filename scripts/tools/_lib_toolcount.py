"""_lib_toolcount.py — Single definition of what counts as a Python tool.

⛔ #1511: this predicate existed three times, in three modules that never
shared a line of code — `validate_docs_versions._count_python_tools`
(checks the number), `bump_docs._count_python_tools` (writes it into
README.md / README.en.md) and `generate_tool_map.gather_tools` (writes
docs/internal/tool-map.md). `bump_docs` skipped no prefixes at all, so the
three agreed only because `ops/`, `dx/` and `lint/` happen to hold no
`_lib*` or `__init__.py` today. Measured on `5cff2359`, dropping one
`scripts/tools/lint/_lib_probe.py` into the tree: checker 220, writer 221,
and from there `bump_docs --sync-counts` rewrote README to 221,
`validate_docs_versions --ci --fix` rewrote it back to 220, and neither
side could clear the warning.

⛔ There are TWO scopes here, not one, and collapsing them would break a
live gate:

  count_scope     `scripts/tools/{ops,dx,lint}` — the scope the counted
                  sentence states verbatim (the repo-map row of README.md
                  and README.en.md). The repo root is NOT in it.
                  Consumers: the checker and the writer.

  tool_map_scope  the same three subdirectories PLUS the repo root, which
                  is what docs/internal/tool-map.md inventories.
                  `check_tool_map_coverage` scans the repo root and
                  requires every file it finds to be listed there;
                  measured, with `validate_all.py`'s row deleted from
                  tool-map.md: `tool not listed in tool-map: validate_all.py`.
                  So the generator has to keep the root, and it has never
                  produced the same number as the other two.

⛔ Do not "derive" the subdirectory tuple with `rglob`. Measured on
`5cff2359`: 224, because `dx/custom_alerts/` is a package (`__init__.py`,
no `main()` in any of its three modules) — library code, not tools. The
tuple is not an enumeration standing in for a rule; it *is* the documented
scope, and `test_a_new_tool_subdirectory_cannot_be_dropped_in_silence`
goes red when a new non-package subdirectory appears without this tuple
and that sentence being updated together.

⚠️ Residual, disclosed rather than papered over: classifying a directory
as library code by its `__init__.py` is this repo's own convention, not a
proof, and a package directory that really does hold tools still passes in
silence. Likewise `_lib` is the naming convention for shared modules —
`_grar_*.py` and `_registry_lib.py` are helper modules that do NOT carry
it, and they are counted as tools today.
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Filename prefixes that mark a file as something other than a tool.
# ⚠️ `__pycache__` is inert against the flat globs below — a `*.py` glob
# never returns a directory's children — and is kept only so the predicate
# stays correct if a caller ever hands it a recursive listing. It is not a
# second check on today's inputs; do not read it as one.
TOOL_SKIP_PREFIXES: Tuple[str, ...] = ("_lib", "__init__", "__pycache__")

# The subdirectories both scopes cover, in the order the tool map lists them.
COUNT_SUBDIRS: Tuple[str, ...] = ("ops", "dx", "lint")


def is_tool_file(path: Path) -> bool:
    """Whether one path is a Python tool rather than library/bytecode."""
    return path.suffix == ".py" and not any(
        path.name.startswith(prefix) for prefix in TOOL_SKIP_PREFIXES)


def scan(tools_root: Path, *,
         include_root: bool) -> List[Tuple[Optional[str], Path]]:
    """Every tool file under *tools_root*, tagged with its subdirectory.

    Returns ``[(subdir_or_None, path), ...]``: the `COUNT_SUBDIRS` in
    order, each sorted by name, then — when *include_root* is set — the
    repo-root tools tagged ``None``. Flat globs on purpose; see the module
    docstring for why `rglob` is wrong here.

    Prefer `count_scope` / `tool_map_scope`: each names the declaration it
    serves, so a caller cannot pick the wrong scope by accident.
    """
    found: List[Tuple[Optional[str], Path]] = []
    for subdir in COUNT_SUBDIRS:
        directory = tools_root / subdir
        if not directory.is_dir():
            continue
        found.extend((subdir, f) for f in sorted(directory.glob("*.py"))
                     if is_tool_file(f))
    if include_root:
        found.extend((None, f) for f in sorted(tools_root.glob("*.py"))
                     if is_tool_file(f))
    return found


def count_scope(tools_root: Path) -> List[Tuple[Optional[str], Path]]:
    """The tools the counted sentence covers: `{ops,dx,lint}`, no root."""
    return scan(tools_root, include_root=False)


def tool_map_scope(tools_root: Path) -> List[Tuple[Optional[str], Path]]:
    """The tools docs/internal/tool-map.md inventories: the above + root."""
    return scan(tools_root, include_root=True)


def count_by_subdir(tools_root: Path) -> Dict[str, int]:
    """`{subdir: n}` over `COUNT_SUBDIRS`; every key is always present.

    A missing directory reports 0 rather than dropping out, so a caller
    unpacking the breakdown cannot silently lose a line.
    """
    counts = {subdir: 0 for subdir in COUNT_SUBDIRS}
    for subdir, _path in count_scope(tools_root):
        counts[str(subdir)] += 1
    return counts
