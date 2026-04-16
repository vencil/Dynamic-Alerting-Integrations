#!/usr/bin/env python3
"""Describe effective tenant config — resolve _defaults.yaml inheritance chain.

Usage:
    python3 scripts/tools/dx/describe_tenant.py <tenant-id> [--conf-d PATH]
    python3 scripts/tools/dx/describe_tenant.py <tenant-id> --show-sources
    python3 scripts/tools/dx/describe_tenant.py <tenant-id> --diff <tenant-id-2>
    python3 scripts/tools/dx/describe_tenant.py <tenant-id> --what-if <path/to/_defaults.yaml>
    python3 scripts/tools/dx/describe_tenant.py --all --conf-d PATH --output effective.json

Resolves the full inheritance chain (L0→L1→L2→L3→tenant) using deep merge
with override semantics (ADR-018). Array fields are replaced, not concatenated.

Output: JSON or YAML of the effective (merged) config.
"""
import argparse
import copy
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    yaml = None  # fallback to simple parser

# ---------------------------------------------------------------------------
# Deep merge logic (ADR-018 semantics)
# ---------------------------------------------------------------------------

def deep_merge(base: dict, override: dict) -> dict:
    """Deep merge two dicts. Override wins for scalars and arrays; dicts recurse.

    ADR-018 rules:
    - Dict fields: deep merge (child adds new keys, overrides same keys)
    - Array fields: REPLACE (not concat)
    - Scalar fields: child overrides parent
    - Explicit None/null: deletes parent's key
    - _metadata: never inherited (skipped in merge)
    """
    result = copy.deepcopy(base)
    for k, v in override.items():
        if k == "_metadata":
            continue  # _metadata is never inherited
        if v is None:
            result.pop(k, None)  # explicit null = opt-out
        elif isinstance(v, dict) and isinstance(result.get(k), dict):
            result[k] = deep_merge(result[k], v)
        else:
            result[k] = copy.deepcopy(v)
    return result


# ---------------------------------------------------------------------------
# Filesystem scanning
# ---------------------------------------------------------------------------

def _load_yaml(path: Path) -> dict:
    """Load a YAML file, returning its parsed dict."""
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    if yaml:
        return yaml.safe_load(content) or {}
    # Minimal fallback — only works for simple flat YAML
    raise RuntimeError(f"PyYAML is required for describe-tenant. Install: pip install pyyaml")


def _file_hash(path: Path) -> str:
    """SHA-256 of file bytes."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def _canonical_hash(data: dict) -> str:
    """SHA-256 of canonical JSON representation."""
    canonical = json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


class ConfDScanner:
    """Scan a conf.d/ directory and build the inheritance graph."""

    def __init__(self, conf_d: Path):
        self.conf_d = conf_d.resolve()
        self.tenants: dict[str, dict] = {}           # tenant_id → raw config
        self.tenant_files: dict[str, Path] = {}       # tenant_id → file path
        self.defaults_chain: dict[str, list[Path]] = {}  # tenant_id → [L0, L1, ...] defaults paths
        self.defaults_data: dict[str, dict] = {}      # defaults path str → parsed defaults dict
        self._scan()

    def _scan(self) -> None:
        """Recursively scan conf.d/ and build tenant + defaults maps."""
        # Collect all _defaults.yaml files
        defaults_files: dict[str, dict] = {}
        for dp in self.conf_d.rglob("_defaults.yaml"):
            defaults_files[str(dp.resolve())] = _load_yaml(dp)
        for dp in self.conf_d.rglob("_defaults.yml"):
            defaults_files[str(dp.resolve())] = _load_yaml(dp)
        self.defaults_data = defaults_files

        # Collect all tenant files
        for fp in self.conf_d.rglob("*.yaml"):
            if fp.name.startswith("_"):
                continue
            data = _load_yaml(fp)
            if not isinstance(data, dict):
                continue
            tenants_block = data.get("tenants", {})
            if not isinstance(tenants_block, dict):
                continue
            for tid, tconfig in tenants_block.items():
                self.tenants[tid] = tconfig
                self.tenant_files[tid] = fp.resolve()
                self.defaults_chain[tid] = self._resolve_defaults_chain(fp)

        for fp in self.conf_d.rglob("*.yml"):
            if fp.name.startswith("_"):
                continue
            data = _load_yaml(fp)
            if not isinstance(data, dict):
                continue
            tenants_block = data.get("tenants", {})
            if not isinstance(tenants_block, dict):
                continue
            for tid, tconfig in tenants_block.items():
                if tid not in self.tenants:
                    self.tenants[tid] = tconfig
                    self.tenant_files[tid] = fp.resolve()
                    self.defaults_chain[tid] = self._resolve_defaults_chain(fp)

    def _resolve_defaults_chain(self, tenant_file: Path) -> list[Path]:
        """Walk from tenant file up to conf.d/ root, collecting _defaults.yaml at each level."""
        chain: list[Path] = []
        current = tenant_file.resolve().parent
        root = self.conf_d

        while True:
            for name in ("_defaults.yaml", "_defaults.yml"):
                dp = current / name
                if dp.exists():
                    chain.append(dp.resolve())
            if current == root or current == current.parent:
                break
            current = current.parent

        chain.reverse()  # L0 (root) first, L3 (nearest) last
        return chain

    def effective_config(self, tenant_id: str) -> dict:
        """Compute effective config by merging defaults chain + tenant config."""
        if tenant_id not in self.tenants:
            raise KeyError(f"Tenant '{tenant_id}' not found in {self.conf_d}")

        merged = {}
        # Apply defaults chain (L0 → L3)
        for dp in self.defaults_chain[tenant_id]:
            ddata = self.defaults_data.get(str(dp), {})
            defaults_block = ddata.get("defaults", ddata)
            merged = deep_merge(merged, defaults_block)

        # Apply tenant config (highest priority)
        tenant_raw = self.tenants[tenant_id]
        merged = deep_merge(merged, tenant_raw)

        return merged

    def source_info(self, tenant_id: str) -> dict:
        """Return source traceability for a tenant."""
        if tenant_id not in self.tenants:
            raise KeyError(f"Tenant '{tenant_id}' not found")

        chain = self.defaults_chain[tenant_id]
        effective = self.effective_config(tenant_id)
        source_h = _file_hash(self.tenant_files[tenant_id])
        merged_h = _canonical_hash(effective)

        return {
            "tenant_id": tenant_id,
            "source_file": str(self.tenant_files[tenant_id].relative_to(self.conf_d)),
            "source_hash": source_h,
            "merged_hash": merged_h,
            "defaults_chain": [
                str(p.relative_to(self.conf_d)) for p in chain
            ],
            "effective_config": effective,
        }

    def diff_tenants(self, id_a: str, id_b: str) -> dict:
        """Compare effective configs of two tenants."""
        eff_a = self.effective_config(id_a)
        eff_b = self.effective_config(id_b)

        only_a, only_b, different = {}, {}, {}
        all_keys = set(eff_a.keys()) | set(eff_b.keys())
        for k in sorted(all_keys):
            if k not in eff_b:
                only_a[k] = eff_a[k]
            elif k not in eff_a:
                only_b[k] = eff_b[k]
            elif eff_a[k] != eff_b[k]:
                different[k] = {"a": eff_a[k], "b": eff_b[k]}

        return {
            "tenant_a": id_a,
            "tenant_b": id_b,
            f"only_in_{id_a}": only_a,
            f"only_in_{id_b}": only_b,
            "different": different,
        }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Describe effective tenant config with _defaults.yaml inheritance (ADR-018).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "tenant_id", nargs="?",
        help="Tenant ID to describe (omit with --all)",
    )
    parser.add_argument(
        "--conf-d", "-c", type=str, default=None,
        help="Path to conf.d/ directory (default: auto-detect from repo root)",
    )
    parser.add_argument(
        "--show-sources", "-s", action="store_true",
        help="Show inheritance chain sources for each field",
    )
    parser.add_argument(
        "--diff", "-d", type=str, default=None, metavar="TENANT_ID_2",
        help="Diff effective config against another tenant",
    )
    parser.add_argument(
        "--what-if", "-w", type=str, default=None, metavar="DEFAULTS_PATH",
        help="Simulate effect of a modified _defaults.yaml (not yet implemented)",
    )
    parser.add_argument(
        "--all", action="store_true",
        help="Dump all tenants' effective configs",
    )
    parser.add_argument(
        "--output", "-o", type=str, default=None,
        help="Output file (default: stdout). Used with --all.",
    )
    parser.add_argument(
        "--format", "-f", choices=["json", "yaml"], default="json",
        help="Output format (default: json)",
    )
    args = parser.parse_args()

    # Resolve conf.d path
    if args.conf_d:
        conf_d = Path(args.conf_d)
    else:
        repo_root = Path(__file__).resolve().parent.parent.parent.parent
        conf_d = repo_root / "conf.d"
        if not conf_d.exists():
            # Try tests/fixtures as fallback
            for fixture_dir in sorted((repo_root / "tests" / "fixtures").glob("synthetic-*")):
                candidate = fixture_dir / "conf.d"
                if candidate.exists():
                    conf_d = candidate
                    break

    if not conf_d.exists():
        print(f"❌ conf.d/ not found at {conf_d}", file=sys.stderr)
        print(f"   Use --conf-d to specify the path.", file=sys.stderr)
        sys.exit(1)

    scanner = ConfDScanner(conf_d)
    print(f"📂 Scanned {conf_d}: {len(scanner.tenants)} tenants, {len(scanner.defaults_data)} defaults files", file=sys.stderr)

    def _output(data: Any) -> str:
        if args.format == "yaml":
            if yaml:
                return yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False)
            return json.dumps(data, indent=2, ensure_ascii=False)
        return json.dumps(data, indent=2, ensure_ascii=False)

    # --all mode
    if args.all:
        result = {}
        for tid in sorted(scanner.tenants.keys()):
            info = scanner.source_info(tid)
            result[tid] = info
        out = _output(result)
        if args.output:
            Path(args.output).write_text(out, encoding="utf-8")
            print(f"✅ Written {len(result)} tenants to {args.output}", file=sys.stderr)
        else:
            print(out)
        return

    if not args.tenant_id:
        parser.error("tenant_id is required (or use --all)")

    tid = args.tenant_id
    if tid not in scanner.tenants:
        print(f"❌ Tenant '{tid}' not found. Available: {', '.join(sorted(scanner.tenants.keys())[:10])}...", file=sys.stderr)
        sys.exit(1)

    # --diff mode
    if args.diff:
        if args.diff not in scanner.tenants:
            print(f"❌ Tenant '{args.diff}' not found.", file=sys.stderr)
            sys.exit(1)
        result = scanner.diff_tenants(tid, args.diff)
        print(_output(result))
        return

    # --what-if mode (stub)
    if args.what_if:
        print("⚠️  --what-if is not yet implemented. Coming in Phase .b B-2.", file=sys.stderr)
        sys.exit(0)

    # Default: show effective config
    if args.show_sources:
        result = scanner.source_info(tid)
    else:
        result = {
            "tenant_id": tid,
            "effective_config": scanner.effective_config(tid),
        }
    print(_output(result))


if __name__ == "__main__":
    main()
