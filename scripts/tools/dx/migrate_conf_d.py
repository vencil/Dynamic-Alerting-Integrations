#!/usr/bin/env python3
"""Migrate flat conf.d/ to hierarchical domain/region/env/ layout.

Usage:
    python3 scripts/tools/dx/migrate_conf_d.py --conf-d conf.d/ --dry-run
    python3 scripts/tools/dx/migrate_conf_d.py --conf-d conf.d/ --apply
    python3 scripts/tools/dx/migrate_conf_d.py --conf-d conf.d/ --dry-run --infer-from metadata

Generates `git mv` commands to restructure flat tenant YAML files into
domain/region/env/ subdirectories (ADR-016). Uses git mv to preserve
file history.

Modes:
    --dry-run   Print git mv commands without executing (default)
    --apply     Execute git mv commands (requires clean working tree)

Inference:
    --infer-from metadata   Use _metadata.domain/region/environment to place files
                            (default behavior; skip files without _metadata)

Reading (ADR-036 §2, #2906): which files there are and what each one's
`_metadata` says come from `da-guard parse-tree` (yaml.v3's reading: an
unquoted `domain: yes` is the string "yes"), never from PyYAML. Before any
directory is made or file moved, each planned destination is read back by
parse-tree in file mode — the file's bytes at the path they are moved to —
and must be a path a reader selects with the same parse result; otherwise
nothing is moved and the run exits 2, naming the file. A conf.d file
parse-tree cannot read, or no usable da-guard (`$DA_GUARD_BINARY`, then
`$PATH`), is exit 2 as well.
"""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

# Pull `try_utf8_stdout` from the shared compat lib at scripts/tools/.
# Migrated in #489 Phase B (was missing encoding setup → would crash on
# legacy Windows cp950/cp936 consoles when printing emoji to stdout).
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, str(_THIS_DIR))
sys.path.insert(0, os.path.join(str(_THIS_DIR), ".."))
from _lib_compat import try_utf8_stdout  # noqa: E402
from _lib_exitcodes import EXIT_CALLER_ERROR  # noqa: E402
from _lib_io import exit_on_output_write_error, output_write  # noqa: E402  (#1789)
from _lib_confd import warn_nested  # noqa: E402
import _lib_parse_cache  # noqa: E402  (ADR-036 §2, #2906)
import _lib_write_check  # noqa: E402
from _lib_parse_tree import FileUnreadable  # noqa: E402


class MoveRefused(Exception):
    """A planned destination parse-tree does not read as the source: nothing
    is moved (ADR-036 §2)."""


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
    """Scan flat conf.d/ and plan git mv operations.

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
    tree = reader.tree()
    # #3206: which entries this level holds and what each one is (Go's
    # `role`) are the entries of the document the content is read from —
    # no second walk, no verdict of this tool's own.
    listing = reader.listing()
    # #1911: flat read — a hierarchical conf.d must not look empty.
    warn_nested(conf_d, tool="migrate_conf_d", listing=listing)
    for e in listing.entries:
        if e.not_a_file or e.unlistable_dir:
            # Refused, whatever its name or depth (#3206): a directory it
            # cannot list makes the plan incomplete (what is under it is
            # unknown); a link to a directory is NOT "already nested" — the
            # exporter does not follow it, nothing under it is loaded; a
            # dangling link or a FIFO is nothing to move. Named; nothing is
            # planned or moved.
            kind, message = e.problem or ("read_failure", e.role or "")
            raise FileUnreadable(e.path, kind, message)
    top_entries = [e for e in listing.entries if e.depth == 0]
    top = sorted(e.path for e in top_entries if e.config_file)

    # The directories the walk enters (Go's role `dir`, a config-named one
    # included: its files are read like any directory's) — already nested.
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


def confirm_moves(conf_d: Path, actions: list[dict],
                  reader: "_lib_parse_cache.ConfdReader | None" = None) -> None:
    """ADR-036 §2: every "ok" move, before any is made — the source's bytes
    read by `da-guard parse-tree` in file mode as the file at its target
    path must be a file a reader selects, with the parse result the source
    has now. Raises MoveRefused naming the first that is not."""
    reader = reader or _lib_parse_cache.ConfdReader(conf_d)
    for a in actions:
        if a["status"] != "ok":
            continue
        # A destination that leaves the tree (`..`, an absolute part, a link
        # out) is this tool's refusal, before parse-tree is asked about it.
        parts = a["target"].split("/")
        outside = any(p in ("", ".", "..") for p in parts) or a["target"].startswith("/")
        if not outside:
            try:
                (conf_d / a["target"]).resolve().relative_to(conf_d.resolve())
            except ValueError:
                outside = True
        if outside:
            raise MoveRefused(f"{a['source']} -> {a['target']}: the destination is not a "
                              f"path inside {conf_d} (_metadata.domain / region / "
                              f"environment form it)")
        data = (conf_d / a["source"]).read_bytes()
        try:
            _lib_write_check.confirm(conf_d, a["target"], data, reader.node(a["source"]))
        except _lib_write_check.WriteRefused as exc:
            raise MoveRefused(f"{a['source']} -> {a['target']}: {exc}") from None


def generate_git_commands(actions: list[dict], conf_d: Path) -> list[str]:
    """Generate git mv commands from migration plan."""
    commands = []
    # mkdir -p for unique target directories
    target_dirs = set()
    for a in actions:
        if a["status"] != "ok":
            continue
        target_path = conf_d / a["target"]
        target_dirs.add(str(target_path.parent))

    for d in sorted(target_dirs):
        commands.append(["mkdir", "-p", d])

    for a in actions:
        if a["status"] != "ok":
            continue
        src = str(conf_d / a["source"])
        dst = str(conf_d / a["target"])
        commands.append(["git", "mv", src, dst])

    return commands


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
        help="Print git mv commands without executing (default; also wins "
        "over --apply)",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Execute git mv commands",
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
    if not conf_d.exists():
        print(f"❌ conf.d/ not found at {conf_d}", file=sys.stderr)
        sys.exit(EXIT_CALLER_ERROR)

    reader = _lib_parse_cache.ConfdReader(conf_d)
    try:
        actions = plan_migration(conf_d, reader)
        confirm_moves(conf_d, actions, reader)
    except FileUnreadable as exc:
        print(f"❌ {exc.file}: da-guard parse-tree cannot read it ({exc.kind}: "
              f"{exc.message}); nothing is planned or moved", file=sys.stderr)
        sys.exit(EXIT_CALLER_ERROR)
    except MoveRefused as exc:
        print(f"❌ refusing to move: {exc}; nothing is moved", file=sys.stderr)
        sys.exit(EXIT_CALLER_ERROR)

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

    commands = generate_git_commands(actions, conf_d)

    # --dry-run wins over --apply. It used to be `default=True` and never read,
    # so `--apply --dry-run` ran the mkdir + `git mv` plan (issue #1454; pinned
    # by tests/shared/test_dry_run_no_write.py).
    if args.apply and args.dry_run:
        print("note: --dry-run given, ignoring --apply; nothing will be moved",
              file=sys.stderr)
    if args.apply and not args.dry_run:
        print("🚀 Executing migration...")
        for cmd in commands:
            print(f"  $ {' '.join(cmd)}")
            result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
            if result.returncode != 0:
                print(f"  ❌ Failed: {result.stderr.strip()}", file=sys.stderr)
                sys.exit(EXIT_CALLER_ERROR)
        print(f"\n✅ Migrated {len(ok)} files.")
        print(f"   Run `git status` to review, then `git commit`.")
    else:
        print("📋 Dry-run — commands that would be executed:")
        print()
        for cmd in commands:
            print(f"  {' '.join(cmd)}")
        print()
        print(f"Run with --apply to execute. ({len(ok)} files will be moved)")

    # Warn about skipped files
    if skipped_meta:
        print(f"\n⚠️  {len(skipped_meta)} file(s) skipped (missing _metadata). Decide manually:")
        for a in skipped_meta[:10]:
            print(f"    {a['source']}")
        if len(skipped_meta) > 10:
            print(f"    ... and {len(skipped_meta) - 10} more")


if __name__ == "__main__":
    main()
