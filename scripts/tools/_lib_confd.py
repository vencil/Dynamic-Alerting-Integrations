"""Single answer to "what is in a conf.d/ directory" (#1339).

ADR-016 introduced a hierarchical `conf.d/` (L0 global → L1 domain → L2
region → L3 env → tenant) and ADR-017 defined how the levels merge. The
shipping exporter implements it: `pkg/config/hierarchy.go` walks the tree
with `filepath.WalkDir`.

The Python tool suite did not. Measured on the tree that introduced this
module: **11 tools enumerated a tenant config dir flat** (`os.listdir` /
`glob("*.yaml")`) while **6 recursed** — so pointing two tools at the same
hierarchical directory produced opposite answers about whether it had any
tenants at all. The worst case was `validate_config.py`, which reported
`Result: PASS` / exit 0 while scanning **0 tenants**: not a refusal, a
green light for a directory it never read.

This module exists so that question has ONE answer, and so a flat reader
has to say so out loud instead of silently reporting nothing.

Two things a caller can do — pick deliberately, never by accident:

1. `iter_config_files(dir)` — read the whole tree, like the exporter does.
   Correct for anything whose job is "tell me about this configuration"
   (validators, describers, linters).

2. `nested_yaml_warning(dir, tool=...)` — stay flat, but detect the case
   where that is about to give a wrong answer and return a message saying
   which files are being skipped. Correct for tools that are flat BY
   DESIGN (e.g. `assemble_config_dir.py` merges several flat sharded
   sources) and for ones whose recursive semantics are not yet decided.

`tests/shared/test_confd_enumeration_contract.py` enforces that every tool
reading a tenant config dir does one or the other — a new tool cannot
quietly join the silent-zero class.
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = [
    "CONFIG_SUFFIXES",
    "iter_config_files",
    "nested_yaml_files",
    "nested_yaml_warning",
]

CONFIG_SUFFIXES = (".yaml", ".yml")


def _is_hidden(name: str) -> bool:
    """Mirror of the exporter's skip rule — DERIVED, not an allowlist.

    `pkg/config/hierarchy.go` skips any entry whose name starts with `.`
    (`SkipDir` for directories, plain skip for files), and every flat
    Python reader already does `not f.startswith(".")`. Listing specific
    names here instead (`.git`, `__pycache__`, ...) would make this
    module's answer differ from the oracle it exists to mirror — which is
    the very divergence #1339 is about.
    """
    return name.startswith(".")


def _is_config(name: str) -> bool:
    return name.endswith(CONFIG_SUFFIXES) and not _is_hidden(name)


def iter_config_files(config_dir: str | os.PathLike[str], *, recursive: bool = True):
    """Yield config files under `config_dir`, sorted, deepest path last.

    Sorted by POSIX-style relative path so callers get a stable order on
    every platform (a plain `Path.rglob` yields OS-dependent order, and
    `os.sep` differences leak into any output that echoes the path — the
    trap that turned the Go/Python golden parity red in #1341).

    `recursive=False` reproduces the historical flat behaviour; it exists
    so a caller can be explicit rather than accidentally flat.
    """
    root = Path(config_dir)
    if not root.is_dir():
        return
    if not recursive:
        for p in sorted(root.iterdir(), key=lambda q: q.name):
            if p.is_file() and _is_config(p.name):
                yield p
        return
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not _is_hidden(d))
        for fn in filenames:
            if _is_config(fn):
                found.append(Path(dirpath) / fn)
    for p in sorted(found, key=lambda q: q.relative_to(root).as_posix()):
        yield p


def nested_yaml_files(config_dir: str | os.PathLike[str]) -> list[Path]:
    """Config files a FLAT reader would miss (i.e. below the top level)."""
    root = Path(config_dir)
    if not root.is_dir():
        return []
    return [
        p for p in iter_config_files(root)
        if len(p.relative_to(root).parts) > 1
    ]


def nested_yaml_warning(
    config_dir: str | os.PathLike[str],
    *,
    tool: str,
    limit: int = 5,
) -> str | None:
    """Return a message when a flat read of `config_dir` would mislead.

    `None` when the directory is flat (nothing to say). Callers MUST
    surface the message — printing it to stderr is enough; the point is
    that "0 tenants" can never again look like "no problems".
    """
    missed = nested_yaml_files(config_dir)
    if not missed:
        return None
    root = Path(config_dir)
    shown = [p.relative_to(root).as_posix() for p in missed[:limit]]
    more = f" (+{len(missed) - limit} more)" if len(missed) > limit else ""
    return (
        f"{tool}: reads {config_dir} FLAT, but {len(missed)} config file(s) "
        f"live in subdirectories and are being SKIPPED: "
        f"{', '.join(shown)}{more}. "
        f"threshold-exporter reads this tree recursively (ADR-016/017), so "
        f"this tool's answer does not describe what the exporter is doing. "
        f"See issue #1339."
    )
