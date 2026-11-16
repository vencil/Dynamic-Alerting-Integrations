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
  * every gauge in the file has ``expiration`` (query-exporter keeps a gauge's last
    value while the database is unreachable; expiration drops it);
  * no metric sets ``increment: true`` (a counter must carry the database's own
    cumulative value, which is what rate() expects);
  * the pack does not rate() / irate() / increase() / delta() a served gauge.

  * every `query="<name>"` the pack — or the platform's presence check for this
    pack (_lib_exporter_presence) — matches on names a query in the file, and every
    `query=~"<re>"` matches at least one (a renamed query would leave those
    selectors empty: OracleDatabaseDown silent, TenantExporterAbsent paging).

Whether the SQL runs, and serves what is derived, is not a static question: a step
of CI's Lint Rule Packs job (scripts/ops/exporter_queries_oracle_runtime.sh) runs the
Oracle file against gvenzl/oracle-free and calls --compare-served.

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
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as e:
        raise CannotRun(f"cannot read {path}: {e}") from e
    if not isinstance(doc, dict):
        raise CannotRun(f"{path}: the top-level YAML document is not a mapping")
    return doc


def served_names(path: Path, doc: dict) -> set[str]:
    """The metric names query-exporter serves for *path*, from its own code."""
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
            raise CannotRun(f"{path}: query-exporter rejects it: {e}") from e
    registry = MetricsRegistry()
    registry.create_metrics(config.metrics.values())
    for name, metric in config.metrics.items():
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


def pack_rules(rule_pack: Path) -> tuple[set[str], set[str], list[str]]:
    """(raw tenant metrics read, recording rule names, every expr) of *rule_pack*."""
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
    raw: set[str] = set()
    for expr in exprs:
        raw |= _lib_metric_sources.extract_metrics(expr)
    raw = {m for m in raw
           if ":" not in m and m not in records
           and _lib_metric_sources.resolve_family(m) is None}
    return raw, records, exprs


# PromQL strings take " or ' (backticks are raw); label_replace can write `query`.
_QUERY_EQ = re.compile(r"""\bquery\s*=\s*(["'])((?:\\.|(?!\1).)*)\1""")
_QUERY_RE = re.compile(r"""\bquery\s*=~\s*(["'])((?:\\.|(?!\1).)*)\1""")
_QUERY_SET = re.compile(r"""label_replace\s*\(.*?,\s*(["'])query\1\s*,\s*(["'])((?:\\.|(?!\2).)*)\2""",
                        re.S)


def _unescape(value: str) -> str:
    """A PromQL double/single-quoted string's value (Go escapes)."""
    return re.sub(r"\\(.)", r"\1", value)


def query_label_findings(texts: list[str], queries: set[str], where: str) -> list[str]:
    """`query` label matchers (and label_replace targets) in *texts* that name no
    query of the file. A regex that is a plain alternation of names is checked name
    by name, so one existing alternative cannot cover a stale one."""
    out = []
    for text in texts:
        names = [_unescape(m[1]) for m in _QUERY_EQ.findall(text)]
        names += [_unescape(m[2]) for m in _QUERY_SET.findall(text)]
        for name in names:
            if name not in queries:
                out.append(f"{where} matches query=\"{name}\", which the file does not define")
        for _q, raw in _QUERY_RE.findall(text):
            pattern = _unescape(raw)
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
    served = served_names(queries, doc)
    raw, _records, exprs = pack_rules(rule_pack)
    for m in sorted(raw - served):
        out.append(f"{rule_pack.name} reads {m}, which {rel} does not serve")
    names = set(doc.get("queries") or {})
    out += query_label_findings(exprs, names, rule_pack.name)
    out += query_label_findings(_presence_selectors(pack), names,
                                "the presence check (_lib_exporter_presence)")
    gauges = {n for n, s in metrics.items() if (s or {}).get("type") == "gauge"}
    pattern = re.compile(r"\b(" + "|".join(_RATE_FUNCS) + r")\s*\(\s*([a-zA-Z_:][a-zA-Z0-9_:]*)")
    for expr in exprs:
        for func, metric in pattern.findall(expr):
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
    declared = set(doc.get("metrics") or {})
    expected = {n for n in served_names(queries, doc)
                if any(n == m or n.startswith(m + "_") for m in declared)}
    for name in sorted(expected - served):
        out.append(f"{name} is derived from {queries.relative_to(_REPO)} but not served")
    for name, spec in sorted((doc.get("metrics") or {}).items()):
        want = (spec or {}).get("type")
        got = types.get(name)
        if want and got and got != want:
            out.append(f"{name} is served as {got}, the file declares {want}")
    # What the pack itself reads, query-exporter's built-ins included: for those,
    # a series per `query` label value the pack (or its presence check) names.
    raw, _records, exprs = pack_rules(_rule_pack)
    for m in sorted(raw - served):
        out.append(f"{_rule_pack.name} reads {m}, which the exporter does not serve")
    named = {_unescape(m[1]) for text in exprs + _presence_selectors(pack)
             for m in _QUERY_EQ.findall(text)}
    for m in sorted(raw & {"query_timestamp", "query_interval"}):
        have = {lb.get("query") for n, lb, _v in samples if n == m}
        for q in sorted(named - have):
            out.append(f"{m}{{query=\"{q}\"}} is read by the pack but not served")
    return out


def main() -> "int | Report":
    ap = argparse.ArgumentParser(description=i18n_text(
        "rule pack 讀的原始指標必須由它在 registry 登記的 query-exporter 查詢設定產生；"
        "名稱以 query-exporter 自己的程式推導（#3833）。另查 gauge 有 expiration、"
        "沒有 increment: true、沒有對 gauge 做 rate()。",
        "A rule pack's raw metrics must be served by the query-exporter config its "
        "registry entry names; names are derived with query-exporter's own code "
        "(#3833). Also: every gauge has an expiration, no increment: true, no rate() "
        "of a gauge."))
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
