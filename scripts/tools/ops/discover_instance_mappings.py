#!/usr/bin/env python3
"""discover_instance_mappings.py — Auto-discover 1:N instance-tenant mappings.

Scrapes a database exporter's /metrics endpoint (or queries Prometheus) to
discover unique label values (schema, tablespace, database, etc.) and
generates an _instance_mapping.yaml draft that the DBA only needs to fill
in tenant names.

ADR-006 companion: lowers the barrier to adopting 1:N mapping topology.

Usage:
    # Scrape exporter directly
    discover-mappings --endpoint http://exporter:9104/metrics

    # Query Prometheus for an instance's labels
    discover-mappings --prometheus http://localhost:9090 --instance oracle-prod:9161

    # Auto-detect from running Prometheus targets
    discover-mappings --prometheus http://localhost:9090 --job oracle-exporter

    # Output to file
    discover-mappings --endpoint http://exporter:9104/metrics -o _instance_mapping.yaml
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from collections import defaultdict
from typing import Any, Optional

import yaml

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)
sys.path.insert(0, os.path.join(_THIS_DIR, '..'))
from _lib_python import (  # noqa: E402
    detect_cli_lang,
    http_get_json,
    write_text_secure,
)

_LANG = detect_cli_lang()

# ---------------------------------------------------------------------------
# Label discovery heuristics
# ---------------------------------------------------------------------------

# Labels that typically represent schema/tablespace/database boundaries
# in common database exporters.  Priority order matters for ranking.
PARTITION_LABEL_CANDIDATES: list[str] = [
    "schema",
    "schemaname",
    "tablespace",
    "datname",         # PostgreSQL database name
    "database",
    "db",
    "dbname",
    "keyspace",        # Cassandra
    "namespace",       # MongoDB
    "vhost",           # RabbitMQ
    "topic",           # Kafka
    "index",           # Elasticsearch
    "container_name",  # Generic
]

# Metric name prefixes that hint at database-level metrics
DB_METRIC_PREFIXES: list[str] = [
    "oracle_", "oracledb_",
    "pg_", "postgres_",
    "mysql_", "mysqld_",
    "mongodb_", "mongo_",
    "redis_",
    "mssql_",
    "db2_",
    "clickhouse_",
    "kafka_", "kafka_topic_",
    "rabbitmq_",
    "elasticsearch_", "es_",
]


def parse_prometheus_text(raw: str) -> dict[str, set[str]]:
    """Parse Prometheus text exposition format and extract label values.

    Returns dict mapping label_name → set of unique values.
    Only considers labels in PARTITION_LABEL_CANDIDATES.
    """
    label_values: dict[str, set[str]] = defaultdict(set)
    # Match metric lines with labels: metric_name{label="value",...} value
    label_re = re.compile(r'(\w+)="([^"]*)"')

    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        # Find labels block
        brace_start = line.find('{')
        if brace_start < 0:
            continue
        brace_end = line.find('}', brace_start)
        if brace_end < 0:
            continue
        labels_str = line[brace_start + 1:brace_end]
        for m in label_re.finditer(labels_str):
            label_name = m.group(1)
            label_value = m.group(2)
            if label_name in PARTITION_LABEL_CANDIDATES and label_value:
                label_values[label_name].add(label_value)

    return dict(label_values)


def detect_db_type(raw: str) -> str:
    """Heuristically detect database type from metric names."""
    line_sample = raw[:8000].lower()
    for prefix in DB_METRIC_PREFIXES:
        if prefix in line_sample:
            return prefix.rstrip('_')
    return "unknown"


def scrape_metrics_endpoint(endpoint: str,
                            timeout: int = 15) -> tuple[Optional[str], Optional[str]]:
    """Scrape a Prometheus-format /metrics endpoint.

    Returns (raw_text, None) on success, (None, error) on failure.
    """
    import urllib.request
    import urllib.error
    from _lib_python import _validate_url_scheme

    err = _validate_url_scheme(endpoint)
    if err:
        return None, err
    try:
        req = urllib.request.Request(endpoint)  # nosec B310
        req.add_header("Accept", "text/plain")
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310
            return resp.read().decode("utf-8", errors="replace"), None
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
        return None, str(exc)


def query_prometheus_label_values(
    prom_url: str,
    instance: str | None = None,
    job: str | None = None,
    timeout: int = 10,
) -> dict[str, set[str]]:
    """Query Prometheus /api/v1/label/<name>/values for partition labels.

    Uses instance or job selector to filter.
    Returns dict mapping label_name → set of unique values.
    """
    label_values: dict[str, set[str]] = defaultdict(set)
    base = prom_url.rstrip('/')

    for label in PARTITION_LABEL_CANDIDATES:
        # Build matcher
        matchers = []
        if instance:
            matchers.append(f'instance="{instance}"')
        if job:
            matchers.append(f'job="{job}"')
        matcher_str = ','.join(matchers)

        # Use series API with matcher
        if matcher_str:
            url = f"{base}/api/v1/series?match[]={{{matcher_str}}}"
        else:
            url = f"{base}/api/v1/label/{label}/values"

        data, err = http_get_json(url, timeout=timeout)
        if err or not data:
            continue

        if "data" in data:
            # /api/v1/series returns list of label sets
            if isinstance(data["data"], list):
                for series in data["data"]:
                    if isinstance(series, dict) and label in series:
                        val = series[label]
                        if val:
                            label_values[label].add(val)
            # /api/v1/label/.../values returns list of strings
            elif isinstance(data["data"], list):
                for val in data["data"]:
                    if val:
                        label_values[label].add(val)

    return dict(label_values)


def rank_partition_labels(
    label_values: dict[str, set[str]],
) -> list[tuple[str, set[str], int]]:
    """Rank discovered labels by suitability for partition.

    Considers: number of unique values (2-200 is ideal),
    label name priority from PARTITION_LABEL_CANDIDATES.
    Returns sorted list of (label_name, values, score).
    """
    scored: list[tuple[str, set[str], int]] = []
    for label, values in label_values.items():
        count = len(values)
        if count < 2:
            continue  # not useful for partitioning

        # Base score from priority
        try:
            priority_idx = PARTITION_LABEL_CANDIDATES.index(label)
        except ValueError:
            priority_idx = len(PARTITION_LABEL_CANDIDATES)
        score = 100 - priority_idx * 5

        # Bonus for ideal cardinality range
        if 2 <= count <= 50:
            score += 30
        elif count <= 200:
            score += 10
        else:
            score -= 20  # too many values, less useful

        scored.append((label, values, score))

    scored.sort(key=lambda x: -x[2])
    return scored


def generate_mapping_draft(
    instance: str,
    label_name: str,
    label_values: set[str],
    db_type: str = "unknown",
) -> dict:
    """Generate _instance_mapping.yaml draft structure.

    Returns YAML-serialisable dict.
    """
    entries = []
    for val in sorted(label_values):
        entries.append({
            "tenant": f"<FILL_TENANT_FOR_{val}>",
            "filter": f'{label_name}="{val}"',
        })

    return {
        "# Auto-generated by discover-mappings": f"db_type={db_type}",
        "# Partition label": label_name,
        "# Fill in tenant names and remove unused entries": True,
        "instance_tenant_mapping": {
            instance: entries,
        },
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

_HELP: dict[str, dict[str, str]] = {
    "description": {
        "zh": "自動發現 1:N 實例-租戶映射（ADR-006）：掃描 exporter /metrics 或查詢 Prometheus",
        "en": "Auto-discover 1:N instance-tenant mappings (ADR-006): scrape exporter /metrics or query Prometheus",
    },
    "endpoint": {
        "zh": "Database exporter /metrics 端點 URL",
        "en": "Database exporter /metrics endpoint URL",
    },
    "prometheus": {
        "zh": "Prometheus 查詢 URL（替代直接 scrape）",
        "en": "Prometheus query URL (alternative to direct scrape)",
    },
    "instance": {
        "zh": "目標實例標籤（搭配 --prometheus 使用）",
        "en": "Target instance label (use with --prometheus)",
    },
    "job": {
        "zh": "目標 job 名稱（搭配 --prometheus 使用）",
        "en": "Target job name (use with --prometheus)",
    },
    "output": {
        "zh": "輸出路徑（省略則輸出到 stdout）",
        "en": "Output file path (omit for stdout)",
    },
    "json": {
        "zh": "以 JSON 格式輸出",
        "en": "Output in JSON format",
    },
}


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: discover_instance_mappings.py."""
    lang = detect_cli_lang()

    def _h(key: str) -> str:
        return _HELP[key].get(lang, _HELP[key]["en"])

    parser = argparse.ArgumentParser(description=_h("description"))
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--endpoint", help=_h("endpoint"))
    group.add_argument("--prometheus", help=_h("prometheus"))
    parser.add_argument("--instance", help=_h("instance"))
    parser.add_argument("--job", help=_h("job"))
    parser.add_argument("-o", "--output", help=_h("output"))
    parser.add_argument("--json", action="store_true", help=_h("json"))

    args = parser.parse_args(argv)

    # ── Discover label values ──────────────────────────────────────
    label_values: dict[str, set[str]] = {}
    db_type = "unknown"
    instance_id = args.instance or "unknown"

    if args.endpoint:
        print(f"Scraping {args.endpoint} ...")
        raw, err = scrape_metrics_endpoint(args.endpoint)
        if err:
            print(f"ERROR: {err}", file=sys.stderr)
            return 1
        label_values = parse_prometheus_text(raw)
        db_type = detect_db_type(raw)
        # Use endpoint as instance identifier
        instance_id = args.endpoint.split("//")[-1].split("/")[0]
        metric_count = sum(1 for line in raw.splitlines()
                          if line and not line.startswith('#'))
        print(f"  Scraped {metric_count} metric samples, db_type={db_type}")

    elif args.prometheus:
        if not args.instance and not args.job:
            print("ERROR: --prometheus requires --instance or --job",
                  file=sys.stderr)
            return 1
        print(f"Querying Prometheus at {args.prometheus} ...")
        label_values = query_prometheus_label_values(
            args.prometheus,
            instance=args.instance,
            job=args.job,
        )
        instance_id = args.instance or args.job or "unknown"

    if not label_values:
        msg = ("未發現可用於分區的標籤值" if lang == "zh"
               else "No partition-suitable label values discovered")
        print(f"\n⚠ {msg}")
        print("  " + ("檢查 exporter 是否暴露 schema/tablespace 等標籤" if lang == "zh"
                       else "Check if the exporter exposes schema/tablespace labels"))
        return 1

    # ── Rank and display ──────────────────────────────────────────
    ranked = rank_partition_labels(label_values)

    header = "發現的分區標籤:" if lang == "zh" else "Discovered partition labels:"
    print(f"\n{header}")
    for label, values, score in ranked:
        count = len(values)
        sample = sorted(values)[:5]
        more = f"  (+{count - 5} more)" if count > 5 else ""
        print(f"  {label}: {count} values (score={score})")
        print(f"    sample: {', '.join(sample)}{more}")

    if not ranked:
        print("  (none suitable for partitioning)")
        return 1

    # Use top-ranked label
    best_label, best_values, _ = ranked[0]
    print(f"\n{'推薦分區標籤' if lang == 'zh' else 'Recommended partition label'}: {best_label}")

    # ── Generate mapping draft ────────────────────────────────────
    draft = generate_mapping_draft(instance_id, best_label, best_values, db_type)

    if args.json:
        import json
        output = json.dumps(draft, indent=2, ensure_ascii=False, default=str)
    else:
        # Clean output: skip comment keys
        clean = {"instance_tenant_mapping": draft["instance_tenant_mapping"]}
        output = (
            f"# Auto-generated by discover-mappings (db_type={db_type})\n"
            f"# Partition label: {best_label}\n"
            f"# Fill in tenant names, remove unused entries, then save as _instance_mapping.yaml\n"
            f"#\n"
            + yaml.dump(clean, default_flow_style=False, allow_unicode=True, sort_keys=False)
        )

    if args.output:
        write_text_secure(args.output, output)
        saved_msg = "已儲存" if lang == "zh" else "Saved to"
        print(f"\n{saved_msg}: {args.output}")
    else:
        print(f"\n{'─' * 50}")
        print(output)

    return 0


if __name__ == "__main__":
    sys.exit(main())
