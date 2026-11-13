#!/usr/bin/env python3
"""db_type → exporter presence metric: the one table, and the rules made from it (#3771).

A tenant declares the database kinds it runs in ``_metadata.db_type``; the
threshold-exporter turns each into a ``tenant_expected_exporter{tenant,
db_type}`` series. ``TenantExporterAbsent`` (rule-packs/rule-pack-liveness.yaml)
asks, per (tenant, db_type), whether an exporter OF THAT KIND is reporting:

    tenant_expected_exporter unless on(tenant, db_type) (<presence>)

``up`` cannot answer that — no label on it says which kind of exporter a
target is — so the answer is read from a metric only that kind of exporter
emits, on every scrape it answers, whether or not the database behind it is
reachable (``mysql_up`` is 0 when MariaDB is down and still present; a target
that does not answer leaves no series at all). The table below names that
metric for every kind; :func:`presence_expr` turns it into ``<presence>``:
one ``label_replace(<metric>{job="tenant-exporters"}, "db_type", "<kind>", "",
"")`` per kind, joined by ``or``. No aggregation: ``unless on(tenant,
db_type)`` matches many right-hand series as well as one, and an aggregation
``by (tenant)`` in MassExporterOutage would read to the platform-alert guard
(_grar_validate) as an alert that carries a tenant label, which it does not.

The expression is written INTO each alert that needs it — TenantExporterAbsent
and TenantExporterUndeclared (liveness pack) and MassExporterOutage (platform
pack) — rather than recorded once and read back (#3771 review). A recording
rule one alert reads is a second rule that has to be loaded with it, in the
same Prometheus, before it: a hand-split copy, a pack loaded alone, or the
federation split (records on the edge, alerts on the central Prometheus,
which pulls only ``{tenant!=""}`` series) each left the alert reading an
empty vector — silent, or paging for every declared pair. Inline, each alert
reads only the raw tenant-labelled series it always read. The presence
metrics are not in the DB packs either: most are ``default_on: false``.

Why each metric (read from the exporter's own source):

* mariadb       ``mysql_up`` — prom/mysqld-exporter, emitted every scrape
                (0 when the server is unreachable); the MariaDB pack reads it.
* postgresql    ``pg_up`` — prometheus-community/postgres_exporter, same shape.
* redis         ``redis_up`` — oliver006/redis_exporter, same shape.
* mongodb       ``mongodb_up`` — percona/mongodb_exporter, same shape.
* oracle        ``oracledb_up`` — iamseth/oracledb_exporter, same shape.
* db2           ``db2_up`` — the DB2 exporter's up gauge (the DB2 pack reads it).
* kafka         ``kafka_brokers`` — danielqsj/kafka_exporter: Collect emits it
                first, unconditionally (kafka_exporter.go, clusterBrokers); the
                exporter has no ``kafka_up``.
* clickhouse    ``ClickHouseMetrics_TCPConnection`` — there is no separate
                exporter: the server's built-in endpoint
                (src/Server/PrometheusMetricsWriter.cpp) writes every
                CurrentMetrics value with the ``ClickHouseMetrics_`` prefix on
                each scrape, 0 included; the ClickHouse pack reads this one.
                The endpoint is the server, so "absent" means the server.
* elasticsearch ``elasticsearch_exporter_build_info`` — the exporter registers
                ``versioncollector.NewCollector("elasticsearch_exporter")``
                (main.go), always emitted; the health / node collectors emit
                nothing when the cluster call fails, so none of them is a
                presence signal.
* rabbitmq      two exporters, two metrics (the registry declares both):
                ``rabbitmq_up`` — kbudde/rabbitmq_exporter
                (exporter.go upMetric, namespace "rabbitmq"); and
                ``rabbitmq_identity_info`` — the built-in rabbitmq_prometheus
                plugin (prometheus_rabbitmq_core_metrics_collector.erl,
                emit_identity_info on every endpoint). Either one counts.

Every series is read under ``job="tenant-exporters"`` — the scrape contract
liveness is written for — so a same-named series from elsewhere does not count.

The key set must equal the ``_metadata.db_type`` enum
(docs/schemas/tenant-config.schema.json ``definitions.dbTypeValue``) in both
directions; ``tests/rulepacks/test_exporter_presence.py`` holds that, and holds
the three alerts' ``expr`` equal to :func:`alert_exprs` (``--write`` and the
check mode compare the parsed ``expr``, not a text block).

Regenerate the three expressions after editing the table or a template::

    python3 scripts/tools/_lib_exporter_presence.py --write

Standard library only (the block is rendered as text, not through a YAML
emitter, so the committed file is byte-stable).
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
import _lib_exitcodes  # noqa: E402  (the one exit path: tests/shared/test_single_exit_path.py)
from _lib_consumer_read import RULE_PACKS  # noqa: E402  (#3471: the rule-pack file names)

#: db_type → the metric(s) whose presence under job="tenant-exporters" means an
#: exporter of that kind is answering. Several metrics: any one counts.
EXPORTER_PRESENCE: dict[str, tuple[str, ...]] = {
    "mariadb": ("mysql_up",),
    "postgresql": ("pg_up",),
    "redis": ("redis_up",),
    "mongodb": ("mongodb_up",),
    "elasticsearch": ("elasticsearch_exporter_build_info",),
    "oracle": ("oracledb_up",),
    "db2": ("db2_up",),
    "clickhouse": ("ClickHouseMetrics_TCPConnection",),
    "kafka": ("kafka_brokers",),
    "rabbitmq": ("rabbitmq_up", "rabbitmq_identity_info"),
}

JOB_SELECTOR = 'job="tenant-exporters"'
#: How far back TenantExporterUndeclared looks for a database exporter: an
#: undeclared tenant whose exporters all went down stays named for this long
#: (a gone exporter is not a fixed declaration), then the alert resolves.
#: 1h is the longest range the rule-pack CI policy allows
#: (.github/custom-rule-policy.yaml max_range_duration; only a GENERATED pack
#: may be granted more).
UNDECLARED_LOOKBACK = "1h"

LIVENESS_PACK = Path(RULE_PACKS.directory) / RULE_PACKS.file_name("liveness")
PLATFORM_PACK = Path(RULE_PACKS.directory) / RULE_PACKS.file_name("platform")

_PAD = "  "


def _indent(text: str, depth: int) -> str:
    return "\n".join(_PAD * depth + line for line in text.split("\n"))


def presence_expr() -> str:
    """Every series of a table metric under the tenant-exporters job, with
    its kind as ``db_type`` — one ``label_replace`` term per table row,
    ``or``-ed (the terms differ in metric name and db_type, so ``or`` keeps
    them all)."""
    terms = []
    for db_type, metrics in EXPORTER_PRESENCE.items():
        inner = " or ".join(f"{m}{{{JOB_SELECTOR}}}" for m in metrics)
        terms.append(f'label_replace({inner}, "db_type", "{db_type}", "", "")')
    return "\nor ".join(terms)


def presence_over_time_expr() -> str:
    """:func:`presence_expr` remembered for UNDECLARED_LOOKBACK: every series a
    table metric had under the tenant-exporters job within that window, with
    its kind as ``db_type``."""
    terms = []
    for db_type, metrics in EXPORTER_PRESENCE.items():
        inner = " or ".join(f"max_over_time({m}{{{JOB_SELECTOR}}}[{UNDECLARED_LOOKBACK}])"
                            for m in metrics)
        terms.append(f'label_replace({inner}, "db_type", "{db_type}", "", "")')
    return "\nor ".join(terms)


def _absent_pairs() -> str:
    return ("max without(instance, pod) (tenant_expected_exporter)\n"
            "unless on(tenant, db_type) (\n" + _indent(presence_expr(), 1) + "\n)")


def alert_exprs() -> dict[tuple[Path, str], str]:
    """(pack, alertname) → the alert's whole ``expr`` (no trailing newline)."""
    absent = _absent_pairs()
    # The floor counts TENANTS (#3771 review): one tenant declaring seven kinds,
    # all down, is seven pairs but no shared failure. `count without` both
    # times — a `by (tenant)` would read to the platform-alert guard
    # (_grar_validate) as an alert carrying a tenant label.
    mass = "\n".join([
        "(",
        "  count without (tenant) (count without (db_type) (",
        _indent(absent, 2),
        "  )) > 5",
        "  and",
        "  (",
        "    count without (tenant, db_type) (",
        _indent(absent, 3),
        "    )",
        "    /",
        "    count without (tenant, db_type) (max without (instance, pod) (tenant_expected_exporter))",
        "  ) > 0.1",
        ")",
        'unless on() absent(up{job="tenant-exporters"})',
    ])
    # Per (tenant, db_type) (#3771 review): a tenant declaring [mariadb] while
    # it also runs a redis exporter is named for redis. The guard joins on
    # tenant: threshold-exporter down (no tenant_metadata_info at all) and a
    # de-provisioned tenant (its config gone, the lookback's memory of its
    # exporter not yet) both stay silent.
    undeclared = "\n".join([
        "group by(tenant, db_type) (",
        _indent(presence_over_time_expr(), 1),
        ")",
        "unless on(tenant, db_type) tenant_expected_exporter",
        "and on(tenant) tenant_metadata_info",
        "unless on(tenant)",
        '(user_state_filter{filter="maintenance"} == 1)',
    ])
    return {
        (LIVENESS_PACK, "TenantExporterAbsent"): absent,
        (LIVENESS_PACK, "TenantExporterUndeclared"): undeclared,
        (PLATFORM_PACK, "MassExporterOutage"): mass,
    }


def _expr_span(text: str, alert: str) -> tuple[int, int, str]:
    """(start, stop, indent) of the ``expr: |`` block scalar body of *alert*;
    ValueError when the alert or a block-scalar expr is not there."""
    lines = text.split("\n")
    offsets, pos = [], 0
    for line in lines:
        offsets.append(pos)
        pos += len(line) + 1
    heads = [i for i, line in enumerate(lines) if line.strip() == f"- alert: {alert}"]
    if len(heads) != 1:
        raise ValueError(f"alert {alert}: found {len(heads)} times, want 1")
    i = heads[0] + 1
    key_indent = len(lines[heads[0]]) - len(lines[heads[0]].lstrip()) + 2
    if lines[i].strip() != "expr: |" or len(lines[i]) - len(lines[i].lstrip()) != key_indent:
        raise ValueError(f"alert {alert}: the line after it is not `expr: |`")
    j = i + 1
    while j < len(lines) and (lines[j].strip() == "" or
                              len(lines[j]) - len(lines[j].lstrip()) > key_indent):
        j += 1
    return offsets[i + 1], offsets[j], " " * (key_indent + 2)


def rewrite(text: str, alert: str, expr: str) -> str:
    """*text* with *alert*'s expr body replaced by *expr*."""
    start, stop, pad = _expr_span(text, alert)
    body = "".join(pad + line + "\n" for line in expr.split("\n"))
    return text[:start] + body + text[stop:]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--write", action="store_true",
                        help="rewrite the three alerts' expr from the table")
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[2]),
                        help="repository root (default: this tree's)")
    args = parser.parse_args(argv)
    stale = []
    texts: dict[Path, str] = {}
    try:
        for (pack, alert), expr in alert_exprs().items():
            path = Path(args.root) / pack
            text = texts.get(path)
            if text is None:
                text = path.read_text(encoding="utf-8")
            new = rewrite(text, alert, expr)
            if new != text:
                stale.append(f"{pack}: {alert}")
            texts[path] = new
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if not stale:
        print("exporter-presence expressions up to date")
        return 0
    if not args.write:
        for item in stale:
            print(f"{item}: expr is stale; run with --write", file=sys.stderr)
        return 1
    for path, text in texts.items():
        path.write_text(text, encoding="utf-8", newline="\n")
    for item in stale:
        print(f"{item}: expr rewritten")
    return 0


if __name__ == "__main__":
    _lib_exitcodes.run(main)
