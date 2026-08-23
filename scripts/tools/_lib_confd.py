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

2. `warn_nested(dir)` — stay flat, but say so out loud: it prints the
   files being skipped to stderr, once per directory per process. Correct
   for tools that are flat BY DESIGN (e.g. `assemble_config_dir.py` merges
   several flat sharded sources) and for ones whose recursive semantics
   are not yet decided. (`nested_yaml_warning` is the pure form underneath
   it, for callers that want the string rather than the side effect.)

`tests/shared/test_confd_enumeration_contract.py` enforces that every tool
reading a tenant config dir does one or the other — a new tool cannot
quietly join the silent-zero class.

Alongside those, `unusable_config_paths(dir)` (#1469) answers the question
`iter_config_files` deliberately does not: what did it DROP that the
operator will still call configuration — a directory named `beta.yaml`, a
broken symlink, a file it cannot read. Every reader should name those; the
selection being shared is what stops two readers reaching two verdicts,
and this is what stops "shared" from meaning "equally silent".
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

__all__ = [
    "CONFIG_SUFFIXES",
    "iter_config_files",
    "nested_yaml_files",
    "nested_yaml_warning",
    "reset_warned_for_test",
    "unusable_config_paths",
    "unusable_reason",
    "warn_nested",
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
    """Yield config files under `config_dir`, sorted by POSIX relative path.

    That ordering is the whole promise — it is NOT depth-first, and
    shallow files do not come first: `a/tenant.yaml` sorts before a
    root-level `z.yaml`. (An earlier docstring here claimed "deepest path
    last", which lexicographic order never guaranteed; the current fixture
    set happened not to expose the difference.)

    Sorting on the POSIX-style relative path gives a stable order on every
    platform — a plain `Path.rglob` yields OS-dependent order, and `os.sep`
    differences leak into any output that echoes the path, the trap that
    turned the Go/Python golden parity red in #1341.

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


def _is_readable_file(p: Path) -> bool:
    """Can this path be opened and read as a file, right now?"""
    try:
        return p.is_file() and os.access(p, os.R_OK)
    except OSError:
        return False


def unusable_reason(p: Path) -> str:
    """Why `p` is not usable as a config file — one short clause.

    Split out so the two readers phrase the same finding the same way; a
    reader that invented its own wording would put a second answer to
    "what happened to beta.yaml" in front of the operator.
    """
    try:
        if p.is_dir():
            return "is a directory, not a config file"
        if p.is_symlink() and not p.exists():
            return "is a broken symlink"
        if p.exists() and not os.access(p, os.R_OK):
            return "is not readable (permission denied)"
    except OSError as e:  # noqa: BLE001 — surfacing the errno IS the answer
        return f"could not be stat'ed — {e.__class__.__name__}: {e.strerror}"
    return "is not a readable file"


def unusable_config_paths(
    config_dir: str | os.PathLike[str], *, recursive: bool = True,
) -> list[Path]:
    """Paths NAMED like a config file that are not a readable config file.

    Sibling of `iter_config_files`, and the reason it can stay simple.
    `iter_config_files` answers "what should I read"; anything it drops
    disappears without a trace, which is fine for `notes.txt` and wrong
    for a *directory* called `beta.yaml` (an interrupted `mkdir`, a bad
    merge, a ConfigMap projected as a dir), a broken symlink, or a file
    with no read permission. Those look like configuration to the operator
    who put them there, so silence reads as "your config is fine".

    Measured before this existed (#1469): `_grar_parse._parse_config_files`
    listed the directory, tried to `open()` it and reported
    `WARN: skip beta.yaml`, while `check_yaml_syntax` — walking the same
    tree through `iter_config_files` — never saw it at all. Two readers,
    two answers. Unifying the *selection* alone would have settled that by
    making BOTH silent, i.e. one signal fewer than before; this function is
    the half that keeps the signal, so both readers can name the path.

    `recursive` mirrors `iter_config_files` exactly, so a caller gets the
    unusable set for the same tree it just read — recursive for
    `validate-config`, flat for the routing parser (ADR-016 hierarchy is
    reported separately by `warn_nested`).

    Sorted by POSIX relative path, the same promise `iter_config_files`
    makes, so a report can interleave the two lists deterministically.

    ⛔ DISJOINT from `iter_config_files` by construction: this returns only
    paths that iteration will NOT hand the caller. The two are consumed
    together, so any overlap is reported twice.

    That is not hypothetical. An unreadable REGULAR `.yaml` file satisfies
    "config-named and not readable", but it is still a file, so
    `iter_config_files` yields it too — and the caller then reports it once
    from this list and again when its own `open()` raises `PermissionError`.
    Measured before this guard: `iter_config_files` and
    `unusable_config_paths` both returned `locked.yaml`.

    The complement is also the honest division of labour. A path this
    function is *for* — a config-named directory, a broken symlink — can
    never be opened, so nothing else will ever speak for it. A file that
    exists but cannot be read WILL be spoken for, by the reader's own
    `open()` failure, and with the real errno attached, which is strictly
    more informative than this function's one-clause summary.

    ⚠️ Deliberately NOT fixed by filtering `iter_config_files` instead. That
    would put an `os.access` syscall on every file of every scan for a
    check that cannot be trusted anyway — the file can turn unreadable
    between the check and the open — so readers would still need the
    `open()` fallback. Narrowing this function removes the duplicate
    without paying for a guarantee the filesystem will not give.

    ⚠️ The permission case is only observable when the process is NOT
    root: `os.access` reports success for mode-000 files under uid 0.
    Directories and broken symlinks are detected regardless.
    """
    root = Path(config_dir)
    if not root.is_dir():
        return []

    def _unusable(p: Path) -> bool:
        # `is_file()` is the exact predicate `iter_config_files` uses to
        # decide it will hand this path over; if it will, this list must
        # not also carry it. Wrapped because a broken symlink or a
        # permission-denied parent can make the stat itself raise, and a
        # path we cannot even stat is precisely one to report.
        try:
            if p.is_file():
                return False
        except OSError:
            return True
        return not _is_readable_file(p)

    found: list[Path] = []
    if not recursive:
        for p in sorted(root.iterdir(), key=lambda q: q.name):
            if _is_config(p.name) and _unusable(p):
                found.append(p)
        return found
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not _is_hidden(d))
        # Directories first: a config-named DIRECTORY is the case that made
        # this function necessary, and `os.walk` never puts it in filenames.
        for name in list(dirnames) + list(filenames):
            p = Path(dirpath) / name
            if _is_config(name) and _unusable(p):
                found.append(p)
    found.sort(key=lambda q: q.relative_to(root).as_posix())
    return found


def nested_yaml_files(config_dir: str | os.PathLike[str]) -> list[Path]:
    """Config files a FLAT reader would miss (i.e. below the top level)."""
    root = Path(config_dir)
    if not root.is_dir():
        return []
    return [
        p for p in iter_config_files(root)
        if len(p.relative_to(root).parts) > 1
    ]


def _running_tool() -> str:
    """Best guess at the command the operator actually typed.

    Derived from `sys.argv[0]`, not hard-coded: a SHARED helper such as
    `_lib_io.iter_yaml_files` is reached from several entry points, and
    labelling its warning with the helper's own name tells the operator
    nothing about which command produced it. Falls back to a neutral
    label when argv is unavailable (embedded / REPL use).
    """
    argv0 = (sys.argv[0] if sys.argv else "") or ""
    name = os.path.basename(argv0)
    return name[:-3] if name.endswith(".py") else (name or "conf.d reader")


def nested_yaml_warning(
    config_dir: str | os.PathLike[str],
    *,
    tool: str | None = None,
    limit: int = 5,
) -> str | None:
    """Return a message when a flat read of `config_dir` would mislead.

    `None` when the directory is flat (nothing to say). Callers MUST
    surface the message — printing it to stderr is enough; the point is
    that "0 tenants" can never again look like "no problems".

    Omit `tool` from a shared helper so the message names the entry point
    the operator ran rather than the helper; pass it explicitly from a
    single-purpose tool where that name is the more useful one.
    """
    if tool is None:
        tool = _running_tool()
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


# Directories already warned about in THIS process. Module-level state, so it
# carries the repo's usual obligation (CLAUDE.md): anything that writes it must
# be resettable idempotently rather than save-and-restore.
_WARNED: set[str] = set()


def reset_warned_for_test() -> None:
    """Idempotent reset of the once-per-directory memo.

    Idempotent CLEAR, not save-then-restore: a restoring fixture is
    "last cleanup wins" and would undo a parallel test's writes.
    """
    _WARNED.clear()


def warn_nested(config_dir: str | os.PathLike[str], *, tool: str | None = None) -> bool:
    """Print the nested-config warning to stderr, at most once per directory.

    Returns whether anything was printed.

    Why the memo: one command often scans the same `conf.d/` from more than
    one place — `validate_config.py` reaches the routing parser twice, and
    `migrate_to_operator.analyze_migration` both scans directly and calls
    `discover_tenant_configs`. Printing the identical WARN twice reads as
    two separate problems (CodeRabbit, PR #1343). De-duplicating here rather
    than deleting one of the guards keeps every flat scan covered — a scan
    whose only warning lives in some other function is exactly the
    "gate exists but never fires" shape this module was written against.

    Callers should use this instead of hand-rolling the print, so the
    dedup and the stderr choice live in one place.
    """
    msg = nested_yaml_warning(config_dir, tool=tool)
    if msg is None:
        return False
    key = os.path.abspath(os.fspath(config_dir))
    if key in _WARNED:
        return False
    _WARNED.add(key)
    print(f"WARN: {msg}", file=sys.stderr)
    return True
