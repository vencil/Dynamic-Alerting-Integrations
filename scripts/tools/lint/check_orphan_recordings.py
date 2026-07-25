#!/usr/bin/env python3
"""Orphan recording-rule gate — every recording rule must have a consumer somewhere in the platform (TRK-339 WS2a / #1200).

WHY: a recording rule with no consumer is not "free" — it evaluates every
interval, emits series, and (worse) LOOKS load-bearing, so nobody dares delete
or rename it, while renames of its intended consumer silently sever the pair
(the #1189 hand-copied-contract disease, recording-rule edition). The WS2a
tree scan (#1200) found 12 such orphans already shipped; #1204 (TRK-341)
tracks their per-record disposition (rewire vs delete). This gate freezes the
count: NEW orphans fail loud at commit time.

WHAT THIS CHECKS: the set of `record:` names defined across the two rule
trees (T1 `rule-packs/rule-pack-*.yaml` incl. custom-alerts; T2
`k8s/03-monitoring/configmap-rules-*.yaml` with ConfigMap `data` values
re-parsed as YAML — the platform pack is T2-only) must each be consumed by at
least one of:
  - another alert/recording expr in either tree (self-reference not counted);
  - a Grafana dashboard query (`k8s/03-monitoring/*.json`, the embedded
    `configmap-grafana-*.yaml` dashboards, and try-local's home dashboard);
  - `scripts/tools/ops/metric_observed_map.yaml` `observed_series` values.

IDENTITY COMPARISON (the substring trap): consumption is decided by exact
full-token equality, never substring. Tokens are extracted with a PromQL
identifier regex that treats `:` as part of the token, so
`tenant:alert_threshold:db2_lock_wait_time` can never "consume"
`tenant:db2_lock_wait_time:rate5m` (old-name-is-a-prefix-of-new-name is
exactly the drift this family of gates exists to catch — see
feedback: substring match hides drift).

KNOWN_ORPHANS: the 12 records already orphaned when this gate landed,
grandfathered as INFO with a pointer to #1204. The list is EXIT-LOCKED (same
discipline as check_threshold_reachability.KNOWN_UNWIRED): a grandfathered
record that gains a consumer or is deleted becomes a HARD error, forcing the
list to shrink as #1204 fixes land. EXEMPT_ORPHANS holds the intentional
inventory-style series (see the dict for rationale) — existence-checked only.

SCOPE NOTE (deliberate non-gate): the WS2a S2 or-masking scan is NOT gated
here or anywhere. Its classification (same-label-set `or` arms → possible
masking) is a heuristic that needs human judgment per finding; it stays a
periodic read-only script, not a pre-commit lint. Only the two mechanical
identity checks (this gate + check_metric_group_pairs) were promoted.

Exit codes (_lib_exitcodes): 0 clean / 1 violation (--ci) / 2 caller error.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)
sys.path.insert(0, os.path.join(_THIS_DIR, ".."))
from _lib_compat import try_utf8_stdout  # noqa: E402
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR  # noqa: E402
from _lib_validation import i18n_text  # noqa: E402

import yaml  # noqa: E402

# The 12 recording rules already orphaned when this gate landed — series with
# values that nothing reads. Grandfathered as INFO (see module docstring);
# per-record disposition (rewire the intended consumer vs delete) is tracked
# in #1204 (TRK-341). EXIT-LOCKED: entries must be removed as fixes land.
KNOWN_ORPHANS: dict[str, str] = {
    "tenant:clickhouse_merges_rate:rate5m": "no consumer anywhere — disposition in #1204 (TRK-341)",
    "tenant:es_indexing:rate5m": "no consumer anywhere — disposition in #1204 (TRK-341)",
    "tenant:mongodb_connections_available:min": "no consumer anywhere — disposition in #1204 (TRK-341)",
    "tenant:mongodb_document_ops:rate5m": "no consumer anywhere — disposition in #1204 (TRK-341)",
    "tenant:mysql_buffer_pool_usage:ratio": "no consumer anywhere — disposition in #1204 (TRK-341)",
    "tenant:mysql_connection_usage:ratio": "no consumer anywhere — disposition in #1204 (TRK-341)",
    "tenant:mysql_uptime:hours": "no consumer anywhere — disposition in #1204 (TRK-341)",
    "tenant:pg_connections:max": "no consumer anywhere — disposition in #1204 (TRK-341)",
    "tenant:pg_transactions:rate5m": "no consumer anywhere — disposition in #1204 (TRK-341)",
    "tenant:pg_uptime:hours": "no consumer anywhere — disposition in #1204 (TRK-341)",
    "tenant:redis_commands:rate5m": "no consumer anywhere — disposition in #1204 (TRK-341)",
    "tenant:redis_memory_usage:ratio": "no consumer anywhere — disposition in #1204 (TRK-341)",
}

# Intentional no-consumer series — NOT drift, NOT tracked in #1204. These are
# inventory/info-style records whose value is being scrapeable (federation /
# ad-hoc PromQL), not being read by a shipped rule or dashboard. They are
# existence-checked (deleting the record without dropping the exemption is a
# STALE-EXEMPTION error) but never reported as orphans.
#   custom_recipe_info: ADR-024 custom-alerts inventory series — one labelled
#   sample per compiled recipe, exists so operators/federation can enumerate
#   deployed recipes; by design nothing in the rule trees consumes it. (Also
#   the only colon-less record name — see the token-extraction STOP list.)
EXEMPT_ORPHANS: dict[str, str] = {
    "custom_recipe_info": "ADR-024 inventory series — enumeration surface, intentionally consumer-less",
}

# ── rule-tree + dashboard + observed-map collection ─────────────────────────

T1_GLOB = ("rule-packs", "rule-pack-*.yaml")
T2_GLOB = ("k8s/03-monitoring", "configmap-rules-*.yaml")
DASHBOARD_DIRS = ("k8s/03-monitoring",)
EXTRA_DASHBOARDS = ("try-local/grafana/dashboards/trylocal-home.json",)
GRAFANA_CM_GLOB = ("k8s/03-monitoring", "configmap-grafana-*.yaml")
OBSERVED_MAP = "scripts/tools/ops/metric_observed_map.yaml"

# PromQL identifier: `:` is part of the token, which is what makes the
# comparison colon-adjacency-safe (see module docstring).
TOKEN_RE = re.compile(r"[a-zA-Z_:][a-zA-Z0-9_:]*")

# Tokens that are PromQL/Grafana syntax, not series names. Colon-less record
# names (custom_recipe_info) share the namespace with these keywords — keep
# any addition here from shadowing a real record.
_AGGS = {"sum", "min", "max", "avg", "group", "stddev", "stdvar", "count",
         "count_values", "bottomk", "topk", "quantile", "limitk", "limit_ratio"}
_KWS = {"by", "without", "on", "ignoring", "group_left", "group_right",
        "offset", "bool", "and", "or", "unless", "atan2", "start", "end"}
_FUNCS = {"abs", "absent", "absent_over_time", "acos", "acosh", "asin", "asinh",
          "atan", "atanh", "avg_over_time", "ceil", "changes", "clamp",
          "clamp_max", "clamp_min", "cos", "cosh", "count_over_time",
          "day_of_month", "day_of_week", "day_of_year", "days_in_month", "deg",
          "delta", "deriv", "exp", "floor", "histogram_avg", "histogram_count",
          "histogram_sum", "histogram_fraction", "histogram_quantile",
          "histogram_stddev", "histogram_stdvar", "holt_winters", "hour",
          "idelta", "increase", "info", "irate", "label_join", "label_replace",
          "last_over_time", "ln", "log10", "log2", "mad_over_time",
          "max_over_time", "min_over_time", "minute", "month", "pi",
          "predict_linear", "present_over_time", "quantile_over_time", "rad",
          "rate", "resets", "round", "scalar", "sgn", "sin", "sinh", "sort",
          "sort_by_label", "sort_by_label_desc", "sort_desc", "sqrt",
          "stddev_over_time", "stdvar_over_time", "sum_over_time", "tan",
          "tanh", "time", "timestamp", "vector", "year"}
_GRAFANA = {"label_values", "query_result", "tag_values", "__interval",
            "__rate_interval", "__range", "__auto", "__name__"}
_UNITS = {"s", "m", "h", "d", "w", "y", "ms"}
STOP_TOKENS = _AGGS | _KWS | _FUNCS | _GRAFANA | _UNITS


def _strip_strings(s: str) -> str:
    s = re.sub(r'"(?:\\.|[^"\\])*"', '""', s)
    s = re.sub(r"'(?:\\.|[^'\\])*'", "''", s)
    # PromQL also accepts Go-style backtick raw strings (no escapes inside);
    # without this strip their contents would leak into TOKEN_RE (Gemini #1214).
    s = re.sub(r'`[^`]*`', '``', s)
    return s


def metric_tokens(expr: str) -> set[str]:
    """Full-token identifier set of a PromQL/Grafana expression."""
    return {t for t in TOKEN_RE.findall(_strip_strings(expr))
            if t not in STOP_TOKENS}


def _load_yaml(path: Path):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def iter_rules(root: Path = PROJECT_ROOT):
    """Yield (source, kind, name, expr) for every alert/recording rule in
    both trees. T2 ConfigMap `data` values are re-parsed as YAML."""
    def eat(doc, source):
        if not doc or "groups" not in doc:
            return
        for g in doc.get("groups") or []:
            for r in g.get("rules") or []:
                if "record" in r:
                    yield source, "record", r["record"], str(r.get("expr", ""))
                elif "alert" in r:
                    yield source, "alert", r["alert"], str(r.get("expr", ""))

    t1_dir, t1_pat = T1_GLOB
    for f in sorted((root / t1_dir).glob(t1_pat)):
        yield from eat(_load_yaml(f), f.name)

    t2_dir, t2_pat = T2_GLOB
    for f in sorted((root / t2_dir).glob(t2_pat)):
        cm = _load_yaml(f)
        for key, val in (cm.get("data") or {}).items():
            yield from eat(yaml.safe_load(val), f"{f.name}#{key}")


def _walk_json_strings(obj, keys=("expr", "query")):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in keys and isinstance(v, str):
                yield v
            else:
                yield from _walk_json_strings(v, keys)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk_json_strings(item, keys)


def iter_dashboard_exprs(root: Path = PROJECT_ROOT):
    """Yield every query/expr string from dashboards (standalone JSON files
    plus JSON embedded in configmap-grafana-* `data` values)."""
    paths = []
    for d in DASHBOARD_DIRS:
        paths.extend(sorted((root / d).glob("*.json")))
    paths.extend(root / p for p in EXTRA_DASHBOARDS)
    for p in paths:
        if not p.exists():
            continue
        # fail-loud: an unparseable dashboard would silently drop consumers
        # and fabricate orphans — that is a caller error, not a finding.
        doc = json.loads(p.read_text(encoding="utf-8"))
        yield from _walk_json_strings(doc)

    cm_dir, cm_pat = GRAFANA_CM_GLOB
    for p in sorted((root / cm_dir).glob(cm_pat)):
        cm = _load_yaml(p)
        for _key, val in (cm.get("data") or {}).items():
            yield from _walk_json_strings(json.loads(val))


def _walk_observed_series(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "observed_series" and isinstance(v, str):
                yield v
            else:
                yield from _walk_observed_series(v)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk_observed_series(item)


def collect_record_names(root: Path = PROJECT_ROOT) -> set[str]:
    return {name for _s, kind, name, _e in iter_rules(root) if kind == "record"}


def collect_consumed_tokens(root: Path = PROJECT_ROOT) -> set[str]:
    """Union of consumer-side tokens over all four surfaces.

    A recording rule's own expr does not consume its own name
    (self-reference is definition, not consumption)."""
    consumed: set[str] = set()
    for _source, kind, name, expr in iter_rules(root):
        toks = metric_tokens(expr)
        if kind == "record":
            toks.discard(name)
        consumed |= toks
    for expr in iter_dashboard_exprs(root):
        consumed |= metric_tokens(expr)
    for series in _walk_observed_series(_load_yaml(root / OBSERVED_MAP)):
        consumed.add(series)  # exact full-string identity
    return consumed


# ── the check ───────────────────────────────────────────────────────────────

def run_check(
    record_names: set[str] | None = None,
    consumed: set[str] | None = None,
    known_orphans: dict[str, str] | None = None,
    exempt: dict[str, str] | None = None,
) -> dict[str, list[str]]:
    """Return {errors, infos}. errors fail --ci; infos are report-only.

    Inputs default to the real repo collectors; hermetic tests inject
    synthetic sets to exercise each branch without editing repo artifacts.
    """
    if record_names is None:
        record_names = collect_record_names()
    if consumed is None:
        consumed = collect_consumed_tokens()
    if known_orphans is None:
        known_orphans = KNOWN_ORPHANS
    if exempt is None:
        exempt = EXEMPT_ORPHANS

    orphans = record_names - consumed

    errors: list[str] = []
    infos: list[str] = []

    # NEW orphans (not grandfathered, not exempt) — fresh dead weight.
    for name in sorted(orphans - set(known_orphans) - set(exempt)):
        errors.append(
            f"ORPHAN-RECORDING: {name!r} is defined but consumed by no rule "
            "expr, no dashboard, and no observed-map entry — it burns "
            "evaluation cycles and reads as load-bearing while being dead. "
            "Wire up its consumer, or delete the record. (TRK-339 / #1200)"
        )

    # Grandfathered orphans are report-only WHILE still orphaned...
    for name in sorted(orphans & set(known_orphans)):
        infos.append(f"known-orphan {name} — {known_orphans[name]}")

    # ...but the ledger is exit-locked: an entry that got a consumer or was
    # deleted must be dropped, else it rots into a permanent exemption.
    for name in sorted(set(known_orphans) - orphans):
        if name in record_names:
            errors.append(
                f"STALE-EXEMPTION: {name!r} is in KNOWN_ORPHANS but now HAS a "
                "consumer — the rewire landed; remove it from KNOWN_ORPHANS "
                "so the gate protects it."
            )
        else:
            errors.append(
                f"STALE-EXEMPTION: {name!r} is in KNOWN_ORPHANS but is no "
                "longer defined in any tree — remove it from KNOWN_ORPHANS."
            )

    # Exempt entries are permanent by design, but must still exist.
    for name in sorted(set(exempt) - record_names):
        errors.append(
            f"STALE-EXEMPTION: {name!r} is in EXEMPT_ORPHANS but is no longer "
            "defined in any tree — remove the exemption."
        )

    return {"errors": errors, "infos": infos}


def main(argv: list[str] | None = None) -> int:
    try_utf8_stdout()
    parser = argparse.ArgumentParser(
        description=i18n_text(
            "孤兒 recording rule gate：兩樹每條 recording rule 都須有消費者（TRK-339 WS2a / #1200）",
            "Orphan recording-rule gate: every recording rule across both "
            "trees must have a consumer (TRK-339 WS2a / #1200)"))
    parser.add_argument(
        "--ci", action="store_true",
        help=i18n_text("新孤兒或 KNOWN_ORPHANS exit-lock 破口即 exit 1（已知 12 個為 INFO）",
                       "exit 1 on NEW orphans or a KNOWN_ORPHANS exit-lock "
                       "breach (the known 12 are INFO)"))
    args = parser.parse_args(argv)

    try:
        result = run_check()
    except Exception as exc:  # noqa: BLE001 — caller error, not a violation
        print(f"ERROR: orphan-recording check crashed: {exc}", file=sys.stderr)
        return EXIT_CALLER_ERROR

    errors = result["errors"]
    infos = result["infos"]

    for msg in infos:
        print(f"INFO: {msg}", file=sys.stderr)
    for msg in errors:
        print(f"❌ {msg}", file=sys.stderr)

    if errors:
        print(
            f"\n{len(errors)} orphan-recording violation(s) — TRK-339/#1200.\n"
            "An unconsumed recording rule is dead weight that looks "
            "load-bearing. See scripts/tools/lint/check_orphan_recordings.py.",
            file=sys.stderr)
        return EXIT_VIOLATION if args.ci else EXIT_OK

    print(
        f"✅ orphan recordings OK — {len(infos)} known-orphan "
        "(grandfathered, #1204), 0 new drift.",
        file=sys.stderr)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
