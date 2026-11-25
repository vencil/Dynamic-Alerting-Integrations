#!/usr/bin/env python3
"""Exporter kind → presence metric, read from the threshold registry, and the rules made from it (#3771, #4097).

A tenant declares the exporters it runs in ``_expected_exporters`` (a list of
kinds); the threshold-exporter turns each into a ``tenant_expected_exporter{
tenant, pack}`` series. ``TenantExporterAbsent`` (rule-packs/rule-pack-liveness.yaml)
asks, per (tenant, pack), whether an exporter OF THAT KIND is reporting:

    tenant_expected_exporter unless on(tenant, pack) (<presence>)

An entry may also be ``kind@cluster`` (ADR-006 contract 6): the series then
carries ``expected_cluster`` and the question is asked per cluster — see
"Per cluster" below.

``up`` cannot answer that — no label on it says which kind of exporter a
target is — so the answer is read from a metric only that kind of exporter
emits, on every scrape it answers, whether or not what it watches is
reachable (``mysql_up`` is 0 when MariaDB is down and still present; a target
that does not answer leaves no series at all). A KIND is a rule pack of
rule-packs/threshold-registry.yaml that names such a metric in ``presence:``
— the one table (:func:`presence_table`); :func:`presence_expr` turns it into
``<presence>``: one ``label_replace(<metric>{job="tenant-exporters"}, "pack",
"<kind>", "", "")`` per kind, joined by ``or``. No aggregation: ``unless
on(tenant, pack)`` matches many right-hand series as well as one, and an
aggregation ``by (tenant)`` in MassExporterOutage would read to the
platform-alert guard (_grar_validate) as an alert that carries a tenant
label, which it does not.

The expression is written INTO each alert that needs it — TenantExporterAbsent
and TenantExporterUndeclared (liveness pack) and MassExporterOutage (platform
pack) — rather than recorded once and read back (#3771 review). A recording
rule one alert reads is a second rule that has to be loaded with it, in the
same Prometheus, before it: a hand-split copy, a pack loaded alone, or the
federation split (records on the edge, alerts on the central Prometheus,
which pulls only ``{tenant!=""}`` series) each left the alert reading an
empty vector — silent, or paging for every declared pair. Inline, each alert
reads only the raw tenant-labelled series it always read. The presence
metrics are not in the packs either: most are ``default_on: false``.

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
* jvm           ``jvm_memory_used_bytes`` — the heap used series of
                client_java / jmx_exporter / Micrometer — and
                ``jvm_memory_bytes_used`` — jmx_exporter before 1.0; the two
                names rule-pack-jvm.yaml reads. Many applications expose
                ``jvm_*`` without anyone meaning to run "a JVM exporter", so
                the registry marks the pack ``undeclared_notice: false``:
                TenantExporterUndeclared does not name it, a tenant may still
                declare it.
* nginx         ``nginx_up`` — nginxinc/nginx-prometheus-exporter, written on
                every scrape.

Every series is read under ``job="tenant-exporters"`` — the scrape contract
liveness is written for — so a same-named series from elsewhere does not count.

The kinds are the registry's and nobody else's: the exporter's allowed-kind
table (generated), the ``_expected_exporters`` enum of
docs/schemas/tenant-config.schema.json and ``_lib_constants.JOB_DB_MAP`` are
held to them by ``tests/rulepacks/test_exporter_presence.py``, which also
holds the three alerts' ``expr`` equal to :func:`alert_exprs` (``--write`` and
the check mode compare the parsed ``expr``, not a text block).

Per cluster (``kind@cluster``, #4097)
-------------------------------------
``tenant_expected_exporter{tenant, pack, expected_cluster}`` says the kind must
report FROM that cluster; a row without ``expected_cluster`` keeps the meaning
above (one cluster reporting is enough). The cluster a presence series is from
is its ``da_cluster`` — the external label of the Prometheus it came through,
so it is on the series where two clusters' data meet (a central Prometheus)
and on nothing a Prometheus scraped itself.

* TenantExporterAbsent is two branches joined by ``or``
  (:func:`absent_expr`). Rows with ``expected_cluster`` are reduced to one per
  (tenant, pack, expected_cluster), the cluster is copied into ``da_cluster``
  and ``expected_cluster`` dropped — the alert's identity is (tenant, pack,
  da_cluster) — then ``unless on(tenant, pack, da_cluster) (<presence>)``.
  A second ``unless on(tenant, pack)`` reads the presence series that carry
  NO ``da_cluster`` (:func:`presence_expr` with ``unlabelled=True`` — the
  same table, each selector with ``da_cluster=""``): such a series counts
  for every declared cluster. That is what a Prometheus that scrapes the
  exporters itself sees, so ``kind@cluster`` there reads as the bare kind
  instead of paging for every declared cluster of every tenant. It is a
  term of the expression, not a gate on "do I see labelled series": a gate
  would flip whenever a federation link flaps, and reset ``for:``.
  Rows without ``expected_cluster`` take the rule as it was.
* TenantExporterUndeclared is per (tenant, pack, da_cluster) on its left
  already. What is declared for such a row: the kind alone; or the kind for
  that cluster; or — a row with no ``da_cluster`` — the kind for any cluster.
* MassExporterOutage counts (tenant, pack) PAIRS, and a pair with entries for
  three clusters is three rows: both sides are reduced to pairs first, and a
  pair is absent when TenantExporterAbsent's expression has any row for it.
  A pair carries no ``da_cluster`` either: floor and ratio are the fleet's.
  That expression is written there with ``without`` instead of ``by`` (see
  :func:`absent_expr`).

Regenerate the three expressions after editing the registry or a template::

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
from _lib_output import (  # noqa: E402  (#3981: the artifact entry)
    check_repo_artifact, exit_on_output_write_error, is_special_file, write_repo_artifact)
from _lib_python import detect_cli_lang  # noqa: E402  (dev-rules §9: CLI help 語言)

#: The label of ``tenant_expected_exporter`` that carries the kind — the label
#: ``user_threshold_unset{tenant, pack, key}`` already uses.
KIND_LABEL = "pack"
#: The label of ``tenant_expected_exporter`` that carries the cluster of a
#: ``kind@cluster`` entry (collector.go collectTenantExpectedExporter). Not
#: CLUSTER_LABEL: a row for a kind alone, federated from an edge, arrives with
#: that edge's ``da_cluster`` stamped on it and would read as declared.
EXPECTED_CLUSTER_LABEL = "expected_cluster"
#: ADR-006: the label that says which cluster a series came from
#: (check_cluster_identity.CLUSTER_LABEL; pinned equal by
#: tests/rulepacks/test_exporter_presence.py).
CLUSTER_LABEL = "da_cluster"


def _registry():
    ops_dir = os.path.join(_HERE, "ops")
    if ops_dir not in sys.path:
        sys.path.insert(0, ops_dir)
    import _registry_lib  # noqa: PLC0415  (scripts/tools/ops)
    return _registry_lib


def presence_table() -> dict[str, tuple[str, ...]]:
    """kind → the selector(s) whose presence under job="tenant-exporters"
    means an exporter of that kind is answering (several: any one counts).
    Every registry pack with ``presence:``, in the registry's order."""
    lib = _registry()
    return lib.pack_presence(lib.load_registry())


def undeclared_kinds() -> list[str]:
    """The kinds TenantExporterUndeclared names: every kind whose pack does
    not say ``undeclared_notice: false``."""
    lib = _registry()
    return lib.undeclared_notice_kinds(lib.load_registry())


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


#: The matcher of a presence series that carries no cluster.
UNLABELLED_SELECTOR = f'{CLUSTER_LABEL}=""'


def selector(metric: str, unlabelled: bool = False) -> str:
    """*metric* under the tenant-exporters job. A table entry is a bare name or
    a selector with its own matchers (query-exporter's built-ins are told apart
    by their `query` label); the job matcher joins those. *unlabelled*: only
    the series that carry no ``da_cluster``."""
    matchers = JOB_SELECTOR + (f", {UNLABELLED_SELECTOR}" if unlabelled else "")
    if metric.endswith("}"):
        return f"{metric[:-1]}, {matchers}}}"
    return f"{metric}{{{matchers}}}"


def _indent(text: str, depth: int) -> str:
    return "\n".join(_PAD * depth + line for line in text.split("\n"))


def _terms(kinds, wrap, unlabelled: bool = False) -> str:
    table = presence_table()
    terms = []
    for kind in kinds:
        inner = " or ".join(wrap(selector(m, unlabelled)) for m in table[kind])
        terms.append(f'label_replace({inner}, "{KIND_LABEL}", "{kind}", "", "")')
    return "\nor ".join(terms)


def presence_expr(unlabelled: bool = False) -> str:
    """Every series of a presence metric under the tenant-exporters job, with
    its kind as ``pack`` — one ``label_replace`` term per kind, ``or``-ed
    (the terms differ in metric name and pack, so ``or`` keeps them all).
    *unlabelled*: the same table, only the series with no ``da_cluster``."""
    return _terms(presence_table(), lambda sel: sel, unlabelled)


def presence_over_time_expr() -> str:
    """:func:`presence_expr` remembered for UNDECLARED_LOOKBACK, over the
    kinds TenantExporterUndeclared names (:func:`undeclared_kinds`): every
    series such a kind's metric had under the tenant-exporters job within
    that window, with its kind as ``pack``."""
    return _terms(undeclared_kinds(),
                  lambda sel: f"max_over_time({sel}[{UNDECLARED_LOOKBACK}])")


def pack_presence(pack: str) -> tuple[str, ...]:
    """The presence selector(s) of *pack*'s exporter; () when it has none."""
    return presence_table().get(pack, ())


def unset_notice_packs() -> list[str]:
    """The packs that read a threshold key the platform ships no value for —
    each carries <prefix>ThresholdNotConfigured (#1200 D-A A3). Derived from the
    threshold registry, never listed here."""
    lib = _registry()
    return sorted(set(lib.unset_notice_keys(lib.load_registry()).values()))


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
                         "(threshold-registry.yaml packs.<pack>.presence)")
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


def _unless(on: str, body: str) -> str:
    return f"unless on({on}) (\n" + _indent(body, 1) + "\n)"


def absent_expr(aggregate_by: bool = True) -> str:
    """TenantExporterAbsent: every declared expectation nothing answers for.

    Two branches, ``or``-ed (their label sets differ, so ``or`` keeps both):

    * per cluster — the rows with ``expected_cluster``, the cluster copied
      into ``da_cluster``; gone when a presence series of the kind carries
      that ``da_cluster``, or when one carries no ``da_cluster`` at all.
    * any cluster — the rows without ``expected_cluster``: the rule as it
      was, labels included (``max without(instance, pod)`` keeps whatever
      else the scrape put on the row).

    *aggregate_by* is how the per-cluster rows are reduced before the cluster
    is copied. The reduction is not optional: the same entry federated from
    two edges arrives as two rows that differ only in the ``da_cluster`` each
    edge stamped, and writing the declared cluster over it would leave two
    series with one label set — an evaluation error, the rule silent.

    * True (the alert): ``max by(tenant, pack, expected_cluster)``, then
      ``expected_cluster`` is dropped. One alert per (tenant, pack, cluster)
      whatever else the rows carry.
    * False (MassExporterOutage, which only asks which PAIRS have a row):
      ``max without(instance, pod, da_cluster)``. The platform-alert guard
      (_grar_validate) reads any ``by (… tenant …)`` in a platform alert's
      expr as "this alert carries a tenant label", which it does not.
    """
    declared = f'tenant_expected_exporter{{{EXPECTED_CLUSTER_LABEL}!=""}}'
    if aggregate_by:
        rows = "\n".join([
            "label_replace(",
            "  label_replace(",
            f"    max by(tenant, {KIND_LABEL}, {EXPECTED_CLUSTER_LABEL}) ({declared}),",
            f'    "{CLUSTER_LABEL}", "$1", "{EXPECTED_CLUSTER_LABEL}", "(.+)"),',
            f'  "{EXPECTED_CLUSTER_LABEL}", "", "", "")',
        ])
    else:
        rows = "\n".join([
            "label_replace(",
            f"  max without(instance, pod, {CLUSTER_LABEL}) ({declared}),",
            f'  "{CLUSTER_LABEL}", "$1", "{EXPECTED_CLUSTER_LABEL}", "(.+)")',
        ])
    per_cluster = "\n".join([
        rows,
        _unless(f"tenant, {KIND_LABEL}, {CLUSTER_LABEL}", presence_expr()),
        _unless(f"tenant, {KIND_LABEL}", presence_expr(unlabelled=True)),
    ])
    any_cluster = "\n".join([
        f'max without(instance, pod) (tenant_expected_exporter{{{EXPECTED_CLUSTER_LABEL}=""}})',
        _unless(f"tenant, {KIND_LABEL}", presence_expr()),
    ])
    return "\n".join(["(", _indent(per_cluster, 1), ")", "or", "(", _indent(any_cluster, 1), ")"])


#: Every declared (tenant, pack) once, however many clusters it is declared
#: for, however many exporter replicas serve the row, and whichever Prometheus
#: the row came through: federated from the edges, the same declarations arrive
#: with each edge's ``da_cluster``, and a pair that kept it would be counted —
#: floor and ratio both — once per source instead of once in the fleet.
DECLARED_PAIRS = (f"max without (instance, pod, {EXPECTED_CLUSTER_LABEL}, {CLUSTER_LABEL}) "
                  "(tenant_expected_exporter)")


def absent_pairs_expr() -> str:
    """The declared (tenant, pack) pairs TenantExporterAbsent has ANY row for
    — one row per pair, with the labels of :data:`DECLARED_PAIRS`, so the
    count of them divides by the count of those."""
    return "\n".join([
        DECLARED_PAIRS,
        f"and on(tenant, {KIND_LABEL}) (",
        _indent(absent_expr(aggregate_by=False), 1),
        ")",
    ])


def undeclared_expr() -> str:
    """TenantExporterUndeclared: per (tenant, pack, cluster), an exporter that
    answered and that no entry covers.

    Per (tenant, pack) (#3771 review): a tenant declaring [mariadb] while it
    also runs a redis exporter is named for redis. And per cluster (ADR-006,
    #4097): the left side is a row for each ``da_cluster`` the exporter
    answers in. Such a row is covered by

    * the kind written alone — ``unless on(tenant, pack)``;
    * the kind for that cluster — ``unless on(tenant, pack, da_cluster)``
      against the declared rows with their cluster copied into
      ``da_cluster``;
    * for a row with NO ``da_cluster`` (a Prometheus that scraped the
      exporter itself cannot say which cluster it is), the kind for any
      cluster: the same ``unless``, whose right side also carries each
      declared (tenant, pack) with no ``da_cluster`` — the label a left row
      without one matches on.

    So `mariadb@east` with a mariadb exporter answering in west names west:
    nothing pages if it stops. The guard joins on tenant: threshold-exporter
    down (no tenant_metadata_info at all) and a de-provisioned tenant (its
    config gone, the lookback's memory of its exporter not yet) both stay
    silent.
    """
    declared = f'tenant_expected_exporter{{{EXPECTED_CLUSTER_LABEL}!=""}}'
    return "\n".join([
        f"group by(tenant, {CLUSTER_LABEL}, {KIND_LABEL}) (",
        _indent(presence_over_time_expr(), 1),
        ")",
        f'unless on(tenant, {KIND_LABEL}) tenant_expected_exporter{{{EXPECTED_CLUSTER_LABEL}=""}}',
        f"unless on(tenant, {KIND_LABEL}, {CLUSTER_LABEL}) (",
        f"  label_replace(group by(tenant, {KIND_LABEL}, {EXPECTED_CLUSTER_LABEL}) ({declared}),",
        f'    "{CLUSTER_LABEL}", "$1", "{EXPECTED_CLUSTER_LABEL}", "(.+)")',
        f"  or group by(tenant, {KIND_LABEL}) ({declared})",
        ")",
        "and on(tenant) tenant_metadata_info",
        "unless on(tenant)",
        '(user_state_filter{filter="maintenance"} == 1)',
    ])


def mass_outage_expr() -> str:
    """MassExporterOutage: more than 5 tenants and over 10% of the declared
    (tenant, pack) pairs absent, while the job itself is there.

    The floor counts TENANTS (#3771 review): one tenant declaring seven kinds,
    all down, is seven pairs but no shared failure. The ratio counts PAIRS on
    both sides (#4097): a pair declared for three clusters is three rows of
    tenant_expected_exporter and up to three of TenantExporterAbsent — counted
    as rows, one such tenant would weigh three times in the numerator, the
    denominator, or both. Both are FLEET-WIDE: the pairs drop the `da_cluster`
    of the Prometheus the declaration came through (:data:`DECLARED_PAIRS`),
    so the same absent set gives one alert, with no `da_cluster`, however the
    declarations are split across sources. `count without` throughout — a `by (tenant)` would
    read to the platform-alert guard (_grar_validate) as an alert carrying a
    tenant label.
    """
    absent = absent_pairs_expr()
    return "\n".join([
        "(",
        f"  count without (tenant) (count without ({KIND_LABEL}) (",
        _indent(absent, 2),
        "  )) > 5",
        "  and",
        "  (",
        f"    count without (tenant, {KIND_LABEL}) (",
        _indent(absent, 3),
        "    )",
        "    /",
        f"    count without (tenant, {KIND_LABEL}) ({DECLARED_PAIRS})",
        "  ) > 0.1",
        ")",
        'unless on() absent(up{job="tenant-exporters"})',
    ])


def alert_exprs() -> dict[tuple[Path, str], str]:
    """(pack, alertname) → the alert's whole ``expr`` (no trailing newline)."""
    notices = {(Path(RULE_PACKS.directory) / RULE_PACKS.file_name(p), unset_notice_alert(p)):
               unset_notice_expr(p) for p in unset_notice_packs()}
    return {
        **notices,
        (LIVENESS_PACK, "TenantExporterAbsent"): absent_expr(),
        (LIVENESS_PACK, "TenantExporterUndeclared"): undeclared_expr(),
        (PLATFORM_PACK, "MassExporterOutage"): mass_outage_expr(),
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


@exit_on_output_write_error
def main(argv: list[str] | None = None) -> int:
    zh = detect_cli_lang() == "zh"
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--write", action="store_true",
                        help=("依 registry 的 presence 改寫各告警的 expr" if zh
                              else "rewrite the alerts' expr from the registry's presence table"))
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[2]),
                        help=("repo 根目錄（預設：本工作樹）" if zh
                              else "repository root (default: this tree's)"))
    args = parser.parse_args(argv)
    stale = []
    texts: dict[Path, str] = {}
    originals: dict[Path, str] = {}
    try:
        for (pack, alert), expr in alert_exprs().items():
            path = Path(args.root) / pack
            text = texts.get(path)
            if text is None:
                # A FIFO would block the read (#3981); check mode is read-only,
                # so the stdin-tolerant test, not the artifact's.
                if is_special_file(path):
                    raise ValueError(f"{path}: not a regular file")
                text = originals[path] = path.read_text(encoding="utf-8")
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
    # #3981: only the packs that change, every one judged before the first
    # is written; a bad `--root` fails on the read above, before this. No
    # `flag=`: the write gate reads a `_`-prefixed module's writers as a
    # library's, forwarding a caller's flag.
    changed = [path for path, text in texts.items() if text != originals[path]]
    for path in changed:
        check_repo_artifact(path)
    for path in changed:
        write_repo_artifact(path, texts[path])
    for item in stale:
        print(f"{item}: expr rewritten")
    return 0


if __name__ == "__main__":
    _lib_exitcodes.run(main)
