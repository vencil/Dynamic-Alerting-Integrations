#!/usr/bin/env python3
"""state_reconcile.py — Migration State directory reconciliation.

Single declarative command — make `.da/state/` directory consistent. Replaces
the manual jq workflow from troubleshooting-checklist.md §schema_version drift
+ §manifest drift, and consolidates what would otherwise be two micro-commands
(state-migrate + manifest regenerate) into one (issue #405 Category A).

What it does:
  1. Scans .da/state/<cluster>.json files
  2. Validates each file's `schema_version` against current
  3. Auto-applies known schema migrations (1.0 → 1.1, etc.) when registered
     — the MIGRATIONS registry is the extension point; for v1.0 only there are
     no migrations to apply yet
  4. Rebuilds .da/manifest.json from the filesystem (manifest = derived view,
     state files = source of truth)

Why a single declarative command:
  Both schema_version drift and manifest drift are forms of "state directory
  out of sync". Two micro-commands force users to remember the right order
  (migrate-then-rebuild-manifest); one declarative command lets users just
  say "make it consistent". See issue #405 design discussion.

Usage:
  da-tools state-reconcile                          # default --state-dir .da/state/
  da-tools state-reconcile --state-dir custom/      # custom location
  da-tools state-reconcile --dry-run                # report changes, do not write
  da-tools state-reconcile --ci                     # exit 1 if dry-run shows changes
                                                    # needed (suitable for CI gate)
  da-tools state-reconcile --json                   # machine-readable JSON output

Exit codes:
  0  state directory consistent (or all changes applied successfully)
  1  unresolvable schema drift (e.g. missing schema_version) — or, in --ci mode,
     changes are needed but --dry-run skipped applying them
  2  caller error (bad arguments / missing state directory when not --json)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Current schema version — keep aligned with docs/schemas/migration-state.md.
# Bump when introducing a breaking schema change AND adding a corresponding
# entry to MIGRATIONS below.
CURRENT_SCHEMA_VERSION = "1.0"

# Schema migration registry. Maps (from_version, to_version) tuple to a
# function that takes a state dict and returns a migrated state dict.
#
# Empty for v1.0 (no prior versions to migrate from). When v1.1 schema ships,
# add an entry like:
#     ("1.0", "1.1"): _migrate_1_0_to_1_1,
# where the migration function adds new fields with safe defaults, e.g.:
#     def _migrate_1_0_to_1_1(state):
#         state["schema_version"] = "1.1"
#         state.setdefault("gate_log", [])  # 1.1 added gate_log[]
#         return state
MIGRATIONS: dict = {}


def find_state_files(state_dir: Path) -> list[Path]:
    """Enumerate .json files in state_dir, sorted for deterministic output."""
    if not state_dir.is_dir():
        return []
    return sorted(state_dir.glob("*.json"))


def read_state(filepath: Path) -> tuple[dict | None, str | None]:
    """Read a state file. Returns (data, error_message)."""
    try:
        with filepath.open(encoding="utf-8") as f:
            return json.load(f), None
    except OSError as exc:
        return None, f"cannot read: {exc}"
    except json.JSONDecodeError as exc:
        return None, f"invalid JSON: {exc}"


def apply_migration_chain(
    state: dict, from_version: str, to_version: str
) -> tuple[dict | None, str | None]:
    """Apply registered migrations to walk from from_version to to_version.

    Returns (migrated_state, error_message). When no migration path exists,
    returns (None, reason).
    """
    if from_version == to_version:
        return state, None
    key = (from_version, to_version)
    if key in MIGRATIONS:
        return MIGRATIONS[key](state), None
    return (
        None,
        f"no registered migration from {from_version} to {to_version}",
    )


def build_manifest(state_files: list[Path], state_dir: Path) -> dict:
    """Build manifest.json content from the filesystem state.

    `path` field is recorded relative to repo root (canonical form `.da/state/X.json`)
    so the manifest is portable across checkouts regardless of CWD.
    """
    states = []
    # Manifest paths are recorded as .da/<state-dir-basename>/<file>
    # so callers checking out the repo from any CWD can resolve them.
    rel_root = f".da/{state_dir.name}"
    for sf in state_files:
        states.append(
            {
                "cluster": sf.stem,
                "path": f"{rel_root}/{sf.name}",
            }
        )
    return {
        "schema_version": CURRENT_SCHEMA_VERSION,
        "states": states,
    }


def write_json(path: Path, data: dict) -> None:
    """Write JSON with stable formatting (2-space indent, trailing newline)."""
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def reconcile(
    state_dir: Path,
    manifest_path: Path,
    *,
    dry_run: bool = False,
) -> dict:
    """Core reconciliation logic. Returns a report dict suitable for JSON output."""
    report: dict = {
        "state_dir": str(state_dir),
        "manifest_path": str(manifest_path),
        "schema_migrations": [],
        "schema_drift_unresolvable": [],
        "manifest_change": None,
        "state_file_count": 0,
    }

    state_files = find_state_files(state_dir)
    report["state_file_count"] = len(state_files)

    # 1. Schema validation + migration per file
    for sf in state_files:
        data, err = read_state(sf)
        if err is not None:
            report["schema_drift_unresolvable"].append(
                {"file": str(sf), "reason": err}
            )
            continue

        sv = data.get("schema_version")
        if sv is None:
            report["schema_drift_unresolvable"].append(
                {"file": str(sf), "reason": "missing schema_version field"}
            )
            continue

        if sv == CURRENT_SCHEMA_VERSION:
            continue

        migrated, migrate_err = apply_migration_chain(
            data, sv, CURRENT_SCHEMA_VERSION
        )
        if migrate_err is not None:
            report["schema_drift_unresolvable"].append(
                {
                    "file": str(sf),
                    "from": sv,
                    "to": CURRENT_SCHEMA_VERSION,
                    "reason": migrate_err,
                }
            )
            continue

        if not dry_run:
            write_json(sf, migrated)
        report["schema_migrations"].append(
            {"file": str(sf), "from": sv, "to": CURRENT_SCHEMA_VERSION}
        )

    # 2. Manifest rebuild
    new_manifest = build_manifest(state_files, state_dir)
    old_manifest: dict | None = None
    if manifest_path.exists():
        old_data, old_err = read_state(manifest_path)
        if old_err is None:
            old_manifest = old_data

    manifest_changed = old_manifest != new_manifest
    if manifest_changed:
        if not dry_run:
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            write_json(manifest_path, new_manifest)
        report["manifest_change"] = {
            "old_state_count": (
                len(old_manifest.get("states", [])) if old_manifest else 0
            ),
            "new_state_count": len(new_manifest["states"]),
        }

    return report


def render_text(report: dict, *, dry_run: bool) -> None:
    """Print human-readable summary of the reconciliation report."""
    count = report["state_file_count"]
    if count == 0:
        print(f"⚠️  No state files found in {report['state_dir']}")
    else:
        print(f"Scanned {count} state file(s) in {report['state_dir']}")

    for m in report["schema_migrations"]:
        action = "[DRY-RUN] Would migrate" if dry_run else "✓ Migrated"
        print(f"  {action} {m['file']}: schema {m['from']} → {m['to']}")

    for m in report["schema_drift_unresolvable"]:
        if "from" in m:
            print(
                f"  ❌ Unresolvable drift: {m['file']} ({m['from']} → "
                f"{m.get('to', '?')}): {m['reason']}"
            )
        else:
            print(f"  ❌ Read error: {m['file']}: {m['reason']}")

    mc = report["manifest_change"]
    if mc is not None:
        action = "[DRY-RUN] Would rebuild" if dry_run else "✓ Rebuilt"
        delta = mc["new_state_count"] - mc["old_state_count"]
        delta_str = f"{delta:+d}" if delta else "no count change"
        print(
            f"  {action} manifest: {report['manifest_path']} "
            f"({mc['new_state_count']} state(s), {delta_str})"
        )

    needs_action = (
        report["schema_migrations"]
        or report["schema_drift_unresolvable"]
        or mc is not None
    )
    if not needs_action:
        print("✓ State directory already consistent")


def compute_exit_code(report: dict, *, ci: bool, dry_run: bool) -> int:
    """Determine exit code from the reconciliation report."""
    if report["schema_drift_unresolvable"]:
        return 1
    if ci:
        has_pending = (
            dry_run
            and (report["schema_migrations"] or report["manifest_change"])
        )
        if has_pending:
            return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Reconcile migration state directory (schema + manifest).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  da-tools state-reconcile\n"
            "  da-tools state-reconcile --state-dir custom/states/ --dry-run\n"
            "  da-tools state-reconcile --ci --json\n"
        ),
    )
    ap.add_argument(
        "--state-dir",
        default=".da/state",
        help="Directory with per-cluster state files (default: .da/state)",
    )
    ap.add_argument(
        "--manifest-path",
        default=".da/manifest.json",
        help="Manifest file path (default: .da/manifest.json)",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Report changes without writing",
    )
    ap.add_argument(
        "--ci",
        action="store_true",
        help=(
            "Exit 1 when changes are needed (in --dry-run mode) or any drift "
            "remains. Suitable for CI gate."
        ),
    )
    ap.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON report instead of human-readable text.",
    )
    args = ap.parse_args(argv)

    state_dir = Path(args.state_dir)
    manifest_path = Path(args.manifest_path)

    report = reconcile(state_dir, manifest_path, dry_run=args.dry_run)
    report["dry_run"] = args.dry_run

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        render_text(report, dry_run=args.dry_run)

    return compute_exit_code(report, ci=args.ci, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
