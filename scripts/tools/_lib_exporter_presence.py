#!/usr/bin/env python3
"""db_type → exporter presence metric: the one table, and the rules made from it (#3771).

A tenant declares the database kinds it runs in ``_metadata.db_type``; the
threshold-exporter turns each into a ``tenant_expected_exporter{tenant,
db_type}`` series. ``TenantExporterAbsent`` (rule-packs/rule-pack-liveness.yaml)
asks, per (tenant, db_type), whether an exporter OF THAT KIND is reporting:

    tenant_expected_exporter unless on(tenant, db_type) tenant:exporter_present

``up`` cannot answer that — no label on it says which kind of exporter a
target is — so the answer is read from a metric only that kind of exporter
emits, on every scrape it answers, whether or not the database behind it is
reachable (``mysql_up`` is 0 when MariaDB is down and still present; a target
that does not answer leaves no series at all). The table below names that
metric for every kind; :func:`presence_rules` turns it into the
``tenant:exporter_present{tenant,db_type}`` recording rules (``group by(tenant)``
over the metric, db_type as the rule's static label), which live in the
liveness pack (always on) rather than in each DB pack (most are
``default_on: false`` — a tenant declaring a kind whose pack is not deployed
would otherwise page forever).

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
the liveness pack's generated block equal to :func:`render_block`.

Regenerate the block after editing the table::

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

PRESENCE_RECORD = "tenant:exporter_present"
#: Always 1 wherever the presence rules are loaded: the absence rules (here and
#: the platform pack's MassExporterOutage) are AND-ed with it, so a Prometheus
#: that has them without the presence rules stays silent instead of reading
#: every declared (tenant, db_type) as absent, and the platform pack's
#: ExporterPresenceRulesNotLoaded names that state.
SENTINEL_RECORD = "tenant:exporter_presence_loaded"
#: The group the generated rules sit in — the liveness alerts' own, so the
#: ConfigMap generator (one key per recording-only / alerting group set) puts
#: them under the same key as the alerts: one key cannot be projected without
#: the other.
LIVENESS_GROUP = "tenant-liveness"
JOB_SELECTOR = 'job="tenant-exporters"'

BEGIN_MARK = "# >>> GENERATED:exporter-presence"
END_MARK = "# <<< GENERATED:exporter-presence"
_INDENT = "      "

LIVENESS_PACK = Path(RULE_PACKS.directory) / RULE_PACKS.file_name("liveness")


def presence_expr(db_type: str) -> str:
    """The recording expression for one db_type (the db_type label is the
    rule's static label, not part of the expression)."""
    metrics = EXPORTER_PRESENCE[db_type]
    inner = " or ".join(f"{m}{{{JOB_SELECTOR}}}" for m in metrics)
    return f"group by(tenant) ({inner})"


def presence_rules() -> list[dict]:
    """One ``{record, expr, labels: {db_type}}`` per db_type, in table order.

    db_type is a static rule label rather than a ``label_replace``: the rules
    share one record name, and promtool's lint refuses same-named recording
    rules that no static label tells apart.
    """
    return [{"record": PRESENCE_RECORD, "expr": presence_expr(d), "labels": {"db_type": d}}
            for d in EXPORTER_PRESENCE]


def render_block() -> str:
    """The generated rules, delimiters included, as they sit at the head of
    the ``tenant-liveness`` group's ``rules:`` (before the alerts that read
    them: a group is evaluated in order)."""
    i = _INDENT
    lines = [
        f"{i}{BEGIN_MARK} — generated block, DO NOT EDIT"
        "（改 scripts/tools/_lib_exporter_presence.py 的 EXPORTER_PRESENCE 後跑它的 --write）",
    ]
    for d in EXPORTER_PRESENCE:
        lines.append(f"{i}# db_type={d}: {' / '.join(EXPORTER_PRESENCE[d])}")
        lines.append(f"{i}- record: {PRESENCE_RECORD}")
        lines.append(f"{i}  expr: {presence_expr(d)}")
        lines.append(f"{i}  labels:")
        lines.append(f"{i}    db_type: {d}")
    lines.append(f"{i}# 哨兵：存在判定已載入（absence 規則 and on() 它；缺席由平台 pack 的")
    lines.append(f"{i}# ExporterPresenceRulesNotLoaded 指名）")
    lines.append(f"{i}- record: {SENTINEL_RECORD}")
    lines.append(f"{i}  expr: vector(1)")
    lines.append(f"{i}{END_MARK}")
    return "\n".join(lines) + "\n"


def _split(text: str) -> tuple[str, str, str]:
    """(before, block, after) around the generated block; ValueError if absent."""
    start = text.find(f"{_INDENT}{BEGIN_MARK}")
    end = text.find(f"{_INDENT}{END_MARK}")
    if start < 0 or end < 0 or end < start or (start and text[start - 1] != "\n"):
        raise ValueError(f"{LIVENESS_PACK}: generated block markers not found")
    stop = text.index("\n", end) + 1
    return text[:start], text[start:stop], text[stop:]


def committed_block(text: str) -> str:
    """The generated block as committed in the liveness pack's text."""
    return _split(text)[1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--write", action="store_true",
                        help="rewrite the generated block in the liveness pack")
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[2]),
                        help="repository root (default: this tree's)")
    args = parser.parse_args(argv)
    path = Path(args.root) / LIVENESS_PACK
    try:
        text = path.read_text(encoding="utf-8")
        before, block, after = _split(text)
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    want = render_block()
    if block == want:
        print(f"{path}: exporter-presence block up to date")
        return 0
    if not args.write:
        print(f"{path}: exporter-presence block is stale; run with --write", file=sys.stderr)
        return 1
    path.write_text(before + want + after, encoding="utf-8", newline="\n")
    print(f"{path}: exporter-presence block rewritten")
    return 0


if __name__ == "__main__":
    _lib_exitcodes.run(main)
