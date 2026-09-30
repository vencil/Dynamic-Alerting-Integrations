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
    1  — usage / IO error, or a selected `_defaults.yaml` anywhere in the
         tree that does not parse or has an unsupported shape (named on
         stderr)
    2  — verification failed (--expect-merged-hash mismatch, tenant not
         found, or tenant declared in more than one file); with --all,
         any tenant declared in more than one file

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
import os

# Pull `try_utf8_stdout` from the shared compat lib at scripts/tools/.
# Migrated in #489 Phase B (was missing encoding setup → would crash on
# legacy Windows cp950/cp936 consoles when printing emoji to stdout).
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, str(_THIS_DIR))
sys.path.insert(0, os.path.join(str(_THIS_DIR), ".."))
from _lib_compat import try_utf8_stdout  # noqa: E402

# Exit-code contract — SANCTIONED EXCEPTION to the #452 0/1/2 SSOT
# (scripts/tools/_lib_exitcodes.py). tenant-verify predates the SSOT and
# its codes are load-bearing in a shipped customer contract: the
# docs/cli-reference.md §tenant-verify "Exit codes" table AND the Emergency
# Rollback runbook (docs/scenarios/incremental-migration-playbook.md
# checklist item 6 keys on "exit 2 = mismatch"). Here exit 2 is a FINDING
# (verification failed) and exit 1 is a caller error — the inverse of the
# SSOT. Realigning would be a BREAKING change to that contract; do not
# change without a coordinated doc + runbook migration + CHANGELOG note.
# Mirrors diag_pr_ci.py's documented extension.
EXIT_PASS = 0           # verification passed (tenant exists; hash matched if given)
EXIT_USAGE_ERROR = 1    # usage / IO error (bad args, conf.d missing, mutually-excl flags)
EXIT_VERIFY_FAILED = 2  # mismatch, not found, or duplicate (rollback-checklist signal)

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


def _duplicate_detail(tenant_id, files: list[str]) -> str:
    """tenant-verify's own wording for a duplicate declaration.

    ⛔ Not `ConfDScanner.duplicate_error`: that message is describe's
    ("Not describing any of them"). Same predicate, this tool's verdict.
    """
    return (f"tenant '{tenant_id}' is declared in {len(files)} files: "
            f"{', '.join(files)}. Cannot verify it — no merged_hash is "
            f"computed for an ambiguous tenant. Delete the extra "
            f"declaration so the tenant lives in exactly one file, then "
            f"re-run tenant-verify.")


def verify_one(scanner,tenant_id: str, expect_merged_hash: str | None) -> tuple[dict, int]:
    """Verify a single tenant. Returns (info_dict, exit_code).

    info_dict shape:
      { "tenant_id": ..., "source_hash": ..., "merged_hash": ...,
        "defaults_chain": [...], "expected_merged_hash": ... | null,
        "match": bool | null }
    or, on a finding, { "tenant_id": ..., "error": "not_found" } /
      { "tenant_id": ..., "error": "duplicate", "files": [...],
        "detail": ... }

    #2093: a tenant declared by more than one conf.d file is refused
    BEFORE any hash is computed. The scanner keeps one declaration for its
    library callers, and which one depends on filename order — so hashing
    it let rollback checklist item 6 pass (rc 0, `[OK]`) whenever the
    stray file sorted first, and fail as a plain MISMATCH when it sorted
    last. `files` is the scanner's duplicate list (sorted, conf.d-relative
    POSIX paths), the same predicate describe_tenant refuses on (#2049).
    """
    files = scanner.duplicates.get(tenant_id)
    if files:
        return ({
            "tenant_id": tenant_id,
            "error": "duplicate",
            "files": list(files),
            "detail": _duplicate_detail(tenant_id, files),
        }, EXIT_VERIFY_FAILED)
    try:
        info = scanner.source_info(tenant_id)
    except KeyError:
        return ({"tenant_id": tenant_id, "error": "not_found"}, EXIT_VERIFY_FAILED)

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
        return (out, EXIT_PASS if out["match"] else EXIT_VERIFY_FAILED)
    return (out, EXIT_PASS)


def verify_all(scanner) -> list[dict]:
    """Verify every tenant in the scanner. Returns list of info dicts.

    A tenant declared in more than one file comes back as an error entry
    (`error: "duplicate"`, `files`, no `merged_hash`); every other tenant
    is still reported. `main` turns any such entry into exit 2 (#2093).
    """
    results = []
    for tid in sorted(scanner.tenants.keys()):
        info, _ = verify_one(scanner, tid, expect_merged_hash=None)
        results.append(info)
    return results


def _duplicated(results: list[dict]) -> list[dict]:
    """The --all entries refused as declared in more than one file."""
    return [r for r in results if r.get("error") == "duplicate"]


def _print_human(info: dict) -> None:
    """Pretty-print one tenant's verify result for human eyeballs."""
    print(f"tenant_id:     {info['tenant_id']}")
    if "error" in info:
        print(f"  status:      ERROR — {info['error']}")
        if info["error"] == "duplicate":
            # Every carrier, not just the one the scan kept: rollback
            # checklist item 6 runs without --json (#2093).
            for f in info["files"]:
                print(f"  declared in: {f}")
            print("  fix:         keep the tenant in exactly one file "
                  "(remove the extra declaration), then re-run")
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
    try_utf8_stdout()
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
        return EXIT_USAGE_ERROR

    if args.all and args.expect_merged_hash:
        print(
            "error: --expect-merged-hash is incompatible with --all "
            "(use one tenant at a time, or compare two snapshot JSON files)",
            file=sys.stderr,
        )
        return EXIT_USAGE_ERROR

    conf_d = Path(args.conf_d).resolve()
    if not conf_d.is_dir():
        print(f"error: conf.d not found: {conf_d}", file=sys.stderr)
        return EXIT_USAGE_ERROR

    describe_mod = _load_describe_module()
    try:
        scanner = describe_mod.ConfDScanner(conf_d)
    except describe_mod.DefaultsParseError as exc:
        # #2459: a selected `_defaults.yaml` anywhere in the tree that does
        # not parse (DefaultsParseError) or has an unsupported shape
        # (DefaultsShapeError, its subclass) stops the scan, so no
        # merged_hash is reported for any tenant. Named on stderr as an input error
        # (exit 1), not a traceback — and not exit 2, which the rollback
        # checklist reads as "hash mismatch".
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE_ERROR

    if args.all:
        results = verify_all(scanner)
        dups = _duplicated(results)
        if args.json:
            print(json.dumps({"tenants": results}, indent=2, ensure_ascii=False))
        else:
            for r in results:
                _print_human(r)
                print()
            # Verified and refused are counted apart: a duplicated tenant
            # has no merged_hash, so it must not read as one of N verified.
            print(f"# total: {len(results) - len(dups)} tenants verified, "
                  f"{len(dups)} duplicate-declared (not verified) in {conf_d}")
        if dups:
            print(f"error: {len(dups)} tenant(s) declared in more than one "
                  f"file, no merged_hash reported for them: "
                  f"{', '.join(str(r['tenant_id']) for r in dups)}",
                  file=sys.stderr)
            return EXIT_VERIFY_FAILED
        return EXIT_PASS

    info, exit_code = verify_one(scanner, args.tenant_id, args.expect_merged_hash)
    if args.json:
        print(json.dumps(info, indent=2, ensure_ascii=False))
    else:
        _print_human(info)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
