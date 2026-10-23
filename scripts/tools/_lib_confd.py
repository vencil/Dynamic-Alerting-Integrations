"""Single answer to "what is in a conf.d/ directory".

The defect family this module closes: many readers of the same tenant
config tree, each hand-rolling its own enumeration, giving different
answers about what the tree contains — and the flat ones reporting
"nothing" in a way indistinguishable from "nothing to find".

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
has to say so out loud instead of silently reporting nothing. Since #3206
(ADR-036 §7 step 9) that answer is not computed here: which paths a conf.d
holds and what each one is (Go's `role`) are `da-guard parse-tree`'s
entries — the reader's tree-mode document, or a `--list-only` run — and
this module only projects them (see "the walk" below).

Two things a caller can do — pick deliberately, never by accident:

1. `iter_config_files(dir)` — read the whole tree, like the exporter does.
   Correct for anything whose job is "tell me about this configuration"
   (validators, describers, linters).

2. `warn_nested(dir, listing=...)` — stay flat, but say so out loud: it prints the
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
broken symlink, a directory it cannot list. Every reader should name those; the
selection being shared is what stops two readers reaching two verdicts,
and this is what stops "shared" from meaning "equally silent".
"""

from __future__ import annotations

import contextlib
import contextvars
import os
import re
from typing import Any, Callable, Collection, Iterable, Iterator, Mapping, NamedTuple
import sys
from pathlib import Path


__all__ = [
    "LEGACY_STATE_FILES",
    "legacy_state_files",
    "configmap_key_problem",
    "duplicate_declarations",
    "overlay_platform_tenants",
    "tenant_set_unknown_note",
    "iter_config_files",
    "ConfigTreeListing",
    "list_config_tree",
    "config_listing",
    "not_config_files",
    "unusable_entry",
    "unusable_text",
    "FlatRead",
    "resolve_defaults_file",
    "nested_yaml_files",
    "nested_yaml_warning",
    "observe_flat_reads",
    "printable_name",
    "reset_warned_for_test",
    "unusable_config_entries",
    "TenantSetUnknown",
    "TreeTenants",
    "tree_tenants",
    "unusable_config_paths",
    "unusable_reason",
    "warn_nested",
]

# ⛔ NO NAME RULE HERE (#3206, ADR-036 §7 step 9). Which names are config
# files, hidden, reserved (`_`), the defaults carrier, a platform-defaults
# document, which carrier a directory selects and the tenant id a file name
# carries are Go's answers (`pkg/confdname`, `da-guard parse-tree`): an
# entry's `role` / `class` / `tenant_named` / `defaults_document` /
# `not_read` and a listing's `defaults_carriers` for a path of a tree, and
# `_lib_parse_tree.classify_names` for a name that is not on a disk (a
# ConfigMap's keys). The Python copy of those rules (`has_yaml_extension`,
# `is_reserved_name`, `is_defaults_name`, `config_stem`,
# `select_defaults_carrier`, …) and its parity tests were deleted; Go's own
# test pins the shared table (`tests/shared/confd_name_classification_matrix.json`,
# pkg/confdname's matrix test).


# ── the walk: da-guard's per-path index (ADR-036 §7 step 9, #3206) ─────
#
# ⛔ NO WALK OF ITS OWN, AND NO VERDICT OF ITS OWN. Which entries a conf.d
# holds and what each one IS — a config file (role `config`: read, and a
# read that fails fails loudly), a directory the walk enters (`dir`; one with
# a file's name carries a problem), provably not a file and not entered
# (`not_a_file`: a directory symlink the walk does not follow, a FIFO, a
# dangling link), a directory that cannot be listed (`unlistable_dir`) — is Go's
# verdict, `pkg/config`'s, read from `da-guard parse-tree`'s entries through
# `_lib_parse_tree`. Nothing here composes a verdict from the other fields
# (`class`, `problem`) or from a stat: twice a Python default leaning toward
# "not a file" (a refused open, then a refused stat) let a tenant vanish with
# one WARN line.
#
# Where the listing comes from:
# * a caller that holds a `_lib_parse_cache.ConfdReader` passes `reader=` (or
#   the `reader.listing()` itself): the entries of the tree-mode document it
#   reads the content from — one snapshot, never a second walk;
# * otherwise `config_listing(dir)` takes the listing of the run's reader of
#   that tree (`_lib_parse_cache.reader_for`: in a `session()` one per root,
#   else a new one per call — a caller that asks several questions keeps the
#   `Listing` and passes it down, `listing=`). ⛔ Tree mode, never
#   `--list-only` (#3206): only tree mode carries the whole of Go's
#   `verdict`, and the reader's tree read is the run's fail-closed exit
#   (`ConfdReader.tree`), so no file list comes from a tree Go names paths on.
#
# ⛔ `_lib_parse_tree` is imported INSIDE the functions, never at module
# level: it imports `guard_dispatch`, which the recipe-preview image does not
# ship, and every tool that imports this module (or `_lib_io`) only for a
# predicate must not need da-guard to start.
#
# A tree with no usable da-guard raises `_lib_parse_tree.ParseTreeError`
# (`BinaryMissing` for no binary) — never an empty listing. A directory that
# does not exist (or is not a directory) is still the empty answer: that is
# the caller's "no such conf.d", not a da-guard failure.


def config_listing(config_dir: "str | os.PathLike[str]", *, reader: Any = None) -> Any:
    """The `_lib_parse_tree.Listing` every function below answers from, or
    None when *config_dir* is not a directory: *reader*'s (`ConfdReader.
    listing`, the snapshot it reads the content from) when given, else the
    run's reader of the tree (`_lib_parse_cache.reader_for`; see the section
    comment). Raises `_lib_parse_tree.ParseTreeError`; ends the run on a tree
    Go names paths on (`ConfdReader.tree`)."""
    root = Path(config_dir)
    if not root.is_dir():
        return None
    if reader is None:
        from _lib_parse_cache import reader_for  # lazy: see the section comment
        reader = reader_for(root)
    return reader.listing()


def _listing(config_dir: "str | os.PathLike[str]", listing: Any, reader: Any) -> Any:
    return listing if listing is not None else config_listing(config_dir, reader=reader)


def resolve_defaults_file(
    base: str | os.PathLike[str], *, tool: str | None = None,
    skipped_note: str | None = None, observed: bool = True,
    listing: Any = None, reader: Any = None,
) -> Path:
    """The platform-defaults carrier directly in `base`, as da-guard names
    it (`defaults_carriers["."]`: the one spelling the root's chain reads,
    #1674) — from *listing* / *reader* when given (see the section comment).

    Returns the canonical `_defaults.yaml` path when the root has none (or
    `base` is not a directory), so a caller's "this file does not exist"
    branch still names something an operator can create.

    ⚠️ This read is FLAT: on a hierarchical conf.d the exporter also merges
    `sub/_defaults.yaml`, which this function does not return. It prints
    the same stderr warning `warn_nested` would (through the shared
    `_print_nested_once`: at most once per directory per process, NOTHING
    for a flat tree), but records for `observe_flat_reads` only the nested
    defaults carriers it skipped — not every nested file, which is what
    `warn_nested` records (#1652). `tool` is left to `nested_yaml_warning`'s
    own default so the message names the command the operator ran.

    `skipped_note` replaces that warning with one naming only what THIS
    lookup skips — the nested defaults carriers — introduced by the note
    (the `observe_flat_reads` record is unchanged): for a caller that reads
    every other file of the tree some other way (policy_engine, #2115 0-B).
    Nothing is printed when there is no nested carrier.

    `observed=False` neither records nor prints: for a caller whose verdict
    does not rest on the nested carriers this lookup skips (`_lib_key_verdict`,
    ADR-038). A caller that judges files with what it finds here keeps the
    default.

    Raises `_lib_parse_tree.ParseTreeError` (no usable da-guard).
    """
    root = Path(base)
    held = _listing(root, listing, reader)
    if held is None:
        return root / "_defaults.yaml"
    # #1652: what `observe_flat_reads` records here is what THIS lookup
    # skipped — the nested carriers (the exporter merges those, ADR-017) —
    # not the whole nested set `warn_nested` would record.
    if observed and _FLAT_READ_SINKS.get():
        _record_flat_read(root, _nested_carriers(held))
    if not observed:
        pass  # the caller reports what this lookup skips (see `observed`)
    elif skipped_note is None:
        _print_nested_once(root, tool=tool, missed=_nested_files(held))
    else:
        _print_skipped_carriers(root, held, tool=tool, note=skipped_note)
    chosen = held.defaults_carriers.get(".")
    return root / chosen if chosen is not None else root / "_defaults.yaml"


def _nested_files(listing: Any) -> list[Path]:
    """The config files (role `config`) of *listing* below its root."""
    return [listing.root / e.path for e in listing.config_files() if e.depth > 0]


def _nested_carriers(listing: Any) -> list[Path]:
    """The defaults carriers (class `defaults`) of *listing* below its root."""
    return [listing.root / e.path for e in listing.config_files()
            if e.depth > 0 and e.cls == "defaults"]


def iter_config_files(config_dir: str | os.PathLike[str], *, recursive: bool = True,
                      listing: Any = None, reader: Any = None):
    """Yield the config files under `config_dir` — the entries whose role is
    `config` — sorted by POSIX relative path.

    That ordering is the whole promise — it is NOT depth-first, and
    shallow files do not come first: `a/tenant.yaml` sorts before a
    root-level `z.yaml` (da-guard sorts by path, byte order, which is the
    code-point order of a UTF-8 name).

    `recursive=False` keeps only the top level (depth 0); it exists so a
    caller can be explicit rather than accidentally flat.

    ⛔ A file whose stat or open is refused (permission denied) IS yielded:
    it is a config file, and being unreadable is a read-time matter — the
    reader that opens it fails, loudly. Only what Go proves is no config
    file goes to `unusable_config_paths` instead.
    Raises `_lib_parse_tree.ParseTreeError`.
    """
    held = _listing(config_dir, listing, reader)
    if held is None:
        return
    for e in held.config_files():
        if recursive or e.depth == 0:
            yield held.root / e.path


class ConfigTreeListing(NamedTuple):
    """Both halves of ONE listing of a conf.d (#2054).

    `files` is exactly what `iter_config_files(root)` yields, `unusable`
    exactly what `unusable_config_paths(root)` returns — projections of the
    same listing, split by the entry's role, so they cannot drift.

    `unusable` is every entry Go flags (`Entry.flagged`: not a config file,
    with a problem) — a plain directory the walk enters is neither list.
    `unscannable` is the subset of `unusable` whose role is
    `unlistable_dir` (the root itself when it cannot be listed). Kept apart
    because it is a different loss — a whole subtree, not one entry — so a
    caller that filters `unusable` by NAME must not filter these: the
    exporter's walker descends `_`-prefixed directories.
    """
    files: list[Path]
    unusable: list[Path]
    unscannable: list[Path]
    listing: Any = None
    """The `_lib_parse_tree.Listing` the three lists are projections of —
    pass it to `unusable_reason(p, listing)` so a reason comes from the same
    snapshot as the path (None for a directory that does not exist)."""


def list_config_tree(config_dir: str | os.PathLike[str], *, listing: Any = None,
                     reader: Any = None) -> ConfigTreeListing:
    """ONE listing of `config_dir`: what to read AND what was dropped.

    For a reader that needs both lists from one snapshot of the tree
    (`describe_tenant`, `test_reader_walks_the_tree_once`). The listing is
    da-guard's: recursive, `.`-prefixed entries pruned, directory symlinks
    never descended and — unless the walk reaches their target by itself —
    listed as `not_a_file` whatever their name (#1972).
    Raises `_lib_parse_tree.ParseTreeError`.
    """
    held = _listing(config_dir, listing, reader)
    if held is None:
        return ConfigTreeListing([], [], [])
    root = held.root
    bad = [e for e in held.entries if e.flagged]
    return ConfigTreeListing(
        [root / e.path for e in held.config_files()],
        [root / e.path for e in bad],
        [root / e.path for e in bad if e.unlistable_dir],
        held)


def unusable_entry(p: "str | os.PathLike[str]", listing: Any) -> Any:
    """The `_lib_parse_tree.Entry` *listing* (one the caller holds, *p*
    under its root) gives *p*, or None when it does not list it. ⛔
    *listing* is required (#3206): the entry comes from the one snapshot *p*
    was listed in — never from a second walk, which could disagree with it,
    or fail where the first did not (a sibling whose name is not UTF-8 makes
    a listing of the parent refuse)."""
    if listing is None:
        return None
    rel = os.path.relpath(os.path.abspath(p), os.path.abspath(listing.root))
    return listing.entry(Path(rel).as_posix())


def unusable_reason(p: Path, listing: Any) -> str:
    """Why `p` is not usable as a config file — one clause, after the path.

    da-guard's words for `p`'s entry in *listing* (required: the snapshot
    `p` came from, `unusable_entry`), never a second wording here: a reader
    that invented its own would put a second answer to "what happened to
    beta.yaml" in front of the operator. A caller that needs to tell the
    causes apart reads the entry's role (`unusable_entry`), never this text.
    """
    e = unusable_entry(p, listing)
    if e is None or e.problem is None:
        return "is not a config file the exporter's walk reads"
    return unusable_text(*e.problem, dir_link=e.dir_link)


def unusable_text(kind: str, message: str, *, dir_link: bool = False) -> str:
    """`unusable_reason`'s clause for da-guard's problem *kind* and
    *message* — also for a reader that holds them from a tree-mode document,
    so both word it alike. For a DIRECTORY SYMLINK the walk does not follow
    (#1972; *dir_link*: the entry's `Entry.dir_link`, Go's role and class —
    #3206, no stat here) the clause adds what that costs — the files under
    it are not loaded — because "a symlink to a directory" alone reads as
    harmless."""
    text = f"cannot be read as a config file ({kind}): {' '.join(message.split())}"
    if kind == "not_regular_file" and dir_link:
        text += " — the config files under it are NOT loaded"
    return text


def not_config_files(listing: Any) -> "frozenset[str]":
    """The root-relative paths *listing* says are no config file (any role
    but `config`: a directory, a link not followed, a special file, a
    dangling link, a directory it could not list) — for a reader that walks a
    selection of its own (a tree-mode document's `selected`) and must skip
    exactly those. ⛔ Only those (#3206): a path whose stat or open a
    permission refuses is a config file, read and failing loudly."""
    return frozenset() if listing is None else frozenset(
        e.path for e in listing.entries if not e.config_file)


def unusable_config_entries(
    entries: "Iterable[Path]", *, listing: Any = None,
) -> list[Path]:
    """The entries among ALREADY-LISTED `entries` (a caller's own `iterdir`
    of a level) that *listing* — the caller's snapshot of their directory —
    flags (`Entry.flagged`: not a config file, with a problem), sorted by
    POSIX path, so the caller still names what it cannot read. Without
    *listing*, one listing per parent directory (a caller that holds a
    snapshot passes it: the reason it then words comes from the same one).
    ⛔ Whatever the name (#3206): a directory that cannot be listed, or a
    link to a directory the walk does not follow, is named even when its
    name is no config file name — what it costs is a subtree. Hidden names
    are never in a listing, so never here.

    ⚠️ DISJOINT from the config files: a file whose open is refused is a
    config file (not here); its reader's own read names the failure.
    """
    out = []
    held: dict[str, Any] = {}
    for p in entries:
        p = Path(p)
        if listing is not None:
            e = unusable_entry(p, listing)
        else:
            parent = os.path.dirname(os.path.abspath(p))
            if parent not in held:
                held[parent] = config_listing(parent)
            e = unusable_entry(p, held[parent])
        if e is not None and e.flagged:
            out.append(p)
    return sorted(out, key=lambda q: q.as_posix())


def unusable_config_paths(
    config_dir: str | os.PathLike[str], *, recursive: bool = True,
    listing: Any = None, reader: Any = None,
) -> list[Path]:
    """Paths the exporter's walk lists that are not config files — every
    entry Go flags (`Entry.flagged`; a plain directory the walk enters is
    not one): a config-named directory, a directory
    symlink the walk does not follow (#1972), a dangling link, a FIFO, a
    directory that cannot be listed (whatever its name: what it costs is a
    whole subtree; the root itself when it cannot be listed).

    Sibling of `iter_config_files`: that one answers "what should I read",
    this one "what did it drop that the operator will still call
    configuration" (#1469). Disjoint from it — both are projections of one
    listing, split by the role — and sorted the same way.

    `recursive=False` keeps the top level only (depth 0, the root included).

    ⚠️ A file whose stat or open is refused is NOT here: it is a config
    file, yielded by `iter_config_files`, and its reader fails on it with
    the real reason. Raises `_lib_parse_tree.ParseTreeError`.
    """
    held = _listing(config_dir, listing, reader)
    if held is None:
        return []
    return [held.root / e.path for e in held.entries
            if e.flagged and (recursive or e.depth == 0)]


# ── ConfigMap key legality (#1796; shared since #1791) ────────────────
#
# ⛔ ONE copy. `configmap_assemble` (ops, repo layout) and `init_project`'s
# kustomize base (tools, flat da-tools image layout) both need it, and this
# module is the one both can import: it ships flat in the image
# (`components/da-tools/app/build.sh`) and `configmap_assemble` already
# reaches it through `scripts/tools` on `sys.path`.
#
# ⛔ NOT from memory. Transcribed from the authority, k8s.io/apimachinery
# `pkg/util/validation/validation.go`:
#
#   const configMapKeyFmt = `[-._a-zA-Z0-9]+`
#   var configMapKeyRegexp = regexp.MustCompile("^" + configMapKeyFmt + "$")
#   func IsConfigMapKey(value string) []string {
#       if len(value) > DNS1123SubdomainMaxLength { ... }   // 253, BYTES
#       if !configMapKeyRegexp.MatchString(value) { ... }
#       errs = append(errs, hasChDirPrefix(value)...)       // "." ".." "..*"
#   }
#
# `hasChDirPrefix` is the shared helper `IsValidPathSegmentName` also uses; it
# rejects `.`, `..` and any name starting with `..`. Go's `len()` counts
# BYTES, hence the encode below rather than `len(name)`.
#
# ⛔ `\Z`, not `$`: Python's `$` also matches just BEFORE a trailing newline,
# so `db-a.yaml\n` would have been accepted as a key. Go's `regexp` `$`
# (without `(?m)`) means end-of-text, which is `\Z` here — transcribing `$`
# to `$` copied the character and lost the meaning.
_CONFIGMAP_KEY_RE = re.compile(r"\A[-._a-zA-Z0-9]+\Z")
_MAX_KEY_BYTES = 253  # DNS1123SubdomainMaxLength


def _name_bytes(name: str) -> bytes:
    """The bytes the API server will count for `name`, never raising."""
    try:
        return os.fsencode(name)
    except UnicodeEncodeError:
        # A synthesised name under a non-UTF-8 locale. UTF-8 is what the
        # API server counts, and `surrogatepass` keeps a lone surrogate
        # (what `fsdecode` hands back for an undecodable byte) countable.
        return name.encode("utf-8", "surrogatepass")


def configmap_key_problem(name: str) -> str | None:
    """Why `name` cannot be a ConfigMap key, or `None` if it can.

    ⛔ DROPPING the file instead of refusing would be the silent failure (#1603's
    shape) — hence "rename it" or "leave it out on purpose", never
    "skip it". This function makes no claim about what kubectl's own
    message says.

    ⛔ `os.fsencode`, not `name.encode("utf-8")`. A file name is BYTES on
    POSIX; Python hands it back with the undecodable ones smuggled in as
    lone surrogates, and `.encode("utf-8")` then raises `UnicodeEncodeError`
    — a codec traceback in place of the by-name refusal this function exists
    to produce. `os.fsencode` reverses the same escape, so the count is the
    byte count the API server will apply, for every name the filesystem can
    hand us.

    ⛔ …and it raises the same `UnicodeEncodeError` on a name that did NOT
    come from the filesystem, once the locale's filesystem encoding is not
    UTF-8: `os.fsencode` then encodes with `ascii`. No production path
    reaches that (names come from `iterdir`, so they carry the escape
    `fsencode` reverses), but a function whose whole job is "refuse by name
    instead of raising" may not have an input class it raises on.
    """
    if len(_name_bytes(name)) > _MAX_KEY_BYTES:
        return f"longer than {_MAX_KEY_BYTES} bytes"
    if not _CONFIGMAP_KEY_RE.match(name):
        bad = sorted({c for c in name if not _CONFIGMAP_KEY_RE.match(c)})
        shown = " ".join(repr(c) for c in bad) or "(empty name)"
        return f"contains characters a key may not hold: {shown}"
    if name in (".", ".."):
        return f"must not be {name!r}"
    if name.startswith(".."):
        return "must not start with '..'"
    return None


def nested_yaml_files(config_dir: str | os.PathLike[str], *, listing: Any = None,
                      reader: Any = None) -> list[Path]:
    """Config files a FLAT reader would miss: the config files (role
    `config`) the listing holds below the top level. Raises ParseTreeError."""
    held = _listing(config_dir, listing, reader)
    return [] if held is None else _nested_files(held)


def printable_name(name: str) -> str:
    """`name` with every non-printable character escaped, nothing else.

    ⛔ A file name is attacker-controllable in exactly the trees these
    warnings are about (a shared config repo, a checkout from somewhere
    else), and every caller of this module writes it to a TERMINAL. A name
    holding `\\x1b[2J` clears the operator's screen, so the warning that
    exists to make a loss audible can erase the output around it. Measured
    on `configmap_assemble`: `cat -v` showed `^[[2J^[[H` reaching stderr
    verbatim.

    ⛔ Escapes by the PROPERTY `str.isprintable()`, not by a list of
    dangerous characters: the list would be an enumeration of the escapes
    someone thought of, and the next terminal feature would not be on it.
    ⚠️ Identity on every printable name that holds no backslash, CJK and
    emoji included, so the substring assertions callers make on these
    messages are unaffected.

    ⛔ The backslash is escaped too, and that is not decoration: without it
    the mapping is not injective, so `a\\x1bb.yaml` (a real ESC) and
    `a\\\\x1bb.yaml` (four literal characters) print the SAME and the
    operator cannot tell which file to go looking for — in the one function
    whose reason to exist is that a file name is attacker-controllable.
    """
    if name.isprintable() and "\\" not in name:
        return name
    return "".join(c if (c.isprintable() and c != "\\") else repr(c)[1:-1]
                   for c in name)


def _running_tool() -> str:
    """Best guess at the command the operator actually typed.

    Derived from `sys.argv[0]`, not hard-coded: a SHARED helper such as
    `warn_nested` is reached from several entry points, and
    labelling its warning with the helper's own name tells the operator
    nothing about which command produced it. Falls back to a neutral
    label when argv is unavailable (embedded / REPL use).
    """
    argv0 = (sys.argv[0] if sys.argv else "") or ""
    name = os.path.basename(argv0)
    return name[:-3] if name.endswith(".py") else (name or "conf.d reader")


#: How many entries a per-file warning from this plane names before it says
#: "(+N more)". ⛔ Named rather than spelled twice: a second reader writing
#: to the same stderr (`configmap_assemble`'s unreadable-carrier WARN) has to
#: truncate at the SAME count, or the operator cannot tell from the output
#: whether a list is complete without knowing which reader wrote which line.
WARN_LIMIT = 5


def nested_yaml_warning(
    config_dir: str | os.PathLike[str],
    *,
    tool: str | None = None,
    limit: int = WARN_LIMIT,
    missed: "list[Path] | None" = None,
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
    if missed is None:
        missed = nested_yaml_files(config_dir)
    if not missed:
        return None
    root = Path(config_dir)
    shown = [printable_name(p.relative_to(root).as_posix())
             for p in missed[:limit]]
    more = f" (+{len(missed) - limit} more)" if len(missed) > limit else ""
    # ⛔ Scoped to THIS read, not to "this tool's output" (#1652): one command
    # can mix readers — validate_config reads yaml_syntax / profiles /
    # tenant_uniqueness recursively in the same run that prints this for its
    # routing reader — so a sentence about the whole output was false there.
    # ⛔ No "run a recursive reader (e.g. validate_config)" advice (#1652):
    # validate_config's schema / routes / policy / policy_dsl rows go through
    # flat readers themselves, so that pointer sent the operator from one
    # flat read to another. Naming which rows ARE recursive here would be a
    # second copy of a fact that lives in validate_config and drifts.
    # ⛔ Self-contained on purpose (#1714): this sentence used to end with a
    # ticket number that belongs to an unrelated topic, so an operator who
    # followed it landed on the wrong page. Say what happened, what it
    # means, and what to do — nothing here needs a lookup.
    return (
        f"{tool}: read {config_dir} FLAT (top level only), but {len(missed)} "
        f"config file(s) live in subdirectories and were SKIPPED: "
        f"{', '.join(shown)}{more}. "
        f"threshold-exporter reads this tree recursively (ADR-016/017). "
        f"Whatever {tool} derived from THIS read covers the top-level files "
        f"only and says nothing about the skipped ones; flatten the tree if "
        f"every reader must see the same files."
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


class FlatRead(NamedTuple):
    """One flat read seen by `observe_flat_reads`."""

    directory: str
    """The directory as the reader was handed it."""

    skipped: tuple[Path, ...]
    """Config files below its top level that THIS read did not open."""


# Sinks collecting the flat reads made while they are installed. A
# ContextVar holding an immutable tuple, not a module-level list: nothing
# has to be reset between tests, and a nested or concurrent observer cannot
# see (or clear) another one's records.
_FLAT_READ_SINKS: contextvars.ContextVar[tuple[list[FlatRead], ...]] = (
    contextvars.ContextVar("_FLAT_READ_SINKS", default=()))


def _record_flat_read(config_dir: str | os.PathLike[str],
                      skipped: "Iterable[Path]") -> None:
    """Hand one flat read to every installed observer (no-op with none)."""
    sinks = _FLAT_READ_SINKS.get()
    if not sinks:
        return
    rec = FlatRead(os.fspath(config_dir), tuple(skipped))
    for sink in sinks:
        sink.append(rec)


@contextlib.contextmanager
def observe_flat_reads(sink: list[FlatRead]) -> Iterator[list[FlatRead]]:
    """Append to `sink` each flat read made in this block, with what it skipped.

    #1652: `validate_config` uses this to stop a row whose tenants came
    from a flat reader reporting PASS about files that reader never opened.

    What is OBSERVED — exactly two call sites, both in this module:

    * `warn_nested(d, ...)`: recorded with every config file below the top level
      of `d` (the same set it prints), on every call — including a flat
      tree (nothing skipped) and repeats its print memo silences.
    * `resolve_defaults_file(d)`: recorded with only the nested DEFAULTS
      carriers (class `defaults`), because locating the root carrier is
      all it does; a nested tenant is not something it was going to read.

    What is NOT observed, and why that is not closed by construction:

    * A flat read that calls neither. The enumeration contract
      (`tests/shared/test_confd_enumeration_contract.py`) makes a flat scan
      name a guard, but it matches call NAMES only and its guard set also
      admits `nested_yaml_warning` / `nested_yaml_files`, neither of which
      records anything. `tests/shared/test_flat_read_observation_pin.py`
      pins that no production code outside this module calls those two, so
      a new caller has to decide here instead of silently escaping.
    * A read that is not an enumeration at all — `Path(d) / "_profiles.yaml"`
      opens a root file by name and no guard is involved.

    The sink is passed in rather than created here so the caller still has
    it when the block exits by exception.
    """
    token = _FLAT_READ_SINKS.set(_FLAT_READ_SINKS.get() + (sink,))
    try:
        yield sink
    finally:
        _FLAT_READ_SINKS.reset(token)


_NO_SNAPSHOT: Any = object()


def warn_nested(config_dir: str | os.PathLike[str], *, tool: str | None = None,
                listing: Any = _NO_SNAPSHOT, reader: Any = None) -> bool:
    """Print the nested-config warning to stderr, at most once per directory.

    Returns whether anything was printed.

    Why the memo: one command often scans the same `conf.d/` from more than
    one place — `validate_config.py` reaches the routing parser twice.
    Printing the identical WARN twice reads as
    two separate problems (CodeRabbit, PR #1343). De-duplicating here rather
    than deleting one of the guards keeps every flat scan covered — a scan
    whose only warning lives in some other function is exactly the
    "gate exists but never fires" shape this module was written against.

    Callers should use this instead of hand-rolling the print, so the
    dedup and the stderr choice live in one place.

    The nested files are the caller's snapshot, one of which is required:
    *listing* (a listing it holds) or *reader* (its `ConfdReader`, whose
    tree-mode document it reads the content from) — no second walk (#3206).
    `listing=None` is a snapshot too: the one `config_listing` gives for a
    path that is not a directory (nothing nested). Raises TypeError with
    neither.
    """
    if listing is _NO_SNAPSHOT:
        if reader is None:
            raise TypeError("warn_nested: pass the caller's snapshot (listing= or reader=)")
        listing = config_listing(config_dir, reader=reader)  # None: not a directory
    missed = [] if listing is None else _nested_files(listing)
    # Recorded BEFORE the flat-tree early return and the memo: an observer
    # asks "did this code path read `config_dir` flat?", and the answer is
    # yes on the second call too, when nothing is printed.
    if _FLAT_READ_SINKS.get():
        _record_flat_read(config_dir, missed)
    return _print_nested_once(config_dir, tool=tool, missed=missed)


def _print_skipped_carriers(root: Path, listing: Any, *, tool: str | None,
                            note: str) -> None:
    """`resolve_defaults_file(skipped_note=...)`'s stderr line: the nested
    defaults carriers the root lookup does not read, behind `note`."""
    missed = _nested_carriers(listing)
    if not missed:
        return
    shown = ", ".join(printable_name(p.relative_to(root).as_posix())
                      for p in missed[:WARN_LIMIT])
    more = f" (+{len(missed) - WARN_LIMIT} more)" if len(missed) > WARN_LIMIT else ""
    print(f"WARN: {tool or _running_tool()}: {note} {len(missed)} defaults file(s) "
          f"in subdirectories: {shown}{more}", file=sys.stderr)


def _print_nested_once(config_dir: str | os.PathLike[str], *,
                       tool: str | None = None,
                       missed: "list[Path]") -> bool:
    """The stderr half of `warn_nested`, with no `observe_flat_reads` record.
    *missed* is the caller's nested files (its snapshot's).

    ⛔ Private on purpose: the only caller outside `warn_nested` is
    `resolve_defaults_file`, which records a narrower skipped set itself.
    `test_flat_read_observation_pin` keeps it from being called elsewhere.
    """
    key = os.path.abspath(os.fspath(config_dir))
    if key in _WARNED:
        return False
    msg = nested_yaml_warning(config_dir, tool=tool, missed=missed)
    if msg is None:
        return False
    _WARNED.add(key)
    print(f"WARN: {msg}", file=sys.stderr)
    return True


class TreeTenants(NamedTuple):
    """The tenant ids a conf.d tree declares, split by a name predicate."""

    tenants: list[str]
    """Declared ids that passed `validate`, sorted."""

    invalid: list[str]
    """Declared ids `validate` rejected, sorted."""


class TenantSetUnknown(ValueError):
    """The tree's tenant set cannot be answered: a tenant file `da-guard
    parse-tree` cannot read (what it declares is unknown), or one tenant id
    declared by more than one file (threshold-exporter serves neither)."""


def tree_tenants(
    config_dir: "str | os.PathLike[str]",
    *,
    validate: "Callable[[str], bool] | None" = None,
    reader: Any = None,
) -> TreeTenants:
    """Every tenant id the conf.d at *config_dir* declares, as the exporter
    keys it: the keys of the `tenants:` mapping of each tenant file
    `da-guard parse-tree` selects, at any depth, as source text
    (`_lib_parse_cache.declared_tenants`; ADR-036 step 4, #2906).

    ⛔ Not a file STEM. `team-a.yaml` holding `tenants: {db-a: …}` declares
    `db-a`; `sub/x.yaml` declares its tenants too; a file with no
    `tenants:` mapping (the flat format) declares none — the exporter
    serves no tenant from it. Which files exist, which are hidden, which
    spelling counts (`.yml`, upper case), and what a directory named
    `notes.yaml/` or a dangling link is: all parse-tree's answer, none
    decided here.

    Fail-closed, raising `TenantSetUnknown`, when the set cannot be
    answered:

    * an entry that may be — or hide — a tenant file (a directory Go
      could not list, a link to a directory it does not follow, a dangling
      link, a special file, a directory with a config file's name, unless Go
      classes the name a platform one), and a tenant file parse-tree cannot
      read — both halves of `_lib_parse_cache.declared_tenants`' one
      `unknown` list (#3206: `_lib_parse_tree.TenantScope`), each named with
      its kind; a file whose top level is not a mapping declares nothing and
      is not one of them;
    * an id declared by two or more files (`duplicate_declarations`): the
      exporter rejects such a tree (`DuplicateTenantError`), so emitting one
      tenant from it would describe a state the exporter refuses.

    *validate* splits the ids into `tenants` / `invalid` (None accepts every
    id). *reader* is a `_lib_parse_cache.ConfdReader` of *config_dir* (the
    session's when None). Raises `_lib_parse_tree.ParseTreeError` for no
    usable parse-tree — never an empty set for a tree that was not read. The
    caller owns "directory missing": it words that for its own CLI.
    """
    from _lib_parse_cache import declared_tenants
    reader = _reader_of(config_dir, reader)
    declared, unknown = declared_tenants(reader)
    if unknown:
        shown = "; ".join(
            f"{printable_name(u.file)} ({printable_name(u.kind)}: "
            f"{printable_name(' '.join(u.message.split()))})"
            for u in unknown[:WARN_LIMIT])
        more = (f" (+{len(unknown) - WARN_LIMIT} more)"
                if len(unknown) > WARN_LIMIT else "")
        raise TenantSetUnknown(
            f"cannot tell which tenants {printable_name(os.fspath(config_dir))} "
            f"declares: da-guard parse-tree cannot read {len(unknown)} "
            f"path(s) that are or may hide a tenant file: {shown}{more}")
    duplicates = duplicate_declarations(declared)
    if duplicates:
        shown = "; ".join(
            f"'{printable_name(t)}' in {', '.join(printable_name(f) for f in files)}"
            for t, files in sorted(duplicates.items()))
        raise TenantSetUnknown(
            f"tenant id declared in more than one file: {shown} — "
            f"threshold-exporter rejects a tree that declares one tenant in "
            f"two files (DuplicateTenantError); keep each tenant in one file")
    tenants: list[str] = []
    invalid: list[str] = []
    for tenant in sorted(declared):
        (tenants if validate is None or validate(tenant) else invalid).append(tenant)
    return TreeTenants(tenants, invalid)


def _reader_of(config_dir: "str | os.PathLike[str]", reader: Any) -> Any:
    # Imported here: `_lib_parse_cache` runs da-guard through modules that
    # import this one (ADR-036 step 4, #2906).
    if reader is not None:
        return reader
    from _lib_parse_cache import reader_for
    return reader_for(config_dir)


def tenant_set_unknown_note(unknown: "Iterable[Any]") -> str:
    """The clause a "no tenant file declares X" message adds when the tree's
    tenant set is not fully known (#3206): *unknown* is the open half of Go's
    `TenantScope` (`_lib_parse_cache.declared_tenants`' second element —
    `FileUnreadable`s), each a path that is or may hide a tenant file, so a
    tenant it declares cannot be told apart from one nobody declares. ""
    when the set is known."""
    unknown = list(unknown)
    if not unknown:
        return ""
    shown = ", ".join(printable_name(u.file) for u in unknown[:WARN_LIMIT])
    more = f" (+{len(unknown) - WARN_LIMIT} more)" if len(unknown) > WARN_LIMIT else ""
    return (f" that da-guard parse-tree can read (it may be declared in one of the "
            f"{len(unknown)} path(s) it cannot: {shown}{more})")


def tenant_declarations(config_dir: "str | os.PathLike[str]", reader: Any = None
                        ) -> "tuple[dict[str, set[str]], list[str]]":
    """``({tenant_id: {label, ...}}, unreadable)`` over the whole tree.

    ⛔ THE one answer to "which files declare which tenant" — the scan
    ``validate_config.check_tenant_uniqueness`` runs and the routing
    generator's duplicate refusal reuses (#2315), so the two cannot disagree
    about a duplicate. Feed the first element to ``duplicate_declarations``.

    Selection is the exporter's walker: the reader's listing (recursive,
    case-insensitive, both spellings; Go's `role`) and its class (a ``_`` file
    is never read for tenants). A declaration is a key of a mapping-shaped
    ``tenants:`` in a file `da-guard parse-tree` reads (*reader*, a
    `_lib_parse_cache.ConfdReader` of *config_dir*, the session's when None),
    keyed by its source text. A label is the
    root-relative POSIX path (the file name when it is not under the root).
    *unreadable* lists, as one list (#3206: both halves of
    `_lib_parse_tree.TenantScope`), the labels of the paths that are or may
    hide a tenant file (a directory Go could not list, a link it does not
    follow, …), then of the tenant files parse-tree could not read, in walk
    order — a limit on the answer, which the caller must name — but a file
    whose top level is not a mapping, which declares nothing.
    Raises `_lib_parse_tree.ParseTreeError` (no da-guard, a failing one) —
    never an empty answer for a tree that was not read.
    """
    from _lib_parse_tree import FileUnreadable
    from _lib_parse_cache import tenant_scope
    reader = _reader_of(config_dir, reader)
    root = Path(config_dir)
    declared: dict[str, set[str]] = {}
    scope = tenant_scope(reader) if config_listing(root, reader=reader) is not None else None
    unreadable: list[str] = [] if scope is None else [u.file for u in scope.unknown]
    for rel in ([] if scope is None else scope.files):
        path, label = root / rel, rel
        try:
            data = reader.read(path)
        except FileUnreadable as e:  # `yaml_syntax` owns naming the reason
            # A top level that is not a mapping is known content: it
            # declares no tenant (as before ADR-036 step 4). Any other kind
            # leaves what the file declares unknown.
            if e.kind != "top_level_not_mapping":
                unreadable.append(label)
            continue
        tenants = data.get("tenants")
        if not isinstance(tenants, dict):
            continue
        for tenant_id in tenants:
            declared.setdefault(tenant_id, set()).add(label)
    return declared, unreadable


def duplicate_declarations(declared: "Mapping[Any, Iterable[str]]"
                           ) -> "dict[Any, list[str]]":
    """The tenants declared by MORE THAN ONE carrier, each with its sorted
    carrier labels: ``{tenant: [label, ...]}`` for every tenant whose
    labels hold two or more distinct values.

    ⛔ THE predicate for "one tenant id, two files" — the state in which,
    when every carrier involved parses, the exporter's walker raises
    ``*DuplicateTenantError`` for that tenant (``pkg/config``
    ``TreeScan.Locate``) and a full load rejects the whole dir.
    ``validate_config.check_tenant_uniqueness`` and ``describe_tenant``
    both ask it; one function, so those two Python readers cannot disagree
    with EACH OTHER about which tenants are duplicated (#2049).

    ⚠️ They can disagree with the walker. A carrier here is a file whose
    first document has a ``tenants:`` key naming the tenant — the repo's
    decided Python notion of "declares" (see ``tenant_declarations``: the
    Go full decode is deliberately NOT mirrored, #1942). A file the
    exporter's full decode REJECTS (e.g. another tenant's body is a scalar)
    declares nothing on the Go side, so Go may resolve a tenant this
    predicate calls duplicated. Callers must word their verdict for that.

    *declared* maps a tenant id to the labels of every carrier that
    declares it. A label names a carrier the way the exporter counts one:
    the conf.d ENTRY as listed, never its resolved target. ⛔ So a link
    ``acme.yaml -> real.yaml`` beside ``real.yaml`` is TWO carriers — the
    exporter's walker lists directory entries and never follows a link, and
    measured on main c2f29ce9 it rejects that tree. Labelling by the
    resolved path would fold the two into one and describe a tenant the
    exporter refuses. Repeated labels (the same carrier reported twice)
    count once.
    """
    out: dict[Any, list[str]] = {}
    for tenant, labels in declared.items():
        distinct = sorted(set(labels))
        if len(distinct) > 1:
            out[tenant] = distinct
    return out


def overlay_platform_tenants(
    entries: "Iterable[tuple[str, str, dict]]",
    exists: "Callable[[], set[str]]",
    merge: "Callable[[dict, dict], None] | None" = None,
    *,
    platform: "Collection[str]",
) -> "tuple[dict[str, dict], list[tuple[str, str]]]":
    """Merge per-tenant blocks read from ROOT files: platform first, tenant wins.

    *entries* is ``(fname, tenant, overrides)`` for every tenant entry a
    reader took from a root file's ``tenants:`` block, in read order. An
    *fname* in *platform* — the files Go classes platform (`_`) files, the
    caller's entries' class (#3206: no name rule here) — is a PLATFORM file:
    its block is the platform's
    per-tenant DEFAULT, applied first; every tenant-file entry is applied
    after it, key by key — so the tenant wins whatever either file is called
    (the exporter's `sortFlatMergeOrder`). A key the tenant file does not
    write keeps the platform's value; it is never reset to a default.

    A platform entry for a tenant *exists* does not contain is DROPPED and
    returned in the second element as ``(fname, tenant)`` for the caller to
    name: a platform file can only provide defaults for a tenant that
    already exists. *exists* is a thunk so a tree whose platform files name
    no tenant never pays for the recursive read behind it.

    Returns ``({tenant: merged_overrides}, orphans)``; the merged dicts are
    fresh (shallow) copies, and each tenant appears once.

    *merge* ``(dst, src)`` applies one entry; default ``dst.update(src)``,
    per literal key — right for the routing readers, whose keys have one
    spelling. A reader of THRESHOLD keys passes
    ``_grar_validate.overlay_across_spellings`` so a tenant's legacy
    ``mysql_cpu`` beats a platform ``mysql_threads_running`` (#2368) as it
    does on /metrics.
    """
    apply = merge if merge is not None else dict.update
    platform_entries: list[tuple[str, str, dict]] = []
    owned: list[tuple[str, str, dict]] = []
    for fname, tenant, overrides in entries:
        (platform_entries if fname in platform else owned
         ).append((fname, tenant, overrides))
    orphans: list[tuple[str, str]] = []
    merged: dict[str, dict] = {}
    known: "set[str] | None" = None
    for fname, tenant, overrides in platform_entries:
        if known is None:
            known = exists()
        if tenant not in known:
            orphans.append((fname, tenant))
            continue
        apply(merged.setdefault(tenant, {}), overrides)
    for _fname, tenant, overrides in owned:
        apply(merged.setdefault(tenant, {}), overrides)
    return merged, orphans


#: Files that held tenant-api's own records (groups, saved views, the
#: federation whitelist, the AccountID registry) before #2856 moved them to
#: tenant-api's state store. tenant-api no longer reads them from conf.d; a
#: tool that finds one names it instead of using it (#2883).
LEGACY_STATE_FILES = ("_groups.yaml", "_views.yaml", "_federation_policy.yaml",
                      "_account_registry.yaml")


def legacy_state_files(config_dir: "str | os.PathLike[str]", *, listing: Any = None,
                       reader: Any = None) -> list[str]:
    """The pre-#2856 state record files still sitting at the top of
    *config_dir*: the config files (Go's `role`, #3206 — never `is_file()`)
    tenant-api wrote under those names. Raises `_lib_parse_tree.ParseTreeError`."""
    held = _listing(config_dir, listing, reader)
    if held is None:
        return []
    # A lookup of each name at the root, not a scan: these files only ever
    # lived there (tenant-api reads the root).
    return [n for n in LEGACY_STATE_FILES
            if (e := held.entry(n)) is not None and e.config_file]
