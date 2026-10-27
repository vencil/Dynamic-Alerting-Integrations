#!/usr/bin/env python3
"""Sharded GitOps Assembly Tool — merge multiple conf.d/ sources into one config-dir.

Enables domain teams to maintain independent Git repos (or directories),
each containing their own conf.d/ tenant configurations.  This tool merges
them into a single config-dir that threshold-exporter reads.

Usage:
    assemble_config_dir.py --sources team-a/conf.d,team-b/conf.d --output build/config-dir
    assemble_config_dir.py --sources team-a/conf.d,team-b/conf.d --check   # dry-run conflict check
    assemble_config_dir.py --sources ... --output ... --manifest out.json   # assemble + save manifest

Exit codes:
    0  success
    1  conflict detected — one carrier name claimed by two sources (this
       tool's own policy; only one copy could ever ship), or one tenant id
       declared by two carriers (the exporter rejects the WHOLE config-dir in
       that state). Nothing is written in either case.
    2  a source or the output holds a path da-guard validation names (the
       shared conf.d refusal, #3206), the duplicate-tenant question could
       not be answered, or the flags contradict each other (`--check` with
       `--manifest`, `--manifest` without `--sources`) — refused before
       anything is read or written. `--validate` was removed (#3206): every
       file it called an ERROR is refused before assembly.
"""

import argparse
import hashlib
import os
import shutil
import stat
import sys
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Tuple

# Pull `try_utf8_stdout` from the shared compat lib at scripts/tools/.
# Migrated in #489 Phase B (was missing encoding setup → would crash on
# legacy Windows cp950/cp936 consoles when printing emoji to stdout).
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, str(_THIS_DIR))
sys.path.insert(0, os.path.join(str(_THIS_DIR), ".."))
from _lib_compat import try_utf8_stdout  # noqa: E402
from _lib_exitcodes import (  # noqa: E402
    EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR, arm_crash_envelope,
)
from _lib_python import format_json_report  # noqa: E402
from _lib_confd import (  # noqa: E402
    config_listing,
    iter_config_files,
    list_config_tree,
    warn_nested,
)
from _lib_parse_tree import classify_names, names_carrier  # noqa: E402  (#3206: Go judges names)
from _lib_io import exit_on_output_write_error, output_write  # noqa: E402  (#1789)
from _lib_io import claim_stdout, write_operator_output  # noqa: E402  (#3107)
import _lib_parse_cache  # noqa: E402  (ADR-036 step 4: the artifact's reader)
import _lib_tenant_uniqueness as tu  # noqa: E402


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent.parent

# Platform-owned carriers that are merged specially (first source wins):
# the defaults carrier and the profiles file, in any spelling.
#
# ⛔ Which names those are is Go's answer (#3206), not a name rule here. A
# defaults carrier is Go's class `defaults`; the profiles file is a platform
# (`_`) file — Go's class `platform`, which already says the name carries a
# YAML extension — whose stem is `_profiles`, any case. Typing the set out as
# two literal `.yaml` names once let `_defaults.yml` / `_defaults.YAML` /
# `_profiles.yml` fall through to the tenant path, where two sources each
# holding one of them were reported as `❌ 1 tenant conflict(s)` — a platform
# file, named a tenant, on the wrong policy (measured, #1794).
PROFILES_STEM = "_profiles"


def _classes(names: "List[str]") -> Dict[str, str]:
    """Go's class of each base name (names mode, `classify_names`)."""
    return {n: e.cls for n, e in classify_names(names).items()} if names else {}


def is_platform_file(name: str, cls: "str | None" = None) -> bool:
    """Is `name` a carrier for one of the platform-owned roles, any spelling?

    *cls*: Go's class of the name when the caller holds its entry, else the
    name's in names mode (`classify_names`). The defaults half IS Go's class
    `defaults` — there is no second opinion here to pin against it.
    """
    if cls is None:
        cls = _classes([name])[name]
    return cls == "defaults" or (
        cls == "platform" and PurePosixPath(name).stem.lower() == PROFILES_STEM)


# ── Source discovery ─────────────────────────────────────────────────

def list_visible_config_entries(dir_path: Path, listing: Any = None) -> List[Path]:
    """Config-NAMED entries of `dir_path`, flat, minus the hidden ones —
    each of which is named on stderr before it is left out.

    ⛔ The ONE listing behind this tool's scan sites (`discover_yamls`, the
    sources; until #3206 also `--validate`, the artifact). #1827 was fixed at
    the first site and blind review found the second still validating a
    `.`-prefixed carrier the exporter never reads — refusing a deploy (rc 2)
    over a file that is not part of it. Two readers, one question, two
    answers: the shape the conf.d family (#1911) is made of, reproduced
    inside a single file. So the listing, the hidden skip and its wording
    live here once; a third scan site calls this, it does not copy it.

    #3206: the entries are the exporter's own walk — *listing*, da-guard's
    top level — so which names it reads is Go's rule, not one of this
    tool's. Hidden entries are NAMED, not silently dropped: excluding
    without saying so would trade a visible error for a quiet loss; the
    walk does not list them, so the WARN alone asks Go (names mode,
    `classify_names`) about this directory's own names — class `hidden`, and
    a config file's extension (wording only: it selects nothing).

    `warn_nested` is called HERE, in the same scope as the `iterdir()`,
    because `test_confd_enumeration_contract` requires the guard to live
    where the flat scan lives. Callers must not call it again for the same
    directory (it de-duplicates, but a second call reads as a second scan).

    Returns every entry the walk lists at this level (any role); whether
    each is a config file is the caller's question, asked of *listing* — the
    caller's snapshot of `dir_path` (`config_listing(dir_path)` when None),
    the one the nested warning reads too (#3206).
    """
    if listing is None:
        listing = config_listing(dir_path)
    # #1911: flat by design here — but a hierarchical conf.d must not
    # look like an empty one. Name the files this scan cannot see.
    warn_nested(dir_path, tool="assemble_config_dir", listing=listing)
    # ⚠️ WORDING ONLY: this loop selects nothing (the entries are the
    # listing's); a directory this uid cannot list just has no hidden-entry
    # WARN — its own failure is the listing's to report.
    try:
        own = sorted(dir_path.iterdir())
    except OSError:
        own = []
    for p in _hidden_config_named(own):
        print(f"WARN: {p} is hidden (`.`-prefixed) — skipped, the exporter "
              f"does not read it", file=sys.stderr)
    return [] if listing is None else [dir_path / e.path for e in listing.entries
                                       if e.depth == 0]


def _utf8(name: str) -> bool:
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _hidden_config_named(paths: List[Path]) -> List[Path]:
    """The *paths* whose name Go classes `hidden` and that carry a config
    file's extension (the entry's `config_extension`, names mode)."""
    # A name JSON cannot carry (not UTF-8) cannot be asked; its WARN is
    # wording only, and the listing names what it reads.
    asked = [p for p in paths if _utf8(p.name)]
    if not asked:
        return []
    named = classify_names([p.name for p in asked])
    return [p for p in asked
            if named[p.name].cls == "hidden" and named[p.name].config_extension]


def discover_yamls(source_dir: Path) -> List[Path]:
    """List every YAML carrier in a source directory (non-recursive).

    `.yaml` / `.yml`, any case — Go's `role` (the source's listing), the
    exporter's own rule. #1603 (extension-SPELLING axis): this was
    `glob("*.yaml")`, so a `.yml` shard was silently LEFT OUT of the
    assembled config-dir — measured on byte-identical bodies before the
    fix: shard `alpha.yaml` → discovered; `alpha.yml` / `Alpha.YAML` → not,
    the same answer as an empty shard. Blast radius of widening: a `.yml`
    shard that used to be dropped is now merged (and can now collide by
    name with a same-named `.yml` in another shard, which `detect_conflicts`
    reports as before).

    Hidden (`.`-prefixed) entries are EXCLUDED — the exporter's walk does not
    list them (Go's skip rule) — and each one is NAMED on stderr (#1911
    family, #1827). Before that filter this tool copied `.db-c.yaml` into the
    artifact and counted it in the manifest while the exporter never read it:
    the tenant had no alerts at rc 0 with nothing printed. Excluding without
    naming would trade that visible error for a quiet loss, so the entry is
    listed, not dropped. (The earlier deferral here pointed at #1630, a closed
    ticket that never covered this tool.)

    ⚠️ A hidden DIRECTORY (`.draft/db-d.yaml`) is a different case: this scan
    is flat, so it never sees inside it, and `warn_nested` does not name it
    either — both by Go's walk, whose `SkipDir` rule it is. Not seen, not
    reported, on every side: that is parity, not a gap, and it is guaranteed
    by that one walk rather than by two readers happening to agree.

    Raises FileNotFoundError if directory does not exist.
    """
    if not source_dir.is_dir():
        raise FileNotFoundError(f"source directory not found: {source_dir}")
    # ⛔ Same listing as `iter_config_files` (which `carriers_already_in`
    # and the residue question already use), not a second spelling of it:
    # a hidden carrier the exporter will never read must not reach the copy,
    # the count or the manifest. Named first, then excluded, so the operator
    # learns WHICH tenant just lost its carrier instead of finding out from
    # a missing alert. The listing (and the nested-config guard) is the
    # shared one.
    #
    # ⛔ #3206: taking the listing is the run's fail-closed exit
    # (`_lib_parse_cache.ConfdReader.tree`): a source Go names paths on — a
    # shard's broken symlink, a directory called `subteam.yaml`, a source it
    # cannot list, a file the exporter cannot decode — ends the run there,
    # every such path named, exit 2, before anything is staged or written.
    # (These were a WARN and "not merged": the generator refuses such a
    # tree, so the artifact would not have been served either.)
    return [p for p, _e in discover_entries(source_dir)[0]]


def discover_entries(source_dir: Path) -> "Tuple[List[Tuple[Path, Any]], str | None]":
    """`discover_yamls` with each carrier's entry (Go's `role` / `class`),
    and the root carrier the source's own chain selects (Go's
    `defaults_carriers`, None when it has none). Raises FileNotFoundError as
    `discover_yamls`."""
    if not source_dir.is_dir():
        raise FileNotFoundError(f"source directory not found: {source_dir}")
    listing = config_listing(source_dir)
    named = list_visible_config_entries(source_dir, listing)
    selected = None if listing is None else listing.defaults_carriers.get(".")
    # A config file is what Go's `role` says is one — not `is_file()`, which
    # answers False for a file it may not stat and so dropped a tenant with
    # no word.
    out = []
    for p in named:
        e = None if listing is None else listing.entry(p.name)
        if e is not None and e.config_file:
            out.append((p, e))
    return out, selected


def _file_sha256(path: Path) -> str:
    """Compute SHA-256 hex digest of a file."""
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


# ── Conflict detection ───────────────────────────────────────────────

def detect_conflicts(
    sources: List[Path],
) -> Tuple[Dict[str, List[Tuple[str, Path]]], Dict[str, Path]]:
    """Scan sources for tenant file conflicts.

    Returns:
        (conflicts, file_map)
        conflicts: {filename: [(source_label, path), ...]} for duplicates
        file_map:  {filename: first_path} for non-conflicting files
    """
    seen: Dict[str, List[Tuple[str, Path]]] = {}
    # (label, path) of every defaults carrier, in source order (#1674).
    carriers: List[Tuple[str, Path]] = []
    classes: Dict[str, str] = {}
    winners: Dict[str, "str | None"] = {}
    for src in sources:
        label = str(src)
        found, winners[label] = discover_entries(src)
        for f, e in found:
            name = f.name
            classes[name] = e.cls
            seen.setdefault(name, []).append((label, f))
            if e.cls == "defaults":  # Go's class: the chain carrier
                carriers.append((label, f))

    conflicts: Dict[str, List[Tuple[str, Path]]] = {}
    file_map: Dict[str, Path] = {}

    for name, entries in seen.items():
        if is_platform_file(name, classes[name]):
            # Platform files: first source wins, warn if multiple
            file_map[name] = entries[0][1]
            if len(entries) > 1:
                conflicts[name] = entries  # still report as conflict
            continue

        if len(entries) > 1:
            # Check if files are identical (same SHA-256)
            hashes = {_file_sha256(p) for _, p in entries}
            if len(hashes) == 1:
                # Identical content — not a real conflict, take first
                file_map[name] = entries[0][1]
            else:
                conflicts[name] = entries
        else:
            file_map[name] = entries[0][1]

    # ⛔ #1674: two carrier SPELLINGS are one platform role. The output is one
    # flat directory, and the exporter reads exactly ONE carrier per directory
    # (`.yaml` over `.yml`, any casing) — so `a/_defaults.yaml` +
    # `b/_defaults.yml` used to assemble silently (rc 0) into a directory
    # whose `b` defaults every plane then ignores. It is the same duplicate
    # the identical-name branch above reports, with the same policy: the
    # first source holding a carrier wins (its own carrier by Go's selection,
    # `defaults_carriers`, if it holds several), the rest are reported and not
    # copied.
    if len({p.name for _, p in carriers}) > 1:
        first_label = carriers[0][0]
        chosen = winners.get(first_label)
        winner = next(p for lbl, p in carriers
                      if lbl == first_label and (chosen is None or p.name == chosen))
        for _, p in carriers:
            file_map.pop(p.name, None)
            conflicts.pop(p.name, None)
        file_map[winner.name] = winner
        conflicts[winner.name] = carriers

    return conflicts, file_map


def carriers_already_in(output_dir: Path, listing: Any = None) -> Dict[str, Path]:
    """Config carriers the exporter would read in `output_dir` right now.

    ⛔ `assemble()` never clears `--output`, and `make sharded-assemble`
    hardcodes it to `.build/config-dir`, so a previous run's carrier is still
    there and the exporter reads it too. Dropping this function puts #1794's
    own symptom back by that route.

    ⛔ RECURSIVE, through the shared enumerator: the exporter walks this
    directory, so a flat scan under-reports what it loads from a subdirectory
    (`test_confd_enumeration_contract` reddens a silent flat scan).
    `iter_config_files` is also where the `.`-prefix skip, both spellings, the
    case fold and the regular-file check come from — one predicate, not a copy.

    Keys are POSIX-relative paths, not basenames: flattening two same-named
    carriers from different subdirectories drops one out of the question.
    *listing*: the caller's snapshot of `output_dir`, when it holds one.
    """
    if not output_dir.is_dir():
        return {}
    return {p.relative_to(output_dir).as_posix(): p
            for p in iter_config_files(output_dir, listing=listing)}


# ── Assembly ─────────────────────────────────────────────────────────

def assemble(
    file_map: Dict[str, Path],
    output_dir: Path,
    *,
    dry_run: bool = False,
) -> int:
    """Copy files from file_map into output_dir.

    Returns number of files written.
    """
    # #1789: `output_dir` reaches here from `--output` only — main is the
    # sole caller — so the flag is named literally rather than threaded
    # through a parameter no other caller would ever set.
    if not dry_run:
        with output_write(output_dir, flag="--output",
                          action="create directory"):
            output_dir.mkdir(parents=True, exist_ok=True)

    count = 0
    for name in sorted(file_map):
        src = file_map[name]
        dst = output_dir / name
        if dry_run:
            print(f"  {name:40s} ← {src}")
        else:
            # ⚠️ Only the DESTINATION side is converted. `shutil.copy2` reads
            # `src`, which comes from `--sources`, and a source ENTRY that is
            # itself a directory named `x.yaml` raises `IsADirectoryError`
            # naming the SOURCE — `output_write` lets that fly through
            # unchanged rather than telling the operator to check `--output`,
            # the one flag that is correct (measured; pinned by
            # `test_a_bad_source_entry_does_not_blame_the_output_flag`).
            with output_write(dst, flag="--output", action="copy into"):
                shutil.copy2(src, dst)
                os.chmod(dst,
                         stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP
                         | stat.S_IROTH)
        count += 1

    return count


# ── Manifest ─────────────────────────────────────────────────────────

def build_manifest(
    sources: List[Path],
    file_map: Dict[str, Path],
    conflicts: Dict[str, List[Tuple[str, Path]]],
) -> dict:
    """Build a JSON manifest of the assembly."""
    files = {}
    for name, path in sorted(file_map.items()):
        files[name] = {
            "source": str(path),
            "sha256": _file_sha256(path),
        }
    return {
        "sources": [str(s) for s in sources],
        "file_count": len(file_map),
        "conflicts": {
            name: [{"source": lbl, "path": str(p)} for lbl, p in entries]
            for name, entries in conflicts.items()
        },
        "files": files,
    }


# ── Main ─────────────────────────────────────────────────────────────

@exit_on_output_write_error
@_lib_parse_cache.exit_on_parse_tree_error  # #3206: the listing is da-guard's — rc 2, named
def main() -> int:
    """CLI entry point: merge multiple conf.d/ sources into one config-dir."""
    try_utf8_stdout()
    parser = argparse.ArgumentParser(
        description="Merge multiple conf.d/ sources into a single config-dir.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--sources", type=str, default="",
        help="Comma-separated list of source directories",
    )
    parser.add_argument(
        "--output", type=str, default="",
        help="Output config-dir path",
    )
    parser.add_argument(
        "--check", action="store_true",
        help="Dry-run: detect conflicts without writing files",
    )
    parser.add_argument(
        "--manifest", type=str, default="",
        help="Path to save the assembly manifest JSON (needs --sources; "
             "not with --check). There is no load mode.",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Output results as JSON",
    )
    args = parser.parse_args()
    if args.json:
        arm_crash_envelope()  # #3094: a crash in --json mode still prints one envelope

    def reject(reason: str, message: str) -> int:
        """Contradictory or incomplete flags: exit 2 (dev-rules #13).

        Not `parser.error`: under `--json` stdout still has to carry exactly
        one document, so the rejection is answered there too.
        """
        if args.json:
            print(format_json_report({"status": "caller_error",
                                      "reason": reason}))
        parser.print_usage(sys.stderr)
        print(f"{parser.prog}: error: {message}", file=sys.stderr)
        return EXIT_CALLER_ERROR

    # ⛔ `--check` returns before anything is assembled, so a flag that acts
    # on the assembled output is never reached under it. Accepting the pair
    # turned `--check --manifest` into rc 0 with no manifest written (#1828).
    if args.check and args.manifest:
        return reject("check_with_manifest",
                      "--manifest records an assembly and --check assembles "
                      "nothing; drop --check and pass --output to write it")

    # `--manifest` only saves; there is no load path. Accepting it alone ran
    # the assembly over zero sources and overwrote the manifest (#1829).
    if not args.sources:
        if args.manifest:
            return reject("manifest_without_sources",
                          "--sources is required (--manifest only saves a "
                          "manifest; it cannot load one)")
        return reject("sources_required", "--sources is required")
    if not args.check and not args.output:
        return reject("output_required",
                      "--output is required for assembly (or use --check)")

    sources: List[Path] = [Path(s.strip()).resolve()
                           for s in args.sources.split(",") if s.strip()]
    # Resolved early: the duplicate-tenant question below has to include what
    # is already sitting in this directory, and that question is asked before
    # `--check` returns.
    output_dir = Path(args.output).resolve() if args.output else None

    # Detect conflicts
    try:
        conflicts, file_map = detect_conflicts(sources)
    except FileNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return EXIT_CALLER_ERROR

    # Report conflicts
    conflict_classes = _classes(list(conflicts))
    real_conflicts = {
        k: v for k, v in conflicts.items()
        if not is_platform_file(k, conflict_classes[k])
    }
    platform_dups = {
        k: v for k, v in conflicts.items()
        if is_platform_file(k, conflict_classes[k])
    }

    if not args.json:
        if platform_dups:
            print("⚠️  Platform file duplicates (first source wins):")
            for name, entries in platform_dups.items():
                for lbl, p in entries:
                    # p.name, not the key: a carrier-spelling duplicate
                    # (#1674) lists files whose names differ from the winner.
                    print(f"   {p.name} ← {lbl}")

        if real_conflicts:
            print(f"\n❌ {len(real_conflicts)} tenant conflict(s):")
            for name, entries in real_conflicts.items():
                print(f"   {name}:")
                for lbl, p in entries:
                    sha = _file_sha256(p)[:12]
                    print(f"     {lbl} (sha256:{sha})")

    if real_conflicts:
        if args.json:
            print(format_json_report({
                "status": "conflict",
                "conflicts": {
                    n: [{"source": l, "sha256": _file_sha256(p)}
                        for l, p in entries]
                    for n, entries in real_conflicts.items()
                },
            }))
        return EXIT_VIOLATION

    # ⛔ Same call the sibling producer makes for a carrier-less
    # `--config-dir`, for its stated reason: a mis-pointed path far more often
    # than an intentional empty deploy, and the silent version writes a
    # config-dir the exporter reads as "no tenants".
    if not file_map:
        if not sources:
            # `--sources ","` — a value that names no directory. Blaming the
            # carriers there points at the wrong thing.
            print(f"ERROR: --sources names no directory: {args.sources!r}",
                  file=sys.stderr)
        else:
            print(f"ERROR: no config carrier found in "
                  f"{', '.join(str(s) for s in sources)}"
                  f" — assembling this would write an empty config-dir, "
                  f"which the exporter reads as a platform with no tenants.",
                  file=sys.stderr)
        if args.json:
            print(format_json_report({"status": "no_carriers",
                                      "sources": [str(s) for s in sources]}))
        return EXIT_VIOLATION

    # ⛔ The filename question above is not the exporter's question: it
    # rejects on "one tenant id, two files" and never looks at the names
    # (`config_hierarchy.go` compares ids only). Two carriers that share no
    # name still take the whole directory down.
    #
    # Asked about the ARTIFACT, not the sources — a declaration in a source
    # file that lost a name collision never ships, and refusing over it would
    # block a deployable output. Asked BEFORE `--check` returns and before
    # anything is written, so both entry points answer it and no rejected tree
    # is left behind.
    # What the exporter will read after this run: this assembly's carriers,
    # plus anything already in the output directory that this run will not
    # overwrite. `file_map` wins on a name collision because the copy does.
    #
    # ⚠️ `--check` without `--output` cannot see that second half, and says so
    # below rather than implying it answered for the artifact.
    # #3206: the output directory is read through the run's index, whose
    # taking is the fail-closed exit (`_lib_parse_cache.ConfdReader.tree`): a
    # subtree Go could not list ("could not measure"), a dangling link or a
    # directory with a config file's name there ends the run, every such
    # path named, exit 2 — before anything is written. (They were a WARN, or
    # the UNMEASURED verdict, here.)
    out_tree = None
    if output_dir is not None:
        out_tree = list_config_tree(output_dir)

    preexisting = (carriers_already_in(output_dir, out_tree.listing)
                   if output_dir and out_tree is not None else {})
    residue = {n: p for n, p in preexisting.items() if n not in file_map}

    # ⛔ #1674: a LEFTOVER root carrier beside the carrier this run produces is
    # the cross-spelling duplicate `detect_conflicts` refuses among sources,
    # reached through --output instead. The exporter reads exactly ONE
    # carrier per directory (`.yaml` over `.yml`, any casing), so
    # `a/_defaults.yml` + a stale `out/_defaults.yaml` shipped the STALE file
    # and ignored the produced one everywhere, at rc 0 — under a warning that
    # said the exporter "reads them too". Same remedy as a leftover that
    # duplicates a tenant (below): refuse, name it, say how to clear it.
    # Which names are carriers, and which one the exporter reads, are Go's
    # (#3206): the class of the produced names, the output listing's entries,
    # and Go's selection over the names the copy would leave there (names
    # mode: the directory does not exist yet).
    produced_classes = _classes(list(file_map))
    produced_carriers = sorted(n for n in file_map if produced_classes[n] == "defaults")
    out_entries = (out_tree.listing.config_files()
                   if out_tree is not None and out_tree.listing is not None else [])
    stale_carriers = sorted(e.path for e in out_entries
                            if "/" not in e.path and e.cls == "defaults" and e.path in residue)
    if produced_carriers and stale_carriers:
        read = names_carrier(produced_carriers + stale_carriers)
        if args.json:
            print(format_json_report({
                "status": "conflict",
                "conflicts": {
                    "defaults carrier": {"produced": produced_carriers,
                                         "left_over": stale_carriers,
                                         "exporter_reads": read},
                },
            }))
        else:
            print(f"\n❌ {output_dir} already holds defaults carrier(s) "
                  f"{', '.join(stale_carriers)} that no source produces, beside "
                  f"{', '.join(produced_carriers)} from this assembly. The "
                  f"exporter reads exactly one carrier per directory — it "
                  f"would read {read} and ignore the other everywhere. "
                  f"Remove the leftover (or point --output at a clean "
                  f"directory) and re-run; editing the sources cannot reach "
                  f"it.", file=sys.stderr)
        return EXIT_VIOLATION

    if residue and not args.json:
        # ⛔ The machine-readable face is the one a CI parses, and it used to
        # get a plain `"status": "ok"` for a directory holding carriers this
        # run did not produce. That is fixed in the JSON document below, not
        # here — duplicating this line into `--json` mode was measured to buy
        # no detection at all (the mutation removing it kills nothing).
        print(f"\n⚠️  {len(residue)} carrier(s) already in {output_dir} are not "
              f"produced by this assembly and are still read by the exporter "
              f"wherever they are a directory's config (this tool never "
              f"clears --output): {', '.join(sorted(residue))}",
              file=sys.stderr)

    verdict = tu.verdict_for({**residue, **file_map})
    if verdict.outcome != tu.CLEAN:
        if args.json:
            print(format_json_report({
                "status": verdict.outcome,
                "check": tu.CHECK_NAME,
                "reason": verdict.reason,
                "details": verdict.details,
            }))
        elif verdict.outcome == tu.DUPLICATE:
            print(f"\n❌ {tu.CHECK_NAME} failed — refusing to assemble a "
                  f"config-dir the exporter would reject in full.",
                  file=sys.stderr)
            for line in verdict.details:
                print(f"   {line}", file=sys.stderr)
            # ⛔ The check's own advice is "decide which file owns the tenant
            # and remove the other". That is right when both files are yours
            # to edit; it is actively harmful when one of them is a LEFTOVER,
            # because the operator cannot edit a file no source contains — and
            # following it was measured to end at rc 0 with that leftover still
            # shipping and their rename silently undone. Say what applies here.
            # ⛔ EXACT labels. The check writes them as a comma-separated
            # list inside a sentence, so a substring test blames any leftover
            # whose name happens to sit inside another label — `db-a.yaml` is
            # inside `legacy-db-a.yaml`, both of which are names this repo
            # actually uses. Measured: an unrelated leftover was blamed for a
            # duplicate that lived entirely between two SOURCE files, and the
            # `elif` then swallowed the one piece of advice that did apply,
            # making this WORSE than forwarding unconditionally.
            labelled = {tok for line in verdict.details
                        for tok in line.replace(",", " ").split()}
            blamed = [n for n in residue if n in labelled]
            if blamed:
                print(f"   -> {', '.join(sorted(blamed))} is in {output_dir} "
                      f"but no source produces it — it is left over from an "
                      f"earlier run. Remove it (or point --output at a clean "
                      f"directory) and re-run; editing the sources cannot "
                      f"reach it.", file=sys.stderr)
            elif verdict.action:
                print(f"   -> {verdict.action}", file=sys.stderr)
        else:
            print(f"\n❌ {verdict.reason}", file=sys.stderr)
            for line in verdict.details:
                print(f"   {line}", file=sys.stderr)
            print("   This is 'could not measure', NOT 'measured clean'.",
                  file=sys.stderr)
        return tu.EXIT_FOR[verdict.outcome]

    # --check: just report, don't write
    if args.check:
        if not args.json:
            print(f"\n✅ No conflicts. {len(file_map)} file(s) ready "
                  f"to assemble from {len(sources)} source(s).")
            for name in sorted(file_map):
                print(f"   {name}")
            if output_dir is None:
                print("   (answered for these sources only — pass --output to "
                      "include what is already in the target directory, which "
                      "the exporter reads too)")
        else:
            print(format_json_report({
                "status": "ok",
                "file_count": len(file_map),
                "files": sorted(file_map.keys()),
            }))
        return EXIT_OK

    # Assemble (`--output` presence was settled before anything was read)
    count = assemble(file_map, output_dir)

    # Manifest
    if args.manifest:
        manifest = build_manifest(sources, file_map, conflicts)
        # The manifest is a record of what will be READ, not only of what was
        # copied; without this a retired shard is absent from every face.
        if residue:
            manifest["residue"] = sorted(residue)
        # #3107 v2: an operator output — the kernel opens it; `-` is stdout,
        # which the assembly report already uses.
        claim_stdout("the assembly report")
        write_operator_output(args.manifest, format_json_report(manifest) + "\n",
                              flag="--manifest", mode=0o644)

    # Output
    if args.json:
        result = {
            "status": "ok",
            "output": str(output_dir),
            "file_count": count,
            "sources": [str(s) for s in sources],
        }
        # ⛔ `file_count` counts what this run COPIED. The exporter reads the
        # directory, so when the two differ the difference has to be on the
        # record — a retired shard stays in the artifact and keeps alerting.
        if residue:
            result["residue"] = sorted(residue)
            # ⛔ Re-derived from the directory, not `count + len(residue)`:
            # a carrier already sitting in `--output` is counted by the walk
            # (`iter_config_files`), not by this run's copy, so the sum would
            # be two half-answers glued together. This runs after the copy, so
            # it IS the artifact. (The sum once also mixed two predicates —
            # `count` included hidden entries before #1827; it no longer does,
            # and the re-derivation is what keeps that from mattering.)
            result["artifact_file_count"] = len(carriers_already_in(output_dir))
        print(format_json_report(result))
    else:
        print(f"\n✅ Assembled {count} file(s) into {output_dir}")
        if residue:
            print(f"   ⚠️  {output_dir} holds "
                  f"{len(carriers_already_in(output_dir))} carrier(s) the "
                  f"exporter will read — {len(residue)} of them left over "
                  f"from an earlier run (listed above).")

    return EXIT_OK


if __name__ == "__main__":
    import _lib_exitcodes  # noqa: E402
    _lib_exitcodes.run(main)
