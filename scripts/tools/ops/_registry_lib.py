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
SoT: three previously hand-copied surfaces are GENERATED from it inside
delimited blocks (everything outside the delimiters stays hand-written):

  1. rule-pack header threshold sections (the "對應的 threshold-exporter
     defaults" block of every threshold pack, defaults tier + a separate
     optional_overrides block carrying the ⛔ activation-precondition warning);
  2. ``helm/threshold-exporter/values.yaml`` ``thresholdConfig.defaults``
     (the ``chart_default`` key set);
  3. ``components/threshold-exporter/config/conf.d/_defaults.yaml``
     ``defaults`` section (same ``chart_default`` set).

``scaffold_tenant.RULE_PACKS`` remains the operative contract for config
GENERATION until PR-3 flips the direction (scaffold becomes a generated
artifact / runtime loader of the registry — D2 migration shape, 31 import
sites keep their API surface). During the transition the two copies MUST stay
semantically equal AND the three generated surfaces must stay fresh — both
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
    "mysql_cpu",
    "mysql_replication_lag",
    # PostgreSQL warning tier (postgresql pack) — owner-decided promotion,
    # #1200 Q3=C / D4 folding, 2026-07-25.
    "pg_connections",
    "pg_replication_lag",
})

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
    "mongodb_repl_lag_seconds": "replication",
    "kafka_under_replicated_partitions": "replication",
    "clickhouse_replication_queue": "replication",
    # capacity
    "es_filesystem_free_percent": "capacity",
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
    "mongodb_opcounters_total": "throughput",
    # latency
    "jvm_gc_pause": "latency",
    "oracle_wait_time_rate": "latency",
    # state
    "es_cluster_health": "state",
    "kafka_broker_count": "state",
    "kafka_active_controllers": "state",
    "rabbitmq_consumers": "state",
    # efficiency
    "db2_bufferpool_hit_ratio": "efficiency",
    "redis_evicted_keys_total": "efficiency",
    "redis_keyspace_misses_ratio": "efficiency",
    # errors
    "db2_deadlock_rate": "errors",
}

# Canonical per-key field order for the emitted registry document.
_FIELD_ORDER = (
    "pack", "tier", "value", "unit", "desc", "metric_class", "chart_default",
    "critical_of",
)

_REGISTRY_HEADER = (
    "# threshold-registry.yaml — threshold 契約 SoT（TRK-339 WS1a / #1200 D2）\n"
    "#\n"
    "# 地位：epic #1200 D2（2026-07-23 LOCKED）拍板 threshold 契約收斂到本檔——\n"
    "# 語言中立（helm/docs/portal 直讀）、schema 可驗證\n"
    "# （docs/schemas/threshold-registry.schema.json）。\n"
    "#\n"
    "# ✅ PR-2（rewire）後本檔已是真 SoT：三個原手抄面改為由本檔生成（定界符內），\n"
    "# 定界符外手寫 prose 照舊——\n"
    "#   1. rule-pack header 閾值段（defaults tier + optional_overrides ⛔ 警語段）\n"
    "#   2. helm/threshold-exporter/values.yaml thresholdConfig.defaults\n"
    "#      （chart_default 集）\n"
    "#   3. components/threshold-exporter/config/conf.d/_defaults.yaml defaults 段\n"
    "# 新鮮度由 check_threshold_registry.py --check 面強制（stale＝硬錯）。\n"
    "#\n"
    "# ⚠️ 過渡期殘留：scaffold_tenant.RULE_PACKS 仍是 config 生成路徑的運作副本，\n"
    "# 本檔由它機械萃取（scripts/tools/ops/_registry_lib.py build_registry_doc()，\n"
    "# 外加 registry-scope 附掛表 CHART_DEFAULT_KEYS）。兩者的語意等價由 pre-commit\n"
    "# gate check_threshold_registry.py 強制（防雙 SoT 漂移）。\n"
    "# 改閾值請改 scaffold_tenant.RULE_PACKS，再跑：\n"
    "#   python3 scripts/tools/lint/check_threshold_registry.py --regen\n"
    "# （會同時重產本檔＋三個生成面。）PR-3 起 scaffold RULE_PACKS 降為生成物/\n"
    "# runtime 載入，附掛表併回本檔。\n"
    "#\n"
    "# 範圍：threshold 身分（defaults / optional_overrides 兩層＋chart_default）。\n"
    "# state_filters 與 dimensional_example 仍歸 scaffold（非閾值身分）。萃取為全量；\n"
    "# 本 registry 首要服務的 enforcement 路徑是 18-key 修復線（#1196 / TRK-337）。\n"
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

    # canonical field order (stable YAML emission).
    keys = {
        k: {f: e[f] for f in _FIELD_ORDER if f in e} for k, e in keys.items()
    }
    return {"version": 1, "packs": packs, "keys": keys}


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
# Three previously hand-copied surfaces are generated from the registry inside
# delimited blocks. The delimiters are load-bearing: --check compares the
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
    if len(inline) <= 100:
        return [inline]
    return [keyline] + [
        f"{cont}{seg}" for seg in textwrap.wrap(meta, 100 - len(cont))
    ]


# The ⛔ activation-precondition warning generated above every pack's
# optional_overrides block (shape lifted from the hand-written rule-pack-db2
# header that first documented the failure mode — now generated everywhere so
# no pack header can claim a dormant key is a shipped default again).
_OPTIONAL_WARNING_LINES = (
    "#",
    "# optional_overrides（documented-but-dormant——登錄有案、平台預設不出貨）：",
    "# ⛔ 啟用前提（別跳過）：threshold-exporter 只對「存在於 defaults 的 key」",
    "# 發射 user_threshold——resolveBaseRows 迭代的是 c.Defaults，resolveCriticalRows",
    "# 也要求 base key 在 defaults 才處理 _critical 覆寫（components/",
    "# threshold-exporter/app/pkg/config/resolve.go）。下列 key 必須先進",
    "# _defaults.yaml / Helm values 的 defaults，租戶 conf.d 才有東西可覆寫——",
    "# 只在租戶檔填一個不在 defaults 的 key 永遠不會生效：那不是 dormant，",
    "# 是永久無法啟用。",
)


def render_pack_header_lines(doc: dict, pack_name: str) -> list[str]:
    """The generated threshold section of one rule-pack header (body only)."""
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
    if optional:
        lines += list(_OPTIONAL_WARNING_LINES)
        for key, entry in optional.items():
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
            for seg in textwrap.wrap(meta, 100 - indent - 2):
                lines.append(f"{ind}# {seg}")
            lines.append(f"{ind}{key}: {_fmt_value(entry['value'])}")
    return lines


def render_block(surface_id: str, body_lines: list[str], indent: str = "") -> str:
    """Full generated block text (markers + body), newline-joined."""
    return "\n".join(
        [begin_marker(surface_id, indent), *body_lines, end_marker(surface_id, indent)]
    )


def surface_specs(doc: dict) -> list[dict]:
    """Every generated surface: id, absolute path, indent, rendered block.

    Order: helm values, dev template, then one per threshold pack (packs
    without threshold keys — liveness/operational/custom-alerts — carry no
    generated block).
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
    ``<base>_critical`` for any shipped base), plus caller-supplied extras
    (the #1196 KNOWN_UNWIRED pending line, scaffold state-filter names), plus
    structural prose tokens.
    """
    keys = set(registry_keys(doc))
    uni = keys | {k + CRITICAL_SUFFIX for k in keys}
    if extra_allowed:
        uni |= set(extra_allowed)
        uni |= {k + CRITICAL_SUFFIX for k in extra_allowed}
    return uni | STRUCTURAL_PROSE_TOKENS
