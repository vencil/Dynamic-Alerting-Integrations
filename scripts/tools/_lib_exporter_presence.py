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
* oracle        ``query_interval{query="oracle_liveness"}`` — query-exporter
                running rule-packs/exporters/oracle.yaml (#3833). query-exporter
                keeps serving while the database is down and reports no 0 (its
                gauges expire), so presence reads the built-in query_interval of
                the liveness query: set at start-up, served while the exporter
                runs, whatever the database's state.
* db2           ``query_interval{query="db2_liveness"}`` — query-exporter
                running rule-packs/exporters/db2.yaml (#3833), the same shape
                as oracle.
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
from _lib_python import detect_cli_lang  # noqa: E402  (dev-rules §9: CLI help 語言)

#: db_type → the metric(s) whose presence under job="tenant-exporters" means an
#: exporter of that kind is answering. Several metrics: any one counts.
EXPORTER_PRESENCE: dict[str, tuple[str, ...]] = {
    "mariadb": ("mysql_up",),
    "postgresql": ("pg_up",),
    "redis": ("redis_up",),
    "mongodb": ("mongodb_up",),
    "elasticsearch": ("elasticsearch_exporter_build_info",),
    "oracle": ('query_interval{query="oracle_liveness"}',),
    "db2": ('query_interval{query="db2_liveness"}',),
    "clickhouse": ("ClickHouseMetrics_TCPConnection",),
    "kafka": ("kafka_brokers",),
    "rabbitmq": ("rabbitmq_up", "rabbitmq_identity_info"),
}

#: pack → the presence metric(s) of an exporter whose kind is no db_type: the
#: application packs whose threshold keys the platform does not ship a value
#: for (#1200 D-A A3). Kept apart from EXPORTER_PRESENCE, whose keys must be
#: exactly the db_type enum (liveness opt-in). jvm: the heap used series of
#: client_java / jmx_exporter / Micrometer (jvm_memory_used_bytes) and of
#: jmx_exporter before 1.0 (jvm_memory_bytes_used), the two names
#: rule-pack-jvm.yaml reads; nginx: nginxinc/nginx-prometheus-exporter's
#: nginx_up, written on every scrape.
APP_PRESENCE: dict[str, tuple[str, ...]] = {
    "jvm": ("jvm_memory_used_bytes", "jvm_memory_bytes_used"),
    "nginx": ("nginx_up",),
}

#: pack → the prefix every alert of that pack carries. Alertnames are unique
#: across rule packs (check_vmalert_coverage), so the unconfigured-threshold
#: alert is <prefix>ThresholdNotConfigured (#1200 D-A A3);
#: tests/rulepacks/test_exporter_presence.py holds each prefix to the pack's
#: own alerts.
ALERT_PREFIX: dict[str, str] = {
    "clickhouse": "ClickHouse", "db2": "DB2", "elasticsearch": "Elasticsearch",
    "jvm": "JVM", "kafka": "Kafka", "mariadb": "MariaDB", "mongodb": "MongoDB",
    "nginx": "Nginx", "oracle": "Oracle", "rabbitmq": "RabbitMQ", "redis": "Redis",
}
UNSET_NOTICE_SUFFIX = "ThresholdNotConfigured"


def unset_notice_alert(pack: str) -> str:
    """The unconfigured-threshold alert's name in *pack*."""
    if pack not in ALERT_PREFIX:
        raise ValueError(f"pack {pack!r} reads unset-notice keys but has no ALERT_PREFIX")
    return ALERT_PREFIX[pack] + UNSET_NOTICE_SUFFIX


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


def selector(metric: str) -> str:
    """*metric* under the tenant-exporters job. A table entry is a bare name or
    a selector with its own matchers (query-exporter's built-ins are told apart
    by their `query` label); the job matcher joins those."""
    if metric.endswith("}"):
        return f"{metric[:-1]}, {JOB_SELECTOR}}}"
    return f"{metric}{{{JOB_SELECTOR}}}"


def _indent(text: str, depth: int) -> str:
    return "\n".join(_PAD * depth + line for line in text.split("\n"))


def presence_expr() -> str:
    """Every series of a table metric under the tenant-exporters job, with
    its kind as ``db_type`` — one ``label_replace`` term per table row,
    ``or``-ed (the terms differ in metric name and db_type, so ``or`` keeps
    them all)."""
    terms = []
    for db_type, metrics in EXPORTER_PRESENCE.items():
        inner = " or ".join(selector(m) for m in metrics)
        terms.append(f'label_replace({inner}, "db_type", "{db_type}", "", "")')
    return "\nor ".join(terms)


def presence_over_time_expr() -> str:
    """:func:`presence_expr` remembered for UNDECLARED_LOOKBACK: every series a
    table metric had under the tenant-exporters job within that window, with
    its kind as ``db_type``."""
    terms = []
    for db_type, metrics in EXPORTER_PRESENCE.items():
        inner = " or ".join(f"max_over_time({selector(m)}[{UNDECLARED_LOOKBACK}])"
                            for m in metrics)
        terms.append(f'label_replace({inner}, "db_type", "{db_type}", "", "")')
    return "\nor ".join(terms)


def pack_presence(pack: str) -> tuple[str, ...]:
    """The presence metric(s) of *pack*'s exporter: EXPORTER_PRESENCE for a
    database pack (its name is the db_type), APP_PRESENCE otherwise."""
    return EXPORTER_PRESENCE.get(pack) or APP_PRESENCE.get(pack) or ()


def unset_notice_packs() -> list[str]:
    """The packs that read a threshold key the platform ships no value for —
    each carries <prefix>ThresholdNotConfigured (#1200 D-A A3). Derived from the
    threshold registry, never listed here."""
    ops_dir = os.path.join(_HERE, "ops")
    if ops_dir not in sys.path:
        sys.path.insert(0, ops_dir)
    import _registry_lib  # noqa: PLC0415  (scripts/tools/ops)
    return sorted(set(_registry_lib.unset_notice_keys(_registry_lib.load_registry()).values()))


def unset_notice_expr(pack: str) -> str:
    """<prefix>ThresholdNotConfigured's expr for *pack*: every user_threshold_unset
    series of the pack for a tenant whose exporter of that kind is answering
    under the tenant-exporters job, outside maintenance. The exporter's own
    `instance` / `pod` are aggregated away first (as tenant_expected_exporter
    is): with two replicas each key would otherwise fire twice, and a rollout
    would restart the alert's `for:` with the new pod's series."""
    metrics = pack_presence(pack)
    if not metrics:
        raise ValueError(f"pack {pack!r} reads unset-notice keys but has no presence metric "
                         "(EXPORTER_PRESENCE / APP_PRESENCE)")
    inner = " or ".join(selector(m) for m in metrics)
    return "\n".join([
        "(",
        f'  max without(instance, pod) (user_threshold_unset{{pack="{pack}"}})',
        "  and on(tenant)",
        f"  ({inner})",
        ")",
        "unless on(tenant)",
        '(user_state_filter{filter="maintenance"} == 1)',
    ])


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
    # it also runs a redis exporter is named for redis. And per cluster
    # (ADR-006, #4097): the declaration is the tenant's, the exporter is in a
    # cluster, and the alert says which. The guard joins on
    # tenant: threshold-exporter down (no tenant_metadata_info at all) and a
    # de-provisioned tenant (its config gone, the lookback's memory of its
    # exporter not yet) both stay silent.
    undeclared = "\n".join([
        "group by(tenant, da_cluster, db_type) (",
        _indent(presence_over_time_expr(), 1),
        ")",
        "unless on(tenant, db_type) tenant_expected_exporter",
        "and on(tenant) tenant_metadata_info",
        "unless on(tenant)",
        '(user_state_filter{filter="maintenance"} == 1)',
    ])
    notices = {(Path(RULE_PACKS.directory) / RULE_PACKS.file_name(p), unset_notice_alert(p)):
               unset_notice_expr(p) for p in unset_notice_packs()}
    return {
        **notices,
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
    zh = detect_cli_lang() == "zh"
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--write", action="store_true",
                        help=("依對照表改寫三條告警的 expr" if zh
                              else "rewrite the three alerts' expr from the table"))
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[2]),
                        help=("repo 根目錄（預設：本工作樹）" if zh
                              else "repository root (default: this tree's)"))
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
