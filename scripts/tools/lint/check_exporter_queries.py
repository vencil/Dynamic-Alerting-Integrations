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


# A PromQL string: "..." or '...' with Go escapes, or `...` raw. Three disjoint
# alternatives and no backreference: a body branch that could also take a
# backslash overlaps the escape branch, and a run of backslashes then backtracks
# exponentially (CodeQL py/redos).
_STR = r"""(?:"((?:\\.|[^"\\])*)"|'((?:\\.|[^'\\])*)'|`([^`]*)`)"""
# A `query` label matcher, and a label_replace that writes `query`.
_QUERY_MATCHER = re.compile(r"\bquery\s*(=~|!~|!=|=)\s*" + _STR)
# label_replace(v, dst, replacement, src, regex): `query` must be the DESTINATION,
# so the four string arguments are matched up to the closing parenthesis — a
# `query` source label is not a write.
_QUERY_SET = re.compile(r"label_replace\s*\(.*?,\s*[\"'`]query[\"'`]\s*,\s*" + _STR
                        + r"\s*,\s*" + _STR + r"\s*,\s*" + _STR + r"\s*\)", re.S)
# A selector of a query-exporter built-in that carries one series per query. The
# label block skips quoted strings, so a `}` inside a regex does not end it.
_BUILTIN_SELECTOR = re.compile(
    r"\b(query_timestamp|query_interval)\s*\{((?:[^{}\"'`]|" + _STR + r")*)\}")


def _seconds(value: "int | float | str") -> float:
    """A query-exporter duration: a number of seconds, or a number with s/m/h/d."""
    if isinstance(value, (int, float)):
        return float(value)
    m = re.fullmatch(r"\s*([0-9.]+)\s*([smhd]?)\s*", str(value))
    if not m:
        return float("nan")
    return float(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]


_GO_SIMPLE = {"a": "\a", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t",
              "v": "\v", "\\": "\\", "'": "'", '"': '"'}


def _unescape(value: str) -> str:
    """A PromQL double/single-quoted string's value, decoded by Go's rules: \\x
    and octal escapes are BYTES, \\u / \\U are code points, any other character is
    itself (UTF-8); the bytes are decoded as UTF-8 at the end. Python's
    unicode_escape is not this — it reads non-ASCII as Latin-1 (café -> cafÃ©)."""
    out = bytearray()
    i, n = 0, len(value)
    while i < n:
        c = value[i]
        if c != "\\" or i + 1 == n:
            out += c.encode("utf-8")
            i += 1
            continue
        e = value[i + 1]
        if e in _GO_SIMPLE:
            out += _GO_SIMPLE[e].encode("utf-8")
            i += 2
        elif e == "x" and re.fullmatch(r"[0-9a-fA-F]{2}", value[i + 2:i + 4]):
            out.append(int(value[i + 2:i + 4], 16))
            i += 4
        elif e in "uU":
            width = 4 if e == "u" else 8
            digits = value[i + 2:i + 2 + width]
            if not re.fullmatch(r"[0-9a-fA-F]{%d}" % width, digits):
                out += e.encode("utf-8")
                i += 2
                continue
            out += chr(int(digits, 16)).encode("utf-8")
            i += 2 + width
        elif re.fullmatch(r"[0-7]{3}", value[i + 1:i + 4]):
            out.append(int(value[i + 1:i + 4], 8) & 0xFF)
            i += 4
        else:  # not a Go escape (Prometheus would reject it); keep the character
            out += e.encode("utf-8")
            i += 2
    return out.decode("utf-8", errors="replace")


def _str_value(groups: tuple[str, str, str]) -> str:
    dq, sq, raw = groups
    return _unescape(dq or sq) if (dq or sq) else raw


def _query_matchers(text: str) -> list[tuple[str, str]]:
    """(operator, value) of every `query` matcher in *text*; a label_replace that
    writes `query` counts as `=` its replacement."""
    out = [(m.group(1), _str_value(m.groups()[1:4])) for m in _QUERY_MATCHER.finditer(text)]
    out += [("=", _str_value(m.groups()[0:3])) for m in _QUERY_SET.finditer(text)]
    return out


def selected_queries(block: str, queries: set[str]) -> set[str]:
    """The queries of the file that every `query` matcher in one selector's label
    block selects (all of =, !=, =~, !~ apply, as in PromQL). Only this runtime
    expansion reads the negative matchers; query_label_findings checks = and =~."""
    keep = set(queries)
    for op, value in _query_matchers(block):
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
    for text in texts:
        for op, value in _query_matchers(text):
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


_RATE_CALL = re.compile(r"\b(" + "|".join(_RATE_FUNCS) + r")\s*\(")


def rate_arguments(expr: str) -> list[tuple[str, str]]:
    """(function, whole argument text) of every rate-like call in *expr*, the
    argument found by matching parentheses: rate(x[5m]), rate((x)[5m:]) and
    rate((sum(x))[5m:]) all yield the expression that is rated. Strings are
    skipped so a parenthesis inside a label value does not count."""
    out = []
    for m in _RATE_CALL.finditer(expr):
        depth, i, quote = 1, m.end(), None
        while i < len(expr) and depth:
            c = expr[i]
            if quote:
                if c == "\\" and quote != "`":
                    i += 1
                elif c == quote:
                    quote = None
            elif c in "\"'`":
                quote = c
            elif c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
            i += 1
        out.append((m.group(1), expr[m.end():i - 1]))
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
    # One connection per database by default (connection-pool size 1, one worker):
    # a query without a timeout that hangs holds it and every other query on
    # that database waits. The timeout must also end before the next run is due.
    for name, spec in sorted((doc.get("queries") or {}).items()):
        spec = spec or {}
        timeout, interval = spec.get("timeout"), spec.get("interval")
        if not timeout:
            out.append(f"{rel}: query {name} has no timeout — a hung statement holds "
                       "the database's only connection and every other query waits")
        elif interval and _seconds(timeout) >= _seconds(interval):
            out.append(f"{rel}: query {name} timeout {_seconds(timeout):g}s is not below "
                       f"its interval {_seconds(interval):g}s")
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
    raw, _records, exprs = pack_rules(rule_pack)
    for m in sorted(raw - served):
        out.append(f"{rule_pack.name} reads {m}, which {rel} does not serve")
    names = set(doc.get("queries") or {})
    out += query_label_findings(exprs, names, rule_pack.name)
    out += query_label_findings(_presence_selectors(pack), names,
                                "the presence check (_lib_exporter_presence)")
    gauges = {n for n, s in metrics.items() if (s or {}).get("type") == "gauge"}
    for expr in exprs:
        for func, arg in rate_arguments(expr):
            for metric in sorted(_lib_metric_sources.extract_metrics(arg) & gauges):
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
    # Every (built-in, query) pair a selector selects, regex selectors expanded
    # against the file's queries, must have its own series.
    names = set(doc.get("queries") or {})
    wanted: set[tuple[str, str]] = set()
    for text in exprs + _presence_selectors(pack):
        for m in _BUILTIN_SELECTOR.finditer(text):
            metric, block = m.group(1), m.group(2)
            wanted |= {(metric, q) for q in selected_queries(block, names)}
    have = {(n, lb.get("query")) for n, lb, _v in samples}
    for metric, q in sorted(wanted - have):
        out.append(f"{metric}{{query=\"{q}\"}} is read by the pack but not served")
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
