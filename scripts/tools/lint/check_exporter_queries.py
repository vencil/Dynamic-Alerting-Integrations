#!/usr/bin/env python3
"""check_exporter_queries — a rule pack reads only what its query-exporter config serves.

Problem (#3833)
---------------
A pack's raw metric names were hand-written against an exporter nobody pinned:
the Oracle pack read four names neither iamseth/oracledb_exporter nor Oracle's
own exporter serves, so OracleHighSessionUtilization (shipped on) never fired.

What this guards
----------------
A pack whose registry entry names a ``queries:`` file (rule-packs/exporters/*.yaml)
gets its raw metrics from albertodonato/query-exporter running that file. The names
query-exporter serves are DERIVED with query-exporter's own code (its config loader
and metrics registry, given a stub database), not with a copy of its naming rules,
so an upgrade that renames something shows up here. Then, per pack:

  * every raw metric the pack reads — not a recording rule output (no ``:``), not a
    platform metric (``_lib_metric_sources.resolve_family``) — is a served name;
  * every metric the file declares is listed by a query (a metric no query lists is
    never served, and is not counted as served here);
  * every gauge in the file has ``expiration`` (query-exporter keeps a gauge's last
    value while the database is unreachable; expiration drops it);
  * no metric sets ``increment: true`` (a counter must carry the database's own
    cumulative value, which is what rate() expects);
  * the pack does not rate() / irate() / increase() / delta() a served gauge.

  * every `query="<name>"` the pack — or the platform's presence check for this
    pack (_lib_exporter_presence) — matches on names a query in the file, and every
    `query=~"<re>"` matches at least one (a renamed query would leave those
    selectors empty: OracleDatabaseDown silent, TenantExporterAbsent paging).

Whether the SQL runs, and serves what is derived, is not a static question: CI runs
scripts/ops/exporter_queries_runtime.sh <pack> for every pack with a queries file
(Oracle against gvenzl/oracle-free, DB2 against Db2 Community Edition), which calls
--compare-served.

Usage:
  check_exporter_queries.py          # check (0 ok / 1 finding / 2 cannot run)
  check_exporter_queries.py --ci     # same, for CI
  check_exporter_queries.py --json   # findings as JSON
  check_exporter_queries.py --pack oracle --compare-served FILE
      # runtime half: FILE is a /metrics scrape of query-exporter running the
      # pack's queries file against a real database. Every query must have run
      # (queries_total{status="success"}) and none failed, every name the file's
      # metrics derive to must be served with the file's type, and every raw
      # metric the pack reads must be served — query-exporter's built-ins
      # (query_timestamp, query_interval) with each `query` label it matches on.
"""
from __future__ import annotations

import argparse
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import _lib_compat  # noqa: E402,F401  (import-time side effect; see _lib_compat)
import _lib_consumer_read  # noqa: E402  (rule packs as Prometheus reads them)
import _lib_metric_sources  # noqa: E402
from _lib_exitcodes import Report  # noqa: E402
from _lib_validation import i18n_text  # noqa: E402

try:
    import yaml
    from _lib_yaml_strict import strict_safe_load  # noqa: E402,PLC0415  (#4111: duplicate keys refused)
except ImportError:  # pragma: no cover - environment guard
    print("check_exporter_queries: PyYAML required (pip install pyyaml)", file=sys.stderr)
    sys.exit(2)

_REPO = Path(__file__).resolve().parents[3]
_REGISTRY = _REPO / "rule-packs" / "threshold-registry.yaml"

_RATE_FUNCS = ("rate", "irate", "increase", "delta", "idelta", "deriv")


class CannotRun(Exception):
    """The check could not be made (missing package, unreadable input)."""


def bindings() -> list[tuple[str, Path, Path]]:
    """(pack, rule pack file, queries file) for every registry pack with `queries:`."""
    ops = str(Path(__file__).resolve().parents[1] / "ops")
    if ops not in sys.path:
        sys.path.insert(0, ops)
    import _registry_lib  # noqa: PLC0415  (scripts/tools/ops)
    try:
        doc = _registry_lib.load_registry(str(_REGISTRY)) or {}
    except (OSError, ValueError, RuntimeError, yaml.YAMLError) as e:
        raise CannotRun(f"cannot read {_REGISTRY}: {e}") from e
    out = []
    for name, meta in sorted((doc.get("packs") or {}).items()):
        q = (meta or {}).get("queries")
        if q:
            out.append((name, _REPO / meta["rule_pack_file"], _REPO / q))
    return out


def load_queries(path: Path) -> dict:
    try:
        doc = strict_safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as e:
        raise CannotRun(f"cannot read {path}: {e}") from e
    if not isinstance(doc, dict):
        raise CannotRun(f"{path}: the top-level YAML document is not a mapping")
    problem = shape_problem(doc)
    if problem:
        raise CannotRun(f"{path}: {problem}")
    return doc


def _names(value: object) -> bool:
    """A non-empty list of strings: what query-exporter takes for a query's
    `metrics` and `databases`."""
    return isinstance(value, list) and bool(value) and all(isinstance(v, str) for v in value)


def shape_problem(doc: dict) -> "str | None":
    """The first shape of *doc* this tool cannot walk, named; None if it can.

    Only the containers this tool reads (#4119): `metrics` and `queries` are
    mappings keyed by name, every metric and query is a mapping, and a query's
    `metrics` and `databases` are lists of names. Everything else (types,
    intervals, SQL parameters) is left to query-exporter's own validation in
    served_names(). Checked without query-exporter, so a malformed file is
    "cannot run" by name, never a traceback. Each shape refused here is one
    query-exporter refuses too (test_every_shape_refused_here_query_exporter_refuses):
    this is never stricter than the exporter."""
    for section, item in (("metrics", "metric"), ("queries", "query")):
        entries = doc.get(section)
        if entries is None:
            continue
        if not isinstance(entries, dict):
            return f"`{section}` must be a mapping of {item} names, not {type(entries).__name__}"
        for name, spec in entries.items():
            if not isinstance(name, str):
                return f"`{section}`: {item} name {name!r} is not a string"
            if not isinstance(spec, dict):
                return f"{item} {name} must be a mapping, not {type(spec).__name__}"
            if section == "queries":
                for key in ("metrics", "databases"):
                    if not _names(spec.get(key)):
                        return f"query {name}: `{key}` must be a non-empty list of names"
    return None


def unattached_metrics(doc: dict) -> set[str]:
    """Metrics the file declares that no query lists under its `metrics:`.

    Read off the YAML, so it answers without query-exporter installed; a shape
    query-exporter would refuse (shape_problem) is "cannot run" by name here
    too, not a traceback."""
    problem = shape_problem(doc)
    if problem:
        raise CannotRun(problem)
    attached: set[str] = set()
    for q in (doc.get("queries") or {}).values():
        attached |= set(q["metrics"])
    return set(doc.get("metrics") or {}) - attached


def rejection_text(error: Exception) -> str:
    """query-exporter's refusal, with where and why: its ConfigError says only
    "N validation error(s)" and carries each one's location and message in
    `.details` (#4119)."""
    details = getattr(error, "details", None) or ()
    parts = []
    for d in details:
        if isinstance(d, dict):
            where, why = d.get("location"), d.get("error")
            parts.append(f"{where}: {why}" if where else str(why))
    return f"{error} ({'; '.join(parts)})" if parts else str(error)


def served_names(path: Path, doc: dict) -> set[str]:
    """The metric names query-exporter serves for *path*, from its own code.

    Only the metrics a query lists: query-exporter sets a metric from a query's
    result columns, so one that is declared and attached to no query is never
    served (it logs `unused config entries`), whatever the declaration says
    (#4043)."""
    try:
        from prometheus_aioexporter import MetricsRegistry
        from prometheus_client import generate_latest
        from query_exporter.config import load_config
    except ImportError as e:
        raise CannotRun(
            "query-exporter is not installed (the pre-commit hook installs the version "
            f"pinned in requirements/ci-constraints.txt): {e}") from e
    dbs = sorted({db for q in (doc.get("queries") or {}).values()
                  for db in (q.get("databases") or [])})
    with tempfile.TemporaryDirectory() as tmp:
        stub = Path(tmp) / "databases.yaml"
        stub.write_text(yaml.safe_dump(
            {"databases": {db: {"dsn": "sqlite://"} for db in dbs}}),
            encoding="utf-8", newline="\n")
        try:
            config = load_config([stub, path])
        except Exception as e:  # query-exporter's own validation error
            raise CannotRun(f"{path}: query-exporter rejects it: {rejection_text(e)}") from e
    # config.metrics also holds query-exporter's built-ins (query_interval, …),
    # which no query lists and which are served all the same: only a metric of
    # the FILE that no loaded query lists is left out.
    # As query-exporter does it: every metric is created, only one a query
    # lists is ever set — and a metric that was never set exposes no sample.
    attached = {m.name for q in config.queries.values() for m in q.metrics}
    unserved = set(doc.get("metrics") or {}) - attached
    registry = MetricsRegistry()
    registry.create_metrics(config.metrics.values())
    for name, metric in config.metrics.items():
        if name in unserved:
            continue
        labels = {label: "x" for label in metric.labels} or None
        m = registry.get_metric(name, labels=labels)
        if metric.type in ("histogram", "summary"):
            m.observe(1)
        elif metric.type == "counter":
            m.inc(1)
        elif metric.type == "gauge":
            m.set(1)
    text = generate_latest(registry.registry).decode()
    return {line.split("{", 1)[0].split(" ", 1)[0]
            for line in text.splitlines() if line and not line.startswith("#")}


def pack_rules(rule_pack: Path) -> tuple[set[str], set[str], list[str], list[dict]]:
    """(raw tenant metrics read, recording rule names, every expr, the parse
    tree of each expr) of *rule_pack*. What a rule reads comes from its parse
    tree (``_lib_consumer_read.promql_shape_each``, #4219), never its text."""
    found = _lib_consumer_read.rule_files_in([rule_pack], keep_refusals=True)
    records: set[str] = set()
    exprs: list[str] = []
    for rfs in found.values():
        for rf in rfs:
            if rf.problem is not None:
                raise CannotRun(f"{rule_pack.name}: {rf.problem}")
            for grp in rf.groups:
                for rule in (grp.get("rules") or []):
                    if rule.get("record"):
                        records.add(rule["record"])
                    if isinstance(rule.get("expr"), str):
                        exprs.append(rule["expr"])
    trees = _lib_consumer_read.promql_shape_each(exprs, raise_refused=True)
    raw = {m for tree in trees for m in _lib_consumer_read.shape_metric_names(tree)
           if ":" not in m and m not in records
           and _lib_metric_sources.resolve_family(m) is None}
    return raw, records, exprs, trees


# The query-exporter built-ins that carry one series per query (#4264: read
# as selectors on the tree, never off the text).
_BUILTINS = ("query_timestamp", "query_interval")


def interval_seconds(value: object) -> float:
    """A query's `interval` in seconds, as query-exporter reads it: a string
    ``^[0-9]+[smhd]?$``, or anything int() takes. A value query-exporter
    refuses may read as anything here but never raises: query-exporter then
    refuses the file by location in served_names, and the run is "cannot run"
    whatever this said. Pinned by test_interval_and_timeout_parse_as_query_exporter_does
    (#4119)."""
    # The same steps as query-exporter's _validate_interval: `$` there also
    # takes a trailing newline (`interval: |` then `15` is "15\n", 15 s).
    multiplier = 1
    if isinstance(value, str):
        if not re.match(r"^[0-9]+[smhd]?$", value):
            return float("nan")
        multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
        if value[-1] in multipliers:
            value, multiplier = value[:-1], multipliers[value[-1]]
    try:
        return float(int(value) * multiplier)  # type: ignore[call-overload]
    except (TypeError, ValueError, OverflowError):
        return float("nan")


def timeout_seconds(value: object) -> float:
    """A query's `timeout` in seconds, as query-exporter reads it: a float (a
    number, or a string that is one — "1e1" is 10; "15s" is no timeout to it)."""
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return float("nan")


def _shape_trees(texts: "list[str]") -> "list[dict]":
    """Prometheus' tree of each text; one its parser refuses raises
    ``_lib_consumer_read.InputRefused`` naming it."""
    return _lib_consumer_read.promql_shape_each(list(texts), raise_refused=True)


def _tree_query_matchers(tree: dict) -> list[tuple[str, str]]:
    """(operator, value) of every `query` matcher of *tree*; a label_replace
    that writes `query` (its destination argument) counts as `=` its
    replacement."""
    out: list[tuple[str, str]] = []
    for node in _lib_consumer_read.shape_nodes(tree):
        if node["kind"] == "selector":
            out += [(m["type"], m["value"]) for m in node["matchers"] if m["name"] == "query"]
        elif node["kind"] == "call" and node["name"] == "label_replace" and len(node["args"]) == 5:
            dst, repl = node["args"][1], node["args"][2]
            if dst.get("kind") == "string" and dst["value"] == "query" and repl.get("kind") == "string":
                out.append(("=", repl["value"]))
    return out


def _query_matchers(text: str) -> list[tuple[str, str]]:
    """`_tree_query_matchers` of one PromQL text, read here."""
    return _tree_query_matchers(_shape_trees([text])[0])


def _builtin_selections(tree: dict) -> "list[tuple[str, list[tuple[str, str]]]]":
    """(built-in, its `query` matchers) for every selector of a
    query-exporter built-in in *tree*."""
    return [(node["name"], [(m["type"], m["value"]) for m in node["matchers"] if m["name"] == "query"])
            for node in _lib_consumer_read.shape_nodes(tree)
            if node["kind"] == "selector" and node["name"] in _BUILTINS]


def selected_queries(matchers: "list[tuple[str, str]]", queries: set[str]) -> set[str]:
    """The queries of the file that every `query` matcher of one selector
    selects (all of =, !=, =~, !~ apply, as in PromQL). Only this runtime
    expansion reads the negative matchers; query_label_findings checks = and =~."""
    keep = set(queries)
    for op, value in matchers:
        try:
            rx = re.compile(f"^(?:{value})$")
        except re.error:
            return set()
        if op == "=":
            keep = {q for q in keep if q == value}
        elif op == "!=":
            keep = {q for q in keep if q != value}
        elif op == "=~":
            keep = {q for q in keep if rx.match(q)}
        else:
            keep = {q for q in keep if not rx.match(q)}
    return keep


def query_label_findings(texts: list[str], queries: set[str], where: str) -> list[str]:
    """`query` label matchers (and label_replace targets) in *texts* that name no
    query of the file. A regex that is a plain alternation of names is checked name
    by name, so one existing alternative cannot cover a stale one."""
    out = []
    for tree in _shape_trees(texts):
        for op, value in _tree_query_matchers(tree):
            if op == "=":
                if value not in queries:
                    out.append(f"{where} matches query=\"{value}\", which the file does not define")
                continue
            if op != "=~":
                continue
            pattern = value
            if re.fullmatch(r"[A-Za-z0-9_]+(\|[A-Za-z0-9_]+)*", pattern):
                for alt in pattern.split("|"):
                    if alt not in queries:
                        out.append(f"{where} matches query=~\"{pattern}\": {alt} is not a query of the file")
                continue
            try:
                rx = re.compile(f"^(?:{pattern})$")
            except re.error as e:
                out.append(f"{where}: query=~\"{pattern}\" is not a valid regex: {e}")
                continue
            if not any(rx.match(q) for q in queries):
                out.append(f"{where} matches query=~\"{pattern}\", which no query of the file matches")
    return out


def rated_metrics(tree: dict) -> list[tuple[str, str]]:
    """(function, metric) for every metric read under a rate-like call
    (``_RATE_FUNCS``) of a parse tree: rate(x[5m]), rate((x)[5m:]) and
    rate((sum(x))[5m:]) all rate x. Read off the tree, so a parenthesis or a
    name inside a label value is not a call or a metric (#4219)."""
    out: list[tuple[str, str]] = []
    for node in _lib_consumer_read.shape_nodes(tree):
        if node["kind"] == "call" and node["name"] in _RATE_FUNCS:
            for arg in node["args"]:
                out += [(node["name"], m) for m in _lib_consumer_read.shape_metric_names(arg)]
    return out


def _presence_selectors(pack: str) -> list[str]:
    import _lib_exporter_presence  # noqa: PLC0415  (scripts/tools)
    return list(_lib_exporter_presence.pack_presence(pack))


def findings_for(pack: str, rule_pack: Path, queries: Path) -> list[str]:
    doc = load_queries(queries)
    rel = queries.relative_to(_REPO)
    out: list[str] = []
    metrics = doc.get("metrics") or {}
    for name, spec in sorted(metrics.items()):
        spec = spec or {}
        if spec.get("type") == "gauge" and not spec.get("expiration"):
            out.append(f"{rel}: gauge {name} has no expiration — it keeps its last value "
                       "while the database is unreachable")
        if spec.get("increment"):
            out.append(f"{rel}: {name} sets increment: true — a counter must carry the "
                       "database's own cumulative value")
    for name in sorted(unattached_metrics(doc)):
        out.append(f"{rel}: metric {name} is declared but no query lists it under "
                   "`metrics:` — query-exporter never serves it")
    # One connection per database by default (connection-pool size 1, one worker):
    # a query without a timeout that hangs holds it and every other query on
    # that database waits. The timeout must also end before the next run is due.
    for name, spec in sorted((doc.get("queries") or {}).items()):
        spec = spec or {}
        timeout, interval = spec.get("timeout"), spec.get("interval")
        if not timeout:
            out.append(f"{rel}: query {name} has no timeout — a hung statement holds "
                       "the database's only connection and every other query waits")
        elif interval and timeout_seconds(timeout) >= interval_seconds(interval):
            out.append(f"{rel}: query {name} timeout {timeout_seconds(timeout):g}s is not below "
                       f"its interval {interval_seconds(interval):g}s")
    # The query the presence check names is the liveness query. It runs on a
    # database entry no other query uses: each database entry has its own
    # connection pool and worker, so a hung query cannot hold the connection
    # the liveness query needs. Ordering and timeouts cannot give that alone:
    # query-exporter can only abandon a timed-out statement if the driver can
    # cancel it (cancel()/interrupt()), and ibm_db cannot — the statement keeps
    # the connection and the liveness query times out behind it.
    qspecs = doc.get("queries") or {}
    live = {v for text in _presence_selectors(pack)
            for op, v in _query_matchers(text) if op == "=" and v in qspecs}
    for name in sorted(live):
        mine = set((qspecs[name] or {}).get("databases") or [])
        for other, spec in qspecs.items():
            shared = mine & set((spec or {}).get("databases") or [])
            if other != name and shared:
                out.append(f"{rel}: liveness query {name} shares database "
                           f"{sorted(shared)[0]} with {other} — a hung {other} holds "
                           "that database's connection and the liveness query times "
                           "out behind it; give the liveness query a database entry "
                           "of its own (same DSN)")
    served = served_names(queries, doc)
    raw, _records, exprs, trees = pack_rules(rule_pack)
    for m in sorted(raw - served):
        out.append(f"{rule_pack.name} reads {m}, which {rel} does not serve")
    names = set(doc.get("queries") or {})
    out += query_label_findings(exprs, names, rule_pack.name)
    out += query_label_findings(_presence_selectors(pack), names,
                                "the presence check (_lib_exporter_presence)")
    gauges = {n for n, s in metrics.items() if (s or {}).get("type") == "gauge"}
    for tree in trees:
        for func, metric in rated_metrics(tree):
            if metric in gauges:
                out.append(f"{rule_pack.name} applies {func}() to gauge {metric}")
    return sorted(set(out))


def _parse_exposition(text: str) -> list[tuple[str, dict[str, str], float]]:
    out = []
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        head, _, value = line.rpartition(" ")
        name, _, rest = head.partition("{")
        labels = dict(re.findall(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"', rest))
        try:
            out.append((name, labels, float(value)))
        except ValueError:
            continue
    return out


# The TYPE a declared query-exporter metric type is exposed as (prometheus_client):
# an enum is a gauge in the text format and a stateset in OpenMetrics (#4119).
_SERVED_TYPES = {"enum": {"gauge", "stateset"}}


def served_type(types: dict[str, str], name: str) -> "str | None":
    """The TYPE line of the declared metric *name* in a scrape. The text format
    names a counter's TYPE line <name>_total; OpenMetrics names it without
    `_total`, also for a counter declared as <x>_total (#4119)."""
    for candidate in (name, f"{name}_total", name.removesuffix("_total")):
        if candidate in types:
            return types[candidate]
    return None


def compare_served(pack: str, scrape: Path) -> list[str]:
    """Findings of the runtime half for *pack* against the scrape in *scrape*."""
    match = [b for b in bindings() if b[0] == pack]
    if not match:
        raise CannotRun(f"no registry pack {pack!r} with a queries file")
    _pack, _rule_pack, queries = match[0]
    doc = load_queries(queries)
    try:
        text = scrape.read_text(encoding="utf-8")
        samples = _parse_exposition(text)
    except OSError as e:
        raise CannotRun(f"cannot read {scrape}: {e}") from e
    served = {name for name, _labels, _v in samples}
    types = dict(re.findall(r"(?m)^# TYPE (\S+) (\S+)$", text))
    out = []
    for q in sorted(doc.get("queries") or {}):
        ok = sum(v for n, lb, v in samples
                 if n == "queries_total" and lb.get("query") == q and lb.get("status") == "success")
        bad = sum(v for n, lb, v in samples
                  if n == "queries_total" and lb.get("query") == q and lb.get("status") != "success")
        if not ok:
            out.append(f"query {q} has not succeeded once")
        if bad:
            out.append(f"query {q} failed {int(bad)} time(s)")
    # Only the names the file's own metrics expose: query-exporter's built-ins
    # such as database_errors_total appear only after a first error.
    # A declared metric no query lists is not expected on /metrics; say so here
    # as the static half does, so this half alone does not pass over it (#4043).
    for name in sorted(unattached_metrics(doc)):
        out.append(f"metric {name} is declared but no query lists it under `metrics:` — "
                   "query-exporter never serves it")
    declared = set(doc.get("metrics") or {})
    expected = {n for n in served_names(queries, doc)
                if any(n == m or n.startswith(m + "_") for m in declared)}
    for name in sorted(expected - served):
        out.append(f"{name} is derived from {queries.relative_to(_REPO)} but not served")
    for name, spec in sorted((doc.get("metrics") or {}).items()):
        want = (spec or {}).get("type")
        got = served_type(types, name)
        if want and got and got not in _SERVED_TYPES.get(want, {want}):
            out.append(f"{name} is served as {got}, the file declares {want}")
    # What the pack itself reads, query-exporter's built-ins included: for those,
    # a series per `query` label value the pack (or its presence check) names.
    raw, _records, exprs, _trees = pack_rules(_rule_pack)
    for m in sorted(raw - served):
        out.append(f"{_rule_pack.name} reads {m}, which the exporter does not serve")
    # Every (built-in, query) pair a selector selects, regex selectors expanded
    # against the file's queries, must have its own series.
    names = set(doc.get("queries") or {})
    wanted: set[tuple[str, str]] = set()
    for tree in _shape_trees(exprs + _presence_selectors(pack)):
        for metric, matchers in _builtin_selections(tree):
            wanted |= {(metric, q) for q in selected_queries(matchers, names)}
    have = {(n, lb.get("query")) for n, lb, _v in samples}
    for metric, q in sorted(wanted - have):
        out.append(f"{metric}{{query=\"{q}\"}} is read by the pack but not served")
    return out


def main() -> "int | Report":
    ap = argparse.ArgumentParser(description=i18n_text(
        "rule pack 讀的原始指標必須由它在 registry 登記的 query-exporter 查詢設定產生；"
        "名稱以 query-exporter 自己的程式推導（#3833）。另查宣告的 metric 都有 query "
        "列出、gauge 有 expiration、沒有 increment: true、沒有對 gauge 做 rate()。",
        "A rule pack's raw metrics must be served by the query-exporter config its "
        "registry entry names; names are derived with query-exporter's own code "
        "(#3833). Also: every declared metric is listed by a query, every gauge has an "
        "expiration, no increment: true, no rate() of a gauge."))
    ap.add_argument("--ci", action="store_true",
                    help=i18n_text("CI 模式（有發現時 exit 1）", "CI mode (exit 1 on a finding)"))
    ap.add_argument("--json", action="store_true",
                    help=i18n_text("以 JSON 輸出發現", "print findings as JSON"))
    ap.add_argument("--pack", help=i18n_text("搭配 --compare-served：要比對的 pack",
                                             "with --compare-served: the pack to compare"))
    ap.add_argument("--compare-served", type=Path, metavar="FILE",
                    help=i18n_text("執行層：比對這份 /metrics 抓取結果與推導出的名稱",
                                   "runtime half: compare this /metrics scrape with the derived names"))
    ap.add_argument("files", nargs="*",
                    help=i18n_text("忽略（pre-commit 傳入的變更檔）",
                                   "ignored (pre-commit passes changed files)"))
    args = ap.parse_args()
    if bool(args.pack) != bool(args.compare_served):
        print("check_exporter_queries: --pack and --compare-served go together", file=sys.stderr)
        return 2
    try:
        found: dict[str, list[str]] = {}
        if args.compare_served:
            found[args.pack] = compare_served(args.pack, args.compare_served)
        else:
            for pack, rule_pack, queries in bindings():
                found[pack] = findings_for(pack, rule_pack, queries)
    except CannotRun as e:
        print(f"check_exporter_queries: {e}", file=sys.stderr)
        return 2
    except _lib_consumer_read.ConsumerReadError as e:
        print(f"check_exporter_queries: {_lib_consumer_read.error_text(e)}", file=sys.stderr)
        return 2
    total = sum(len(v) for v in found.values())
    if args.json:
        # #3216: run() encodes the --json document.
        return Report({"findings": found}, 1 if total else 0)
    if total == 0:
        print(f"check_exporter_queries: OK — {len(found)} pack(s) read only what their "
              f"query-exporter config serves ({', '.join(found) or 'none bound'}).")
    else:
        for pack, items in found.items():
            for item in items:
                print(f"check_exporter_queries: [{pack}] {item}")
    return 1 if total else 0


if __name__ == "__main__":
    import _lib_exitcodes
    _lib_exitcodes.run(main)
