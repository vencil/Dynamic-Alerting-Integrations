#!/usr/bin/env python3
"""Threshold-key reachability gate — every alert-consumed threshold key must be
producible by the platform-defaults path (TRK-337 / #1189).

WHY: a rule-pack alert reads its threshold from `tenant:alert_threshold:<key>`,
which is populated from `user_threshold{component,metric}` — emitted by the
threshold-exporter ONLY for keys present in `c.Defaults`
(`resolveBaseRows` iterates `c.Defaults`; `resolveCriticalRows` requires the
base key in `c.Defaults`). The platform-defaults surface is produced by
`scaffold_tenant.generate_defaults()`. So an alert that demands a key which
`generate_defaults()` never produces is DEAD — it can never fire, and nothing
says so. This is a DECLARED-BUT-UNWIRED failure: schema validation can't catch
it (it's a cross-artifact topology gap between the alert side and the
config-generation side, maintained as separate hand-copied contracts).

WHAT THIS CHECKS (identity comparison, not substring): the set of conf.d keys
DEMANDED by alerts (via `_observed_map_lib.all_threshold_keys`, the same
extractor the observed-map drift-guard uses) must be a subset of what the
platform-defaults path can SUPPLY, under the exporter's reachability rules:
  - a `_critical` key is reachable iff its base (strip `_critical`) is supplied
    (resolveCriticalRows path);
  - any other key is reachable iff it is itself supplied (resolveBaseRows path);
  - KNOWN_DEFERRED keys (threshold lives in a `:core` recording rule, not a
    `- alert:`, so the alert-based extractor can't reach them) are exempt —
    reused verbatim from `_observed_map_lib` so the two guards agree.

KNOWN_UNWIRED: the 18 keys already dead at the time this gate landed. They are
grandfathered as INFO (not errors) with a pointer to the tracking issue so the
gate can merge without first fixing all 18 — the real fixes (move / rename /
delete per root cause) land in follow-up PRs. The allowlist is EXIT-LOCKED (same
discipline as KNOWN_DEFERRED): a grandfathered key that becomes reachable, or
disappears from the alert demand, is a HARD error — this forces the list to
shrink as fixes land, so it can never rot into a permanent silent exemption.

Exit codes (_lib_exitcodes): 0 clean / 1 violation (--ci) / 2 caller error.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]

_OPS = PROJECT_ROOT / "scripts" / "tools" / "ops"
sys.path.insert(0, str(_OPS))
import _observed_map_lib as observed_map_lib  # noqa: E402
import scaffold_tenant  # noqa: E402

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)
sys.path.insert(0, os.path.join(_THIS_DIR, ".."))
from _lib_compat import try_utf8_stdout  # noqa: E402
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR  # noqa: E402
from _lib_validation import i18n_text  # noqa: E402

# The 18 alert-demanded threshold keys that the platform-defaults path cannot
# yet supply. Grandfathered as INFO (see module docstring). Value = one-line
# root-cause tag; the real fixes are tracked in the TRK-337 follow-ups.
#   A = name-correct, wrong tier (in optional_overrides) → move to defaults
#   B = name is wrong in scaffold (e.g. _total vs _rate) → rename + move
#   C = base default exists but under a different name → align identity
#   D = key absent from scaffold entirely → add
#   E = no alert actually consumes it elsewhere / orphan → delete
KNOWN_UNWIRED: dict[str, str] = {
    "oracle_wait_time_rate": "A: in optional_overrides, move to defaults (TRK-337)",
    "oracle_process_count": "A: in optional_overrides, move to defaults (TRK-337)",
    "oracle_pga_allocated_bytes": "A: in optional_overrides, move to defaults (TRK-337)",
    "db2_log_usage_percent": "A: in optional_overrides, move to defaults (TRK-337)",
    "db2_deadlock_rate": "A: in optional_overrides, move to defaults (TRK-337)",
    "db2_tablespace_used_percent": "A: in optional_overrides, move to defaults (TRK-337)",
    "db2_lock_wait_time": "D: absent from scaffold entirely (TRK-337)",
    "clickhouse_max_part_count": "A: in optional_overrides, move to defaults (TRK-337)",
    "clickhouse_replication_queue": "A: in optional_overrides, move to defaults (TRK-337)",
    "clickhouse_memory_tracking_bytes": "A: in optional_overrides, move to defaults (TRK-337)",
    "redis_evicted_keys_rate": "B: scaffold has _total, alert wants _rate (TRK-337)",
    "redis_replication_lag": "C: scaffold has a differently-named lag key (TRK-337)",
    "mongodb_opcounters_rate": "B: scaffold has _total, alert wants _rate (TRK-337)",
    "mongodb_replication_lag": "C: scaffold has repl_lag_seconds (TRK-337)",
    "es_heap_usage_percent": "C: scaffold base default name differs (TRK-337)",
    "es_disk_usage_percent": "C: scaffold base default name differs (TRK-337)",
    "es_pending_tasks": "C: scaffold base default name differs (TRK-337)",
    "es_search_latency_ms": "C: scaffold base default name differs (TRK-337)",
}


def _supply() -> set[str]:
    """Every conf.d threshold key the platform-defaults path can produce."""
    db_packs = [k for k in scaffold_tenant.RULE_PACKS if k != "kubernetes"]
    generated = scaffold_tenant.generate_defaults(db_packs)
    return set(generated["defaults"].keys())


def _reachable(key: str, supply: set[str], deferred: set[str]) -> bool:
    if key in deferred:
        return True
    if key.endswith("_critical"):
        return key[: -len("_critical")] in supply
    return key in supply


def run_check(
    demand: set[str] | None = None,
    supply: set[str] | None = None,
    deferred: set[str] | None = None,
    known_unwired: dict[str, str] | None = None,
) -> dict[str, list[str]]:
    """Return {errors, infos}. errors fail --ci; infos are report-only.

    Inputs default to the real extractors; hermetic tests inject synthetic sets
    to exercise each branch without editing repo artifacts.
    """
    if demand is None:
        demand = observed_map_lib.all_threshold_keys(observed_map_lib.default_pack_paths())
    if supply is None:
        supply = _supply()
    if deferred is None:
        deferred = set(observed_map_lib.KNOWN_DEFERRED)
    if known_unwired is None:
        known_unwired = KNOWN_UNWIRED

    dead = {k for k in demand if not _reachable(k, supply, deferred)}

    errors: list[str] = []
    infos: list[str] = []

    # NEW dead keys (not grandfathered) — a fresh declared-but-unwired drift.
    for k in sorted(dead - set(known_unwired)):
        errors.append(
            f"UNREACHABLE: alert-demanded threshold key {k!r} is not produced by "
            "generate_defaults() and is not a known-deferred key — this alert can "
            "never fire. Add the key to the platform defaults, or fix the name "
            "mismatch. (TRK-337)"
        )

    # Grandfathered keys are report-only WHILE still dead...
    for k in sorted(dead & set(known_unwired)):
        infos.append(f"known-unwired {k} — {known_unwired[k]}")

    # ...but the allowlist is exit-locked: a grandfathered key that got FIXED
    # (now reachable) or was REMOVED from the packs must be dropped from the
    # list, else it rots into a permanent silent exemption.
    for k in sorted(set(known_unwired) - dead):
        if k in demand:
            errors.append(
                f"STALE-EXEMPTION: {k!r} is in KNOWN_UNWIRED but is now REACHABLE — "
                "the fix landed; remove it from KNOWN_UNWIRED so the gate protects it."
            )
        else:
            errors.append(
                f"STALE-EXEMPTION: {k!r} is in KNOWN_UNWIRED but no alert demands it "
                "anymore — remove it from KNOWN_UNWIRED."
            )

    return {"errors": errors, "infos": infos}


def main(argv: list[str] | None = None) -> int:
    try_utf8_stdout()
    parser = argparse.ArgumentParser(
        description=i18n_text(
            "閾值 key 可達性 gate：每個 alert 消費的閾值 key 都須能由平台 defaults 產生（TRK-337）",
            "Threshold-key reachability gate: every alert-consumed key must be "
            "producible by the platform-defaults path (TRK-337)"))
    parser.add_argument(
        "--ci", action="store_true",
        help=i18n_text("僅在出現新的不可達 key 時 exit 1（已知的 18 個為 INFO）",
                       "exit 1 only on NEW unreachable keys (the known 18 are INFO)"))
    args = parser.parse_args(argv)

    try:
        result = run_check()
    except Exception as exc:  # noqa: BLE001 — caller error, not a violation
        print(f"ERROR: reachability check crashed: {exc}", file=sys.stderr)
        return EXIT_CALLER_ERROR

    errors = result["errors"]
    infos = result["infos"]

    for msg in infos:
        print(f"INFO: {msg}", file=sys.stderr)
    for msg in errors:
        print(f"❌ {msg}", file=sys.stderr)

    if errors:
        print(
            f"\n{len(errors)} threshold-reachability violation(s) — TRK-337.\n"
            "A declared-but-unwired alert key can never fire and no other gate "
            "catches it. See scripts/tools/lint/check_threshold_reachability.py.",
            file=sys.stderr)
        return EXIT_VIOLATION if args.ci else EXIT_OK

    print(
        f"✅ threshold reachability OK — {len(infos)} known-unwired "
        "(grandfathered, TRK-337), 0 new drift.",
        file=sys.stderr)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
