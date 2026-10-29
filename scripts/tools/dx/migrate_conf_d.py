#!/usr/bin/env python3
"""Migrate flat conf.d/ to hierarchical domain/region/env/ layout.

Usage:
    python3 scripts/tools/dx/migrate_conf_d.py --conf-d conf.d/ --dry-run
    python3 scripts/tools/dx/migrate_conf_d.py --conf-d conf.d/ --apply
    python3 scripts/tools/dx/migrate_conf_d.py --conf-d conf.d/ --dry-run --infer-from metadata

Plans moving flat tenant YAML files into domain/region/env/ subdirectories
(ADR-016) and, with --apply, moves them.

Modes:
    --dry-run   Judge the moves and list them, move nothing (default)
    --apply     Move the files

Inference:
    --infer-from metadata   Use _metadata.domain/region/environment to place files
                            (default behavior; skip files without _metadata)

Reading (ADR-036 §2, #2906): which files there are and what each one's
`_metadata` says come from `da-guard parse-tree` (yaml.v3's reading: an
unquoted `domain: yes` is the string "yes"), never from PyYAML. A conf.d file
parse-tree cannot read, or no usable da-guard (`$DA_GUARD_BINARY`, then
`$PATH`), is exit 2.

Moving (ADR-036 §2: only Go writes conf.d; #3206): the whole plan is ONE
`da-guard write` batch of move entries — each file moved byte for byte (the
same file: its mode and owner kept), the missing directories made by
da-guard, the tree with every file at its new path judged once. A move that
does not hold (a destination no reader reads, one outside the conf.d, one
that exists, a tree the batch would make worse) is named on stderr with its
reason and exits 2: no file is moved. --dry-run asks the same batch with
`--dry-run`, so a plan that would be refused is refused, named, exit 2.

Git: when the conf.d is in a git working tree, every source must be a file
git tracks (as `git mv` requires; checked before anything is moved, exit 2
naming the others), and after the move each source's index entry is moved
to its target as it was — same blob, same mode — so `git status` shows a
rename, as after `git mv`; changes the user had not staged stay unstaged.
Not in a git working tree (or no git): the files are moved, nothing else.
"""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

# Pull `try_utf8_stdout` from the shared compat lib at scripts/tools/.
# Migrated in #489 Phase B (was missing encoding setup → would crash on
# legacy Windows cp950/cp936 consoles when printing emoji to stdout).
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, str(_THIS_DIR))
sys.path.insert(0, os.path.join(str(_THIS_DIR), ".."))
from _lib_compat import try_utf8_stdout  # noqa: E402
from _lib_exitcodes import EXIT_CALLER_ERROR  # noqa: E402
from _lib_io import (  # noqa: E402  (#1789; #3276)
    classify_path, exit_on_output_write_error, output_write)
from _lib_confd import warn_nested  # noqa: E402
import _lib_confedit  # noqa: E402  (ADR-036 §2: Go writes conf.d, #3206)
import _lib_parse_cache  # noqa: E402  (ADR-036 §2, #2906)

TOOL = "migrate_conf_d"
#: One git command on the index (rev-parse, ls-files, update-index).
_GIT_TIMEOUT_S = 60


def _extract_metadata(data: dict) -> dict | None:
    """Extract _metadata from a tenant YAML file."""
    tenants = data.get("tenants", {})
    if not isinstance(tenants, dict):
        return None
    for tid, tconfig in tenants.items():
        if isinstance(tconfig, dict) and "_metadata" in tconfig:
            return tconfig["_metadata"]
    return None


_PATH_FIELDS = ("domain", "region", "environment")


def _unplaceable_reason(meta) -> str:
    """Why a non-empty `_metadata` cannot place its file, or "" when it can:
    it is not a mapping (e.g. the string form), or a path field is not a
    string. The text names the shape, never the value."""
    if not isinstance(meta, dict):
        return f"_metadata is not a mapping ({type(meta).__name__})"
    for field in _PATH_FIELDS:
        value = meta.get(field, "")
        # An empty value (`environment:`) is not written, as before.
        if value is not None and not isinstance(value, str):
            return f"_metadata.{field} is not a string ({type(value).__name__})"
    return ""


def plan_migration(conf_d: Path, reader: "_lib_parse_cache.ConfdReader | None" = None
                   ) -> list[dict]:
    """Scan flat conf.d/ and plan the moves.

    The files and their content are `da-guard parse-tree`'s (ADR-036 §2):
    the top-level files it selects, each read as yaml.v3 reads it. Raises
    FileUnreadable for a top-level tenant file it cannot read, and
    ParseTreeError when there is no usable da-guard.

    Returns a list of migration actions:
    [
        {
            "source": "tenant-a.yaml",
            "target": "finance/us-east/prod/tenant-a.yaml",
            "tenant_id": "tenant-a",
            "metadata": {"domain": "finance", "region": "us-east", "environment": "prod"},
            "status": "ok" | "skip_no_metadata" | "skip_unplaceable_metadata"
                      | "skip_already_nested" | "skip_system_file"
        }
    ]
    """
    actions = []
    reader = reader or _lib_parse_cache.ConfdReader(conf_d)
    # #3206: the run's index, whose taking is the fail-closed exit
    # (`ConfdReader.tree`): a tree Go names paths on — a directory it cannot
    # list, a link to a directory, a dangling link, a FIFO, a directory with
    # a config file's name (`x.yaml/`), a file the exporter cannot decode —
    # ends the run there, every such path named, exit 2: nothing is planned
    # or moved.
    tree = reader.tree()
    # Which entries this level holds and what each one is (Go's `role`) are
    # the entries of the document the content is read from — no second
    # walk, no verdict of this tool's own.
    listing = reader.listing()
    # #1911: flat read — a hierarchical conf.d must not look empty.
    warn_nested(conf_d, tool="migrate_conf_d", listing=listing)
    top_entries = [e for e in listing.entries if e.depth == 0]
    top = sorted(e.path for e in top_entries if e.config_file)

    # The directories the walk enters (Go's role `dir`) — already nested.
    for e in top_entries:
        if not e.is_dir:
            continue
        actions.append({
            "source": e.path + "/",
            "target": e.path + "/",
            "tenant_id": None,
            "metadata": None,
            "status": "skip_already_nested",
        })

    reserved = {e.path for e in top_entries if e.reserved}
    for name in top:
        if name in reserved:
            actions.append({
                "source": name,
                "target": name,
                "tenant_id": None,
                "metadata": None,
                "status": "skip_system_file",
            })
            continue
        if name not in tree.selected:
            continue  # a name no reader reads (parse-tree's `not_read`)
        fp = conf_d / name

        data = reader.read(name)
        meta = _extract_metadata(data)

        # Extract tenant ID
        tenants = data.get("tenants", {})
        tid = next(iter(tenants.keys()), fp.stem) if isinstance(tenants, dict) else fp.stem

        if not meta:
            actions.append({
                "source": fp.name,
                "target": fp.name,
                "tenant_id": tid,
                "metadata": None,
                "status": "skip_no_metadata",
            })
            continue

        # #2830: this plan reads the tenant file's own `_metadata` as a
        # mapping of strings. Another shape — the string form, or a
        # non-string domain/region/environment, both of which the exporter
        # reads — is not guessed at: the file is listed for a manual move.
        unplaceable = _unplaceable_reason(meta)
        if unplaceable:
            actions.append({
                "source": fp.name,
                "target": fp.name,
                "tenant_id": tid,
                "metadata": None,
                "status": "skip_unplaceable_metadata",
                "reason": unplaceable,
            })
            continue

        domain = meta.get("domain", "")
        region = meta.get("region", "")
        env = meta.get("environment", "")

        if not domain:
            actions.append({
                "source": fp.name,
                "target": fp.name,
                "tenant_id": tid,
                "metadata": meta,
                "status": "skip_no_metadata",
            })
            continue

        # Build target path
        parts = [domain]
        if region:
            parts.append(region)
        if env:
            parts.append(env)
        target = "/".join(parts) + "/" + fp.name

        actions.append({
            "source": fp.name,
            "target": target,
            "tenant_id": tid,
            "metadata": {"domain": domain, "region": region, "environment": env},
            "status": "ok",
        })

    actions.sort(key=lambda a: a["source"].rstrip("/"))
    return actions



def move_entries(actions: list[dict]) -> list[dict]:
    """Every "ok" action as a `da-guard write` move entry (its target, from
    its source): the whole plan is ONE batch (ADR-036 §2)."""
    return [_lib_confedit.move_entry(a["target"], a["source"])
            for a in actions if a["status"] == "ok"]


class GitIndexError(Exception):
    """A git command on the index failed."""


class GitIndex:
    """What `git mv` did besides moving the file: each source's index entry
    moved to its target as it is — same blob, same mode — so `git status`
    shows a rename and nothing the user had not staged is staged."""

    def __init__(self, top: Path, prefix: str) -> None:
        self.top = top
        self.prefix = prefix  # the conf.d relative to the working tree's top: "" or "x/"
        self.entries: dict[str, tuple[str, str]] = {}  # source -> (mode, blob)

    @staticmethod
    def _git(cwd: Path, *args: str, stdin: "bytes | None" = None
             ) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "--literal-pathspecs", *args], cwd=cwd, input=stdin,
                              capture_output=True, timeout=_GIT_TIMEOUT_S)

    @classmethod
    def find(cls, conf_d: Path) -> "tuple[GitIndex | None, str]":
        """(the index of the working tree *conf_d* is in, "") — or (None,
        why there is none: not in a working tree, or no git)."""
        try:
            r = cls._git(conf_d, "rev-parse", "--is-inside-work-tree", "--show-toplevel",
                         "--show-prefix")
        except FileNotFoundError:
            return None, "git is not installed"
        out = r.stdout.decode("utf-8", "surrogateescape").splitlines()
        if r.returncode != 0 or not out or out[0] != "true" or len(out) < 2:
            # git's own words when it said why (not a repository, a
            # repository it will not use, …) — never a silent skip.
            said = r.stderr.decode("utf-8", "replace").strip().splitlines()
            return None, (f"{conf_d} is not in a git working tree"
                          + (f" (git: {said[0]})" if said else ""))
        return cls(Path(out[1]), out[2] if len(out) > 2 else ""), ""

    def untracked(self, sources: "list[str]") -> "list[str]":
        """Read every source's index entry; the sources `git mv` refuses
        (not tracked, or in a merge conflict), in order."""
        paths = [self.prefix + s for s in sources]
        r = self._git(self.top, "ls-files", "--stage", "-z", "--", *paths)
        if r.returncode != 0:
            raise GitIndexError("git ls-files: " + r.stderr.decode("utf-8", "replace").strip())
        found: "dict[str, list[tuple[str, str, str]]]" = {}
        for rec in r.stdout.split(b"\0"):
            if not rec:
                continue
            meta, _, path = rec.partition(b"\t")
            mode, blob, stage = meta.decode("ascii").split(" ")
            found.setdefault(path.decode("utf-8", "surrogateescape"), []).append(
                (mode, blob, stage))
        bad = []
        for src, path in zip(sources, paths):
            got = found.get(path, [])
            if len(got) != 1 or got[0][2] != "0":
                bad.append(src)
            else:
                self.entries[src] = (got[0][0], got[0][1])
        return bad

    def move(self, moves: "list[tuple[str, str]]") -> None:
        """Each (source, target) of *moves* (sources read by `untracked`):
        the source's entry removed, the same entry added at the target."""
        records = []
        for src, dst in moves:
            mode, blob = self.entries[src]
            records.append(f"0 {'0' * len(blob)}\t{self.prefix}{src}")
            records.append(f"{mode} {blob}\t{self.prefix}{dst}")
        body = "".join(rec + "\0" for rec in records).encode("utf-8", "surrogateescape")
        r = self._git(self.top, "update-index", "-z", "--index-info", stdin=body)
        if r.returncode != 0:
            raise GitIndexError("git update-index: "
                                + r.stderr.decode("utf-8", "replace").strip())
        # git skips a record it will not take with a warning and exit 0
        # ("Ignoring path …"): each target must now be in the index, once.
        targets = [self.prefix + dst for _, dst in moves]
        r = self._git(self.top, "ls-files", "--stage", "-z", "--", *targets)
        if r.returncode != 0:
            raise GitIndexError("git ls-files: " + r.stderr.decode("utf-8", "replace").strip())
        listed = [rec.partition(b"\t")[2].decode("utf-8", "surrogateescape")
                  for rec in r.stdout.split(b"\0") if rec]
        missing = [t for t in targets if listed.count(t) != 1]
        if missing:
            said = r.stderr.decode("utf-8", "replace").strip()
            raise GitIndexError("git update-index did not record "
                                + ", ".join(missing) + (f" ({said})" if said else ""))


def _die(message: str) -> None:
    print(f"{TOOL}: ERROR: {message}", file=sys.stderr)
    sys.exit(EXIT_CALLER_ERROR)


def _git_index_or_exit(conf_d: Path, ok: "list[dict]") -> "tuple[GitIndex | None, str]":
    """The git index the moves are recorded in (None, and why, when there is
    none); exit 2 naming every source `git mv` would refuse — before any
    file is moved."""
    index, why = GitIndex.find(conf_d)
    if index is None:
        return None, why
    try:
        bad = index.untracked([a["source"] for a in ok])
    except (GitIndexError, OSError, subprocess.SubprocessError) as exc:
        _die(f"cannot read the git index of {conf_d}: {exc}; nothing was moved")
    if bad:
        for src in bad:
            print(f"{TOOL}: ERROR: {src}: not a file git tracks (or in a merge conflict)",
                  file=sys.stderr)
        _die(f"{len(bad)} file(s) to move are not tracked by git in {index.top} — "
             f"`git add` them first (as `git mv` requires); nothing was moved")
    return index, ""


@exit_on_output_write_error
@_lib_parse_cache.exit_on_parse_tree_error  # ADR-036 §2: no usable da-guard → rc 2
def main() -> None:
    try_utf8_stdout()
    parser = argparse.ArgumentParser(
        description="Migrate flat conf.d/ to hierarchical layout (ADR-016).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--conf-d", "-c", type=str, required=True,
        help="Path to conf.d/ directory",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Judge the moves through `da-guard write --dry-run` and list "
        "them, moving nothing (default; also wins over --apply)",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Move the files (one `da-guard write` batch; git index updated "
        "as by `git mv`)",
    )
    parser.add_argument(
        "--infer-from", choices=["metadata"], default="metadata",
        help="Inference source for target directories (default: metadata)",
    )
    parser.add_argument(
        "--output-plan", "-o", type=str, default=None,
        help="Write migration plan as JSON to file",
    )
    args = parser.parse_args()

    conf_d = Path(args.conf_d).resolve()
    # #3276: the shared judgement of a caller-given path — a path under a
    # parent the caller cannot enter is named, not a traceback.
    problem = classify_path(conf_d, "dir", flag="--conf-d")
    if problem is not None:
        word = "not found" if problem.missing else "not usable"
        print(f"❌ conf.d/ {word}: {problem.message}", file=sys.stderr)
        sys.exit(EXIT_CALLER_ERROR)

    # A file the walk cannot read is a path da-guard validation names: the
    # reader's first read is the shared conf.d refusal (#3206), so the plan
    # is made from readable files only.
    actions = plan_migration(conf_d, _lib_parse_cache.ConfdReader(conf_d))

    # Summary
    ok = [a for a in actions if a["status"] == "ok"]
    skipped_meta = [a for a in actions if a["status"] == "skip_no_metadata"]
    skipped_nested = [a for a in actions if a["status"] == "skip_already_nested"]
    skipped_sys = [a for a in actions if a["status"] == "skip_system_file"]
    skipped_unplaceable = [a for a in actions if a["status"] == "skip_unplaceable_metadata"]

    print(f"📂 Scanned {conf_d}")
    print(f"   Migratable:        {len(ok)}")
    print(f"   Skip (no metadata): {len(skipped_meta)}")
    print(f"   Skip (nested):      {len(skipped_nested)}")
    print(f"   Skip (system):      {len(skipped_sys)}")
    print(f"   Skip (unplaceable): {len(skipped_unplaceable)}")
    for a in skipped_unplaceable:
        print(f"     {a['source']}: {a['reason']} — move it by hand")
    print()

    if args.output_plan:
        with output_write(args.output_plan, flag="-o/--output-plan"):
            Path(args.output_plan).write_text(
                json.dumps(actions, indent=2, ensure_ascii=False), encoding="utf-8", newline="\n"
            )
        print(f"✅ Plan written to {args.output_plan}")

    if not ok:
        print("ℹ️  Nothing to migrate.")
        return

    # --dry-run wins over --apply. It used to be `default=True` and never read,
    # so `--apply --dry-run` ran the mkdir + `git mv` plan (issue #1454; pinned
    # by tests/shared/test_dry_run_no_write.py).
    if args.apply and args.dry_run:
        print("note: --dry-run given, ignoring --apply; nothing will be moved",
              file=sys.stderr)
    apply = args.apply and not args.dry_run

    # Both modes judge the same: git's precondition first, then the whole
    # batch by `da-guard write` (a dry run of it when not applying) — every
    # refused move named, exit 2, nothing moved.
    index, no_git = _git_index_or_exit(conf_d, ok)
    _lib_confedit.write_all_or_exit(
        conf_d, move_entries(actions), tool=TOOL, dry_run=not apply,
        partial_note=None if index is None else
        "the git index was not updated: it does not record any of these moves")

    if apply:
        print("🚀 Moved (da-guard write):")
        for a in ok:
            print(f"  {a['source']} -> {a['target']}")
        if index is None:
            print(f"\n✅ Migrated {len(ok)} files. Not recorded in git: {no_git}.")
        else:
            try:
                index.move([(a["source"], a["target"]) for a in ok])
            except (GitIndexError, OSError, subprocess.SubprocessError) as exc:
                _die(f"the {len(ok)} file(s) above were moved, but the git index does not "
                     f"record the moves: {exc}; record each with "
                     f"`git rm --cached --ignore-unmatch -- <old>` and "
                     f"`git add -- <new>` (unlike `git mv`, that also stages any "
                     f"change to the file not staged before)")
            print(f"\n✅ Migrated {len(ok)} files (staged as renames, as `git mv` does).")
            print("   Run `git status` to review, then `git commit`.")
    else:
        print("📋 Dry-run — `da-guard write --dry-run` accepts these moves:")
        print()
        for a in ok:
            print(f"  {a['source']} -> {a['target']}")
        print()
        print(f"Run with --apply to move them. ({len(ok)} files will be moved)")

    # Warn about skipped files
    if skipped_meta:
        print(f"\n⚠️  {len(skipped_meta)} file(s) skipped (missing _metadata). Decide manually:")
        for a in skipped_meta[:10]:
            print(f"    {a['source']}")
        if len(skipped_meta) > 10:
            print(f"    ... and {len(skipped_meta) - 10} more")


if __name__ == "__main__":
    main()
