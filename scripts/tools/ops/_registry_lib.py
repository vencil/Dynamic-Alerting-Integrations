#!/usr/bin/env python3
"""_registry_lib.py — threshold-registry SoT loader / validator / query lib (TRK-339 WS1a / #1200).

Epic #1200 D2 (LOCKED, 2026-07-23): the threshold contract gets a standalone
YAML registry as its Source of Truth — language-neutral (helm/docs/portal can
read it directly, no Python import), JSON-Schema validatable (confd-schema
family gate). ``rule-packs/threshold-registry.yaml`` is that registry;
``docs/schemas/threshold-registry.schema.json`` is its shape contract.

TRANSITION STATE (WS1a step 2 — the PR-2 rewire): the registry content is
still MECHANICALLY EXTRACTED from ``scaffold_tenant.RULE_PACKS`` via
``build_registry_doc()`` (never hand-copied), but the registry is now a REAL
SoT: previously hand-copied surfaces are GENERATED from it inside delimited
blocks (everything outside the delimiters stays hand-written):

  1. rule-pack header threshold sections (the "對應的 threshold-exporter
     defaults" block of every threshold pack, defaults tier + up to three
     optional_overrides sub-sections, each under its own warning — see
     ``optional_warning_groups`` / ``render_pack_header_lines``);
  2. ``helm/threshold-exporter/values.yaml`` ``thresholdConfig.defaults``
     (the ``chart_default`` key set);
  3. ``components/threshold-exporter/config/conf.d/_defaults.yaml``
     ``defaults`` section (same ``chart_default`` set);
  4. the ``optional_overrides:`` LIST on both of those runtime surfaces
     (#1310) — the declared-without-value key names the exporter recognises
     but asserts no value for. A LIST, not a mapping: see
     ``render_optional_overrides_lines``.

``scaffold_tenant.RULE_PACKS`` remains the operative contract for config
GENERATION until PR-3 flips the direction (scaffold becomes a generated
artifact / runtime loader of the registry — D2 migration shape, 31 import
sites keep their API surface). During the transition the two copies MUST stay
semantically equal AND every generated surface must stay fresh — both
enforced by ``scripts/tools/lint/check_threshold_registry.py`` (schema +
equivalence + surface-freshness + header-prose key membership), wired as a
pre-commit gate. On drift the fix is: edit ``scaffold_tenant.RULE_PACKS``
(still operative), then run ``check_threshold_registry.py --regen`` to refresh
the registry AND all generated surfaces in one shot.

REGISTRY-SCOPE ENRICHMENT TABLES (authored HERE during the transition):
``CHART_DEFAULT_KEYS`` (which defaults-tier keys the Helm chart ships enabled)
is a registry-scope fact that scaffold's operative role never consumes, so it
is authored in this lib and merged during extraction rather than widening the
scaffold contract right before PR-3 demotes it. PR-3 folds it into the
authored registry file.

SCOPE (WS1a): threshold identity — ``defaults`` + ``optional_overrides`` tiers
({pack, tier, value, unit, desc, metric_class, chart_default, critical
variant}). ``state_filters`` / ``dimensional_example`` stay scaffold-owned
(they are state/config surface, not threshold identity; absorb later if a
consumer needs them). Extraction is FULL (all packs, all keys — mechanical and
cheap); the enforcement path this registry exists to serve first is the 18-key
repair line (#1196 / TRK-337).
"""
from __future__ import annotations

import os
import re
import sys
import textwrap
from typing import Any, Callable, Optional

try:
    import yaml
except ImportError:  # pragma: no cover - environments without pyyaml
    yaml = None  # type: ignore

try:
    import jsonschema
except ImportError:  # pragma: no cover - environments without jsonschema
    jsonschema = None  # type: ignore

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
# scripts/tools/ops/ -> repo root is three levels up.
_REPO_ROOT = os.path.abspath(os.path.join(_THIS_DIR, "..", "..", ".."))

REGISTRY_PATH = os.path.join(_REPO_ROOT, "rule-packs", "threshold-registry.yaml")
SCHEMA_PATH = os.path.join(
    _REPO_ROOT, "docs", "schemas", "threshold-registry.schema.json"
)

CRITICAL_SUFFIX = "_critical"

# The two threshold tiers carried by the registry. Order matters: it is the
# extraction order, and a same-name key in both tiers of one pack would be a
# contract bug (build_registry_doc fails loudly on it).
TIERS = ("defaults", "optional_overrides")

# ---------------------------------------------------------------------------
# chart_default — the "the Helm chart ships this key enabled" set (#1200 WS1a
# PR-2). The initial 6-key set was BEHAVIOR-PRESERVING by construction:
# exactly the key set the hand-written helm/threshold-exporter/values.yaml
# thresholdConfig.defaults carried before the rewire. Flipping more keys on
# is a deliberate owner decision, NOT a mechanical edit — see the D4
# shadow/would-fire policy in epic #1200.
# Owner-decided promotions so far:
#   - pg_connections / pg_replication_lag (postgresql pack) — D4
#     definite-imminent 折衷落地（#1200 Q3=C；Gemini #1222 review 同向建議；
#     owner 2026-07-25 核可）。the dev template historically carried these
#     but the chart never did; their _critical variants stay opt-in
#     (mysql_replication_lag precedent).
# Only defaults-tier keys are eligible (build_registry_doc fails loudly
# otherwise): optional_overrides keys are dormant by definition.
# ---------------------------------------------------------------------------
CHART_DEFAULT_KEYS: frozenset[str] = frozenset({
    # Scenario B: container resource thresholds (kubernetes pack)
    "container_cpu",
    "container_cpu_throttle",
    "container_memory",
    # Scenario A: MySQL thresholds (mariadb pack)
    "mysql_connections",
    "mysql_threads_running",
    "mysql_replication_lag",
    # PostgreSQL warning tier (postgresql pack) — owner-decided promotion,
    # #1200 Q3=C / D4 folding, 2026-07-25.
    "pg_connections",
    "pg_replication_lag",
})

# ---------------------------------------------------------------------------
# deprecated_aliases — retired threshold-key spellings still honored during
# their 2-release transition window (#1231). REGISTRY-SCOPE fact, same
# rationale as CHART_DEFAULT_KEYS: scaffold's operative config-generation role
# never consumes aliases, so the table is authored here and merged into the
# registry document rather than widening the scaffold contract before PR-3.
#
# This table is the alias SSOT. The two runtime mirrors —
#   - Go   components/threshold-exporter/app/pkg/config/aliases.go
#          (deprecatedKeyAliases)
#   - Py   scripts/tools/ops/_grar_validate.py (DEPRECATED_KEY_ALIASES)
# — are PINNED to the registry's deprecated_aliases section by tests
# (Go: pkg/config/aliases_registry_test.go; Py:
# tests/lint/test_check_threshold_registry.py), so a drifted mirror fails CI.
#
# Contract (fail-loud in build_registry_doc): the OLD spelling must NOT be an
# active registry key (it is retired), the target MUST be an active key, and
# old != new. When a window closes, delete the entry here, regen, and drop the
# mirror entries (the pin tests force the mirrors to follow).
# ---------------------------------------------------------------------------
DEPRECATED_KEY_ALIASES: dict[str, str] = {
    # #944 / #1231: the metric has always measured mysql threads_running
    # saturation, never host CPU% — the poisoned name is retired (2-release
    # alias window from v2.10.0).
    "mysql_cpu": "mysql_threads_running",
}

# ---------------------------------------------------------------------------
# metric_class backfill — semantic classification of every threshold key
# (#1200 WS1a PR-2; schema now REQUIRES metric_class on every registry key).
#
# Taxonomy (definitions live in threshold-registry.schema.json):
#   saturation  — bounded-resource usage/backlog + paired symptom metric.
#                 ⚠️ CURATED, PRODUCT-FACING set: it drives scaffold's
#                 saturation_default_keys() educational _critical annotation
#                 and the portal metricClass hints, so the 22 scaffold-authored
#                 labels are NOT re-curated here — this table deliberately
#                 assigns NO new 'saturation' (behavior-preserving; upgrade
#                 candidates flagged for owner review in the PR report).
#   capacity    — fill level of a bounded space/allocation (disk, tablespace,
#                 log, parts, memory budget) without the paired-symptom
#                 curation; capacity-planning signal.
#   throughput  — work-rate volume (qps / ops/s / req/s / msg/s).
#   latency     — time-to-complete or stall accumulation (GC pause, wait-time).
#   replication — replica sync health (lag seconds, under-replication, queue).
#   state       — discrete expected-value / topology invariant (cluster color,
#                 broker count, active controllers, consumers present).
#   efficiency  — cache/buffer effectiveness & waste (hit/miss ratio, evictions).
#   errors      — undesirable-event rate (deadlocks).
#
# Authoring rules (fail-loud in build_registry_doc):
#   - only keys NOT already classified in scaffold may appear here;
#   - ``_critical`` derivatives are never listed — they inherit the base class;
#   - after backfill + inheritance every key must be classified (strict path).
# PR-3 folds this table into the authored registry file.
# ---------------------------------------------------------------------------
METRIC_CLASS_BACKFILL: dict[str, str] = {
    # replication
    "pg_replication_lag": "replication",
    "mysql_replication_lag": "replication",
    "mongodb_replication_lag": "replication",  # renamed from mongodb_repl_lag_seconds (#1196 C)
    "redis_replication_lag": "replication",  # #1196 C repair line, new supply key
    "kafka_under_replicated_partitions": "replication",
    "clickhouse_replication_queue": "replication",
    # capacity
    "es_disk_usage_percent": "capacity",  # renamed from es_filesystem_free_percent (#1196 C, semantic flip free→used; still a disk fill level)
    "oracle_tablespace_used_percent": "capacity",
    "oracle_process_count": "capacity",
    "oracle_pga_allocated_bytes": "capacity",
    "db2_log_usage_percent": "capacity",
    "db2_tablespace_used_percent": "capacity",
    "clickhouse_max_part_count": "capacity",
    "clickhouse_memory_tracking_bytes": "capacity",
    # throughput
    "clickhouse_queries_rate": "throughput",
    "kafka_request_rate": "throughput",
    "nginx_request_rate": "throughput",
    "mongodb_opcounters_rate": "throughput",  # renamed from mongodb_opcounters_total (#1196 B)
    # latency
    "jvm_gc_pause": "latency",
    "oracle_wait_time_rate": "latency",
    "db2_lock_wait_time": "latency",  # #1196 D repair line: stall accumulation rate, same shape as oracle_wait_time_rate
    "es_search_latency_ms": "latency",  # #1196 D repair line: time-to-complete (avg per-query)
    # state
    "kafka_broker_count": "state",
    "kafka_active_controllers": "state",
    "rabbitmq_consumers": "state",
    # NOTE (#1196 D): es_pending_tasks is classed 'state' (master cluster-state
    # task queue health, the coordination-health family es_cluster_health sat
    # in) rather than 'saturation' — this table deliberately assigns no new
    # saturation (see header), and schema marks saturation membership a
    # deliberate product decision. Flagged as a saturation-upgrade candidate
    # for owner review in the #1231 PR report.
    "es_pending_tasks": "state",
    # efficiency
    "db2_bufferpool_hit_ratio": "efficiency",
    "redis_evicted_keys_rate": "efficiency",  # renamed from redis_evicted_keys_total (#1196 B)
    # errors
    "db2_deadlock_rate": "errors",
}

# Canonical per-key field order for the emitted registry document.
_FIELD_ORDER = (
    "pack", "tier", "value", "unit", "desc", "metric_class", "chart_default",
    "critical_of", "value_counterexample",
)

_REGISTRY_HEADER = (
    "# threshold-registry.yaml — threshold 契約 SoT（TRK-339 WS1a / #1200 D2）\n"
    "#\n"
    "# 地位：epic #1200 D2（2026-07-23 LOCKED）拍板 threshold 契約收斂到本檔——\n"
    "# 語言中立（helm/docs/portal 直讀）、schema 可驗證\n"
    "# （docs/schemas/threshold-registry.schema.json）。\n"
    "#\n"
    "# ✅ PR-2（rewire）後本檔已是真 SoT：原手抄面改為由本檔生成（定界符內），\n"
    "# 定界符外手寫 prose 照舊——\n"
    "#   1. rule-pack header 閾值段（defaults tier + optional_overrides 警語段，\n"
    "#      依「在出貨清單裡 / 不在但 base 已出貨 / 兩者皆無」分三小段各配一份警語）\n"
    "#   2. helm/threshold-exporter/values.yaml thresholdConfig.defaults\n"
    "#      （chart_default 集）\n"
    "#   3. components/threshold-exporter/config/conf.d/_defaults.yaml defaults 段\n"
    "#   4. 上述兩個 runtime 面的 optional_overrides: 宣告 key 清單（#1310；\n"
    "#      是 list 不是 map——平台認得該 key 但不主張其值）\n"
    "# 新鮮度由 check_threshold_registry.py --check 面強制（stale＝硬錯）。\n"
    "#\n"
    "# ⚠️ 過渡期殘留：scaffold_tenant.RULE_PACKS 仍是 config 生成路徑的運作副本，\n"
    "# 本檔由它機械萃取（scripts/tools/ops/_registry_lib.py build_registry_doc()，\n"
    "# 外加 registry-scope 附掛表 CHART_DEFAULT_KEYS）。兩者的語意等價由 pre-commit\n"
    "# gate check_threshold_registry.py 強制（防雙 SoT 漂移）。\n"
    "# 改閾值請改 scaffold_tenant.RULE_PACKS，再跑：\n"
    "#   python3 scripts/tools/lint/check_threshold_registry.py --regen\n"
    "# （會同時重產本檔＋所有生成面。）PR-3 起 scaffold RULE_PACKS 降為生成物/\n"
    "# runtime 載入，附掛表併回本檔。\n"
    "#\n"
    "# 範圍：threshold 身分（defaults / optional_overrides 兩層＋chart_default）。\n"
    "# state_filters 與 dimensional_example 仍歸 scaffold（非閾值身分）。萃取為全量；\n"
    "# 本 registry 首要服務的 enforcement 路徑是 18-key 修復線（#1196 / TRK-337）。\n"
    "#\n"
    "# deprecated_aliases（#1231）：退役舊拼法 → canonical key（2-release 過渡窗）。\n"
    "# 本區段是 alias SSOT——Go（pkg/config/aliases.go）與 Python（_grar_validate.py）\n"
    "# 兩個 runtime 鏡像由 pin 測試釘住本區段（鏡像漂移＝CI 紅）。\n"
)


def _load_scaffold():
    """Lazy import of scaffold_tenant (same directory)."""
    if _THIS_DIR not in sys.path:
        sys.path.insert(0, _THIS_DIR)
    import scaffold_tenant  # noqa: E402
    return scaffold_tenant


# ---------------------------------------------------------------------------
# Extraction (scaffold RULE_PACKS -> registry doc) — mechanical, never hand-copy
# ---------------------------------------------------------------------------

def build_registry_doc(
    rule_packs: Optional[dict] = None,
    *,
    chart_default_keys: Optional[frozenset[str]] = None,
    metric_class_backfill: Optional[dict[str, str]] = None,
    strict_metric_class: Optional[bool] = None,
    deprecated_aliases: Optional[dict[str, str]] = None,
) -> dict:
    """Mechanically extract the registry document from RULE_PACKS.

    Per pack: {display, exporter, default_on, rule_pack_file}.
    Per key : {pack, tier, value, unit, desc[, metric_class][, chart_default]
    [, critical_of]}. ``critical_of`` is derived (never authored): key ends
    with ``_critical`` AND its base key exists in the same pack -> the base
    key name. ``chart_default: true`` is merged from the registry-scope
    enrichment table (CHART_DEFAULT_KEYS — see module docstring); emitted
    sparsely (present only when true, like metric_class).

    ``metric_class`` is REQUIRED on every registry key (schema-enforced):
    scaffold-authored classes pass through; unclassified keys are backfilled
    from ``METRIC_CLASS_BACKFILL``; ``_critical`` derivatives inherit the base
    key's class. On the real extraction path any key left unclassified after
    that is a hard error (``strict_metric_class``) — a new threshold key
    cannot land without a deliberate classification.

    Enrichment defaults are applied only on the REAL extraction path
    (``rule_packs is None``); synthetic packs stay pure unless the caller
    injects a table — hermetic tests depend on that.

    Fails loudly on identity collisions (same key in two tiers of one pack, or
    in two packs) — a collision would silently shadow a contract entry — on a
    chart_default table entry that is unknown or not defaults-tier (a typo'd
    table would silently un-ship a chart key), and on a backfill entry that is
    unknown or shadows a scaffold-authored class.

    ``deprecated_aliases`` (#1231) rides along as a top-level registry section
    (old spelling -> canonical key). Fail-loud contract: the old spelling must
    NOT be an active key (it is retired), the target MUST be an active key
    (an alias onto nothing would silently accept configs that resolve to no
    threshold), and old != new.
    """
    if chart_default_keys is None:
        chart_default_keys = (
            CHART_DEFAULT_KEYS if rule_packs is None else frozenset()
        )
    if metric_class_backfill is None:
        metric_class_backfill = (
            METRIC_CLASS_BACKFILL if rule_packs is None else {}
        )
    if strict_metric_class is None:
        strict_metric_class = rule_packs is None
    if deprecated_aliases is None:
        deprecated_aliases = (
            DEPRECATED_KEY_ALIASES if rule_packs is None else {}
        )
    if rule_packs is None:
        rule_packs = _load_scaffold().RULE_PACKS

    packs: dict[str, Any] = {}
    keys: dict[str, Any] = {}
    for pack_name, pack in rule_packs.items():
        packs[pack_name] = {
            "display": pack["display"],
            "exporter": pack["exporter"],
            "default_on": bool(pack.get("default_on", False)),
            "rule_pack_file": pack["rule_pack_file"],
        }
        combined: dict[str, tuple[str, dict]] = {}
        for tier in TIERS:
            for key, info in (pack.get(tier) or {}).items():
                if key in combined:
                    raise ValueError(
                        f"threshold key {key!r} appears in BOTH tiers of pack "
                        f"{pack_name!r} — ambiguous contract identity"
                    )
                combined[key] = (tier, info)
        for key, (tier, info) in combined.items():
            if key in keys:
                raise ValueError(
                    f"threshold key {key!r} appears in packs "
                    f"{keys[key]['pack']!r} AND {pack_name!r} — ambiguous "
                    "contract identity"
                )
            entry: dict[str, Any] = {
                "pack": pack_name,
                "tier": tier,
                "value": info["value"],
                "unit": info["unit"],
                "desc": info["desc"],
            }
            if "metric_class" in info:
                entry["metric_class"] = info["metric_class"]
            if "value_counterexample" in info:
                # Passed through verbatim, never derived: it records that the
                # ADR-030 reference library produced a case THIS value gets
                # wrong. Deliberately not a verdict — that library is a
                # sampling probe, not a spec (its README's anti-Goodhart
                # clause), so the registry states the measurement and lets the
                # reader judge. #1176.
                entry["value_counterexample"] = dict(info["value_counterexample"])
            if key in chart_default_keys:
                if tier != "defaults":
                    raise ValueError(
                        f"chart_default key {key!r} sits in tier {tier!r} — "
                        "only defaults-tier keys are chart-shippable"
                    )
                entry["chart_default"] = True
            if key.endswith(CRITICAL_SUFFIX):
                base = key[: -len(CRITICAL_SUFFIX)]
                if base in combined:
                    entry["critical_of"] = base
            keys[key] = entry

    missing_chart = set(chart_default_keys) - set(keys)
    if missing_chart:
        raise ValueError(
            "CHART_DEFAULT_KEYS entries not found in RULE_PACKS: "
            f"{sorted(missing_chart)} — a typo here would silently un-ship a "
            "chart key"
        )

    # metric_class: backfill (never shadow scaffold) -> _critical inheritance
    # -> strict completeness.
    unknown_backfill = set(metric_class_backfill) - set(keys)
    if unknown_backfill:
        raise ValueError(
            "METRIC_CLASS_BACKFILL entries not found in RULE_PACKS: "
            f"{sorted(unknown_backfill)} — a typo here would leave the real "
            "key unclassified"
        )
    for key, cls in metric_class_backfill.items():
        if "metric_class" in keys[key]:
            raise ValueError(
                f"METRIC_CLASS_BACKFILL shadows scaffold-authored class for "
                f"{key!r} — remove it from the table (scaffold wins)"
            )
        keys[key]["metric_class"] = cls
    for key, entry in keys.items():
        if "metric_class" not in entry and "critical_of" in entry:
            base = keys.get(entry["critical_of"], {})
            if "metric_class" in base:
                entry["metric_class"] = base["metric_class"]
    if strict_metric_class:
        unclassified = sorted(
            k for k, e in keys.items() if "metric_class" not in e
        )
        if unclassified:
            raise ValueError(
                f"unclassified threshold keys {unclassified} — author "
                "metric_class in scaffold RULE_PACKS or METRIC_CLASS_BACKFILL "
                "(schema requires it on every key)"
            )

    # deprecated_aliases (#1231): retired spelling -> canonical key. Fail-loud
    # so a stale table entry cannot ship a half-renamed contract.
    for old, new in deprecated_aliases.items():
        if old == new:
            raise ValueError(
                f"deprecated alias {old!r} maps to itself — remove the entry"
            )
        if old in keys:
            raise ValueError(
                f"deprecated alias {old!r} is still an ACTIVE registry key — "
                "an alias entry means the old spelling is retired; finish the "
                "rename (or drop the alias)"
            )
        if new not in keys:
            raise ValueError(
                f"deprecated alias {old!r} targets {new!r} which is not a "
                "registry key — the alias would resolve to no threshold"
            )

    # canonical field order (stable YAML emission).
    keys = {
        k: {f: e[f] for f in _FIELD_ORDER if f in e} for k, e in keys.items()
    }
    doc: dict[str, Any] = {"version": 1, "packs": packs, "keys": keys}
    if deprecated_aliases:
        doc["deprecated_aliases"] = dict(sorted(deprecated_aliases.items()))
    return doc


def write_registry(
    path: Optional[str] = None, rule_packs: Optional[dict] = None
) -> dict:
    """Regenerate the registry from RULE_PACKS and write it to ``path``.

    Returns a summary dict {path, packs, keys}. Uses write_text_secure (the
    repo SAST convention chmod for write-mode files, tests/shared/test_sast.py).
    """
    if yaml is None:
        raise RuntimeError("pyyaml required")
    sys.path.insert(0, _THIS_DIR)
    sys.path.insert(0, os.path.join(_THIS_DIR, ".."))
    from _lib_python import write_text_secure  # noqa: E402

    out = path or REGISTRY_PATH
    doc = build_registry_doc(rule_packs)
    body = yaml.safe_dump(
        doc, sort_keys=False, allow_unicode=True, default_flow_style=False
    )
    write_text_secure(out, _REGISTRY_HEADER + body)
    return {"path": out, "packs": len(doc["packs"]), "keys": len(doc["keys"])}


# ---------------------------------------------------------------------------
# Loading + schema validation
# ---------------------------------------------------------------------------

def load_registry(path: Optional[str] = None) -> dict:
    """Load the committed registry document (the whole doc, not just keys)."""
    if yaml is None:
        raise RuntimeError("pyyaml required")
    p = path or REGISTRY_PATH
    with open(p, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def validate_registry(
    doc: dict, schema_path: Optional[str] = None
) -> list[str]:
    """Validate a registry doc against the JSON Schema. Returns error strings.

    Empty list == valid. Uses Draft-07 (the docs/schemas/ family convention).
    """
    if jsonschema is None:
        raise RuntimeError("jsonschema required")
    import json

    sp = schema_path or SCHEMA_PATH
    with open(sp, encoding="utf-8") as fh:
        schema = json.load(fh)
    validator = jsonschema.Draft7Validator(schema)
    errors = []
    for err in sorted(validator.iter_errors(doc), key=lambda e: list(e.absolute_path)):
        where = "/".join(str(p) for p in err.absolute_path) or "<root>"
        errors.append(f"schema: {where}: {err.message}")
    return errors


# ---------------------------------------------------------------------------
# Convenience queries
# ---------------------------------------------------------------------------

def registry_keys(doc: dict) -> dict[str, dict]:
    """The flat ``keys`` mapping of a registry doc (or {})."""
    return doc.get("keys", {}) or {}


def keys_by_pack(doc: dict) -> dict[str, dict[str, dict]]:
    """Group the flat keys map by pack: {pack: {key: entry}}."""
    out: dict[str, dict[str, dict]] = {}
    for key, entry in registry_keys(doc).items():
        out.setdefault(entry.get("pack", "?"), {})[key] = entry
    return out


def keys_in_tier(doc: dict, tier: str) -> dict[str, dict]:
    """All keys of one tier ('defaults' / 'optional_overrides')."""
    return {
        k: e for k, e in registry_keys(doc).items() if e.get("tier") == tier
    }


def default_value(doc: dict, key: str):
    """The registry default value for ``key`` (None if absent)."""
    entry = registry_keys(doc).get(key)
    return None if entry is None else entry.get("value")


# ---------------------------------------------------------------------------
# Transition-period equivalence (anti dual-SoT drift)
# ---------------------------------------------------------------------------

def diff_docs(committed: dict, fresh: dict) -> list[str]:
    """Semantic diff of a committed registry vs a fresh scaffold extraction.

    Returns one message per divergence (empty == semantically equal). Messages
    are per-field so the gate output is directly actionable.
    """
    diffs: list[str] = []

    c_packs = committed.get("packs", {}) or {}
    f_packs = fresh.get("packs", {}) or {}
    for name in sorted(set(f_packs) - set(c_packs)):
        diffs.append(f"pack {name!r}: in scaffold RULE_PACKS but missing from registry")
    for name in sorted(set(c_packs) - set(f_packs)):
        diffs.append(f"pack {name!r}: in registry but missing from scaffold RULE_PACKS")
    for name in sorted(set(c_packs) & set(f_packs)):
        for field in sorted(set(c_packs[name]) | set(f_packs[name])):
            cv, fv = c_packs[name].get(field), f_packs[name].get(field)
            if cv != fv:
                diffs.append(
                    f"pack {name!r}.{field}: registry={cv!r} vs scaffold={fv!r}"
                )

    c_keys = committed.get("keys", {}) or {}
    f_keys = fresh.get("keys", {}) or {}
    for key in sorted(set(f_keys) - set(c_keys)):
        diffs.append(f"key {key!r}: in scaffold RULE_PACKS but missing from registry")
    for key in sorted(set(c_keys) - set(f_keys)):
        diffs.append(f"key {key!r}: in registry but missing from scaffold RULE_PACKS")
    for key in sorted(set(c_keys) & set(f_keys)):
        for field in sorted(set(c_keys[key]) | set(f_keys[key])):
            cv, fv = c_keys[key].get(field), f_keys[key].get(field)
            if cv != fv:
                diffs.append(
                    f"key {key!r}.{field}: registry={cv!r} vs scaffold={fv!r}"
                )

    # deprecated_aliases (#1231) — same anti-dual-SoT treatment as keys: any
    # divergence in either direction is a drift (a missing section counts as
    # an empty table).
    c_alias = committed.get("deprecated_aliases", {}) or {}
    f_alias = fresh.get("deprecated_aliases", {}) or {}
    for old in sorted(set(f_alias) - set(c_alias)):
        diffs.append(
            f"deprecated_alias {old!r}: in authored table but missing from registry"
        )
    for old in sorted(set(c_alias) - set(f_alias)):
        diffs.append(
            f"deprecated_alias {old!r}: in registry but missing from authored table"
        )
    for old in sorted(set(c_alias) & set(f_alias)):
        if c_alias[old] != f_alias[old]:
            diffs.append(
                f"deprecated_alias {old!r}: registry={c_alias[old]!r} vs "
                f"authored={f_alias[old]!r}"
            )

    if committed.get("version") != fresh.get("version"):
        diffs.append(
            f"version: registry={committed.get('version')!r} vs "
            f"expected={fresh.get('version')!r}"
        )
    return diffs


def diff_vs_scaffold(doc: dict, rule_packs: Optional[dict] = None) -> list[str]:
    """Diff a loaded registry doc against a fresh RULE_PACKS extraction."""
    return diff_docs(doc, build_registry_doc(rule_packs))


# ---------------------------------------------------------------------------
# Generated surfaces (PR-2 rewire) — renderers, delimiter splice, freshness
# ---------------------------------------------------------------------------
# Previously hand-copied surfaces are generated from the registry inside
# delimited blocks (a file may host more than one — helm values and the dev
# template each carry both a `defaults` mapping and an `optional_overrides`
# list). The delimiters are load-bearing: --check compares the
# whole block (markers included) against a fresh render, --regen splices a
# fresh render between them. Hand-written prose OUTSIDE the block is never
# touched (and is separately covered by the header-prose membership lint).

HELM_VALUES_PATH = os.path.join(
    _REPO_ROOT, "helm", "threshold-exporter", "values.yaml"
)
DEV_DEFAULTS_PATH = os.path.join(
    _REPO_ROOT, "components", "threshold-exporter", "config", "conf.d",
    "_defaults.yaml",
)
# ⛔ The try-local seed used to be the THIRD copy of the declared list,
# hand-maintained by a comment that told the reader "if the generated copies
# diverge, THEY are right and this copy needs a hand edit" — i.e. a documented
# manual sync step. Its only gate compared the PARSED list, so anything
# comment-shaped (which is where the counter-example caveat lives) was
# invisible to it and the seed silently became the one shipped conf.d without
# the caveat (blind review, #1344). Generating it deletes the manual step.
TRY_LOCAL_DEFAULTS_PATH = os.path.join(
    _REPO_ROOT, "try-local", "seed", "conf.d", "_defaults.yaml",
)

_MARKER_STEM = "GENERATED:threshold-registry:"


def begin_marker(surface_id: str, indent: str = "") -> str:
    return (
        f"{indent}# >>> {_MARKER_STEM}{surface_id} — generated block, DO NOT "
        "EDIT（改 scaffold_tenant.RULE_PACKS 後跑 check_threshold_registry.py "
        "--regen）"
    )


def end_marker(surface_id: str, indent: str = "") -> str:
    return f"{indent}# <<< {_MARKER_STEM}{surface_id}"


def _fmt_value(v: Any) -> str:
    """Deterministic YAML-comment-safe scalar rendering (int stays int)."""
    if isinstance(v, bool):  # bool is an int subclass — guard first
        return "true" if v else "false"
    return repr(v) if isinstance(v, float) else str(v)


def _entry_lines(
    key: str, entry: dict, prefix: str = "#   ", cont: str = "#       "
) -> list[str]:
    """One registry key as pack-header comment lines (inline when short)."""
    tag = "   [chart-default]" if entry.get("chart_default") else ""
    keyline = f"{prefix}{key}: {_fmt_value(entry['value'])}{tag}"
    meta = f"{entry['unit']} — {entry['desc']}"
    inline = f"{keyline}   # {meta}"
    lines = [inline] if len(inline) <= _COMMENT_WIDTH else [keyline] + [
        f"{cont}{seg}" for seg in _wrap_comment(meta, _COMMENT_WIDTH - len(cont))
    ]
    return lines + _counterexample_lines(entry, cont)


# The two directions are NOT interchangeable, and a single blanket caveat would
# be false for one of them: over_fire means a benign scenario crossed the
# threshold, under_fire means a real fault stayed under it. #1176 has both.
#
# ⛔ ONE source for the verdict wording, in both languages, for every surface —
# Python and JS alike (the JS copy lives in `_common/data/rule-packs.js` and is
# imported by both portal callers). What is deliberately NOT unified is the
# SENTENCE each medium composes around it: a YAML comment, a CLI prompt line
# and a React element cannot share a string. The facts (issue / observed /
# direction verdict / the mark) have one source; the framing is per-medium.
# Saying more than that is what the previous docstring here did, and it was
# false the moment the second composer appeared (blind review, #1344).
COUNTEREXAMPLE_DIRECTION = {
    "zh": {
        "over_fire": "此值會對正常運作誤警",
        "under_fire": "此值接不到該抓的故障",
    },
    "en": {
        "over_fire": "fires on benign load",
        "under_fire": "misses the fault it should catch",
    },
}

# The phrase every rendered counter-example carries, exported so the gates can
# key off ONE string. Three test files had each hand-copied a fragment of it —
# the same duplication this whole line of work exists to delete, reproduced
# inside the tests that guard against it (CodeRabbit, #1344).
COUNTEREXAMPLE_MARK = "實測反例"
COUNTEREXAMPLE_MARKS = {"zh": COUNTEREXAMPLE_MARK, "en": "measured counter-example"}


def counterexample_verdict(ce: dict, lang: str = "zh") -> str:
    """What the measured counter-example says about the value, in `lang`.

    Lookup, never a ternary: an unknown ``direction`` must be a hard error and
    not silently render as the OTHER direction's wording. The portal copy of
    this table fails the same way by design (`rule-packs.js`) — telling an
    operator "this value fires on benign load" when the measurement said the
    opposite points them at the wrong knob.

    ⛔ Fail loud, but say WHAT is wrong. A bare ``KeyError: 'over-fire'`` out of
    a dict lookup surfaces to an operator running `scaffold_tenant` as a
    traceback with no hint that the typo is in a registry field (blind review,
    #1344) — and `scaffold_tenant` has no except around the write path.
    """
    require_counterexample(ce)
    return COUNTEREXAMPLE_DIRECTION[lang][ce["direction"]]


def require_counterexample(ce: dict) -> dict:
    """Validate a `value_counterexample` and say what is wrong if it is not.

    ⛔ ALL THREE fields, in one place. An earlier version guarded only
    ``direction``, so a missing ``issue`` or ``observed`` still reached an
    operator as a bare ``KeyError: 'observed'`` from whichever composer got
    there first — the cited instance fixed, the class left open (blind review,
    #1344).

    ⛔ ``observed`` must be one non-blank line. The schema's ``pattern`` catches
    an EMBEDDED newline but not a trailing one: jsonschema matches with
    ``re.search`` and Python's ``$`` also matches just before a final newline,
    so ``observed: |`` (the natural YAML way to write a long clause) produces a
    string the schema accepts. Every composer collapses whitespace defensively,
    but the useful place to refuse it is here, where the message can name the
    field — not silently, five surfaces downstream.
    """
    missing = [f for f in ("issue", "direction", "observed") if f not in ce]
    if missing:
        raise ValueError(
            f"value_counterexample is missing {missing} (needs issue / "
            f"direction / observed; see docs/schemas/threshold-registry.schema.json). "
            f"Got keys {sorted(ce)}.")
    if ce["direction"] not in COUNTEREXAMPLE_DIRECTION["zh"]:
        raise ValueError(
            f"value_counterexample.direction={ce['direction']!r} is not one of "
            f"{sorted(COUNTEREXAMPLE_DIRECTION['zh'])} (key metadata in "
            f"scaffold_tenant.RULE_PACKS / threshold-registry.yaml). The two "
            f"directions mean opposite things, so there is no safe default.")
    for field in ("observed", "observed_zh"):
        if field not in ce:
            continue  # `observed_zh` is optional in the schema; see below
        observed = ce[field]
        if not isinstance(observed, str) or not observed.strip():
            raise ValueError(
                f"value_counterexample.{field}={observed!r} is blank — an empty "
                "clause renders as a warning that says nothing, which reads as "
                "'measured and fine'.")
        if observed != " ".join(observed.split()):
            raise ValueError(
                f"value_counterexample.{field}={observed!r} contains a newline "
                "or padding. It is spliced into single-line `#` comments on "
                "several surfaces; write it as one clause.")
    return ce


def counterexample_observed(ce: dict, lang: str = "zh") -> str:
    """The measured clause in `lang`.

    ⛔ Falls back to `observed` (English) rather than rendering nothing. A
    missing translation must not make the warning disappear: absence of the
    caveat is the repo's signal for "nothing was measured", and borrowing it
    for "nothing was translated" would turn a data-entry gap into a false
    all-clear. `tests/lint/test_counterexample_coverage.py` requires every
    shipped counter-example to carry `observed_zh`, so the fallback is a
    safety net rather than a path anything actually takes (owner decision on
    the mixed-language ZH surfaces, #1344).
    """
    if lang == "zh":
        return ce.get("observed_zh") or ce["observed"]
    return ce["observed"]


def counterexample_for_key(key: str, rule_packs: Optional[dict] = None) -> Optional[dict]:
    """The measured counter-example for `key`, or None. Looks across both tiers."""
    if rule_packs is None:
        rule_packs = _load_scaffold().RULE_PACKS
    for pack in (rule_packs or {}).values():
        for tier in ("defaults", "optional_overrides"):
            info = (pack.get(tier) or {}).get(key)
            if isinstance(info, dict) and "value_counterexample" in info:
                return info["value_counterexample"]
    return None


def counterexample_sentence(ce: dict, lang: str = "zh") -> str:
    """The one-line comment framing used by the two `_defaults.yaml` writers."""
    mark = COUNTEREXAMPLE_MARKS[lang]
    verdict = counterexample_verdict(ce, lang)
    if lang == "zh":
        return f"#{ce['issue']} {mark}：{counterexample_observed(ce, 'zh')}（{verdict}）"
    return f"#{ce['issue']} {mark}: {counterexample_observed(ce, 'en')} ({verdict})"


def annotate_defaults_counterexamples(dumped: str, rule_packs: Optional[dict] = None,
                                      lang: str = "zh") -> str:
    """Splice the ⚠ comment above each emitted key that has one.

    ⛔ Why this exists at all: the interactive prompt only reaches a human who
    is ANSWERING PROMPTS. `generate_defaults` → `write_outputs` is the path
    taken by `--non-interactive`, by `--from-onboard` batch onboarding, by the
    interactive run whose operator declines to customise, and by
    `da-tools init` — and it writes the platform-asserted numbers straight into
    `_defaults.yaml` with nobody prompted.

    ⛔ It lives HERE, not in `scaffold_tenant`, because there are TWO producers
    of that file: `scaffold_tenant.write_outputs` and
    `init_project._gen_defaults_yaml`. It was in `scaffold_tenant`, so the
    second producer wrote the same declared keys bare — same directory, same
    filename, one annotated and one not (blind review, #1344).

    A text splice rather than a comment-preserving dumper: the file is
    `yaml.safe_dump`ed, which drops comments by construction, and swapping the
    dumper for one annotation would rewrite every line of a file the Go loader
    reads. The splice keys off the emitted line, so it can only annotate a key
    this run actually wrote.

    ⛔ BOTH shapes, not just `key: value`. `optional_overrides:` is a plain
    string LIST (`- oracle_process_count`), so a colon-only splice skipped the
    entire declared tier — the tier #1320 D1 says a tenant can fill in today.

    ⛔ WRAPPED, like every other composer. This one used to interpolate the
    sentence raw, so an `observed` containing a newline emitted its tail at
    column 0 and the written `_defaults.yaml` stopped being valid YAML. The
    schema now forbids the newline at the source; wrapping here means a single
    bad field cannot corrupt a customer file even if it slips in some other way.
    """
    out: list[str] = []
    for line in dumped.split("\n"):
        stripped = line.strip()
        if stripped.startswith("- "):
            key = stripped[2:].strip().strip("'\"")
        elif stripped.endswith(":") or ":" not in stripped:
            out.append(line)
            continue
        else:
            key = stripped.split(":", 1)[0].strip()
        ce = counterexample_for_key(key, rule_packs)
        if ce:
            indent = line[: len(line) - len(line.lstrip())]
            cont = f"{indent}# "
            body = "⚠ " + " ".join(counterexample_sentence(ce, lang).split())
            out.extend(f"{cont}{seg}" for seg in _wrap_comment(
                body, _COMMENT_WIDTH - len(cont)))
        out.append(line)
    return "\n".join(out)


def _counterexample_lines(entry: dict, cont: str = "#       ") -> list[str]:
    """The ⚠ line under a key whose value has a measured counter-example.

    Rendered FROM the registry field, never authored per pack — the same
    discipline as the tier warnings next door: a header must not be able to
    claim something the registry contradicts. Absence of the field renders
    nothing at all, so "no line" means "no counter-example measured" and never
    "validated".
    """
    ce = entry.get("value_counterexample")
    if not ce:
        return []
    verdict = counterexample_verdict(ce)
    body = (f"⚠ 參考庫實測反例（#{ce['issue']}）："
            f"{counterexample_observed(ce, 'zh')} —— {verdict}；"
            f"這個數字是起點，不是平台背書的值")
    # `_wrap_comment` owns the no-split rule (both flags). The note that used to
    # sit here named only the hyphen half.
    return [f"{cont}{seg}"
            for seg in _wrap_comment(body, _COMMENT_WIDTH - len(cont))]


# ---------------------------------------------------------------------------
# optional_overrides — ONE tier, TWO populations, two different truths (#1310)
# ---------------------------------------------------------------------------
# Until PR-C (#1314) the whole tier got a SINGLE warning stating that a
# tenant-set key "永遠不會生效 ... 永久無法啟用". That sentence is now FALSE for
# half the tier and still TRUE for the other half:
#
#   * FLAT keys (today oracle_* / db2_* / clickhouse_*) — ``resolveDeclaredRows``
#     walks the DECLARED set and emits a row as soon as the TENANT supplies a
#     value, and #1310 ships that declared list itself in ``_defaults.yaml`` /
#     Helm values. Tenant fills one in → it fires. No platform fallback, by
#     design: the platform asserts no value.
#   * ``<base>_critical`` keys — never on the list. ``resolveDeclaredRows``
#     skips the critical suffix outright and ``resolveCriticalRows`` keys off
#     ``defaults[base]``, so they are deliberately EXCLUDED (#1311): listing all
#     25 tier keys would make 16 of them decoration.
#
# ⛔ But "off the list" is NOT "dormant", and conflating the two is the second
# mistake this block has now made in both directions. ``resolveCriticalRows``
# needs ``defaults[base]``, and for 5 of the 16 (the postgresql / mariadb ones)
# that base IS a shipped chart default — so a tenant filling one in today gets a
# real critical row. Only the other 11, whose base is not shipped either, are
# actually dormant.
#
# So the warning is chosen PER KEY by TWO derived predicates —
# ``is_shipped_optional_key`` (the SAME one that decides list membership, so
# header claim and shipped list cannot drift apart) and ``has_shipped_chart_base``
# (which asks the resolver's real entry condition). See
# ``optional_warning_groups``.


def is_shipped_optional_key(key: str) -> bool:
    """Whether an ``optional_overrides``-tier key belongs in the SHIPPED list.

    Flat keys only. Identical predicate to the one ``scaffold_tenant`` uses for
    the same split (``not key.endswith("_critical") and "{" not in key``), which
    in turn mirrors the exporter's own refusals in
    ``components/threshold-exporter/app/pkg/config/resolve.go``:
    ``resolveDeclaredRows`` skips both the ``_critical`` suffix (that tier is
    ``resolveCriticalRows``' business, and it needs ``defaults[base]``) and
    dimensional ``key{...}`` tokens (already emitted by
    ``resolveDimensionalRows``).

    NOTE the registry models no dimensional keys today (they are scaffold's
    ``dimensional_example`` domain, see this module's SCOPE note), so the
    ``{`` arm is defence, not a live branch.
    """
    return not key.endswith(CRITICAL_SUFFIX) and "{" not in key


def shipped_optional_keys_for_packs(
    pack_names, rule_packs: Optional[dict] = None
) -> list[str]:
    """The shipped ``optional_overrides:`` key names for a SELECTION of packs.

    The onboarding generators (``scaffold_tenant.generate_defaults`` and
    ``init_project._gen_defaults_yaml``) write a customer-side ``_defaults.yaml``
    for the packs that customer selected, and that file is the ONLY declared-key
    source the tenant-api write path ever reads
    (``config.mergeTenantConfig`` joins ``configDir/_defaults.yaml``). They must
    ship the same names the chart does, or a tenant on the GitOps topology gets
    a 400 for a key the platform believes it declared.

    ⛔ Both call this instead of re-spelling ``not endswith("_critical")``.
    ``init_project`` keeps its OWN ``RULE_PACK_CATALOG`` for the ``defaults:``
    values — that copy is a KNOWN divergence from ``scaffold_tenant.RULE_PACKS``
    with its own consolidation tracked separately, and this function is
    deliberately NOT that consolidation: it unifies only the declared-key
    derivation, so the two generators cannot disagree about which keys a tenant
    may set. Splitting that derivation in two would just have created a fourth
    hand-copy of the predicate (#1189).

    Order is RULE_PACKS order (== registry pack order), not the caller's
    selection order, so a full selection renders byte-identical to
    ``optional_override_list_keys``.
    """
    if rule_packs is None:
        rule_packs = _load_scaffold().RULE_PACKS
    selected = set(pack_names)
    return [
        key
        for pack, meta in rule_packs.items()
        if pack in selected
        for key in (meta or {}).get("optional_overrides", {})
        if is_shipped_optional_key(key)
    ]


def has_shipped_chart_base(doc: dict, key: str) -> bool:
    """Whether an UNSHIPPED optional key's base is itself a shipped chart default.

    Only ``<base>_critical`` keys have a base at all (``critical_of`` is derived,
    never authored — see ``build_registry_doc``). ``resolveCriticalRows`` emits a
    row the moment ``defaults[base]`` carries a value, so a ``_critical`` whose
    base is ``chart_default: true`` is settable by a TENANT TODAY even though the
    key itself is (correctly) off the declared list — it never needed to be on
    it. A ``_critical`` whose base is NOT shipped is the genuinely dormant case:
    the deployment must supply the base first.

    ⛔ This distinction is why the pack-header warning is three-way and not two.
    The pre-#1310 single warning, and the two-way split that first replaced it,
    both told postgresql/mariadb readers their ``_critical`` keys "並不會發射任何
    user_threshold" — false for all five of them, and false in the direction that
    talks a tenant out of protection they already have.
    """
    base = (doc.get("keys", {}).get(key) or {}).get("critical_of")
    if not base:
        return False
    return bool((doc.get("keys", {}).get(base) or {}).get("chart_default"))


# Warning A — the keys the platform SHIPS in the optional_overrides: list.
_OPTIONAL_SHIPPED_WARNING_LINES = (
    "#",
    "# optional_overrides（已出貨在清單裡——平台認得這個 key，但不主張它的值）：",
    "# 下列 key 由平台出貨在 _defaults.yaml / Helm values 的 `optional_overrides:`",
    "# 清單中，租戶可直接在自己的 conf.d 填值並生效（resolveDeclaredRows 走宣告集，",
    "# 只在租戶給了值時發射；components/threshold-exporter/app/pkg/config/resolve.go）。",
    "# 平台不主張值 ⇒ 沒有平台回退：租戶不填就是靜默，那正是這一格的用意。",
    "# ⚠️ 下列數字是「參考起點」不是背書——正式採用前必須以該租戶實際 baseline 校準。",
    "# 已測到反例的那幾個，反例逐條標在該 key 下（registry `value_counterexample`）。",
)
# ⛔ The three counter-examples used to be spelled out HERE, as prose, inside a
# GENERATED block — and the Oracle header enumerated DB2's case, so a pack
# header asserted a fact about a pack it does not own. That is the hand-copied
# contract this whole registry exists to delete: re-tuning db2_deadlock_rate
# would have left two other packs quietly claiming the old number. They are now
# rendered per key from `value_counterexample`, which also means a pack with no
# measured counter-example silently gets no such claim (#1176).

# Warning B — off the shipped list, but the BASE is a shipped chart default, so
# `resolveCriticalRows` already has everything it needs. These keys are settable
# by a tenant TODAY. Saying otherwise (as the single pre-#1310 warning and its
# first two-way replacement both did) is the expensive direction of wrong: it
# talks a reader out of protection that is already available.
_OPTIONAL_CRITICAL_BASE_SHIPPED_WARNING_LINES = (
    "#",
    "# optional_overrides（不在出貨清單裡，但 base 已出貨——租戶今天就設得動）：",
    "# 下列 `<base>_critical` 不在平台出貨的 `optional_overrides:` 清單裡，也不需要列進去。",
    "# 它們的解析走 resolveCriticalRows，入場條件是 `defaults[base]` 有值",
    "# （components/threshold-exporter/app/pkg/config/resolve.go），",
    "# 而下列每個 key 的 base 都標了",
    "# [chart-default]＝出貨的 Helm values / dev conf.d 範本就供給它。⇒ 租戶在自己的",
    "# conf.d 填一個，ValidateTenantKeys 放行、resolveCriticalRows 產出一條真的",
    "# critical row。清單刻意不收 _critical 也是同一個理由：它們用不到宣告集（#1311）。",
    "# ⚠️ 下列數字是「參考起點」不是背書——正式採用前必須以該租戶實際 baseline 校準。",
)

# Warning C — off the shipped list AND the base is not shipped either. This is
# the only population the original "documented-but-dormant" wording is still
# true for. Worded by the property that actually gates them (`defaults[base]`),
# not by the `_critical` spelling, so it stays true if the populations shift.
_OPTIONAL_UNSHIPPED_WARNING_LINES = (
    "#",
    "# optional_overrides（不在出貨清單裡，base 也未出貨——documented-but-dormant）：",
    "# ⛔ 啟用前提（別跳過）：下列 key **不在**平台出貨的 `optional_overrides:` 清單，",
    "# 所以走不到 resolveDeclaredRows；`<base>_critical` 的解析走 resolveCriticalRows，",
    "# 它讀的是 defaults[base]，base 沒有值就整列丟棄",
    "# （components/threshold-exporter/app/pkg/config/resolve.go）。",
    "# 而下列每個 key 的 base 都**未**標 [chart-default]",
    "# ⇒ 出貨的 Helm values / dev conf.d 範本沒有供給它：",
    "# 租戶在這個拓撲下填一個並不會發射任何 user_threshold。",
    "# 要能用，部署面必須先供給 base（scaffold_tenant 針對選到",
    "# 的 pack 產生的 _defaults.yaml 就含 base，operator 也可自行填 Helm values），",
    "# critical 覆寫才有東西可加嚴。清單刻意排除 _critical 正是因為如此：放進去只會是",
    "# 一格裝飾（#1311）。",
)


def optional_warning_groups(doc: dict, optional: dict) -> list[tuple[tuple, dict]]:
    """[(warning_lines, {key: entry}), …] — the optional tier split by TRUTH.

    Three populations, three different truths, and the split is DERIVED
    (``is_shipped_optional_key`` for membership, ``has_shipped_chart_base`` for
    the rest) rather than spelled out per pack — the whole point is that a
    header can never claim something the resolver contradicts.

    Empty groups are dropped, so a pack with no optional tier gets no warning
    line at all and a single-population pack gets exactly one.
    """
    shipped, base_shipped, dormant = {}, {}, {}
    for key, entry in optional.items():
        if is_shipped_optional_key(key):
            shipped[key] = entry
        elif has_shipped_chart_base(doc, key):
            base_shipped[key] = entry
        else:
            dormant[key] = entry
    groups = [
        (_OPTIONAL_SHIPPED_WARNING_LINES, shipped),
        (_OPTIONAL_CRITICAL_BASE_SHIPPED_WARNING_LINES, base_shipped),
        (_OPTIONAL_UNSHIPPED_WARNING_LINES, dormant),
    ]
    return [(w, keys) for w, keys in groups if keys]


def render_pack_header_lines(doc: dict, pack_name: str) -> list[str]:
    """The generated threshold section of one rule-pack header (body only).

    The optional tier renders as up to THREE sub-sections, each under its own
    warning (see ``optional_warning_groups``): the keys the platform ships on
    the ``optional_overrides:`` list, the ``_critical`` keys whose base is a
    shipped chart default (settable today), and the ones whose base is not
    (genuinely dormant). A pack gets exactly the sub-sections its keys populate,
    in that order; a pack with no optional-tier key gets no warning whatsoever.
    """
    by_pack = keys_by_pack(doc).get(pack_name, {})
    defaults = {k: e for k, e in by_pack.items() if e.get("tier") == "defaults"}
    optional = {
        k: e for k, e in by_pack.items() if e.get("tier") == "optional_overrides"
    }
    lines = [
        "# 對應的 threshold-exporter defaults"
        "（defaults tier——scaffold onboarding 供給；[chart-default]=chart 出貨即啟用；未標者需部署面顯式供給後才發射可達）:",
    ]
    for key, entry in defaults.items():
        lines += _entry_lines(key, entry)
    for warning, keys in optional_warning_groups(doc, optional):
        lines += list(warning)
        for key, entry in keys.items():
            lines += _entry_lines(key, entry)
    return lines


def _critical_sibling(doc: dict, key: str) -> Optional[tuple[str, dict]]:
    crit = registry_keys(doc).get(key + CRITICAL_SUFFIX)
    if crit is not None and crit.get("critical_of") == key:
        return key + CRITICAL_SUFFIX, crit
    return None


def render_chart_defaults_lines(doc: dict, indent: int) -> list[str]:
    """The chart-shipped ``defaults`` mapping body (helm values / dev template).

    Only ``chart_default: true`` keys, grouped by pack in registry order.
    unit+desc render as comment lines above each key; when the registry
    carries a ``<key>_critical`` sibling, an opt-in hint (with the registry's
    suggested value) is appended so the hand-written hints the old copies
    carried are reproduced mechanically.
    """
    ind = " " * indent
    lines: list[str] = []
    grouped = keys_by_pack(doc)
    for pack_name, pack_meta in (doc.get("packs", {}) or {}).items():
        chart_keys = [
            (k, e)
            for k, e in grouped.get(pack_name, {}).items()
            if e.get("chart_default")
        ]
        if not chart_keys:
            continue
        lines.append(f"{ind}# ── {pack_name}：{pack_meta['display']} ──")
        for key, entry in chart_keys:
            meta = f"{entry['unit']} — {entry['desc']}"
            sibling = _critical_sibling(doc, key)
            if sibling is not None:
                crit_key, crit = sibling
                meta += (
                    f"（critical 加嚴 opt-in：{crit_key}，"
                    f"registry 建議 {_fmt_value(crit['value'])}）"
                )
            for seg in _wrap_comment(meta, _COMMENT_WIDTH - indent - 2):
                lines.append(f"{ind}# {seg}")
            # Wired now, not "when a chart_default key gets one": no shipped
            # chart_default carries a counter-example today, so this emits
            # nothing — but the alternative is a face that silently ships a
            # bare number the day the registry learns something about it.
            lines.extend(_counterexample_lines(entry, f"{ind}# "))
            lines.append(f"{ind}{key}: {_fmt_value(entry['value'])}")
    return lines


_COMMENT_WIDTH = 100


def _wrap_comment(body: str, width: int) -> list[str]:
    """Wrap prose for a generated comment block WITHOUT ever splitting a token.

    ⛔ BOTH flags, and neither is style. `textwrap` defaults to breaking on
    hyphens AND to breaking long words, so either one can cut an identifier or a
    path in half across the two comment lines it emits — and a reference split
    that way is invisible to `git grep`, which is the #1373 accident shape that
    `tests/ops/test_wrapped_path_references.py` exists to catch.

    ⛔ THE POINT OF THIS FUNCTION IS THAT THERE IS ONLY ONE. The hyphen half was
    fixed at three of the six call sites in an earlier change (its comment names
    `negative-db2.yaml` being cut after the hyphen); the long-word half was
    fixed at NONE of them, and the other three never got the hyphen fix either.
    Six call sites meant six chances to get half of it. Deciding here means a
    caller cannot (#1453).

    ⚠️ THAT SENTENCE WAS TRUE OF THE FLAGS AND FALSE OF THE WIDTH when it was
    written: three call sites still passed a bare `100` while three used
    `_COMMENT_WIDTH`, so the base column budget was exactly the kind of
    half-applied decision this function exists to end — the same defect, one
    parameter over. All of them route through `_COMMENT_WIDTH` now, and nothing
    asserts that, so re-read it if you add a call site. What legitimately stays
    per-call-site is the SUBTRACTION (`- len(cont)` versus `- indent - 2`):
    different renderers spend different amounts of the line on their prefix.
    """
    return textwrap.wrap(body, width, break_on_hyphens=False,
                         break_long_words=False)


def _append_wrapped_comment(lines: list[str], item: str, meta: str, cont: str,
                            width: int = _COMMENT_WIDTH) -> list[str]:
    """Append ``item`` carrying ``meta`` as a trailing comment; wrap past ``width``.

    Both list-shaped renderers below (the shipped ``optional_overrides:`` list
    and the tenant stub's declared block) need the identical rule: keep the
    comment INLINE while the whole line fits in ``width`` columns, otherwise
    emit the item alone and continue ``meta`` on comment-only lines indented to
    ``cont``. Having written that rule twice is how the two blocks would
    eventually stop looking alike — they render the SAME declared set to two
    audiences, so a divergence in wrapping is a divergence nobody chose.

    ⚠️ THAT RULE IS NO LONGER "whatever each renderer did inline", and this
    paragraph used to say it was. The layout is unchanged — three spaces before
    the ``#``, continuation indented to ``cont`` — but the wrapping goes through
    ``_wrap_comment``, whose entire purpose is that ``textwrap``'s two splitting
    defaults are OFF (#1453). For any ``meta`` carrying a hyphenated or
    over-width token the output therefore DIFFERS from the old inline call, on
    purpose. ⚠️ No ``meta`` reaching THIS renderer carries such a token today, so
    reverting just this call site regenerates the surfaces byte-for-byte —
    measured. The regen diff #1453 landed came from a different renderer.

    Mutates and returns ``lines`` (the callers accumulate into one list).
    """
    inline = f"{item}   # {meta}"
    if len(inline) <= width:
        lines.append(inline)
        return lines
    lines.append(item)
    lines += [f"{cont}{seg}" for seg in _wrap_comment(meta, width - len(cont))]
    return lines


def _shipped_optional_by_pack(doc: dict) -> list[tuple[str, dict, list]]:
    """[(pack_name, pack_meta, [(key, entry), …]), …] — shipped optional keys.

    Deterministic order, and the ONLY traversal used by both the renderer and
    ``optional_override_list_keys``: registry pack order (``doc["packs"]``),
    then registry key order within each pack. Packs with no shipped optional
    key are omitted entirely.
    """
    grouped = keys_by_pack(doc)
    out: list[tuple[str, dict, list]] = []
    for pack_name, pack_meta in (doc.get("packs", {}) or {}).items():
        pack_keys = [
            (k, e)
            for k, e in grouped.get(pack_name, {}).items()
            if e.get("tier") == "optional_overrides" and is_shipped_optional_key(k)
        ]
        if pack_keys:
            out.append((pack_name, pack_meta, pack_keys))
    return out


def optional_override_list_keys(doc: dict) -> list[str]:
    """The key names shipped in the runtime ``optional_overrides:`` list.

    Same traversal + predicate as ``render_optional_overrides_lines``, exposed
    so callers can assert membership without re-parsing rendered comments.
    """
    return [k for _n, _m, pairs in _shipped_optional_by_pack(doc) for k, _e in pairs]


def render_optional_overrides_lines(doc: dict, indent: int) -> list[str]:
    """The shipped ``optional_overrides:`` LIST body (helm values / dev template).

    A YAML **array of strings**, not a ``key: value`` mapping — the platform
    RECOGNISES these keys and asserts NO value for them
    (``docs/schemas/platform-defaults.schema.json`` types ``optional_overrides``
    as ``array of string``; a value here would arm the key for every tenant,
    the exact opposite of the tier's meaning).

    ORDER: registry pack order, then registry key order within the pack (see
    ``_shipped_optional_by_pack``) — the same traversal
    ``render_chart_defaults_lines`` uses, so the two blocks read alike.

    Each item carries the registry's ``unit — desc`` as an INLINE comment,
    wrapped onto comment-only continuation lines when it would pass 100
    columns (``_entry_lines`` house style — the wrapping itself is
    ``_append_wrapped_comment``, shared with the tenant stub renderer).
    Everything added is a comment, so the block still parses as a plain list of
    key names — ``regen_surfaces`` ``yaml.safe_load``s the spliced document
    before writing, which would catch a regression here.
    """
    ind = " " * indent
    cont = f"{ind}  # "
    lines: list[str] = []
    for pack_name, pack_meta, pack_keys in _shipped_optional_by_pack(doc):
        lines.append(f"{ind}# ── {pack_name}：{pack_meta['display']} ──")
        for key, entry in pack_keys:
            _append_wrapped_comment(
                lines, f"{ind}- {key}", f"{entry['unit']} — {entry['desc']}", cont)
            # The tenant-facing copy of this same declared set already carries
            # the caveat per key; an operator reading the chart got less than a
            # tenant reading their own file. "Names a key" is the trigger, not
            # "hands over a number" — a reader who goes and looks the key up is
            # exactly the reader about to copy the reference figure (#1344).
            #
            # ⚠️ BELOW the item and indented, whereas
            # `annotate_defaults_counterexamples` puts it ABOVE the key. Not an
            # oversight: this block is a YAML LIST whose items already carry a
            # trailing `# unit — desc` comment that continues on indented
            # comment lines (`_append_wrapped_comment`), so the caveat is the
            # next continuation of the item it belongs to. A mapping key has no
            # such continuation, and a comment above it is the only position
            # that reads as a preamble rather than as a note on the PREVIOUS
            # key. Same composer, two host grammars.
            lines.extend(_counterexample_lines(entry, cont))
    return lines


# ---------------------------------------------------------------------------
# The SAME declared set, told to the OTHER audience (#1321)
# ---------------------------------------------------------------------------
# Everything above renders the declared tier for a reader who owns the PLATFORM
# side: rule-pack headers, helm values, the dev conf.d template. None of those
# files is one a tenant ever opens. The single file a tenant does open is its
# own ``<tenant>.yaml``, and until #1321 that file's generated header said the
# opposite of the truth ("Omitted keys inherit from _defaults.yaml" /
# "省略=Default") — true for every key with a platform default, FALSE for the
# declared tier, which has no value to inherit at all.
#
# So the tenant stub gets its own rendering of the declared set. Deliberately
# NOT a second derivation: both generators hand this renderer a key list that
# came out of ``shipped_optional_keys_for_packs`` (``init_project`` calls it
# directly; ``scaffold_tenant`` passes the very list ``generate_defaults``
# already wrote into the sibling ``_defaults.yaml``, so the stub cannot
# advertise a key that file does not declare — that mismatch is exactly what
# earns a tenant an HTTP 400 from the only supported writer).
#
# ⛔ The values render as REFERENCE-ONLY comment text and every emitted key line
# is itself a comment carrying a ``<your value>`` placeholder. Filling a real
# number in for the tenant would re-arm, on the generated-artifact surface, the
# exact keystroke #1310/PR-C removed from the interactive prompt (see
# ``scaffold_tenant.prompt_value``): ADR-030's blind-write reference library has
# measured counter-examples for these very numbers.

TENANT_STUB_DECLARED_HEADING = {
    "zh": "# ── 平台已宣告、但不主張值的 key（_defaults.yaml 的 optional_overrides 清單）──",
    "en": "# -- Declared keys: the platform recognises these but asserts no value --",
}

_TENANT_STUB_PLACEHOLDER = {"zh": '"<你的值>"', "en": '"<your value>"'}

_TENANT_STUB_PROSE = {
    "zh": (
        "#",
        "# 平台「認得」下列 key：你在上面 tenants: 底下自己填值就會生效，不必再找",
        "# operator 開通。但平台刻意不給它們預設值 ⇒ 不填＝沒有值＝不產生 series，",
        "# 不會有告警，也不會有任何錯誤訊息。那是設計，不是漏掉的預設——所以本檔開頭",
        "# 的三態在這一格只有兩態：填值，或靜默（沒有「省略＝用預設」可用）。",
        "# ⚠️ 下列數字是「參考起點」不是背書：這種閾值只有你自己的 baseline 校準得",
        "#    出來。平台自家的盲寫參考庫（ADR-030）已經對其中一些值量到反例，逐條",
        "#    標在該 key 自己底下——沒有標的就是沒測過，永遠不讀作「已驗證」。",
        "# 用法：整行複製到上面自己那個租戶底下、去掉開頭的「# 」、把佔位字串換成你",
        "#    校準出來的數字（縮排已對齊）。若上面該租戶目前是 `<tenant>: {}`（空設",
        "#    定），先把 ` {}` 刪掉再貼——flow mapping 底下接縮排行不是合法 YAML。",
    ),
    "en": (
        "#",
        "# The platform RECOGNISES these keys: set one under `tenants:` above and",
        "# it takes effect — no operator action needed. But the platform ships no",
        "# default for them, so leaving one out means NO value at all: no series,",
        "# no alert, and no error message either. That silence is the design, not",
        "# a missing default — the three-state rule at the top of this file has",
        "# only two states here: set a value, or stay silent.",
        "# WARNING: the numbers below are a STARTING REFERENCE, not an endorsement",
        "#    — only your own baseline can calibrate this tier. Where the",
        "#    platform's own blind-write reference library (ADR-030) measured a",
        "#    counter-example it is noted under that key; no note means nothing",
        "#    was measured, and never means 'validated'.",
        "# To use: copy a whole line under your tenant above, drop the leading",
        "#    '# ', and replace the placeholder with a number you calibrated",
        "#    yourself (the indent already lines up). If your tenant currently",
        "#    reads `<tenant>: {}`, delete the ` {}` first — indented lines under",
        "#    a flow mapping are not valid YAML.",
    ),
}

_TENANT_STUB_META = {
    "zh": "{desc} — 參考起點 {value} {unit}（非背書，須依自身 baseline 校準）",
    "en": "{desc} — reference start {value} {unit} (not an endorsement; calibrate)",
}


def stub_counterexample_lines(ce: dict, cont: str, lang: str = "zh") -> list[str]:
    """The ⚠ continuation lines under ONE stub key that has a measured
    counter-example.

    ⛔ Per KEY, deliberately. This block used to carry a single hand-written
    paragraph naming two Oracle cases, printed unconditionally — so a db2-only
    tenant was handed a caveat about two keys its file does not contain, while
    the one key in it that DOES have a measurement (``db2_deadlock_rate``) said
    nothing, and the blanket "both would over-fire" was the exact reversal the
    registry's ``direction`` enum exists to prevent (it is ``under_fire``).
    Two independent blind reviews found this as their top finding (#1344).
    """
    verdict = counterexample_verdict(ce, lang)
    mark = COUNTEREXAMPLE_MARKS[lang]
    if lang == "zh":
        body = (f"⚠ #{ce['issue']} {mark}："
                f"{counterexample_observed(ce, 'zh')}（{verdict}）")
    else:
        body = (f"⚠ #{ce['issue']} {mark}: "
                f"{counterexample_observed(ce, 'en')} ({verdict})")
    # No-split rule lives in `_wrap_comment`, for the same reason as the header
    # renderer: the defaults cut `negative-db2.yaml` in half across two lines.
    return [f"{cont}{seg}" for seg in
            _wrap_comment(body, _COMMENT_WIDTH - len(cont))]


def _optional_entry_index(rule_packs: Optional[dict] = None) -> dict[str, dict]:
    """key -> optional_overrides entry, across every pack (meta lookup only)."""
    if rule_packs is None:
        rule_packs = _load_scaffold().RULE_PACKS
    return {
        key: entry
        for meta in (rule_packs or {}).values()
        for key, entry in ((meta or {}).get("optional_overrides") or {}).items()
    }


def render_tenant_declared_stub_lines(
    keys, rule_packs: Optional[dict] = None, lang: str = "zh", indent: int = 4
) -> list[str]:
    """The tenant-stub comment block for ``keys`` (all lines are comments).

    ``keys`` is the caller's already-derived declared list (see the section
    note above — this function never re-derives it, so it cannot disagree with
    the ``_defaults.yaml`` written alongside). Empty list → empty block.

    Every key renders as ``# <indent>key: "<placeholder>"``, i.e. commented out
    AND valueless: dropping the leading ``"# "`` yields a line already at the
    right indent for ``tenants: <tenant>:``.

    ⚠️ Be precise about what the placeholder buys, because the natural claim —
    "an unsubstituted placeholder gets caught by validation" — is FALSE and was
    written here before it was measured. ``thresholdScalar`` in
    ``docs/schemas/tenant-config.schema.json`` is ``{"type": "string"}`` with no
    pattern, so ``key: "<your value>"`` is a schema-valid threshold: measured,
    ``validate_config.py`` reports 5 checks / 5 pass / 0 warn on exactly that
    file. What the placeholder DOES buy is that no platform-chosen number is
    silently armed — the failure mode it removes is a wrong threshold quietly
    alerting, not a wrong threshold being rejected. The only signal a
    substituted-nothing key produces today is exporter-side and
    tenant-invisible: ``resolveDeclaredRows`` cannot parse the value, logs
    ``WARN: invalid declared threshold`` and emits no row (``resolve.go``), so
    the tenant sees silence — the same thing it would see had it never set the
    key at all. Tightening the schema is deliberately NOT done here: that
    validator is on the path of every tenant threshold, so it is its own change.
    """
    if not keys:
        return []
    entries = _optional_entry_index(rule_packs)
    prefix = f"# {' ' * indent}"
    cont = f"{prefix}  # "
    lines = [TENANT_STUB_DECLARED_HEADING[lang], *_TENANT_STUB_PROSE[lang]]
    for key in keys:
        entry = entries.get(key) or {}
        keyline = f"{prefix}{key}: {_TENANT_STUB_PLACEHOLDER[lang]}"
        if not entry:
            lines.append(keyline)
            continue
        meta = _TENANT_STUB_META[lang].format(
            desc=entry.get("desc", ""),
            value=_fmt_value(entry.get("value")),
            unit=entry.get("unit", ""),
        )
        # Same wrapping rule as the shipped `optional_overrides:` list — one
        # implementation, because the two blocks render the same declared set.
        _append_wrapped_comment(lines, keyline, meta, cont)
        ce = entry.get("value_counterexample")
        if ce:
            lines.extend(stub_counterexample_lines(ce, cont, lang))
    return lines


# ---------------------------------------------------------------------------
# The THIRD population — and its truth is per-GENERATOR, not per-repo (#1321)
# ---------------------------------------------------------------------------
# The tenant header names three groups: keys `_defaults.yaml` gives a value to,
# keys it only DECLARES, and `<base>_critical`. The third sentence was first
# written as a static claim ("in neither block; it fires once `<base>` has a
# value") — measured true for what ``scaffold_tenant`` writes and FALSE for what
# ``init_project`` writes in the very same command:
#
#   * ``scaffold_tenant.RULE_PACKS`` puts ZERO ``_critical`` in its ``defaults``
#     tier, and the shipped ``helm/threshold-exporter/values.yaml``
#     ``thresholdConfig.defaults`` carries zero as well.
#   * ``init_project.RULE_PACK_CATALOG`` USED TO put sixteen of them straight
#     into ``defaults`` (11 of its 15 packs — e.g. mariadb's
#     ``mysql_connections_critical: 150``), so ``da-tools init --rule-packs
#     mariadb`` wrote a ``_defaults.yaml`` whose ``defaults:`` armed nothing at
#     all for that key, while the ``<tenant>.yaml`` written by the SAME run
#     told the tenant it was already covered.
#
# ⛔ PAST TENSE from here down, and the tense is the point: #1218 moved those
# sixteen into the catalog's ``critical_overrides`` tier and
# ``check_threshold_reachability``'s defaults-tier face now fails CI on any
# producer that reintroduces the shape, so BOTH generators are in the
# ``_critical``-free regime today. The derivation below is kept exactly because
# that was not always true and need not stay true: a sentence pinned to either
# regime goes quietly false the next time a generator moves, which is the whole
# defect #1321 fixed and #1218 found the other half of. What IS fixed here is
# the header lying about it: the claim is DERIVED from the ``defaults:`` mapping
# the generator is about to write, so whichever regime a generator is in, the
# file says the true one.

def defaults_critical_keys(defaults) -> list[str]:
    """The ``<base>_critical`` names a ``defaults:`` mapping assigns a value to.

    ``defaults`` is the mapping the caller is ABOUT TO WRITE into its
    ``_defaults.yaml`` — not a pack roster, not a registry query. Both
    generators already hold it (``scaffold_tenant`` as
    ``defaults_data["defaults"]``, ``init_project`` as ``_catalog_defaults()``),
    so nothing new is derived and the header cannot drift from its sibling file.
    """
    return [k for k in (defaults or {}) if k.endswith(CRITICAL_SUFFIX)]


# Two regimes, two truths. Keyed (lang, defaults-ships-critical) so a generator
# in either regime gets prose that is true for the file it just wrote.
#
# ⛔ The True regime is a MISPLACEMENT REPORT, not the good case, and saying so
# is the whole point of #1218 / TRK-344. It first shipped as reassurance — "the
# critical row fires without you doing anything" — written from reading
# `resolveCriticalRows` rather than running it. Measured (pkg/config/
# critical_tier_placement_test.go): a `<base>_critical` under `defaults:` is
# walked by `resolveBaseRows`, `parseMetricKey` splits on the FIRST underscore,
# and the row that comes out is
# `{component="<prefix>", metric="<rest>_critical", severity="warning"}`
# — a series no recording rule joins — with no critical row anywhere. So the one
# branch that existed to describe a defective file was telling its reader the
# file was fine.
#
# No producer reaches this branch today (`check_threshold_reachability`'s
# defaults-tier face fails CI first), and it is kept rather than deleted for
# that reason: the alternative to a truthful branch is a KeyError inside a CLI
# a customer is running.
_TENANT_STUB_CRITICAL_NOTE = {
    ("zh", False): (
        "# 第三類 <base>_critical：本次一起產出的 _defaults.yaml 的 defaults: 一個都沒有列，",
        "# 平台不供給 critical 值 ⇒ 你在這裡填一個，只要 <base> 在 defaults: 有值，就會",
        "# 產生一條真的 critical 閾值（不填則無此列，base 的 warning 列不受影響）。",
    ),
    ("zh", True): (
        "# ⚠️ 第三類 <base>_critical：本次一起產出的 _defaults.yaml 把其中 {n} 個放進了",
        "# defaults:，那是放錯區塊、不是可繼承的平台值 —— critical 層只認租戶覆寫，那 {n} 個",
        "# 只會各發一條 warning 級的 <base>_critical series（無人消費），critical 列並不存在。",
        "# 要 critical 列請在本檔填 <base>_critical，只要 <base> 在 defaults: 有值就會生效。",
    ),
    ("en", False): (
        "# A third group is `<base>_critical`. The _defaults.yaml generated next to",
        "# this file lists none of them under `defaults:`, so the platform supplies",
        "# no critical value: set one here and it produces a real critical-severity",
        "# threshold as long as `<base>` has a value under `defaults:`; leave it out",
        "# and there simply is no critical row.",
    ),
    ("en", True): (
        "# ⚠️ A third group is `<base>_critical`. The _defaults.yaml generated next",
        "# to this file puts {n} of them under `defaults:` — the wrong section, not a",
        "# platform value you can inherit: the critical tier reads tenant overrides",
        "# only, so each of those {n} emits a warning-severity `<base>_critical`",
        "# series that nothing consumes, and no critical row exists. Set",
        "# `<base>_critical` HERE to get one; it takes effect while `<base>` has a",
        "# value under `defaults:`.",
    ),
}


def render_tenant_critical_note_lines(defaults, lang: str = "zh") -> list[str]:
    """The tenant header's ``<base>_critical`` paragraph, true for ``defaults``.

    ⛔ Derived from the artifact, never asserted statically. Both generators
    ship the False regime today, and that is precisely why the derivation has
    to stay: they did NOT always agree (``init_project`` shipped 16 ``_critical``
    keys under ``defaults:`` until #1218), and a sentence fixed to either regime
    is a sentence that goes quietly false the next time one of them moves. The
    count is rendered too — it is what makes the claim checkable against the
    sibling ``_defaults.yaml`` rather than merely plausible.
    """
    shipped = defaults_critical_keys(defaults)
    template = _TENANT_STUB_CRITICAL_NOTE[(lang, bool(shipped))]
    return [line.format(n=len(shipped)) for line in template]


# The header sentence that POINTS AT the block above — and the pointer is the
# half that was still asserted rather than derived (#1321 review). The block is
# emitted only when the declared list is non-empty (``append_tenant_declared_stub``
# returns ``text`` unchanged for an empty list), while both generators' headers
# said "the list is at the end of this file" unconditionally. Measured: the
# optional tier of mariadb / redis / postgresql / kafka / rabbitmq / jvm / nginx
# is `_critical`-only, so `shipped_optional_keys_for_packs` returns [] for every
# one of them — a tenant onboarded onto any of those packs got a header
# pointing at a section that is not in its file.
#
# ⛔ Not fixed by making the sentence conditional prose ("if there are declared
# keys, see the end of the file") — that hands the tenant the judgement call the
# generator already has the data to make. Same discipline as the `_critical`
# paragraph next door: render the claim FROM the artifact this run produces.
_TENANT_STUB_DECLARED_NOTE = {
    ("zh", True): (
        "# 宣告層（_defaults.yaml 的 optional_overrides: 清單）沒有值可繼承 ⇒"
        " 省略＝沒有值＝靜默，",
        "# 不是「用預設」；那一格只有「填值」與「不填」兩態。本次選的 pack 有 {n} 個"
        "這種 key，",
        "# 清單見檔尾。",
    ),
    ("zh", False): (
        "# 宣告層（_defaults.yaml 的 optional_overrides: 清單）沒有值可繼承 ⇒"
        " 省略＝沒有值＝靜默，",
        "# 不是「用預設」；那一格只有「填值」與「不填」兩態。本次選的 pack 一個這種"
        " key 都沒有，",
        "# 所以本檔沒有那一段可看（換 pack 時會有）。",
    ),
    ("en", True): (
        "# Omitting a key _defaults.yaml only DECLARES (its `optional_overrides:`",
        "# list) inherits NOTHING — the platform asserts no value there, so an unset",
        "# declared key stays silent. The {n} such keys for the packs selected here",
        "# are listed at the end of this file.",
    ),
    ("en", False): (
        "# Omitting a key _defaults.yaml only DECLARES (its `optional_overrides:`",
        "# list) inherits NOTHING — the platform asserts no value there, so an unset",
        "# declared key stays silent. The packs selected here declare no such key,",
        "# so this file carries no such list.",
    ),
}


def render_tenant_declared_note_lines(keys, lang: str = "zh") -> list[str]:
    """The tenant header's declared-tier paragraph, true for ``keys``.

    ``keys`` must be the SAME list the caller hands
    ``append_tenant_declared_stub`` — that is what makes "the list is at the end
    of this file" a fact about the file being written rather than a hope. Both
    generators hold exactly one such list per run (``scaffold_tenant`` from the
    sibling ``_defaults.yaml`` it just built, ``init_project`` from
    ``shipped_optional_keys_for_packs``), so nothing new is derived here.

    The count is rendered for the same reason ``render_tenant_critical_note_lines``
    renders one: it makes the claim checkable against the emitted block instead
    of merely plausible.
    """
    keys = list(keys or [])
    return [line.format(n=len(keys))
            for line in _TENANT_STUB_DECLARED_NOTE[(lang, bool(keys))]]


def append_tenant_declared_stub(
    text: str,
    keys,
    rule_packs: Optional[dict] = None,
    lang: str = "zh",
    indent: int = 4,
) -> str:
    """Append the declared-key block to a rendered tenant stub. Idempotent.

    A file-level reference section appended AFTER the dumped YAML, not spliced
    into the tenant's own mapping: a stub with no overrides dumps as
    ``<tenant>: {}`` (flow style), so there is no block-mapping context to
    splice into at all. The prose says so — copying a line into a tenant that
    still reads ``{}`` needs the ``{}`` removed first, and a hint that quietly
    produces a YAML syntax error would be worse than no hint.
    """
    lines = render_tenant_declared_stub_lines(keys, rule_packs, lang, indent)
    if not lines or TENANT_STUB_DECLARED_HEADING[lang] in text:
        return text
    if text and not text.endswith("\n"):
        text += "\n"
    return text + "\n".join(lines) + "\n"


def render_block(surface_id: str, body_lines: list[str], indent: str = "") -> str:
    """Full generated block text (markers + body), newline-joined."""
    return "\n".join(
        [begin_marker(surface_id, indent), *body_lines, end_marker(surface_id, indent)]
    )


def surface_specs(doc: dict) -> list[dict]:
    """Every generated surface: id, absolute path, indent, rendered block.

    Order: the two ``defaults`` mappings (helm values, dev template), the two
    ``optional_overrides`` lists on the SAME two files (#1310), then one per
    threshold pack (packs without threshold keys —
    liveness/operational/custom-alerts — carry no generated block).

    Two surfaces per file is fine: ``regen_surfaces`` re-reads each file inside
    the loop, so the second splice sees the first one's write.
    """
    specs = [
        {
            "id": "helm-defaults",
            "path": HELM_VALUES_PATH,
            "indent": " " * 4,
            "body": render_chart_defaults_lines(doc, 4),
        },
        {
            "id": "dev-defaults",
            "path": DEV_DEFAULTS_PATH,
            "indent": " " * 2,
            "body": render_chart_defaults_lines(doc, 2),
        },
        {
            # thresholdConfig.optional_overrides — key at indent 2, list items
            # (and therefore the markers) at 4.
            "id": "helm-optional",
            "path": HELM_VALUES_PATH,
            "indent": " " * 4,
            "body": render_optional_overrides_lines(doc, 4),
        },
        {
            # top-level optional_overrides — key at indent 0, items at 2.
            "id": "dev-optional",
            "path": DEV_DEFAULTS_PATH,
            "indent": " " * 2,
            "body": render_optional_overrides_lines(doc, 2),
        },
        {
            # try-local ships its own conf.d; same list, same shape.
            "id": "try-local-optional",
            "path": TRY_LOCAL_DEFAULTS_PATH,
            "indent": " " * 2,
            "body": render_optional_overrides_lines(doc, 2),
        },
    ]
    grouped = keys_by_pack(doc)
    for pack_name, pack_meta in (doc.get("packs", {}) or {}).items():
        if not grouped.get(pack_name):
            continue
        specs.append({
            "id": f"pack-{pack_name}",
            "path": os.path.join(_REPO_ROOT, *pack_meta["rule_pack_file"].split("/")),
            "indent": "",
            "body": render_pack_header_lines(doc, pack_name),
        })
    for spec in specs:
        spec["block"] = render_block(spec["id"], spec["body"], spec["indent"])
    return specs


def _find_block(lines: list[str], surface_id: str, indent: str):
    """(begin_idx, end_idx) of the marker lines, or an error string."""
    b, e = begin_marker(surface_id, indent), end_marker(surface_id, indent)
    b_idx = [i for i, ln in enumerate(lines) if ln == b]
    e_idx = [i for i, ln in enumerate(lines) if ln == e]
    if len(b_idx) != 1 or len(e_idx) != 1:
        return (
            f"markers for surface {surface_id!r} not found exactly once "
            f"(begin×{len(b_idx)}, end×{len(e_idx)}) — the delimiters are "
            "load-bearing; restore them (git checkout) or re-carve the block"
        )
    if e_idx[0] <= b_idx[0]:
        return f"markers for surface {surface_id!r} are out of order"
    return b_idx[0], e_idx[0]


def check_surface(text: str, spec: dict) -> Optional[str]:
    """None if the file's generated block is byte-fresh, else an error."""
    lines = text.split("\n")
    found = _find_block(lines, spec["id"], spec["indent"])
    if isinstance(found, str):
        return f"{spec['path']}: {found}"
    b, e = found
    current = "\n".join(lines[b : e + 1])
    if current != spec["block"]:
        return (
            f"{spec['path']}: generated block {spec['id']!r} is STALE vs the "
            "registry — run check_threshold_registry.py --regen"
        )
    return None


def splice_surface(text: str, spec: dict) -> str:
    """Replace the delimited block with a fresh render (idempotent)."""
    lines = text.split("\n")
    found = _find_block(lines, spec["id"], spec["indent"])
    if isinstance(found, str):
        raise ValueError(f"{spec['path']}: {found}")
    b, e = found
    return "\n".join(lines[:b] + spec["block"].split("\n") + lines[e + 1 :])


def regen_surfaces(doc: Optional[dict] = None) -> list[str]:
    """Splice every generated surface in place. Returns touched paths."""
    sys.path.insert(0, _THIS_DIR)
    sys.path.insert(0, os.path.join(_THIS_DIR, ".."))
    from _lib_python import write_text_secure  # noqa: E402

    if doc is None:
        doc = load_registry()
    touched = []
    repo_root = os.path.realpath(_REPO_ROOT)
    for spec in surface_specs(doc):
        # Containment: surface paths derive from committed registry fields
        # (e.g. rule_pack_file) that --regen consumes WITHOUT schema
        # validation — a `..`-carrying value must fail loud, not splice a
        # file outside the repo (CodeRabbit #1222 nitpick).
        real = os.path.realpath(spec["path"])
        if not real.startswith(repo_root + os.sep):
            raise ValueError(
                f"generated-surface path escapes repo root: {spec['path']!r}"
            )
        with open(spec["path"], encoding="utf-8") as fh:
            old = fh.read()
        new = splice_surface(old, spec)
        if new != old:
            # A spliced block that breaks the host document must fail loud
            # BEFORE the write: the freshness gate string-compares and would
            # not catch invalid YAML (burned once — a two-element heading
            # emitted a bare comment continuation line into 13 packs).
            if spec["path"].endswith((".yaml", ".yml")):
                yaml.safe_load(new)
            write_text_secure(spec["path"], new)
            touched.append(spec["path"])
    return touched


# ---------------------------------------------------------------------------
# Header-prose key membership (F6) — hand-written pack-header prose may only
# name conf.d threshold keys that actually exist somewhere in the contract
# ---------------------------------------------------------------------------
# The disease this kills: a pack header hand-claiming keys that exist nowhere
# (the pre-rewire elasticsearch header listed four demand-side names absent
# from the platform-defaults contract). Scanned region: the leading comment
# header of each rule-pack file, MINUS the generated block. Dimensional
# tokens (``key{selector=...}``) are exempt — the dimensional-threshold
# contract is scaffold's dimensional_example domain, not registry identity.

# YAML-ish structure words that legitimately appear in `key:` position inside
# header prose but are not conf.d threshold keys.
STRUCTURAL_PROSE_TOKENS: frozenset[str] = frozenset({
    "state_filters",
    "default_state",
    "group_by",
    "group_wait",
    "group_interval",
    "repeat_interval",
    "rule_pack_file",
    "default_on",
    "metric_class",
    "chart_default",
    "critical_of",
    "optional_overrides",
    "runbook_url",
    "metric_group",
    "alert_threshold",
    "user_threshold",
    # Alertmanager config vocabulary (operational pack header prose)
    "inhibit_rules",
})

# candidate: snake_case token (≥1 underscore) in key position, not glued to a
# preceding identifier char or a recording-rule ':' segment.
_PROSE_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_:])([a-z][a-z0-9]*(?:_[a-z0-9]+)+)")


def iter_header_prose_lines(text: str):
    """(lineno, line) for the hand-written leading comment header.

    Stops at the first non-comment, non-blank line; skips the generated block
    (its key lines are registry-owned, not prose claims).
    """
    in_generated = False
    for i, line in enumerate(text.split("\n"), start=1):
        stripped = line.strip()
        if not stripped.startswith("#"):
            if stripped == "":
                continue
            return
        if f"# >>> {_MARKER_STEM}" in line:
            in_generated = True
            continue
        if f"# <<< {_MARKER_STEM}" in line:
            in_generated = False
            continue
        if not in_generated:
            yield i, line


def prose_key_tokens(text: str):
    """(lineno, token) for every conf.d-key-shaped claim in header prose."""
    for lineno, line in iter_header_prose_lines(text):
        for m in _PROSE_TOKEN_RE.finditer(line):
            rest = line[m.end():]
            if rest.startswith("{"):
                continue  # dimensional token — out of registry identity scope
            if re.match(r"[\"']?\s*:", rest) is None:
                continue  # not in key position
            yield lineno, m.group(1)


def membership_universe(
    doc: dict, extra_allowed: Optional[set[str]] = None
) -> set[str]:
    """Every token a pack header may legitimately claim as a conf.d key.

    Registry keys + their derived ``_critical`` opt-ins (the exporter accepts
    ``<base>_critical`` for any shipped base), plus deprecated alias spellings
    (#1231 — still-valid conf.d input during their transition window) and
    their ``_critical`` derivatives, plus caller-supplied extras (the #1196
    KNOWN_UNWIRED pending line, scaffold state-filter names), plus structural
    prose tokens.
    """
    keys = set(registry_keys(doc))
    keys |= set(doc.get("deprecated_aliases", {}) or {})
    uni = keys | {k + CRITICAL_SUFFIX for k in keys}
    if extra_allowed:
        uni |= set(extra_allowed)
        uni |= {k + CRITICAL_SUFFIX for k in extra_allowed}
    return uni | STRUCTURAL_PROSE_TOKENS
