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
    2  validation error (malformed YAML), or the duplicate-tenant question
       could not be answered
"""

import argparse
import hashlib
import os
import shutil
import stat
import sys
from pathlib import Path
from typing import Dict, List, Tuple

# Pull `try_utf8_stdout` from the shared compat lib at scripts/tools/.
# Migrated in #489 Phase B (was missing encoding setup → would crash on
# legacy Windows cp950/cp936 consoles when printing emoji to stdout).
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, str(_THIS_DIR))
sys.path.insert(0, os.path.join(str(_THIS_DIR), ".."))
from _lib_compat import try_utf8_stdout  # noqa: E402
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR  # noqa: E402
from _lib_python import format_json_report  # noqa: E402
from _lib_confd import (  # noqa: E402
    CONFIG_SUFFIXES,
    has_yaml_extension,
    iter_config_files,
    unusable_config_entries,
    unusable_config_paths,
    unusable_reason,
    warn_nested,
)
from _lib_io import exit_on_output_write_error, output_write  # noqa: E402  (#1789)
import _lib_tenant_uniqueness as tu  # noqa: E402

# Not optional: `_lib_python` -> `_lib_io` imports PyYAML at module scope, so
# this module cannot load without it. The `try: import yaml / except
# ImportError: yaml = None` fallback that used to live here, and the
# "SKIP: PyYAML not installed" branch it fed, were unreachable (measured by
# shadowing the module: the import fails earlier in this same import block,
# inside `_lib_python` -> `_lib_io`).
import yaml  # noqa: E402

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent.parent

# Platform-owned carriers that are merged specially (first source wins).
#
# ⛔ Derived, not typed out. The two ROLES are a domain fact — the exporter
# names the same two — but which SPELLINGS carry a role is not: that is
# `CONFIG_SUFFIXES` plus case folding. Typing the set out as two literal
# `.yaml` names let `_defaults.yml` / `_defaults.YAML` / `_profiles.yml` fall
# through to the tenant path, where two sources each holding one of them were
# reported as `❌ 1 tenant conflict(s)` — a platform file, named a tenant, on
# the wrong policy (measured, #1794).
PLATFORM_FILES = frozenset(
    f"{stem}{suffix}"
    for stem in ("_defaults", "_profiles")
    for suffix in CONFIG_SUFFIXES
)


def is_platform_file(name: str) -> bool:
    """Is `name` a carrier for one of the platform-owned roles, any spelling?

    ⚠️ The defaults half has a shared oracle — `_lib_confd.is_defaults_name`,
    which answers to `config_hierarchy.go`. This does not restate it and does
    not call it either (that would leave the profiles half unaccounted for);
    the two are pinned to agree by
    `test_the_defaults_half_agrees_with_the_shared_oracle`.
    """
    return name.lower() in PLATFORM_FILES


# ── Source discovery ─────────────────────────────────────────────────

def discover_yamls(source_dir: Path) -> List[Path]:
    """List every YAML carrier in a source directory (non-recursive).

    `.yaml` / `.yml`, any case — `_lib_confd.has_yaml_extension`, the
    exporter's own rule. #1603 (extension-SPELLING axis): this was
    `glob("*.yaml")`, so a `.yml` shard was silently LEFT OUT of the
    assembled config-dir — measured on byte-identical bodies before the
    fix: shard `alpha.yaml` → discovered; `alpha.yml` / `Alpha.YAML` → not,
    the same answer as an empty shard. Blast radius of widening: a `.yml`
    shard that used to be dropped is now merged (and can now collide by
    name with a same-named `.yml` in another shard, which `detect_conflicts`
    reports as before). Hidden (`.`-prefixed) entries were never skipped
    here and still are not — that is a separate axis, #1827. (It used to point
    at #1630, which is CLOSED and whose body names only run_chaos_soak and
    check_threshold_unit_sanity — measured; it never covered this tool.)

    Raises FileNotFoundError if directory does not exist.
    """
    if not source_dir.is_dir():
        raise FileNotFoundError(f"source directory not found: {source_dir}")
    # #1339: flat by design here — but a hierarchical conf.d must not
    # look like an empty one. Name the files this scan cannot see.
    warn_nested(source_dir, tool="assemble_config_dir")
    named = sorted(p for p in source_dir.iterdir()
                   if has_yaml_extension(p.name))

    # ⛔ A config-NAMED entry that is not a readable file (a shard's broken
    # symlink, a directory called `subteam.yaml`) is a fact about the input,
    # not a failure to measure: the exporter logs a WARN and serves the tree,
    # and the sibling producer passes it. Without this filter the gate stages
    # it, the copy raises, and `sharded-check` answers "could not measure".
    # Named, not silently dropped — `unusable_config_entries` is the shared
    # wording so this reader phrases it like every other one.
    unusable = set(unusable_config_entries(named))
    for p in sorted(unusable):
        print(f"WARN: {p} {unusable_reason(p)} — not merged",
              file=sys.stderr)
    return [p for p in named if p not in unusable and p.is_file()]


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
    for src in sources:
        label = str(src)
        for f in discover_yamls(src):
            name = f.name
            seen.setdefault(name, []).append((label, f))

    conflicts: Dict[str, List[Tuple[str, Path]]] = {}
    file_map: Dict[str, Path] = {}

    for name, entries in seen.items():
        if is_platform_file(name):
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

    return conflicts, file_map


def carriers_already_in(output_dir: Path) -> Dict[str, Path]:
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
    """
    if not output_dir.is_dir():
        return {}
    return {p.relative_to(output_dir).as_posix(): p
            for p in iter_config_files(output_dir)}


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


# ── Validation (optional) ───────────────────────────────────────────

ERROR_PREFIX = "ERROR: "
WARN_PREFIX = "WARN: "


def has_errors(issues: List[str]) -> bool:
    """Do these `validate_merged` issues include an ERROR (not just a WARN)?

    Both sides use `ERROR_PREFIX`, so the classification cannot drift the way
    a second copy of the word would. #1794: `validate_merged`'s issues were
    only printed and `main()` returned `EXIT_OK` on every path below, so the
    flag could not fail the command while the module's own exit table promised
    `2  validation error`. Measured on a malformed carrier and on a non-mapping
    top level, with a clean tree as the must-stay-quiet control.
    """
    return any(i.startswith(ERROR_PREFIX) for i in issues)


def validate_merged(output_dir: Path) -> List[str]:
    """Run basic YAML parse + key validation on assembled config-dir.

    Returns list of warning/error messages.
    """
    issues = []
    # #1339: second scan site — the guard must live where the scan does,
    # otherwise a hierarchical conf.d is silently empty on THIS path.
    warn_nested(output_dir, tool="assemble_config_dir")
    # #1603: same predicate as `discover_yamls` — a `.yml` file this tool
    # now copies into the output must also be the one it validates.
    for f in sorted(p for p in output_dir.iterdir()
                    if has_yaml_extension(p.name)):
        try:
            with open(f, encoding="utf-8") as fh:
                data = yaml.safe_load(fh)
            if data is None:
                issues.append(f"{WARN_PREFIX}{f.name} is empty")
            elif not isinstance(data, dict):
                issues.append(
                    f"{ERROR_PREFIX}{f.name} top-level is not a mapping")
        except yaml.YAMLError as e:
            issues.append(f"{ERROR_PREFIX}{f.name} parse error: {e}")

    return issues


# ── Main ─────────────────────────────────────────────────────────────

@exit_on_output_write_error
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
        "--validate", action="store_true",
        help="Run YAML validation on assembled output",
    )
    parser.add_argument(
        "--manifest", type=str, default="",
        help="Path to save/load assembly manifest JSON",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Output results as JSON",
    )
    args = parser.parse_args()

    # Parse sources
    if not args.sources and not args.manifest:
        parser.error("--sources or --manifest is required")

    sources: List[Path] = []
    if args.sources:
        sources = [Path(s.strip()).resolve() for s in args.sources.split(",")
                   if s.strip()]
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
    real_conflicts = {
        k: v for k, v in conflicts.items() if not is_platform_file(k)
    }
    platform_dups = {
        k: v for k, v in conflicts.items() if is_platform_file(k)
    }

    if not args.json:
        if platform_dups:
            print("⚠️  Platform file duplicates (first source wins):")
            for name, entries in platform_dups.items():
                for lbl, p in entries:
                    print(f"   {name} ← {lbl}")

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
            # argparse accepts `--manifest` alone, so this path is reachable
            # with no sources at all. Blaming the carriers there points at the
            # wrong thing: nothing was ever going to be found.
            print("ERROR: --sources is required to assemble (--manifest alone "
                  "loads nothing; there is no load path).", file=sys.stderr)
        else:
            print(f"ERROR: no config carrier found in "
                  f"{', '.join(str(s) for s in sources)}"
                  f" — assembling this would write an empty config-dir, which "
                  f"the exporter reads as a platform with no tenants.",
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
    # ⛔ `iter_config_files` walks with `onerror=None`, so a subdirectory it
    # cannot enumerate disappears silently — and a second declaration of one of
    # our tenants can be sitting in it. Measured as a non-root user (as root the
    # mode bits are ignored, so the first probe wrongly showed nothing): with
    # the subtree unreadable the gate answered rc 0 and said nothing, while the
    # same tree readable was correctly refused. A subtree we could not read is
    # "could not measure", which is the one thing this gate must never round
    # down to clean. An unreadable FILE is different and only gets named: the
    # exporter cannot read it either, so it declares nothing we would miss.
    if output_dir is not None:
        blind = [p for p in unusable_config_paths(output_dir) if p.is_dir()]
        unreadable_files = [p for p in unusable_config_paths(output_dir)
                            if not p.is_dir()]
        for p in sorted(unreadable_files):
            print(f"WARN: {p} {unusable_reason(p)} — the exporter cannot read "
                  f"it either", file=sys.stderr)
        if blind:
            print(f"\n❌ cannot answer the duplicate-tenant question for "
                  f"{output_dir}: " +
                  ", ".join(f"{p} {unusable_reason(p)}" for p in sorted(blind)),
                  file=sys.stderr)
            print("   This is 'could not measure', NOT 'measured clean' — a "
                  "second declaration of one of these tenants could be in "
                  "there.", file=sys.stderr)
            if args.json:
                print(format_json_report({
                    "status": tu.UNMEASURED,
                    "reason": f"unreadable subtree under {output_dir}",
                    "details": [str(p) for p in sorted(blind)],
                }))
            return tu.EXIT_FOR[tu.UNMEASURED]

    preexisting = carriers_already_in(output_dir) if output_dir else {}
    residue = {n: p for n, p in preexisting.items() if n not in file_map}
    if residue and not args.json:
        # ⛔ The machine-readable face is the one a CI parses, and it used to
        # get a plain `"status": "ok"` for a directory holding carriers this
        # run did not produce. That is fixed in the JSON document below, not
        # here — duplicating this line into `--json` mode was measured to buy
        # no detection at all (the mutation removing it kills nothing).
        print(f"\n⚠️  {len(residue)} carrier(s) already in {output_dir} are not "
              f"produced by this assembly, and the exporter reads them too "
              f"(this tool never clears --output): "
              f"{', '.join(sorted(residue))}", file=sys.stderr)

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

    # Assemble
    if output_dir is None:
        parser.error("--output is required for assembly (or use --check)")

    count = assemble(file_map, output_dir)

    # Validate
    validation_issues: List[str] = []
    if args.validate:
        validation_issues = validate_merged(output_dir)

    # Manifest
    if args.manifest:
        manifest = build_manifest(sources, file_map, conflicts)
        # The manifest is a record of what will be READ, not only of what was
        # copied; without this a retired shard is absent from every face.
        if residue:
            manifest["residue"] = sorted(residue)
        manifest_path = Path(args.manifest)
        # A SECOND flag and a second path: `--manifest` is not under
        # `--output`, so its failure must name `--manifest` (#1789). The
        # 0644 chmod is inside the same block as the write.
        with output_write(manifest_path, flag="--manifest"):
            manifest_path.write_text(
                format_json_report(manifest) + "\n",
                encoding="utf-8", newline="\n",
            )
            os.chmod(manifest_path,
                     stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)

    # ⛔ `--validate` has to be able to fail, or it is a report with a name
    # that reads like a gate. WARN stays non-fatal (an empty carrier is a
    # placeholder, not a broken one); ERROR is the module's documented exit 2.
    # The artifact is NOT removed: it may have been written into a directory
    # that already held files, and deleting those would be a second bug.
    failed_validation = has_errors(validation_issues)

    # Output
    if args.json:
        result = {
            "status": "validation_error" if failed_validation else "ok",
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
            # those two are different predicates (`count` is what was copied,
            # hidden entries included; `residue` mirrors the exporter, which
            # skips them), so their sum overcounted what will be read. This
            # runs after the copy, so it IS the artifact.
            result["artifact_file_count"] = len(carriers_already_in(output_dir))
        if validation_issues:
            result["validation"] = validation_issues
        print(format_json_report(result))
    else:
        print(f"\n✅ Assembled {count} file(s) into {output_dir}")
        if residue:
            print(f"   ⚠️  {output_dir} holds "
                  f"{len(carriers_already_in(output_dir))} carrier(s) the "
                  f"exporter will read — {len(residue)} of them left over "
                  f"from an earlier run (listed above).")
        if validation_issues:
            print(f"\nValidation ({len(validation_issues)} issue(s)):")
            for issue in validation_issues:
                print(f"   {issue}")
        if failed_validation:
            print(f"\n❌ {output_dir} was written but does not validate — "
                  f"the exporter will not read it as intended.",
                  file=sys.stderr)

    if failed_validation:
        return EXIT_CALLER_ERROR

    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
