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
    "config_stem",
    "has_yaml_extension",
    "is_defaults_name",
    "is_hidden_name",
    "is_reserved_name",
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
    """Mirror of the exporter's extension rule — DERIVED, not an allowlist.

    CASE-INSENSITIVE, and that is the whole content of this docstring's
    existence. The oracle is the exporter's scanner: `config_hierarchy.go`
    (production hot-reload), `flat_scanner.go`, `pkg/config/hierarchy.go`
    and `pkg/config/scope.go` all lowercase the entry name before testing
    the `.yaml` / `.yml` suffix, so the exporter READS `upper.YAML` and
    merges it into the effective config it serves.

    Testing the suffix exactly — as this did until #1537 — made such a file
    invisible to every Python reader: measured, `iter_config_files` did not
    yield `upper.YAML` AND `unusable_config_paths` did not name it either,
    so nothing in the report so much as mentioned a file the exporter was
    acting on. That is #1339's shape with the *extension* as the divergence
    axis instead of directory depth, and it is the one parity claim in this
    module that was, until now, written down nowhere.

    Note this predicate answers TWO of the three name properties: extension
    (here) and hidden (`_is_hidden`). It deliberately does NOT filter the
    `_` prefix — reserved control files ARE part of "what is in a conf.d
    directory"; callers that want tenant carriers only (`_lib_io`,
    tenant-api) apply that third property themselves.

    ⛔ Nothing here greps the Go source. This repo measured (#1448 blind
    review) that a Python guard asserting things about Go source text goes
    red on legitimate Go refactors and states the opposite of the truth
    when it does. What keeps this honest instead is the shared name
    classification matrix under `tests/shared/` — the exporter's scanner,
    tenant-api's filename→id mapping, `_lib_io.iter_yaml_files` and this
    predicate each assert THAT table, so their agreement is transitive
    rather than claimed, and a rule change turns the owning side red first.
    """
    return name.lower().endswith(CONFIG_SUFFIXES) and not _is_hidden(name)


# ── public name predicates ────────────────────────────────────────────
#
# ⛔ These are PREDICATES, not enumerators, and that distinction is the
# whole reason they exist (#1588).
#
# Nine tools were measured reading a conf.d and disagreeing with the
# exporter about `upper.YAML`, each having hand-written its own
# `endswith(".yaml")`. The obvious remedy — "call `iter_config_files`
# instead" — is NOT a case fix: those tools are flat or `rglob`, so
# switching enumerators silently changes their RECURSION behaviour too,
# and every one of them then needs its own blast-radius argument. That is
# a different change, and bundling it here would hide a behaviour change
# inside a bug fix.
#
# So the extension axis gets shared while the recursion axis stays where
# each caller put it. A tool keeps its own `for f in os.listdir(...)` and
# asks these functions the name questions.
#
# The four booleans are ORTHOGONAL and deliberately mirror the columns of
# `tests/shared/confd_name_classification_matrix.json`
# (`yaml_extension` / `reserved_prefix` / `hidden` / `defaults_file`, plus
# `stem`). Because the exporter's production scanner, tenant-api's
# filename→id mapping and `_lib_io.iter_yaml_files` all assert that same
# table, a caller composing these predicates agrees with the exporter
# TRANSITIVELY — nothing here claims agreement, and no guard greps anyone
# else's source (#1448).


def has_yaml_extension(
    name: str, suffixes: tuple[str, ...] = CONFIG_SUFFIXES
) -> bool:
    """Does this basename carry a YAML extension? CASE-INSENSITIVE.

    The extension axis ALONE — it says nothing about hidden files or the
    `_` prefix. Composing is the caller's job precisely because the four
    live enumerators want four different combinations (see the projection
    table in PR #1590), so a single "is this a config file" answer would
    be wrong for three of them.

    ⚠️ `suffixes` exists so a case fix does not smuggle in a SECOND
    behaviour change. Measured on today's tree, four readers
    (`operator_generate`, `generate_tenant_metadata`,
    `check_path_metadata_consistency`, `custom_alerts/loader`) glob
    `*.yaml` and therefore do not see `db-b.yml` AT ALL, while the
    exporter reads both spellings. That is a real divergence on the
    extension-SPELLING axis and it is filed separately — but widening
    those four here would land it inside a commit whose stated subject is
    case folding, where no reviewer is looking for it. Callers pass the
    set they already accept; the default is both.

    ⛔ Do not "simplify" this by dropping the parameter and folding every
    caller to `CONFIG_SUFFIXES`. That is the behaviour change, spelled as
    a cleanup.
    """
    lowered = name.lower()
    return any(lowered.endswith(s) for s in suffixes)


def is_hidden_name(name: str) -> bool:
    """Dot-prefixed, i.e. skipped by the exporter's walker."""
    return _is_hidden(name)


def is_reserved_name(name: str) -> bool:
    """`_`-prefixed control file — reserved, never a tenant carrier.

    ⚠️ ASCII `_` only, and that is not laziness: the reserved prefix is a
    single ASCII byte with no case, so unlike the extension there is
    nothing here to fold. tenant-api's `isReservedName` makes the same
    call and the shared matrix pins both.
    """
    return name.startswith("_")


def is_defaults_name(name: str) -> bool:
    """The platform-defaults carrier — `_defaults.yaml` in ANY casing.

    ⛔ `_DEFAULTS.YAML` measured as `False` under the hand-written copy in
    `check_confd_schema`, so a broken platform-defaults file skipped the
    schema gate entirely (`rc=0`, "0 tenant conf.d file(s) valid") while
    the exporter merged it into EVERY downstream tenant's effective
    config. Both halves have to fold, which is why this is one function
    rather than two comparisons at each call site.
    """
    lowered = name.lower()
    return lowered.startswith("_defaults") and has_yaml_extension(name)


def config_stem(name: str) -> str:
    """Tenant id carried by a filename, or `""` if it carries none.

    ⛔ The stem keeps the ORIGINAL case: `Upper.YAML` -> `Upper`, never
    `upper`. Returning the folded stem would rename a tenant on the write
    plane only — `GET /tenants`, federation account backfill and both
    orphan scans key on it, while the exporter's tenant id comes from the
    `tenants:` mapping inside the file and never from the name. That is
    the first divergence's own shape, rebuilt worse.

    Reserved and hidden names carry no tenant id and return `""`.
    """
    if is_reserved_name(name) or is_hidden_name(name):
        return ""
    lowered = name.lower()
    for suffix in CONFIG_SUFFIXES:
        if lowered.endswith(suffix):
            # Slice by the CONSTANT's length, never `len(lowered)`:
            # `str.lower()` can shrink byte length (`İ` is 2 -> 1), so an
            # offset taken from the folded copy can cut the original in
            # the wrong place.
            return name[: len(name) - len(suffix)]
    return ""


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
        try:
            entries = sorted(root.iterdir(), key=lambda q: q.name)
        except OSError:
            # ⛔ Do NOT let this escape. Both flat callers (`_grar_parse`,
            # `diagnose`) iterate this generator without a try, so an
            # unlistable conf.d root would kill the tool with a traceback
            # instead of naming the path — the death this module exists to
            # replace with a sentence. `unusable_config_paths` carries the
            # signal for exactly this case; see its `_walk_error` note.
            return
        for p in entries:
            if _is_regular_file(p) and _is_config(p.name):
                yield p
        return
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not _is_hidden(d))
        for fn in filenames:
            if _is_config(fn) and _is_regular_file(Path(dirpath) / fn):
                found.append(Path(dirpath) / fn)
    for p in sorted(found, key=lambda q: q.relative_to(root).as_posix()):
        yield p


def _is_regular_file(p: Path) -> bool:
    """`Path.is_file()` that answers False instead of raising.

    ⛔ THE single predicate BOTH enumerations consult, and that is the
    point: whatever this returns True for `iter_config_files` hands to the
    reader, whatever it returns False for `unusable_config_paths` names.
    One predicate cannot disagree with itself, so the two lists cannot
    overlap.

    Measured before it was shared (#1469 follow-up): the RECURSIVE branch
    of `iter_config_files` applied no file check at all while the flat
    branch did, so a broken symlink came back from BOTH lists under
    `recursive=True` — `validate-config` reported it twice, once as
    `OSError: [Errno 40] Too many levels of symbolic links` and once as
    `is a broken symlink`, and the parametrised disjointness test could not
    see it because its fixture used an unreadable REGULAR file, for which
    `is_file()` is True in both branches.
    """
    try:
        return p.is_file()
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
            # A different fact from "you named a directory like a config
            # file": this one means a whole subtree was NOT scanned, so the
            # report that follows is INCOMPLETE.
            #
            # ⛔ Decided by ATTEMPTING THE SAME OPERATION that failed, not
            # by asking `os.access`. The first version asked, and it was
            # wrong twice over: `os.access` ignores the mode bits for uid 0,
            # so under root this clause was unreachable — and a walk can
            # fail for reasons permission bits do not model at all (EIO,
            # ELOOP, a directory deleted mid-scan, a FUSE mount going away).
            # The two answers could therefore disagree, and the one derived
            # from the weaker probe is the one the operator would read.
            # `os.walk` with an `onerror` recorder is the exact mechanism
            # that put this path in `unusable_config_paths`, stopped after
            # the first step, so the reason cannot drift from the finding.
            probe_failed: list[bool] = []
            for _ in os.walk(p, onerror=lambda _e: probe_failed.append(True)):
                break
            if probe_failed:
                return ("is a directory that could not be read — the config "
                        "files inside it were NOT scanned")
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

    (Plus any directory whose contents could not be enumerated — see the
    ⛔ note below; that one is NOT necessarily config-named, because what
    it costs the caller is a whole subtree rather than one file.)

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

    ⛔ DISJOINT from `iter_config_files`, and enforced by SHARING the
    predicate rather than by two definitions agreeing: both call
    `_is_regular_file`, this one on the complement. The two lists are
    consumed together, so any overlap is reported twice.

    That is not hypothetical, and it was live twice. First an unreadable
    REGULAR `.yaml` appeared in both, because it is still a file so
    `iter_config_files` yields it: measured, both lists returned
    `locked.yaml`. Then — after that was fixed by narrowing THIS function —
    a broken symlink appeared in both, because the fix was written against
    `iter_config_files`'s FLAT branch while its RECURSIVE branch had no
    file check at all: measured, `check_yaml_syntax` reported `ghost.yaml`
    once as `OSError: [Errno 40] Too many levels of symbolic links` and
    again as `is a broken symlink`, and `unusable_files` carried it twice
    so the caveat line said "2 file(s)" for one path. Hence the single
    shared predicate: two branches cannot drift from one function.

    The complement is the honest division of labour. A path this function
    is *for* — a config-named directory, a broken symlink, a FIFO — can
    never be `open()`ed as config, so nothing else will ever speak for it.
    A regular file that exists but cannot be read WILL be spoken for, by
    the reader's own `open()` failure, with the real errno attached, which
    is strictly more informative than a one-clause summary here.

    ⚠️ So an unreadable REGULAR file is deliberately NOT in this list, and
    an earlier version of this note claimed the opposite ("the permission
    case is only observable when the process is NOT root"). That was left
    behind by the narrowing and was simply false afterwards: the permission
    clause of `unusable_reason` is unreachable from here for regular files
    at ANY uid, because they never get past `_is_regular_file`. The clause
    is still live for what this list DOES carry — an unreadable directory.

    ⛔ ALSO RETURNS A DIRECTORY THAT COULD NOT BE TRAVERSED, config-named
    or not. `os.walk` swallows a scandir failure by default and drops the
    whole subtree, which cost more than a single file: measured, one
    chmod-000 sub-directory holding a tenant file made `check_yaml_syntax`
    answer `pass` / `1 files parsed successfully` / `unusable_files: []`.
    The flat branch had the mirror-image bug — `root.iterdir()` raised
    `PermissionError` straight through two callers that do not wrap it.

    ⚠️ Cost: this walks the tree a second time (measured on 1000 tenants /
    20 sub-dirs: 12.5 ms for `iter_config_files`, 11.7 ms for this — both
    negligible beside the YAML parsing that follows). An earlier revision
    of this docstring rejected an alternative design for adding "an
    `os.access` syscall on every file", which did not square with paying
    for a whole extra walk here; the real reason to keep the two separate
    is that they answer different questions, not syscall count.
    """
    root = Path(config_dir)
    if not root.is_dir():
        return []

    found: list[Path] = []
    if not recursive:
        try:
            entries = sorted(root.iterdir(), key=lambda q: q.name)
        except OSError:
            # The root itself cannot be listed (`chmod 111`: traversable,
            # not readable). ⛔ Two wrong answers were both live here:
            # raising killed `_grar_parse` / `diagnose`, which call this
            # OUTSIDE any try, and returning [] was a green light for a
            # directory nothing ever read. Name the root instead.
            return [root]
        for p in entries:
            if _is_config(p.name) and not _is_regular_file(p):
                found.append(p)
        return found

    unscannable: list[Path] = []

    def _walk_error(err: OSError) -> None:
        # ⛔ `os.walk` defaults to onerror=None, which SWALLOWS a scandir
        # failure: an unreadable sub-directory drops out of the walk with
        # its whole subtree and no signal at all. Measured before this
        # callback existed — a conf.d with one chmod-000 sub-directory
        # holding a tenant file made `check_yaml_syntax` report
        # `status: pass` / `1 files parsed successfully` /
        # `unusable_files: []`. That is the #1339 shape ("a green light for
        # a directory it never read") one level further down, inside the
        # very list that exists to make such things audible.
        if err.filename is not None:
            unscannable.append(Path(err.filename))

    for dirpath, dirnames, filenames in os.walk(root, onerror=_walk_error):
        dirnames[:] = sorted(d for d in dirnames if not _is_hidden(d))
        # Directories first: a config-named DIRECTORY is the case that made
        # this function necessary, and `os.walk` never puts it in filenames.
        for name in list(dirnames) + list(filenames):
            p = Path(dirpath) / name
            if _is_config(name) and not _is_regular_file(p):
                found.append(p)
    # An unscannable directory is reported even when it is NOT config-named:
    # what it costs the caller is not one file but everything underneath it.
    found.extend(d for d in unscannable if d not in found)
    found.sort(key=lambda q: q.relative_to(root).as_posix()
               if q != root else "")
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
