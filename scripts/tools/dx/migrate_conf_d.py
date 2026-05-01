#!/usr/bin/env python3
"""Migrate flat conf.d/ to hierarchical domain/region/env/ layout.

Usage:
    python3 scripts/tools/dx/migrate_conf_d.py --conf-d conf.d/ --dry-run
    python3 scripts/tools/dx/migrate_conf_d.py --conf-d conf.d/ --apply
    python3 scripts/tools/dx/migrate_conf_d.py --conf-d conf.d/ --dry-run --infer-from metadata

Generates `git mv` commands to restructure flat tenant YAML files into
domain/region/env/ subdirectories (ADR-017). Uses git mv to preserve
file history.

Modes:
    --dry-run   Print git mv commands without executing (default)
    --apply     Execute git mv commands (requires clean working tree)

Inference:
    --infer-from metadata   Use _metadata.domain/region/environment to place files
                            (default behavior; skip files without _metadata)
"""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    yaml = None


def _load_yaml(path: Path) -> dict:
    """Load a YAML file."""
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    if yaml:
        return yaml.safe_load(content) or {}
    raise RuntimeError("PyYAML is required. Install: pip install pyyaml")


def _extract_metadata(data: dict) -> dict | None:
    """Extract _metadata from a tenant YAML file."""
    tenants = data.get("tenants", {})
    if not isinstance(tenants, dict):
        return None
    for tid, tconfig in tenants.items():
        if isinstance(tconfig, dict) and "_metadata" in tconfig:
            return tconfig["_metadata"]
    return None


def plan_migration(conf_d: Path) -> list[dict]:
    """Scan flat conf.d/ and plan git mv operations.

    Returns a list of migration actions:
    [
        {
            "source": "tenant-a.yaml",
            "target": "finance/us-east/prod/tenant-a.yaml",
            "tenant_id": "tenant-a",
            "metadata": {"domain": "finance", "region": "us-east", "environment": "prod"},
            "status": "ok" | "skip_no_metadata" | "skip_already_nested" | "skip_system_file"
        }
    ]
    """
    actions = []

    for fp in sorted(conf_d.iterdir()):
        if fp.is_dir():
            # Already nested — skip
            actions.append({
                "source": fp.name + "/",
                "target": fp.name + "/",
                "tenant_id": None,
                "metadata": None,
                "status": "skip_already_nested",
            })
            continue

        if not fp.suffix in (".yaml", ".yml"):
            continue

        if fp.name.startswith("_"):
            actions.append({
                "source": fp.name,
                "target": fp.name,
                "tenant_id": None,
                "metadata": None,
                "status": "skip_system_file",
            })
            continue

        data = _load_yaml(fp)
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

    return actions


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


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Migrate flat conf.d/ to hierarchical layout (ADR-017).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--conf-d", "-c", type=str, required=True,
        help="Path to conf.d/ directory",
    )
    parser.add_argument(
        "--dry-run", action="store_true", default=True,
        help="Print git mv commands without executing (default)",
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
        sys.exit(1)

    actions = plan_migration(conf_d)

    # Summary
    ok = [a for a in actions if a["status"] == "ok"]
    skipped_meta = [a for a in actions if a["status"] == "skip_no_metadata"]
    skipped_nested = [a for a in actions if a["status"] == "skip_already_nested"]
    skipped_sys = [a for a in actions if a["status"] == "skip_system_file"]

    print(f"📂 Scanned {conf_d}")
    print(f"   Migratable:        {len(ok)}")
    print(f"   Skip (no metadata): {len(skipped_meta)}")
    print(f"   Skip (nested):      {len(skipped_nested)}")
    print(f"   Skip (system):      {len(skipped_sys)}")
    print()

    if args.output_plan:
        Path(args.output_plan).write_text(
            json.dumps(actions, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(f"✅ Plan written to {args.output_plan}")

    if not ok:
        print("ℹ️  Nothing to migrate.")
        return

    commands = generate_git_commands(actions, conf_d)

    if args.apply:
        print("🚀 Executing migration...")
        for cmd in commands:
            print(f"  $ {' '.join(cmd)}")
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if result.returncode != 0:
                print(f"  ❌ Failed: {result.stderr.strip()}", file=sys.stderr)
                sys.exit(1)
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
