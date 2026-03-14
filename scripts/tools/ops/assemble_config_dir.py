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
    1  conflict detected (same tenant in multiple sources)
    2  validation error (malformed YAML)
"""

import argparse
import hashlib
import json
import os
import shutil
import stat
import sys
from pathlib import Path
from typing import Dict, List, Tuple

try:
    import yaml
except ImportError:
    yaml = None  # graceful fallback — only needed for --validate

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent.parent

# Platform-owned files that are merged specially (first source wins).
PLATFORM_FILES = {"_defaults.yaml", "_profiles.yaml"}


# ── Source discovery ─────────────────────────────────────────────────

def discover_yamls(source_dir: Path) -> List[Path]:
    """List all *.yaml files in a source directory (non-recursive).

    Raises FileNotFoundError if directory does not exist.
    """
    if not source_dir.is_dir():
        raise FileNotFoundError(f"source directory not found: {source_dir}")
    return sorted(source_dir.glob("*.yaml"))


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
        if name in PLATFORM_FILES:
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
    if not dry_run:
        output_dir.mkdir(parents=True, exist_ok=True)

    count = 0
    for name in sorted(file_map):
        src = file_map[name]
        dst = output_dir / name
        if dry_run:
            print(f"  {name:40s} ← {src}")
        else:
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

def validate_merged(output_dir: Path) -> List[str]:
    """Run basic YAML parse + key validation on assembled config-dir.

    Returns list of warning/error messages.
    """
    if yaml is None:
        return ["SKIP: PyYAML not installed, skipping validation"]

    issues = []
    for f in sorted(output_dir.glob("*.yaml")):
        try:
            with open(f, encoding="utf-8") as fh:
                data = yaml.safe_load(fh)
            if data is None:
                issues.append(f"WARN: {f.name} is empty")
            elif not isinstance(data, dict):
                issues.append(f"ERROR: {f.name} top-level is not a mapping")
        except yaml.YAMLError as e:
            issues.append(f"ERROR: {f.name} parse error: {e}")

    return issues


# ── Main ─────────────────────────────────────────────────────────────

def main() -> int:
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

    # Detect conflicts
    try:
        conflicts, file_map = detect_conflicts(sources)
    except FileNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    # Report conflicts
    real_conflicts = {
        k: v for k, v in conflicts.items() if k not in PLATFORM_FILES
    }
    platform_dups = {
        k: v for k, v in conflicts.items() if k in PLATFORM_FILES
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
            print(json.dumps({
                "status": "conflict",
                "conflicts": {
                    n: [{"source": l, "sha256": _file_sha256(p)}
                        for l, p in entries]
                    for n, entries in real_conflicts.items()
                },
            }, indent=2, ensure_ascii=False))
        return 1

    # --check: just report, don't write
    if args.check:
        if not args.json:
            print(f"\n✅ No conflicts. {len(file_map)} file(s) ready "
                  f"to assemble from {len(sources)} source(s).")
            for name in sorted(file_map):
                print(f"   {name}")
        else:
            print(json.dumps({
                "status": "ok",
                "file_count": len(file_map),
                "files": sorted(file_map.keys()),
            }, indent=2, ensure_ascii=False))
        return 0

    # Assemble
    if not args.output:
        parser.error("--output is required for assembly (or use --check)")

    output_dir = Path(args.output).resolve()
    count = assemble(file_map, output_dir)

    # Validate
    validation_issues: List[str] = []
    if args.validate:
        validation_issues = validate_merged(output_dir)

    # Manifest
    if args.manifest:
        manifest = build_manifest(sources, file_map, conflicts)
        manifest_path = Path(args.manifest)
        manifest_path.write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        os.chmod(manifest_path,
                 stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)

    # Output
    if args.json:
        result = {
            "status": "ok",
            "output": str(output_dir),
            "file_count": count,
            "sources": [str(s) for s in sources],
        }
        if validation_issues:
            result["validation"] = validation_issues
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(f"\n✅ Assembled {count} file(s) into {output_dir}")
        if validation_issues:
            print(f"\nValidation ({len(validation_issues)} issue(s)):")
            for issue in validation_issues:
                print(f"   {issue}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
