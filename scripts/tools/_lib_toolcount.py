"""_lib_toolcount.py — One scan behind the "N 個 Python 工具" sentence.

⛔ Read the scope note below before reusing this. It answers exactly two
questions — which files the counted sentence covers, and which files
docs/internal/tool-map.md inventories — and it is NOT the repo's only
opinion about what a Python tool is (see "Other corpora").

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

⛔ There are TWO scopes here, not one:

  count_scope     `scripts/tools/{ops,dx,lint}` — the scope the counted
                  sentence states verbatim (the repo-map row of README.md
                  and README.en.md). The repo root is NOT in it.
                  Consumers: the checker and the writer.

  tool_map_scope  the same three subdirectories PLUS the repo root, which
                  is what docs/internal/tool-map.md inventories.
                  `check_tool_map_coverage` scans the repo root and
                  requires every file it finds to be listed there.

⛔ Collapsing the two is not a red/green question, which is why it is
worth stating precisely. Measured, with `validate_all.py`'s row deleted
from tool-map.md: `validate_docs_versions --ci` prints
`⚠️ [tool-map-coverage] … tool not listed in tool-map: validate_all.py`
and exits **0** — that check emits `warn`, and only `error` fails the
gate. So a unified scope buys a warning no tool can clear (the exact
disease #1511 is about) plus `validate_all.py` quietly leaving the tool
map. Not a red build.

⛔ Do not "derive" the subdirectory tuple with `rglob`. Measured on
`5cff2359`: 224, because `dx/custom_alerts/` is a package (`__init__.py`,
no `main()` in any of its three modules) — library code, not tools. The
tuple is not an enumeration standing in for a rule; it *is* the documented
scope, and `test_a_new_tool_subdirectory_cannot_be_dropped_in_silence`
goes red when a new non-package subdirectory appears without this tuple
being updated. ⚠️ That tripwire guards the tuple only — the counted
sentence itself is guarded by a `warn`, so a tuple widened without the
sentence being rewritten exits 0.

⚠️ Residuals, disclosed rather than papered over:
  - Classifying a directory as library code by its `__init__.py` is this
    repo's own convention, not a proof. A package directory that really
    does hold tools passes in silence.
  - #1541 settled the boundary: a name starting with `_` is a helper,
    not a tool — `_lib*` at the repo root, and the `_grar_*`,
    `_version_patterns` style helpers inside the subdirectories alike.
    That is the same line `verify_diff_rules.yaml` (「子目錄共用 helper」)
    and `tests/shared/test_tool_exit_codes.py` already drew.

## Other corpora (this module is not their definition)

  - `scripts/tools/lint/check_build_completeness.py` declares its own
    subdirectory tuple for a different question (which directories hold
    shipped sibling modules).
Both answer questions this module does not; do not "unify" them without
deciding what the published sentence is supposed to mean.
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional, Tuple

# A file whose name starts with this is a helper, not a tool (#1541). It
# also covers `__init__.py` and `__pycache__`.
HELPER_PREFIX = "_"

# The subdirectories both scopes cover, in the order the tool map lists them.
COUNT_SUBDIRS: Tuple[str, ...] = ("ops", "dx", "lint")


def is_tool_file(path: Path) -> bool:
    """Whether one path is a Python tool rather than a helper module.

    ⚠️ Suffix compared case-insensitively (#1541): Windows' `glob("*.py")`
    hands back `B.PY`, Linux's does not, so a case-sensitive test made the
    same tree count differently per platform.
    """
    return (path.suffix.lower() == ".py"
            and not path.name.startswith(HELPER_PREFIX))


def _child_dir(root: Path, name: str) -> Optional[Path]:
    """`root/name`, matched case-insensitively; None if absent.

    `Path.is_dir()` follows the filesystem, so `OPS/` satisfied "ops" on
    Windows and not on Linux — two published numbers for one tree (#1541).
    """
    for child in root.iterdir():
        if child.is_dir() and child.name.lower() == name.lower():
            return child
    return None


def _py_files(directory: Path) -> List[Path]:
    """Tool files directly in *directory*, sorted by name. Not `glob("*.py")`:
    that is case-sensitive on Linux only."""
    return sorted((f for f in directory.iterdir()
                   if f.is_file() and is_tool_file(f)), key=lambda f: f.name)


def scan(tools_root: Path, *,
         include_root: bool) -> List[Tuple[Optional[str], Path]]:
    """Every tool file under *tools_root*, tagged with its subdirectory.

    Returns ``[(subdir_or_None, path), ...]``: the `COUNT_SUBDIRS` in
    order, each sorted by name, then — when *include_root* is set — the
    repo-root tools tagged ``None``. Flat globs on purpose; see the module
    docstring for why `rglob` is wrong here.

    Prefer `count_scope` / `tool_map_scope`: each names the declaration it
    serves, so a caller cannot pick the wrong scope by accident.

    ⛔ Duplicates in `COUNT_SUBDIRS` are collapsed. Measured before this:
    a tuple holding `release` twice walked the directory twice and
    reported 5 files where the tree held 4, and `count_by_subdir` said
    `release: 2` — a wrong number that looks entirely normal, which is
    the failure mode this whole module exists to remove. Order is kept.
    """
    found: List[Tuple[Optional[str], Path]] = []
    if not tools_root.is_dir():
        return found
    for subdir in dict.fromkeys(COUNT_SUBDIRS):
        directory = _child_dir(tools_root, subdir)
        if directory is None:
            continue
        found.extend((subdir, f) for f in _py_files(directory))
    if include_root:
        found.extend((None, f) for f in _py_files(tools_root))
    return found


def count_scope(tools_root: Path) -> List[Tuple[Optional[str], Path]]:
    """The tools the counted sentence covers: `{ops,dx,lint}`, no root."""
    return scan(tools_root, include_root=False)


def tool_map_scope(tools_root: Path) -> List[Tuple[Optional[str], Path]]:
    """The tools docs/internal/tool-map.md inventories: the above + root."""
    return scan(tools_root, include_root=True)


def is_helper_file(path: Path) -> bool:
    """The other half of the partition: a `_`-prefixed module, not a dunder.

    `generate_tool_map` lists these in its shared-helper section, so a file
    in scope is always exactly one of tool or helper and never neither.
    """
    return (path.suffix.lower() == ".py"
            and path.name.startswith(HELPER_PREFIX)
            and not path.name.startswith("__"))


def helper_scope(tools_root: Path) -> List[Tuple[Optional[str], Path]]:
    """Helper modules in the same directories `tool_map_scope` walks."""
    found: List[Tuple[Optional[str], Path]] = []
    if not tools_root.is_dir():
        return found
    dirs = [(None, tools_root)] + [
        (sub, d) for sub in dict.fromkeys(COUNT_SUBDIRS)
        for d in [_child_dir(tools_root, sub)] if d is not None]
    for subdir, directory in dirs:
        found.extend((subdir, f) for f in sorted(
            (f for f in directory.iterdir()
             if f.is_file() and is_helper_file(f)), key=lambda f: f.name))
    return found


def count_by_subdir(tools_root: Path) -> Dict[str, int]:
    """`{subdir: n}` over `COUNT_SUBDIRS`; every key is always present.

    A missing directory reports 0 rather than dropping out, so a caller
    unpacking the breakdown cannot silently lose a line.
    """
    # ⚠️ No `dict.fromkeys` here on purpose: a dict comprehension already
    # collapses a repeated name, so wrapping it read like a second guard
    # while being a no-op. Measured — removing it leaves every test green;
    # the one place collapsing actually changes an answer is `scan`.
    counts = {subdir: 0 for subdir in COUNT_SUBDIRS}
    for subdir, _path in count_scope(tools_root):
        counts[str(subdir)] += 1
    return counts
