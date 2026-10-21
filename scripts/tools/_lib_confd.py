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

import contextlib
import contextvars
import os
import re
from typing import Any, Callable, Iterable, Iterator, Mapping, NamedTuple
import sys
from pathlib import Path


__all__ = [
    "CONFIG_SUFFIXES",
    "LEGACY_STATE_FILES",
    "legacy_state_files",
    "config_stem",
    "configmap_key_problem",
    "declared_tenant_ids",
    "duplicate_declarations",
    "overlay_platform_tenants",
    "defaults_files_in",
    "has_yaml_extension",
    "is_defaults_name",
    "is_hidden_name",
    "is_reserved_name",
    "iter_config_files",
    "ConfigTreeListing",
    "list_config_tree",
    "readable_carriers",
    "warn_dir_symlink_once",
    "is_dir_symlink",
    "dir_link_target_is_walked",
    "select_defaults_carrier",
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

CONFIG_SUFFIXES = (".yaml", ".yml")


def _is_hidden(name: str) -> bool:
    """Mirror of the exporter's skip rule — DERIVED, not an allowlist.

    `pkg/config/hierarchy.go` skips any entry whose name starts with `.`
    (`SkipDir` for directories, plain skip for files). Listing specific
    names here instead (`.git`, `__pycache__`, ...) would make this
    module's answer differ from the oracle it exists to mirror — which is
    the very divergence #1911 is about.
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
    acting on. That is #1911's shape with the *extension* as the divergence
    axis instead of directory depth, and it is the one parity claim in this
    module that was, until now, written down nowhere.

    Note this predicate answers TWO of the three name properties: extension
    (here) and hidden (`_is_hidden`). It deliberately does NOT filter the
    `_` prefix — reserved control files ARE part of "what is in a conf.d
    directory"; callers that want tenant carriers only (tenant-api, or a
    tool composing `is_reserved_name`) apply that third property themselves.

    ⛔ Nothing here greps the Go source. This repo measured (#1448 blind
    review) that a Python guard asserting things about Go source text goes
    red on legitimate Go refactors and states the opposite of the truth
    when it does. What keeps this honest instead is the shared name
    classification matrix under `tests/shared/` — the exporter's scanner,
    tenant-api's filename→id mapping, `iter_config_files` and this
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
# filename→id mapping and `iter_config_files` all assert that same
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
    behaviour change. Measured on the tree at PR #1590, four readers
    (`operator_generate`, `generate_tenant_metadata`,
    `check_path_metadata_consistency`, `custom_alerts/loader`) globbed
    `*.yaml` and therefore did not see `db-b.yml` AT ALL, while the
    exporter reads both spellings. That is a real divergence on the
    extension-SPELLING axis and it is filed separately (#1603) — but
    widening them here would land it inside a commit whose stated subject
    is case folding, where no reviewer is looking for it. Callers pass
    the set they already accept; the default is both.

    ⚠️ That list is a SNAPSHOT, not an invariant. #1603 has since widened
    `custom_alerts/loader`, `generate_tenant_metadata` and
    `check_path_metadata_consistency` and `operator_generate`, and the
    axis reaches call sites
    that list never had (it was drawn from one spelling of the narrowing —
    a keyword `suffixes=` — and missed the positional ones).
    ⛔ Do not quote a number or a name list from here — census the call
    sites by the VALUE passed, at any argument position, to any callee.

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
    """The defaults CHAIN CARRIER — exactly `_defaults.yaml`/`.yml`, any casing.

    ⛔ EXACT, not a prefix, and the difference is load-bearing. The
    oracle is `scanDirHierarchical` (`config_hierarchy.go`), which compares
    the lowercased name
    against the two literals; this table's own contract says the same in
    words ("name lowercased is exactly ..."). And
    `conf.d/examples/_defaults-multidb.yaml` existed in this repo (until
    #3014), while the exporter never merged it into any tenant's chain.

    ⚠️ A prefix version shipped briefly during #1588 and blind review
    measured what it did: `describe_tenant` went from reproducing the
    exporter's `merged_hash` on 5/5 shipped tenants to 3/5 WRONG, and
    `deprecate_rule --execute` wrote "disable" into `_defaults-multidb.yaml`
    while printing 下架完成 — leaving the tenant's own override deleted and
    its threshold silently RELAXED back to the platform value. Fixing a
    divergence by inventing a worse one is the exact failure this line of
    work exists to stop.

    ⛔ Do NOT reuse this for "is this a platform-defaults DOCUMENT to
    schema-validate". That is a prefix question and has its own predicate,
    `is_defaults_document_name` below. Two questions that share a word are
    not one predicate.
    """
    return name.lower() in ("_defaults.yaml", "_defaults.yml")


def is_defaults_document_name(name: str) -> bool:
    """A platform-defaults DOCUMENT — judged by platform-defaults.schema.json.

    PREFIX, any casing (`_defaults.yaml`, `_defaults-multidb.yaml`,
    `_DEFAULTS.YML` …): the answer to "which schema is this file held to",
    not to "does the exporter merge it" (that is `is_defaults_name`). Moved
    here from `check_confd_schema._is_defaults_file` (#2164) so the shipped
    `validate-config` selects the same files that lint does without a second
    copy of the rule; see that function for why it is not the exact match.
    """
    return has_yaml_extension(name) and name.lower().startswith("_defaults")


def select_defaults_carrier(carriers: "Iterable[Path]") -> "Path | None":
    """The ONE carrier a directory's defaults chain reads (#1674, B8).

    `carriers` are the `is_defaults_name` entries of ONE directory, in any
    order. Every plane answers this the same way — the exporter's chain
    (`pkg/config` `SelectDefaultsCarriers`), `/effective`, the flat root
    `Defaults` served on `/metrics`, and `describe_tenant` — so a directory
    holding two spellings is ONE file on every plane, never a merge:

      * a spelling that lowercases to `_defaults.yaml` beats any spelling
        that lowercases to `_defaults.yml`;
      * among `.yaml` case variants the LAST in name order wins, among
        `.yml` variants the FIRST.

    ⚠️ The asymmetry in the second bullet is not a typo. It is the rule
    `/effective` had before #1674 (its walk overwrote on `.yaml` and kept
    the first `.yml`), and the owner ruling keeps it rather than "fixing"
    it. Order is the name's code-point order, which for UTF-8 names is
    Go's byte order — the one `filepath.WalkDir` visits a directory in.

    ⛔ Two carriers in one directory is a misconfiguration: it is WARNed
    (the exporter's `DefaultsCarriers.AmbiguityWarnings`; a conf.d reader
    here names the carrier parse-tree marks `not_read`) instead of being
    chosen silently.
    """
    ordered = sorted(carriers, key=lambda p: Path(p).name)
    yaml_spelling = [p for p in ordered if Path(p).name.lower() == "_defaults.yaml"]
    if yaml_spelling:
        return yaml_spelling[-1]
    yml_spelling = [p for p in ordered if Path(p).name.lower() == "_defaults.yml"]
    return yml_spelling[0] if yml_spelling else None


def readable_carriers(carriers: "Iterable[Path]"
                      ) -> "tuple[list[Path], list[tuple[Path, OSError]]]":
    """Split carriers into (readable, [(unreadable, error)]).

    ⛔ Selection runs over the carriers the exporter's walker KEEPS, and the
    walker logs-and-drops an entry whose read fails (a dangling symlink, a
    permission error) from every map — so such an entry is never a
    candidate there. Selecting among raw directory names instead let a
    dangling `_defaults.yaml` win over a readable `_defaults.yml` and read
    as "no carrier" (blind review of #1674). Callers report the unreadable
    ones, as they did before selection existed.
    """
    readable: list[Path] = []
    unreadable: list[tuple[Path, OSError]] = []
    for p in carriers:
        try:
            with open(p, "rb") as fh:
                fh.read()
        except OSError as exc:
            unreadable.append((Path(p), exc))
            continue
        readable.append(Path(p))
    return readable, unreadable


# Directory symlinks whose skip WARNING this process has already printed.
_DIR_LINK_WARNED: set[str] = set()


def warn_dir_symlink_once(link: Path) -> None:
    """Name a directory symlink the walk skipped, at most ONCE per link per
    process (#1972). describe_tenant names it from its own listing AND calls
    the custom-alerts loader over the same tree (and compile_custom_alerts
    runs the loader more than once): each printing its own copy named every
    link two times — the #1674 once-per-directory shape.
    Keyed by the LINK's own absolute path (not its target, which is what
    two different links may share)."""
    key = os.path.abspath(link)
    if key in _DIR_LINK_WARNED:
        return
    _DIR_LINK_WARNED.add(key)
    print(f"WARNING: skipped {link} — {unusable_reason(link)}",
          file=sys.stderr)


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


def defaults_files_in(root: str | os.PathLike[str],
                      names: "Iterable[str]") -> list[Path]:
    """The defaults carriers among ALREADY-LISTED `names`, sorted.

    ⛔ Takes the names instead of listing the directory, and that is not a
    style choice. The first version called `root.iterdir()` and
    `test_confd_enumeration_contract` correctly reddened `_lib_confd.py`
    itself: a sentinel flat scan with nothing said about nested content.
    Its docstring argued the callers were already recursive — which was
    true, and which is exactly why listing again was wrong. Both callers
    (`loader._dir_defaults_alerts` walking with `os.walk`,
    `describe_tenant._scan` with `rglob`) already hold the names.

    ⚠️ Ordering is `sorted(names)`, so a directory carrying both
    spellings contributes deterministically. Two spellings is already a
    misconfiguration; resolving it by directory order would make it an
    intermittent one.
    """
    base = Path(root)
    return [base / n for n in sorted(names) if is_defaults_name(n)]



def resolve_defaults_file(
    base: str | os.PathLike[str], *, tool: str | None = None,
    skipped_note: str | None = None, observed: bool = True,
) -> Path:
    """The platform-defaults carrier directly in `base`, in ANY casing.

    Returns the canonical `_defaults.yaml` path when nothing matches, so a
    caller's "this file does not exist" branch still names something an
    operator can create.

    ⛔ ONE implementation, deliberately. Three tools grew a private copy of
    this while #1588 was being fixed, which is the same "one rule, many
    hand-copies" shape #1911 is made of — reproduced inside the fix for it.

    ⚠️ This read is FLAT: on a hierarchical conf.d the exporter also merges
    `sub/_defaults.yaml`, which this function does not return. It prints
    the same stderr warning `warn_nested` would (through the shared
    `_print_nested_once`: at most once per directory per process, NOTHING
    for a flat tree), but records for `observe_flat_reads` only the nested
    `_defaults.yaml` carriers it skipped — not every nested file, which is
    what `warn_nested` records (#1652, see the comment in the body). `tool`
    is left to `nested_yaml_warning`'s own default so the message names the
    command the operator ran rather than this helper.

    `skipped_note` replaces that warning with one naming only what THIS
    lookup skips — the nested `_defaults.yaml` carriers — introduced by the
    note (the `observe_flat_reads` record is unchanged): for a caller that
    reads every other file of the tree some other way. policy_engine
    (#2115 0-B) takes its tenants from the exporter's own load and would
    otherwise be told its subdirectory tenants were SKIPPED while it
    evaluates them. Nothing is printed when there is no nested carrier.

    `observed=False` neither records nor prints: for a caller whose verdict
    does not rest on the nested carriers this lookup skips — it reads the
    tree through da-guard and takes only the root's `_policies`, exactly as
    Policy-as-Code does, whose own row reports that gap (`_lib_key_verdict`,
    ADR-038). A caller that judges files with what it finds here keeps the
    default.

    A directory carrying two spellings resolves to the one the exporter
    reads (`select_defaults_carrier`, #1674) — before that it was the first
    in sort order, which picked `_DEFAULTS.YML` over the `_defaults.yaml`
    the exporter merges, so a writer could edit a file nothing reads.
    """
    root = Path(base)
    # #1652: what `observe_flat_reads` records here is what THIS lookup
    # skipped, not the whole nested set `warn_nested` would record. It looks
    # for one carrier at the root, so what it misses is a `sub/_defaults.yaml`
    # — the exporter merges those (ADR-017), and a caller that takes
    # `_policies` from the root carrier silently has none when they live
    # only in `prod/_defaults.yaml` (measured: validate_config's
    # `policy_dsl` FAIL on the flat twin, PASS on the hierarchical one).
    # ⛔ Not the whole nested set: recording every nested file here turned
    # `profiles` (whose tenants are read recursively) and `policy_dsl` with
    # no policies into WARN on a tree whose only nested file was a TENANT —
    # a file this lookup was never going to read. The judge is what was
    # skipped, not who is calling.
    # The walk is only paid when an observer is installed, as in
    # `warn_nested`; `_print_nested_once` does its own.
    if observed and _FLAT_READ_SINKS.get():
        _record_flat_read(root, [p for p in nested_yaml_files(root)
                                 if is_defaults_name(p.name)])
    if not observed:
        pass  # the caller reports what this lookup skips (see `observed`)
    elif skipped_note is None:
        _print_nested_once(root, tool=tool)
    else:
        _print_skipped_carriers(root, tool=tool, note=skipped_note)
    try:
        readable, _unreadable = readable_carriers(
            entry for entry in root.iterdir()
            if entry.is_file() and is_defaults_name(entry.name))
        chosen = select_defaults_carrier(readable)
        if chosen is not None:
            return chosen
    except OSError:
        pass
    return root / "_defaults.yaml"


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
    yield from list_config_tree(root).files


class ConfigTreeListing(NamedTuple):
    """Both halves of ONE recursive walk of a conf.d (#2054).

    `files` is exactly what `iter_config_files(root)` yields, `unusable`
    exactly what `unusable_config_paths(root)` returns — for
    `recursive=True`, those two are projections of this, so they cannot
    drift from it (their flat branches are separate).

    `unscannable` is the subset of `unusable` that is a directory the walk
    could not enumerate. Kept apart because it is a different loss — a
    whole subtree, not one entry — so a caller that filters `unusable` by
    NAME (e.g. drops `_`-prefixed entries it never reads) must not filter
    these: the exporter's walker descends `_`-prefixed directories.
    """
    files: list[Path]
    unusable: list[Path]
    unscannable: list[Path]


def list_config_tree(config_dir: str | os.PathLike[str]) -> ConfigTreeListing:
    """Walk `config_dir` ONCE; return what to read AND what was dropped.

    For a reader that needs both lists: calling `iter_config_files` and
    `unusable_config_paths` separately walks the tree twice, and two walks
    can describe two DIFFERENT trees if anything changes under conf.d
    mid-run — a file created between them is in neither list, one replaced
    by a directory is in both. `describe_tenant` is such a reader
    (`test_reader_walks_the_tree_once`).

    The walk mirrors the exporter's (`pkg/config.ScanDirTree`): recursive,
    `.`-prefixed directories pruned, `.`-prefixed files dropped (`_is_config`),
    directory symlinks never descended (`os.walk` default) and, unless
    hidden or pointing where the walk goes anyway
    (`dir_link_target_is_walked`), named in `unusable` whatever their
    name (#1972).
    See `iter_config_files` for the ordering promise and
    `unusable_config_paths` for what `unusable` carries and why the two
    lists are disjoint (one `_is_regular_file` call per entry, whose
    answer alone picks the list).
    """
    root = Path(config_dir)
    if not root.is_dir():
        return ConfigTreeListing([], [], [])

    files: list[Path] = []
    found: list[Path] = []
    unscannable: list[Path] = []

    def _walk_error(err: OSError) -> None:
        # ⛔ `os.walk` defaults to onerror=None, which SWALLOWS a scandir
        # failure: an unreadable sub-directory drops out of the walk with
        # its whole subtree and no signal at all. Measured before this
        # callback existed — a conf.d with one chmod-000 sub-directory
        # holding a tenant file made `check_yaml_syntax` report
        # `status: pass` / `1 files parsed successfully` /
        # `unusable_files: []`. That is the #1911 shape ("a green light for
        # a directory it never read") one level further down, inside the
        # very list that exists to make such things audible.
        if err.filename is not None:
            unscannable.append(Path(err.filename))

    for dirpath, dirnames, filenames in os.walk(root, onerror=_walk_error):
        dirnames[:] = sorted(d for d in dirnames if not _is_hidden(d))
        # Directories too: a config-named DIRECTORY is the case that made
        # `unusable_config_paths` necessary, and `os.walk` never puts it in
        # filenames. ⛔ ONE `_is_regular_file` per entry decides which list
        # it joins: asking twice (once per list) doubled the stat cost of
        # every `iter_config_files` caller, and an entry swapped between the
        # two asks could land in BOTH lists or in NEITHER — breaking the
        # disjointness promised below.
        n_dirs = len(dirnames)
        for i, name in enumerate(list(dirnames) + list(filenames)):
            p = Path(dirpath) / name
            if not _is_config(name):
                # ⛔ A DIRECTORY SYMLINK, config-named or not (#1972).
                # `os.walk` (followlinks=False) leaves it in `dirnames` and
                # never descends, exactly as the exporter's walker does not.
                # kubelet projects a ConfigMap `items[].path` with a
                # sub-directory as ONE such link (`team-a -> ..data/team-a`),
                # so every tenant under it is lost; it used to be lost with
                # no signal here either. Hidden names were pruned above, so
                # the `..data` link of every ConfigMap volume never gets here.
                # Only `dirnames` entries pay the lstat: a file link in the
                # kubelet flat layout is in `filenames` and costs nothing more.
                # A link whose target this same walk reaches by itself
                # (`current -> team-b` beside a real `team-b/`) loses
                # nothing and is NOT listed — `dir_link_target_is_walked`.
                if (i < n_dirs and p.is_symlink()
                        and not dir_link_target_is_walked(root, p)):
                    found.append(p)
                continue
            (files if _is_regular_file(p) else found).append(p)
    files.sort(key=lambda q: q.relative_to(root).as_posix())
    # An unscannable directory is reported even when it is NOT config-named:
    # what it costs the caller is not one file but everything underneath it.
    found.extend(d for d in unscannable if d not in found)
    found.sort(key=lambda q: q.relative_to(root).as_posix()
               if q != root else "")
    return ConfigTreeListing(files, found, unscannable)


def is_dir_symlink(p: Path) -> bool:
    """A symlink whose target is a directory; False instead of raising.

    The shape `list_config_tree` names in `unusable` for #1972. A reader
    that filters `unusable` by NAME (`_`-prefixed entries it never reads)
    must not filter these, for the same reason it keeps `unscannable`: what
    is lost is a whole subtree, and the exporter descends `_`-prefixed
    directories (`_custom_alerts -> ..data/_custom_alerts`).
    """
    try:
        return p.is_symlink() and p.is_dir()
    except OSError:
        return False


def dir_link_target_is_walked(root: Path, link: Path) -> bool:
    """Does the walk from `root` reach `link`'s final target by itself?

    True when the fully resolved target is `root` or lies inside it with no
    `.`-prefixed segment on the way (those directories are pruned). Then
    the files under the link are loaded through their real path and nothing
    is lost, so naming the link as unusable would be a false alarm (#1972
    blind review F1: `alias/current -> team-b` made `validate_config` FAIL
    for a tenant that WAS loaded).

    Anything else — outside the root, through a hidden segment (kubelet's
    `team-a -> ..data/team-a` resolves into `..<ts>/`), unresolvable — is
    not walked.

    ⛔ Go twin: `pkg/config.dirLinkTargetIsWalked`. Same rule, or one plane
    names a loss the other calls clean.
    """
    try:
        real = Path(os.path.realpath(link))
        rel = real.relative_to(Path(os.path.realpath(root)))
    except (OSError, ValueError):
        return False
    return not any(part.startswith(".") for part in rel.parts)


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
        if p.is_symlink() and p.is_dir():
            # Asked BEFORE the directory branch (#1972): `is_dir()` follows
            # the link, so that branch would call a directory link "a
            # directory, not a config file" — true, and silent about the
            # part that matters: the exporter never looks inside it.
            return ("is a symlink to a directory — threshold-exporter does "
                    "not follow it, so the config files under it are NOT "
                    "loaded")
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


def unusable_config_entries(
    entries: "Iterable[Path]", *, suffixes: "tuple[str, ...]" = CONFIG_SUFFIXES,
) -> list[Path]:
    """The config-named entries among ALREADY-LISTED `entries` that a reader
    named them for cannot read, sorted by POSIX path.

    Sorted by the whole path, not the basename, so a RECURSIVE caller's two
    `beta.yaml` under different parents keep a stable, distinguishable order
    — the same promise `unusable_config_paths` makes for the tree it lists.

    Sibling of `unusable_config_paths` for callers that already hold the
    listing — the readers that walk a conf.d with their own `iterdir()` /
    `os.walk()` rather than through `iter_config_files`. Same question,
    same predicate, no second listing.

    ⛔ Takes the entries instead of listing, for the reason `defaults_files_in`
    does: a helper that lists is a flat scan with nothing said about nested
    content, and `test_confd_enumeration_contract` reddens `_lib_confd.py`
    itself for it. The caller has already listed and has already called
    `warn_nested`; doing either again here would either duplicate the
    warning or hide a scan from the gate that exists to find them.

    ⛔ `suffixes` mirrors what the CALLER reads, and defaults to both
    spellings only because that is `iter_config_files`'s answer. A reader
    that takes `(".yaml",)` must pass it, or this would name a `db-b.yml/`
    directory the caller was never going to read — a finding the operator
    cannot act on from that tool. (That the narrow readers exist at all is
    the separate `.yml` blind spot, #1603; this parameter keeps THIS report
    honest about the tool printing it rather than quietly widening it.)

    ⛔ Hidden entries are skipped, by the SAME `_is_hidden` the exporter's
    skip rule is mirrored from. Without it this function and
    `unusable_config_paths` returned two different answers for one tree — a
    directory named `.hidden.yaml/` was in this list and not in that one —
    which is the "one question, two answers" shape #1911 is made of,
    reproduced between two functions written for the same question. Blind
    review measured it. Downstream it was a false finding too: every reader
    skips `.`-prefixed entries whatever their shape, so naming one reports a
    loss that did not happen.

    ⚠️ DISJOINT from what the caller then reads, by SHARING
    `_is_regular_file` rather than by two conditions agreeing — the same
    reason `unusable_config_paths` gives at length. So an unreadable
    REGULAR file is deliberately NOT here: it is a file, the caller will
    `open()` it, and that failure carries the real errno.

    Measured (#1607): `operator_generate` on a tree with a directory
    `notes.yaml/` and a broken symlink `broken.yaml` emitted 20 CRDs before
    the `is_file()` filter existed — two of them AlertmanagerConfigs for
    tenants `notes` and `broken` invented from a directory and a dangling
    link — 18 after, with nothing said. Neither number is right on its own:
    the filter is the correctness half, this function is the half that
    keeps the signal.
    """
    return sorted(
        (p for p in entries
         if has_yaml_extension(p.name, suffixes)
         and not _is_hidden(p.name)
         and not _is_regular_file(p)),
        key=lambda q: q.as_posix(),
    )


def unusable_config_paths(
    config_dir: str | os.PathLike[str], *, recursive: bool = True,
) -> list[Path]:
    """Paths NAMED like a config file that are not a readable config file.

    (Plus any directory whose contents could not be enumerated — see the
    ⛔ note below; that one is NOT necessarily config-named, because what
    it costs the caller is a whole subtree rather than one file. The
    recursive branch likewise names every non-hidden DIRECTORY SYMLINK,
    whose subtree neither walker enters — #1972.)

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
    is that they answer different questions, not syscall count. A reader
    that needs both answers from ONE walk calls `list_config_tree` (#2054);
    the recursive branch here is its `unusable` half.
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

    return list_config_tree(root).unusable


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


def nested_yaml_files(config_dir: str | os.PathLike[str]) -> list[Path]:
    """Config files a FLAT reader would miss (i.e. below the top level)."""
    root = Path(config_dir)
    if not root.is_dir():
        return []
    return [
        p for p in iter_config_files(root)
        if len(p.relative_to(root).parts) > 1
    ]


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
    _DIR_LINK_WARNED.clear()


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

    * `warn_nested(d)`: recorded with every config file below the top level
      of `d` (the same set it prints), on every call — including a flat
      tree (nothing skipped) and repeats its print memo silences.
    * `resolve_defaults_file(d)`: recorded with only the nested DEFAULTS
      carriers (`is_defaults_name`), because locating the root carrier is
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


def warn_nested(config_dir: str | os.PathLike[str], *, tool: str | None = None) -> bool:
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
    """
    # Recorded BEFORE the flat-tree early return and the memo: an observer
    # asks "did this code path read `config_dir` flat?", and the answer is
    # yes on the second call too, when nothing is printed. The walk is only
    # paid when an observer is installed.
    if _FLAT_READ_SINKS.get():
        _record_flat_read(config_dir, nested_yaml_files(config_dir))
    return _print_nested_once(config_dir, tool=tool)


def _print_skipped_carriers(root: Path, *, tool: str | None, note: str) -> None:
    """`resolve_defaults_file(skipped_note=...)`'s stderr line: the nested
    defaults carriers the root lookup does not read, behind `note`."""
    missed = [p for p in nested_yaml_files(root) if is_defaults_name(p.name)]
    if not missed:
        return
    shown = ", ".join(printable_name(p.relative_to(root).as_posix())
                      for p in missed[:WARN_LIMIT])
    more = f" (+{len(missed) - WARN_LIMIT} more)" if len(missed) > WARN_LIMIT else ""
    print(f"WARN: {tool or _running_tool()}: {note} {len(missed)} defaults file(s) "
          f"in subdirectories: {shown}{more}", file=sys.stderr)


def _print_nested_once(config_dir: str | os.PathLike[str], *,
                       tool: str | None = None) -> bool:
    """The stderr half of `warn_nested`, with no `observe_flat_reads` record.

    ⛔ Private on purpose: the only caller outside `warn_nested` is
    `resolve_defaults_file`, which records a narrower skipped set itself.
    `test_flat_read_observation_pin` keeps it from being called elsewhere.
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

    * a tenant file parse-tree cannot read — every one is named with its
      kind; a file whose top level is not a mapping declares nothing and is
      not one of them;
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
    declared, unreadable = declared_tenants(reader)
    if unreadable:
        shown = "; ".join(
            f"{printable_name(u.file)} ({printable_name(u.kind)}: "
            f"{printable_name(' '.join(u.message.split()))})"
            for u in unreadable[:WARN_LIMIT])
        more = (f" (+{len(unreadable) - WARN_LIMIT} more)"
                if len(unreadable) > WARN_LIMIT else "")
        raise TenantSetUnknown(
            f"cannot tell which tenants {printable_name(os.fspath(config_dir))} "
            f"declares: da-guard parse-tree cannot read {len(unreadable)} "
            f"tenant file(s): {shown}{more}")
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


def declared_tenant_ids(config_dir: "str | os.PathLike[str]", reader: Any = None) -> set:
    """Tenant ids some TENANT file declares anywhere in the tree: the keys of
    the `tenants:` mapping of every non-`_` config file `da-guard parse-tree`
    can read.

    The ENUMERATION is the exporter walker's (`pkg/config/tree_scan.go`):
    recurse, skip dot-prefixed entries, never read a `_`-prefixed file for
    tenants — `iter_config_files` + `is_reserved_name`. So a flat reader can
    ask "does this tenant exist" with the tree the exporter sees: a tenant
    declared only in `team-a/tx.yaml` exists even for a reader that routes
    only the root.

    The CONTENT is parse-tree's (ADR-036 step 4, #2906; *reader* is a
    `_lib_parse_cache.ConfdReader` of *config_dir*, the session's when None):
    the ids are the keys' source TEXT, as yaml.v3 keys the exporter's
    ``map[string]`` — ``010:`` is ``"010"``, ``yes:`` is ``"yes"``, and
    ``123:`` / ``"123":`` are one id, which parse-tree refuses as a duplicate.
    A file parse-tree cannot read (``unreadable``: several documents, a
    duplicate key, a syntax error, …) declares nothing here; naming it is the
    calling reader's own job. A file the exporter's full decode rejects but
    parse-tree reads (a tenant body that is not a mapping, a `defaults:` of
    the wrong shape) still declares its tenants here — mirroring the Go
    decoder would be an unbounded differential (#1942).

    Raises `_lib_parse_tree.ParseTreeError` (no da-guard, a failing one) —
    never an empty set for a tree that was not read.
    """
    from _lib_parse_tree import FileUnreadable
    reader = _reader_of(config_dir, reader)
    ids: set[str] = set()
    for path in iter_config_files(config_dir, recursive=True):
        if is_reserved_name(path.name):
            continue
        try:
            data = reader.read(path)
        except FileUnreadable:
            continue  # declares nothing; see docstring
        if isinstance(data.get("tenants"), dict):
            # Keys are the source text; the overlay readers key their
            # tenants the same way, so membership compares text to text.
            ids.update(data["tenants"])
    return ids


def tenant_declarations(config_dir: "str | os.PathLike[str]", reader: Any = None
                        ) -> "tuple[dict[str, set[str]], list[str]]":
    """``({tenant_id: {label, ...}}, unreadable)`` over the whole tree.

    ⛔ THE one answer to "which files declare which tenant" — the scan
    ``validate_config.check_tenant_uniqueness`` runs and the routing
    generator's duplicate refusal reuses (#2315), so the two cannot disagree
    about a duplicate. Feed the first element to ``duplicate_declarations``.

    Selection mirrors the exporter's walker: ``iter_config_files`` (recursive,
    case-insensitive, both spellings) and ``is_reserved_name`` (a ``_`` file
    is never read for tenants). A declaration is a key of a mapping-shaped
    ``tenants:`` in a file `da-guard parse-tree` reads (*reader*, as for
    ``declared_tenant_ids``), keyed by its source text. A label is the
    root-relative POSIX path (the file name when it is not under the root).
    *unreadable* lists the labels of the files parse-tree could not read, in
    walk order — a limit on the answer, which the caller must name — but a
    file whose top level is not a mapping, which declares nothing.
    Raises `_lib_parse_tree.ParseTreeError` as ``declared_tenant_ids`` does.
    """
    from _lib_parse_tree import FileUnreadable
    reader = _reader_of(config_dir, reader)
    root = Path(config_dir)
    declared: dict[str, set[str]] = {}
    unreadable: list[str] = []
    for path in iter_config_files(root):
        if is_reserved_name(path.name):
            continue
        try:
            label = path.relative_to(root).as_posix()
        except ValueError:
            label = path.name
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
    decided Python notion of "declares" (see ``declared_tenant_ids``: the
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
) -> "tuple[dict[str, dict], list[tuple[str, str]]]":
    """Merge per-tenant blocks read from ROOT files: platform first, tenant wins.

    *entries* is ``(fname, tenant, overrides)`` for every tenant entry a
    reader took from a root file's ``tenants:`` block, in read order. A
    `_`-prefixed *fname* is a PLATFORM file: its block is the platform's
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
    platform: list[tuple[str, str, dict]] = []
    owned: list[tuple[str, str, dict]] = []
    for fname, tenant, overrides in entries:
        (platform if is_reserved_name(os.path.basename(fname)) else owned
         ).append((fname, tenant, overrides))
    orphans: list[tuple[str, str]] = []
    merged: dict[str, dict] = {}
    known: "set[str] | None" = None
    for fname, tenant, overrides in platform:
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


def legacy_state_files(config_dir: "str | os.PathLike[str]") -> list[str]:
    """The pre-#2856 state record files still sitting in *config_dir*."""
    base = Path(config_dir)
    return [n for n in LEGACY_STATE_FILES if (base / n).is_file()]
