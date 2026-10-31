#!/usr/bin/env python3
"""Verify a tenant's effective config — print merged_hash and source_hash.

Use case: B-4 Emergency Rollback Procedures verification checklist (item 6
in `docs/scenarios/incremental-migration-playbook.md` §Emergency Rollback
Procedures). After a batch-PR rollback wave, the operator needs to confirm
each tenant's `merged_hash` returned to the pre-Base-PR snapshot. This
tool prints `source_hash` + `merged_hash` for one tenant (or all tenants),
optionally comparing against a reference value and returning exit 1 if
they diverge.

Usage:
    da-tools tenant-verify <tenant-id> [--conf-d PATH]
    da-tools tenant-verify <tenant-id> --expect-merged-hash <hash>
    da-tools tenant-verify --all [--conf-d PATH] [--json]

Exit codes (scripts/tools/_lib_exitcodes.py — the shared 0/1/2 contract,
since #3393; before it, 1 and 2 were swapped here):
    0  — verification passed (tenant exists; if --expect-merged-hash given,
         it matched)
    1  — verification failed (--expect-merged-hash mismatch, tenant not
         found, or tenant declared in more than one file); with --all,
         any tenant declared in more than one file
    2  — nothing was verified: usage / IO error, or da-guard is missing,
         fails or refuses the tree (named on stderr), or no merged_hash to
         report: it is `da-guard effective`'s, and the exporter's load
         skips or cannot read a file of the tree, or rejects the whole tree
         because another tenant is declared twice (#1549; named on stderr
         and in the entry's `detail`); also an uncaught exception
         (traceback + one `ERROR: unexpected internal error` line) and a
         stdout that cannot be written. With --json, an exit 2 that printed
         no document of its own prints the shared `caller_error` envelope.

Design note: this tool reuses describe_tenant.ConfDScanner, whose every
field — the inheritance chain, source_hash and merged_hash — is
`da-guard effective`'s (the exporter's and tenant-api's value, #1549,
ADR-036), never one computed in Python. The wrapping here is purposely
thin — verify is a CLI ergonomics layer (terse output + exit codes) on
top of the existing describe primitives, NOT a re-implementation. See
v2.8.0 Phase B closure plan Track A item A5 for context.
"""
from __future__ import annotations

import argparse
import importlib.util
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

from _lib_exitcodes import (  # noqa: E402
    EXIT_CALLER_ERROR, EXIT_OK, EXIT_VIOLATION, Report, arm_crash_envelope,
    exit_caller_error,
)

# Exit-code contract: the #452 0/1/2 SSOT (scripts/tools/_lib_exitcodes.py).
# Until #3393 this tool inverted 1 and 2; the codes are load-bearing in a
# shipped customer contract — the docs/cli-reference.md §tenant-verify
# "Exit codes" table and the Emergency Rollback runbook
# (docs/scenarios/incremental-migration-playbook.md checklist item 6:
# exit 1 = mismatch → rollback decision; exit 2 = nothing verified → fix
# and re-run, never roll back on it). Change neither without that doc and
# runbook.
EXIT_PASS = EXIT_OK                 # verification passed (tenant exists; hash matched if given)
EXIT_VERIFY_FAILED = EXIT_VIOLATION  # mismatch, not found, or duplicate (rollback-checklist signal)
EXIT_USAGE_ERROR = EXIT_CALLER_ERROR  # nothing verified: bad args, conf.d missing,
                                      # mutually-excl flags, no merged_hash from da-guard (#1549)

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


def _describe_mod(scanner):
    """The describe_tenant module `scanner` comes from (its fixed wordings)."""
    return sys.modules[type(scanner).__module__]


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
    or, when there is no merged_hash to verify (#1549: da-guard missing or
    failing, or not resolving this tenant), { "tenant_id": ...,
      "error": "merged_hash_unavailable", "detail": ... } with exit 2 —
    nothing was compared, so not 1, which rollback checklist item 6 reads
    as a mismatch (#3393).

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
    if scanner.refused is not None:
        # The exporter rejects the whole tree (another tenant is declared
        # twice): no tenant of it has a merged_hash.
        return ({
            "tenant_id": tenant_id,
            "error": "merged_hash_unavailable",
            "detail": _describe_mod(scanner).MERGED_HASH_TREE_REFUSED,
        }, EXIT_USAGE_ERROR)
    try:
        info = scanner.source_info(tenant_id)
    except KeyError:
        # (A tree the exporter's load skips a file of is the shared conf.d
        # refusal since #3206: it never reaches here.)
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
    is still reported. `main` turns any such entry into exit 1 (#2093).
    """
    results = []
    for tid in scanner.tenant_ids():
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
        elif info["error"] == "merged_hash_unavailable":
            print(f"  detail:      {info['detail']}")
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
            "exit code 1. Useful for B-4 rollback verification checklist."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of human-readable output.",
    )
    args = parser.parse_args()
    if args.json:
        # #3393: every exit 2 below that prints no document of its own
        # prints the one `caller_error` envelope, as in every --json tool.
        arm_crash_envelope()

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
    # #3276: the shared judgement of a caller-given path. A CallerError
    # leaves through `run()` at the caller-error code 2.
    from _lib_paths import require_dir
    require_dir(conf_d, lang="en", flag="--conf-d")

    describe_mod = _load_describe_module()
    try:
        scanner = describe_mod.ConfDScanner(conf_d)
    except describe_mod.DescribeError as exc:
        # da-guard missing, failing or refusing the tree: no merged_hash is
        # reported for any tenant. Named on stderr as an input error (exit
        # 2), not a traceback — and not exit 1, which the rollback checklist
        # reads as "hash mismatch".
        # With --json, the envelope names the head line (not da-guard's
        # last one) under the shared `served_values_unavailable` reason.
        lines = exc.lines or [str(exc)]
        print(f"error: {lines[0]}", file=sys.stderr)
        for line in lines[1:]:
            print(line, file=sys.stderr)
        exit_caller_error(f"ERROR: {lines[0]}", "served_values_unavailable")
    describe_mod.warn_not_read(scanner)
    if scanner.refused is not None:
        # The exporter rejects the whole tree: no merged_hash anywhere.
        # da-guard's own reason, once.
        print(f"error: {scanner.refused}", file=sys.stderr)
        for line in scanner.stderr_lines:
            print(f"{describe_mod.DA_GUARD_PREFIX}{line}", file=sys.stderr)

    if args.all:
        results = verify_all(scanner)
        dups = _duplicated(results)
        unavailable = [r for r in results if r.get("error") == "merged_hash_unavailable"]
        if not args.json:
            for r in results:
                _print_human(r)
                print()
            # Verified and refused are counted apart: a duplicated tenant
            # has no merged_hash, so it must not read as one of N verified.
            print(f"# total: {len(results) - len(dups) - len(unavailable)} tenants verified, "
                  f"{len(dups)} duplicate-declared (not verified), "
                  f"{len(unavailable)} without merged_hash (not verified) in {conf_d}")
        status = EXIT_PASS
        if dups:
            print(f"error: {len(dups)} tenant(s) declared in more than one "
                  f"file, no merged_hash reported for them: "
                  f"{', '.join(str(r['tenant_id']) for r in dups)}",
                  file=sys.stderr)
            status = EXIT_VERIFY_FAILED
        elif unavailable:
            print(f"error: no merged_hash for {len(unavailable)} tenant(s) — it is "
                  f"da-guard effective's (see above): "
                  f"{', '.join(str(r['tenant_id']) for r in unavailable)}",
                  file=sys.stderr)
            status = EXIT_USAGE_ERROR
        # #3216: run() encodes the --json document
        return Report({"tenants": results}, status) if args.json else status

    info, exit_code = verify_one(scanner, args.tenant_id, args.expect_merged_hash)
    if args.json:
        return Report(info, exit_code)
    _print_human(info)
    return exit_code


# An uncaught exception and a stdout that cannot be written end at the
# shared codes (crash 2, #3090; caller error 2, #3124) since #3393.
if __name__ == "__main__":
    import _lib_exitcodes  # noqa: E402

    _lib_exitcodes.run(main)
