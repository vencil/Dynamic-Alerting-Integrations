#!/usr/bin/env python3
"""Threshold-key reachability gate — every alert-consumed threshold key must be producible by the platform-defaults path (TRK-337 / #1189).

WHY: a rule-pack alert reads its threshold from `tenant:alert_threshold:<key>`,
which is populated from `user_threshold{component,metric}` — emitted by the
threshold-exporter for keys present in `c.Defaults`
(`resolveBaseRows` iterates `c.Defaults`; `resolveCriticalRows` requires the
base key in `c.Defaults`).

⚠️ There is now a THIRD emission path (#1189 / TRK-337): `resolveDeclaredRows`
emits a key listed in `optional_overrides` when — and only when — the TENANT
supplied a value. This gate deliberately keeps measuring the platform-defaults
path, because "the platform can produce it" and "a tenant happened to set it"
are different questions and only the first is a property of what we ship. But
it does mean the class-A remediation this gate prescribes below ("wrong tier —
move it to defaults") is no longer the only fix: for a key meant to be
tenant-calibrated, declaring it and letting the tenant set it is the intended
end state, and moving it to `defaults:` would arm it for everyone instead.

The platform-defaults surface is produced by
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

KNOWN_UNWIRED: the keys already dead at the time this gate landed (18 then;
the 9 B/C/D/E identity repairs shipped in #1231, 9 A-class tier moves remain).
They are grandfathered as INFO (not errors) with a pointer to the tracking
issue so the gate could merge without first fixing all of them — the real
fixes (move / rename / delete per root cause) land per root-cause class in
follow-up PRs. The allowlist is EXIT-LOCKED (same
discipline as KNOWN_DEFERRED): a grandfathered key that becomes reachable, or
disappears from the alert demand, is a HARD error — this forces the list to
shrink as fixes land, so it can never rot into a permanent silent exemption.

TWO SUPPLY FACES (P1-C). The check above answers "can the platform-defaults
path produce this key" using `scaffold_tenant.generate_defaults()` — a TOOL'S
CAPABILITY. That is the right question for "is this key dead", but it is not
what an operator who ran `helm install` receives. The chart ships
`thresholdConfig.defaults`, which is far smaller. So the same demand set is
classified against BOTH faces:

  1. chart-armed          — the shipped chart supplies it
  2. NOT_CHART_ARMED      — the scaffold path can supply it, the chart does not
                            (exit-locked ledger; operator must supply)
  3. KNOWN_UNWIRED        — neither face can supply it (exit-locked ledger)

Before this face existed the gate was green while describing face 1+2 as one
number, so "how many shipped alerts cannot fire" was never measured by
anything. ⚠️ Both faces use the same reachability rule, which means
"chart-armed" says A TENANT CAN ARM IT — not that the alert fires out of the
box. `_critical` keys count as reachable via their base, yet
`resolveCriticalRows` iterates TENANT OVERRIDES: no `_critical` key is ever
platform-supplied, and none is in the chart set today.

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

# The alert-demanded threshold keys that the platform-defaults path cannot
# yet supply. Grandfathered as INFO (see module docstring). Value = one-line
# root-cause tag; the real fixes are tracked in the TRK-337 follow-ups.
# Started at 18 when the gate landed; the B/C/D/E identity repairs shipped in
# #1231 (rename/move/add/delete — 9 keys removed via the exit-lock below),
# leaving the 9 A-class tier moves (a deliberate ship-enabled decision per
# pack, not a mechanical fix — #1196).
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
    "clickhouse_max_part_count": "A: in optional_overrides, move to defaults (TRK-337)",
    "clickhouse_replication_queue": "A: in optional_overrides, move to defaults (TRK-337)",
    "clickhouse_memory_tracking_bytes": "A: in optional_overrides, move to defaults (TRK-337)",
}


# ── Second supply face: what the SHIPPED chart actually installs ────────────
# `_supply()` answers "what CAN the platform-defaults path produce" — it reads
# the scaffold generator. That is the right question for "is this key dead",
# but it is NOT the question an operator who just ran `helm install` cares
# about. The chart ships `thresholdConfig.defaults`, a much smaller set than
# what the scaffold tool is capable of emitting. Everything in between only
# becomes real if somebody runs the onboarding tool or hand-writes the
# defaults — and until this face existed, nothing said so: the gate was green
# while reporting a number that described a tool's capability, not a
# deployment.
#
# ⚠️ "chart-armed" here means A TENANT CAN ARM IT, not "it fires out of the
# box". A `_critical` key counts as reachable when its BASE is supplied (the
# `resolveCriticalRows` path) — but `resolveCriticalRows` iterates TENANT
# OVERRIDES, so no `_critical` key is ever platform-supplied. Today 0 of the 16
# demanded `_critical` keys sit in the chart set and structurally none can.
NOT_CHART_ARMED: frozenset[str] = frozenset({
    "clickhouse_active_connections",
    "clickhouse_queries_rate",
    "db2_bufferpool_hit_ratio",
    "db2_connections_active",
    "db2_lock_wait_time",
    "es_disk_usage_percent",
    "es_heap_usage_percent",
    "es_pending_tasks",
    "es_search_latency_ms",
    "jvm_gc_pause",
    "jvm_gc_pause_critical",
    "jvm_memory",
    "jvm_memory_critical",
    "jvm_threads",
    "jvm_threads_critical",
    "kafka_active_controllers",
    "kafka_broker_count",
    "kafka_consumer_lag",
    "kafka_consumer_lag_critical",
    "kafka_request_rate",
    "kafka_request_rate_critical",
    "kafka_under_replicated_partitions",
    "kafka_under_replicated_partitions_critical",
    "mongodb_connections_current",
    "mongodb_opcounters_rate",
    "mongodb_replication_lag",
    "nginx_connections",
    "nginx_connections_critical",
    "nginx_request_rate",
    "nginx_request_rate_critical",
    "nginx_waiting",
    "nginx_waiting_critical",
    "oracle_sessions_active",
    "oracle_tablespace_used_percent",
    "rabbitmq_connections",
    "rabbitmq_consumers",
    "rabbitmq_node_mem_percent",
    "rabbitmq_node_mem_percent_critical",
    "rabbitmq_queue_messages",
    "rabbitmq_queue_messages_critical",
    "rabbitmq_unacked_messages",
    "redis_connected_clients",
    "redis_evicted_keys_rate",
    "redis_memory_used_bytes",
    "redis_replication_lag",
})

_CHART_VALUES = PROJECT_ROOT / "helm" / "threshold-exporter" / "values.yaml"


def _supply() -> set[str]:
    """Every conf.d threshold key the platform-defaults path can produce."""
    db_packs = [k for k in scaffold_tenant.RULE_PACKS if k != "kubernetes"]
    generated = scaffold_tenant.generate_defaults(db_packs)
    return set(generated["defaults"].keys())


def _chart_supply() -> set[str]:
    """Every threshold key the SHIPPED helm chart installs as a platform default.

    Reads the artifact, not the registry: `chart_default: true` in the registry
    is the DECLARATION, `thresholdConfig.defaults` in values.yaml is what an
    operator actually gets. The two are kept in step by
    check_threshold_registry.py; reading values.yaml here means this gate keeps
    measuring the deployment even if that equivalence ever breaks.
    """
    import yaml  # local import: only this face needs it

    doc = yaml.safe_load(_CHART_VALUES.read_text(encoding="utf-8")) or {}
    return set(((doc.get("thresholdConfig") or {}).get("defaults") or {}).keys())


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
    chart_supply: set[str] | None = None,
    not_chart_armed: frozenset[str] | None = None,
) -> dict[str, list[str]]:
    """Return {errors, infos}. errors fail --ci; infos are report-only.

    Inputs default to the real extractors; hermetic tests inject synthetic sets
    to exercise each branch without editing repo artifacts.
    """
    if demand is None:
        demand = observed_map_lib.all_threshold_keys(observed_map_lib.default_pack_paths())
    injected_supply = supply is not None
    if supply is None:
        supply = _supply()
    if deferred is None:
        deferred = set(observed_map_lib.KNOWN_DEFERRED)
    if known_unwired is None:
        known_unwired = KNOWN_UNWIRED
    if chart_supply is None:
        # The chart face is a NARROWING of the supply face. A caller that
        # injected a synthetic supply but said nothing about the chart is
        # exercising the unreachability contract, not this one — defaulting to
        # the real values.yaml there would silently un-hermeticise their test
        # and flag every synthetic key. No chart info given ⇒ no narrowing.
        chart_supply = supply if injected_supply else _chart_supply()
    if not_chart_armed is None:
        not_chart_armed = frozenset() if injected_supply else NOT_CHART_ARMED

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

    # ── Chart face (C): scaffold-reachable but NOT shipped by the chart ──────
    # Scope: only keys that are reachable at all. A key that is dead
    # everywhere is already reported above; saying "…and it isn't in the chart
    # either" would be noise.
    reachable = demand - dead
    not_armed = {k for k in reachable if not _reachable(k, chart_supply, deferred)}

    for k in sorted(not_armed - not_chart_armed):
        errors.append(
            f"NOT-CHART-ARMED: threshold key {k!r} is demanded by an alert and the "
            "scaffold path can supply it, but the shipped chart does NOT — an "
            "operator who only ran `helm install` gets an alert that cannot fire. "
            "Ship it in helm/threshold-exporter/values.yaml, or add it to "
            "NOT_CHART_ARMED with a deliberate operator-supplied decision. (TRK-337)"
        )

    for k in sorted(not_armed & not_chart_armed):
        infos.append(f"not-chart-armed {k} — operator must supply this default")

    # Exit-lock, both directions — same discipline as KNOWN_UNWIRED.
    for k in sorted(not_chart_armed - not_armed):
        if k not in demand:
            errors.append(
                f"STALE-EXEMPTION: {k!r} is in NOT_CHART_ARMED but no alert demands "
                "it anymore — remove it from NOT_CHART_ARMED."
            )
        elif k in dead:
            errors.append(
                f"STALE-EXEMPTION: {k!r} is in NOT_CHART_ARMED but is now UNREACHABLE "
                "everywhere — it belongs in KNOWN_UNWIRED, not here."
            )
        else:
            errors.append(
                f"STALE-EXEMPTION: {k!r} is in NOT_CHART_ARMED but the chart now "
                "ships it — remove it from NOT_CHART_ARMED so the gate protects it."
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
        help=i18n_text("新增不可達 key 或 KNOWN_UNWIRED exit-lock 破口即 exit 1（列管中的 A 類為 INFO）",
                       "exit 1 on NEW unreachable keys or a KNOWN_UNWIRED exit-lock breach (the tracked A-class keys are INFO)"))
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

    n_unwired = sum(1 for m in infos if m.startswith("known-unwired "))
    n_not_armed = sum(1 for m in infos if m.startswith("not-chart-armed "))
    print(
        f"✅ threshold reachability OK — {n_unwired} unreachable-everywhere "
        f"(KNOWN_UNWIRED, TRK-337) / {n_not_armed} reachable but NOT shipped by "
        "the chart (NOT_CHART_ARMED — operator must supply), 0 new drift.",
        file=sys.stderr)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
