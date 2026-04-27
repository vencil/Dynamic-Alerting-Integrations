#!/usr/bin/env python3
"""Verify a tenant's effective config — print merged_hash and source_hash.

Use case: B-4 Emergency Rollback Procedures verification checklist (item 6
in `docs/scenarios/incremental-migration-playbook.md` §Emergency Rollback
Procedures). After a batch-PR rollback wave, the operator needs to confirm
each tenant's `merged_hash` returned to the pre-Base-PR snapshot. This
tool prints `source_hash` + `merged_hash` for one tenant (or all tenants),
optionally comparing against a reference value and returning nonzero exit
if they diverge.

Usage:
    da-tools tenant-verify <tenant-id> [--conf-d PATH]
    da-tools tenant-verify <tenant-id> --expect-merged-hash <hash>
    da-tools tenant-verify --all [--conf-d PATH] [--json]

Exit codes:
    0  — verification passed (tenant exists; if --expect-merged-hash given,
         it matched)
    1  — usage / IO error
    2  — verification failed (--expect-merged-hash mismatch, or tenant not
         found)

Design note: this tool reuses describe_tenant.ConfDScanner for the actual
inheritance + canonical-hash computation. The wrapping here is purposely
thin — verify is a CLI ergonomics layer (terse output + exit codes) on
top of the existing describe primitives, NOT a re-implementation. See
v2.8.0 Phase B closure plan Track A item A5 for context.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

# Lazy-import describe_tenant — same dir, can't relative-import in script mode
_TOOL_DIR = Path(__file__).resolve().parent
_DESCRIBE_PATH = _TOOL_DIR / "describe_tenant.py"


def _load_describe_module():
    """Load describe_tenant.py as a module without polluting sys.path."""
    spec = importlib.util.spec_from_file_location("describe_tenant_mod", _DESCRIBE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load describe_tenant from {_DESCRIBE_PATH}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["describe_tenant_mod"] = mod
    spec.loader.exec_module(mod)
    return mod


def verify_one(scanner, tenant_id: str, expect_merged_hash: str | None) -> tuple[dict, int]:
    """Verify a single tenant. Returns (info_dict, exit_code).

    info_dict shape:
      { "tenant_id": ..., "source_hash": ..., "merged_hash": ...,
        "defaults_chain": [...], "expected_merged_hash": ... | null,
        "match": bool | null }
    """
    try:
        info = scanner.source_info(tenant_id)
    except KeyError:
        return ({"tenant_id": tenant_id, "error": "not_found"}, 2)

    out = {
        "tenant_id": info["tenant_id"],
        "source_file": info["source_file"],
        "source_hash": info["source_hash"],
        "merged_hash": info["merged_hash"],
        "defaults_chain": info["defaults_chain"],
        "expected_merged_hash": expect_merged_hash,
        "match": None,
    }
    if expect_merged_hash is not None:
        out["match"] = info["merged_hash"] == expect_merged_hash
        return (out, 0 if out["match"] else 2)
    return (out, 0)


def verify_all(scanner) -> list[dict]:
    """Verify every tenant in the scanner. Returns list of info dicts."""
    results = []
    for tid in sorted(scanner.tenants.keys()):
        info, _ = verify_one(scanner, tid, expect_merged_hash=None)
        results.append(info)
    return results


def _print_human(info: dict) -> None:
    """Pretty-print one tenant's verify result for human eyeballs."""
    print(f"tenant_id:     {info['tenant_id']}")
    if "error" in info:
        print(f"  status:      ERROR — {info['error']}")
        return
    print(f"  source_file: {info['source_file']}")
    print(f"  source_hash: {info['source_hash']}")
    print(f"  merged_hash: {info['merged_hash']}")
    if info.get("defaults_chain"):
        print(f"  inherits:    {' -> '.join(info['defaults_chain'])}")
    if info.get("expected_merged_hash") is not None:
        marker = "OK" if info["match"] else "MISMATCH"
        print(f"  expected:    {info['expected_merged_hash']}  [{marker}]")


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="da-tools tenant-verify",
        description=__doc__.split("\n\n")[0],
    )
    parser.add_argument(
        "tenant_id",
        nargs="?",
        help="Tenant ID to verify. Required unless --all is given.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Verify every tenant in the conf.d tree.",
    )
    parser.add_argument(
        "--conf-d",
        default="conf.d",
        help="Path to conf.d/ directory (default: ./conf.d).",
    )
    parser.add_argument(
        "--expect-merged-hash",
        help=(
            "Expected merged_hash. If given and the actual hash differs, "
            "exit code 2. Useful for B-4 rollback verification checklist."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of human-readable output.",
    )
    args = parser.parse_args()

    if not args.all and not args.tenant_id:
        print("error: tenant_id is required (or pass --all)", file=sys.stderr)
        return 1

    if args.all and args.expect_merged_hash:
        print(
            "error: --expect-merged-hash is incompatible with --all "
            "(use one tenant at a time, or compare two snapshot JSON files)",
            file=sys.stderr,
        )
        return 1

    conf_d = Path(args.conf_d).resolve()
    if not conf_d.is_dir():
        print(f"error: conf.d not found: {conf_d}", file=sys.stderr)
        return 1

    describe_mod = _load_describe_module()
    scanner = describe_mod.ConfDScanner(conf_d)

    if args.all:
        results = verify_all(scanner)
        if args.json:
            print(json.dumps({"tenants": results}, indent=2, ensure_ascii=False))
        else:
            for r in results:
                _print_human(r)
                print()
            print(f"# total: {len(results)} tenants in {conf_d}")
        return 0

    info, exit_code = verify_one(scanner, args.tenant_id, args.expect_merged_hash)
    if args.json:
        print(json.dumps(info, indent=2, ensure_ascii=False))
    else:
        _print_human(info)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
