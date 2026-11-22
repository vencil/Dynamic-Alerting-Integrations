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
full-name equality, never substring, so
`tenant:alert_threshold:db2_lock_wait_time` can never "consume"
`tenant:db2_lock_wait_time:rate5m` (old-name-is-a-prefix-of-new-name is
exactly the drift this family of gates exists to catch — see
feedback: substring match hides drift). The names an expression reads are
Prometheus' parse tree's (`_lib_consumer_read.promql_metric_names`, #4150),
never a scan of its text against a list of function names to skip. A
dashboard query is read after Grafana's own variable interpolation
(`grafana_promql`); one of a datasource that is not Prometheus is skipped
and named, and one Prometheus' parser still refuses is a caller error.

KNOWN_ORPHANS: the records already orphaned when this gate landed,
grandfathered as INFO with a pointer to #1204 (12 then; #1204 D-del deleted 11,
the one left is wired by #1320). The list is EXIT-LOCKED (same
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

from _lib_yaml_strict import strict_safe_load  # noqa: E402  (#4111: the observed-map, read strictly)
import _lib_consumer_read  # noqa: E402  (#3206: rule files / manifests as their consumers read them)

# The 12 recording rules already orphaned when this gate landed — series with
# values that nothing reads. Grandfathered as INFO (see module docstring);
# per-record disposition (rewire the intended consumer vs delete) is tracked
# in #1204 (TRK-341). EXIT-LOCKED: entries must be removed as fixes land.
KNOWN_ORPHANS: dict[str, str] = {
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

# #3471: the two trees are the rule-source declaration's entries — their
# directories and which files there are rule files (.yaml|yml; an undeclared
# YAML there is named, `SourcesUnlisted`) — not globs of this lint's own.
T1_SOURCE = _lib_consumer_read.RULE_PACKS
T2_SOURCE = _lib_consumer_read.CONFIGMAP_RULES
DASHBOARD_DIRS = ("k8s/03-monitoring",)
EXTRA_DASHBOARDS = ("try-local/grafana/dashboards/trylocal-home.json",)
GRAFANA_CM_GLOB = ("k8s/03-monitoring", "configmap-grafana-*.yaml")
OBSERVED_MAP = "scripts/tools/ops/metric_observed_map.yaml"

#: Grafana's variable syntax (grafana.com/docs/grafana/latest/dashboards/
#: variables/variable-syntax/) as Grafana interpolates it before the query
#: reaches Prometheus — Grafana's grammar, not PromQL's: the built-ins first,
#: longest name first (`$__interval_ms` is not `$__interval` + `_ms`), then
#: a variable in a duration's place (a range, a subquery, `offset`), then any
#: other variable. The values only have to parse; no name comes from them.
_GRAFANA_NUMBER = re.compile(r"\$\{?__(?:interval|range|rate_interval)_(?:ms|s)\}?(?![\w])")
_GRAFANA_DURATION = re.compile(r"\$\{?__(?:rate_interval|interval|range|auto)\}?(?![\w])")
_GRAFANA_VAR = r"\$(?:\{[^}]*\}|[A-Za-z_]\w*)"
_GRAFANA_OLD = re.compile(r"\[\[(\w+)(?::\w+)?\]\]")  # [[var]], the deprecated spelling of $var
_GRAFANA_BRACKET = re.compile(r"\[[^\[\]]*\]")       # a range or subquery: [5m], [$a:$b]
_GRAFANA_OFFSET = re.compile(r"\boffset(\s+-?)" + _GRAFANA_VAR)
_GRAFANA_ANY = re.compile(_GRAFANA_VAR)
#: Grafana's query-variable functions: label_values(<query>, <label>) and
#: query_result(<query>) wrap PromQL; label_values(<label>), label_names()
#: and metrics() read no series.
_GRAFANA_WRAPS = (re.compile(r"\s*label_values\((.*),\s*(?:[A-Za-z_]\w*|" + _GRAFANA_VAR + r")\s*\)\s*", re.S),
                  re.compile(r"\s*query_result\((.*)\)\s*", re.S))
_GRAFANA_NONE = re.compile(r"\s*(?:label_values\(\s*[A-Za-z_]\w*\s*\)|label_names\(.*\)|metrics\(.*\))\s*", re.S)


def grafana_promql(query: str) -> "str | None":
    """The PromQL Grafana sends for the dashboard *query* (its variables
    interpolated with placeholders that parse), or None for a query-variable
    function that reads no series."""
    if _GRAFANA_NONE.fullmatch(query):
        return None
    for wrap in _GRAFANA_WRAPS:
        m = wrap.fullmatch(query)
        if m:
            query = m.group(1)
            break
    query = _GRAFANA_OLD.sub(r"$\1", query)
    query = _GRAFANA_NUMBER.sub("300", query)
    query = _GRAFANA_DURATION.sub("5m", query)
    query = _GRAFANA_BRACKET.sub(lambda m: _GRAFANA_ANY.sub("5m", m.group(0)), query)
    query = _GRAFANA_OFFSET.sub(r"offset\g<1>5m", query)
    return _GRAFANA_ANY.sub("x", query)


class DashboardQueryRefused(ValueError):
    """A dashboard query Prometheus' parser refuses, after Grafana's
    interpolation: the consumers it reads are unknown, so a verdict would
    fabricate orphans — a caller error, like an unparseable dashboard."""


def _load_yaml(path: Path):
    """The observed-map — the repo's own threshold data, read only by Python
    tools. Every other file here has a consumer and is read its way."""
    with open(path, encoding="utf-8") as f:
        return strict_safe_load(f)


_RULE_FILES: "dict[Path, list]" = {}


def _rule_files(root: Path) -> list:
    """[(source, RuleFile)] for both trees, read the way Prometheus' rule
    loader reads them (#3206, ADR-036 §12: `da-read prometheus-rules`; a T2
    ConfigMap's outer layer the way kubectl reads it) — once per root. A
    rule file the loader refuses raises `RuleFilesRefused` naming it."""
    if root not in _RULE_FILES:
        t1_files = T1_SOURCE.files_under(root)
        t1 = list(zip((f.name for f in t1_files),
                      _lib_consumer_read.prometheus_rule_file_results(t1_files)))
        t2 = [(f"{Path(rf.manifest).name}#{rf.key}", rf)
              for rf in _lib_consumer_read.manifest_rule_file_results(
                  T2_SOURCE.files_under(root)) if rf.kind == "ConfigMap"]
        _lib_consumer_read.require_read([rf for _s, rf in t1 + t2])
        _RULE_FILES[root] = t1 + t2
    return _RULE_FILES[root]


def iter_rules(root: Path = PROJECT_ROOT):
    """Yield (source, kind, name, expr) for every alert/recording rule in
    both trees (`_rule_files`)."""
    def eat(doc, source):
        if not doc or "groups" not in doc:
            return
        for g in doc.get("groups") or []:
            for r in g.get("rules") or []:
                if "record" in r:
                    yield source, "record", r["record"], str(r.get("expr", ""))
                elif "alert" in r:
                    yield source, "alert", r["alert"], str(r.get("expr", ""))

    for source, rule_file in _rule_files(root):
        yield from eat(rule_file.documents[0], source)


def _datasource_type(ds, inherited):
    """A panel's / target's `datasource`: its `type` when Grafana wrote one
    (an object), else the enclosing one's (a uid, a name or `${DS}` names
    no type). Grafana's own pseudo-datasources (type `datasource`: `--
    Mixed --`, `-- Dashboard --`) name none either: a Mixed panel's target
    without its own reads the default datasource."""
    if isinstance(ds, dict) and isinstance(ds.get("type"), str) and ds["type"] != "datasource":
        return ds["type"]
    return inherited


#: Grafana template variable types whose `query` is a datasource query; the
#: others (custom, interval, constant, textbox, datasource, adhoc) hold a
#: list of values, a datasource type or nothing to run.
_QUERY_VARIABLE = "query"


def _walk_json_strings(obj, keys=("expr", "query"), ds_type=None):
    """(query, datasource type or None) of every query Grafana runs: a
    panel's targets under the datasource that panel or target names (a row's
    does not reach the panels it holds), and a template variable's when it
    is a query variable."""
    if isinstance(obj, dict):
        inherited = ds_type
        ds_type = _datasource_type(obj.get("datasource"), ds_type)
        for k, v in obj.items():
            if k == "templating" and isinstance(v, dict):
                for var in v.get("list") or []:
                    if not isinstance(var, dict) or var.get("type") != _QUERY_VARIABLE:
                        continue
                    q = var.get("query")
                    q = q.get("query") if isinstance(q, dict) else q
                    if isinstance(q, str):
                        yield q, _datasource_type(var.get("datasource"), inherited)
            elif k in keys and isinstance(v, str):
                yield v, ds_type
            else:
                child_ds = inherited if obj.get("type") == "row" else ds_type
                yield from _walk_json_strings(v, keys, child_ds)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk_json_strings(item, keys, ds_type)


def iter_dashboard_exprs(root: Path = PROJECT_ROOT):
    """Yield (source, query, datasource type or None) for every query/expr
    string of the dashboards (standalone JSON files plus JSON embedded in
    configmap-grafana-* `data` values)."""
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
        for query, ds in _walk_json_strings(doc):
            yield str(p.relative_to(root)), query, ds

    # #3206: the Grafana ConfigMaps the way kubectl reads them (da-crdecode).
    cm_dir, cm_pat = GRAFANA_CM_GLOB
    for p in sorted((root / cm_dir).glob(cm_pat)):
        for cm in _lib_consumer_read.kubernetes_documents(str(p)):   # a Kubernetes object: the manifest reading's guarantee (#3426)
            for key, val in (cm.get("data") or {}).items():
                for query, ds in _walk_json_strings(json.loads(val)):
                    yield f"{p.relative_to(root)} ({key})", query, ds


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


def collect_consumed_tokens(root: Path = PROJECT_ROOT, skipped: "list[str] | None" = None) -> set[str]:
    """Union of consumer-side tokens over all four surfaces.

    A recording rule's own expr does not consume its own name
    (self-reference is definition, not consumption)."""
    consumed: set[str] = set()
    rules = list(iter_rules(root))
    for (_source, kind, name, expr), names in zip(
            rules, _lib_consumer_read.promql_metric_names([r[3] for r in rules])):
        if isinstance(names, _lib_consumer_read.InputRefused):
            raise names  # a rule file Prometheus loaded parses; never reached
        consumed |= set(names) - ({name} if kind == "record" else set())
    queries = []
    for src, q, ds in iter_dashboard_exprs(root):
        if ds in (None, "prometheus"):
            queries.append((src, q))
        elif skipped is not None:
            skipped.append(f"{src}: a {ds} query, not Prometheus': {q!r}")
    promql = [(src, q, grafana_promql(q)) for src, q in queries]
    promql = [x for x in promql if x[2] is not None]
    for (src, query, _), names in zip(promql, _lib_consumer_read.promql_metric_names(
            [p for *_, p in promql])):
        if isinstance(names, _lib_consumer_read.InputRefused):
            raise DashboardQueryRefused(
                f"{src}: Prometheus' parser refuses the query {query!r} (after Grafana's "
                f"interpolation): {_lib_consumer_read.error_text(names)}")
        consumed |= set(names)
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
    skipped: list[str] = []
    if consumed is None:
        consumed = collect_consumed_tokens(skipped=skipped)
    if known_orphans is None:
        known_orphans = KNOWN_ORPHANS
    if exempt is None:
        exempt = EXEMPT_ORPHANS

    orphans = record_names - consumed

    errors: list[str] = []
    infos: list[str] = [f"not read (another datasource) — {s}" for s in skipped]

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
    except _lib_consumer_read.ConsumerReadError as exc:
        # #3206: a rule file Prometheus would not load (each named, with the
        # loader's reason), or no da-read — malformed input, not a violation.
        print(f"ERROR: {_lib_consumer_read.error_text(exc)}", file=sys.stderr)
        return EXIT_CALLER_ERROR
    except DashboardQueryRefused as exc:
        # #4150: a dashboard query that does not parse reads unknown
        # consumers — the input's problem, named, not a crash.
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_CALLER_ERROR
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
    import _lib_exitcodes
    _lib_exitcodes.run(main)
