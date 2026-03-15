#!/usr/bin/env python3
"""generate_platform_data.py — 共用平台資料產生器

從 rule-packs/*.yaml（權威 recording/alert 計數）、scaffold_tenant.py（metadata）
萃取出 docs/assets/platform-data.json，供所有 JSX 互動工具共用。

消除 JSX 工具間的硬編碼數據不一致問題。

Usage:
    python3 scripts/tools/generate_platform_data.py              # 產生 JSON
    python3 scripts/tools/generate_platform_data.py --check      # CI 模式（drift 偵測）
    python3 scripts/tools/generate_platform_data.py --dry-run    # 只印出 JSON 不寫檔
"""
import argparse
import importlib.util
import json
import os
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent.parent

RULE_PACKS_DIR = REPO_ROOT / "rule-packs"
K8S_RULES_DIR = REPO_ROOT / "k8s" / "03-monitoring"
OUTPUT_PATH = REPO_ROOT / "docs" / "assets" / "platform-data.json"
SCAFFOLD_PATH = SCRIPT_DIR.parent / "ops" / "scaffold_tenant.py"


# ---------------------------------------------------------------------------
# Load scaffold_tenant.py RULE_PACKS without importing the whole module
# ---------------------------------------------------------------------------
def load_scaffold_rule_packs() -> dict:
    """Import RULE_PACKS from scaffold_tenant.py."""
    spec = importlib.util.spec_from_file_location("scaffold_tenant", str(SCAFFOLD_PATH))
    if spec is None or spec.loader is None:
        print(f"WARNING: Cannot load {SCAFFOLD_PATH}", file=sys.stderr)
        return {}
    mod = importlib.util.module_from_spec(spec)
    # Prevent actual execution of main logic
    sys.modules["scaffold_tenant"] = mod
    try:
        spec.loader.exec_module(mod)
    except (ImportError, AttributeError) as e:
        print(f"WARNING: Error loading scaffold_tenant: {e}", file=sys.stderr)
        return {}
    return getattr(mod, "RULE_PACKS", {})


# ---------------------------------------------------------------------------
# Count rules from YAML source files (authoritative)
# ---------------------------------------------------------------------------
def count_rules_in_yaml(filepath: Path) -> tuple:
    """Count recording and alert rules in a rule-pack YAML file."""
    data = yaml.safe_load(filepath.read_text(encoding="utf-8"))
    rec = alert = 0
    if data and "groups" in data:
        for g in data["groups"]:
            for r in g.get("rules", []):
                if "alert" in r:
                    alert += 1
                elif "record" in r:
                    rec += 1
    return rec, alert


def count_rules_in_configmap(filepath: Path) -> tuple:
    """Count rules inside a Kubernetes ConfigMap YAML."""
    data = yaml.safe_load(filepath.read_text(encoding="utf-8"))
    rec = alert = 0
    if data and data.get("kind") == "ConfigMap":
        for _key, inner_yaml in data.get("data", {}).items():
            inner = yaml.safe_load(inner_yaml)
            if inner and "groups" in inner:
                for g in inner["groups"]:
                    for r in g.get("rules", []):
                        if "alert" in r:
                            alert += 1
                        elif "record" in r:
                            rec += 1
    return rec, alert


def gather_rule_counts() -> dict:
    """Gather authoritative rule counts from YAML source files."""
    packs = {}

    # rule-packs/ directory
    for f in sorted(RULE_PACKS_DIR.glob("rule-pack-*.yaml")):
        name = f.stem.replace("rule-pack-", "")
        rec, alert = count_rules_in_yaml(f)
        packs[name] = {"recording": rec, "alert": alert}

    # k8s ConfigMaps (may have additional rules, e.g. platform)
    for f in sorted(K8S_RULES_DIR.glob("configmap-rules-*.yaml")):
        name = f.stem.replace("configmap-rules-", "")
        rec, alert = count_rules_in_configmap(f)
        if name not in packs:
            packs[name] = {"recording": rec, "alert": alert}
        else:
            packs[name]["recording"] = max(packs[name]["recording"], rec)
            packs[name]["alert"] = max(packs[name]["alert"], alert)

    return packs


# ---------------------------------------------------------------------------
# Category & dependency metadata
# ---------------------------------------------------------------------------
CATEGORIES = {
    "database": {"en": "Databases", "zh": "資料庫"},
    "messaging": {"en": "Messaging", "zh": "訊息佇列"},
    "runtime": {"en": "Runtime Environments", "zh": "運行環境"},
    "webserver": {"en": "Web Servers", "zh": "網頁伺服器"},
    "infrastructure": {"en": "Infrastructure", "zh": "基礎設施"},
}

PACK_CATEGORY = {
    "mariadb": "database",
    "postgresql": "database",
    "redis": "database",
    "mongodb": "database",
    "elasticsearch": "database",
    "oracle": "database",
    "db2": "database",
    "clickhouse": "database",
    "kafka": "messaging",
    "rabbitmq": "messaging",
    "jvm": "runtime",
    "nginx": "webserver",
    "kubernetes": "infrastructure",
    "operational": "infrastructure",
    "platform": "infrastructure",
}

# Short exporter name for UI display (label column in matrix)
EXPORTER_SHORT = {
    "mariadb": "mysqld_exporter",
    "postgresql": "postgres_exporter",
    "redis": "redis_exporter",
    "mongodb": "mongodb_exporter",
    "elasticsearch": "elasticsearch_exporter",
    "oracle": "oracledb_exporter",
    "db2": "db2_exporter",
    "clickhouse": "clickhouse_exporter",
    "kafka": "kafka_exporter",
    "rabbitmq": "rabbitmq_exporter",
    "jvm": "jmx_exporter",
    "nginx": "nginx-prometheus-exporter",
    "kubernetes": "cAdvisor + kube-state-metrics",
    "operational": "threshold-exporter",
    "platform": "threshold-exporter",
}

# Display labels
PACK_LABELS = {
    "mariadb": "MariaDB/MySQL",
    "postgresql": "PostgreSQL",
    "redis": "Redis",
    "mongodb": "MongoDB",
    "elasticsearch": "Elasticsearch",
    "oracle": "Oracle",
    "db2": "DB2",
    "clickhouse": "ClickHouse",
    "kafka": "Kafka",
    "rabbitmq": "RabbitMQ",
    "jvm": "JVM",
    "nginx": "Nginx",
    "kubernetes": "Kubernetes",
    "operational": "Operational",
    "platform": "Platform",
}

# Key metrics covered (for matrix display)
METRICS_COVERAGE = {
    "mariadb": ["connections", "cpu", "memory", "slow_queries", "replication_lag", "query_errors"],
    "postgresql": ["connections", "cache_hit", "query_time", "disk_usage", "replication_lag"],
    "redis": ["memory", "evictions", "connected_clients", "keyspace_hits"],
    "mongodb": ["connections", "memory", "page_faults", "replication"],
    "elasticsearch": ["heap", "unassigned_shards", "cluster_health", "indexing_rate"],
    "oracle": ["sessions", "tablespace", "wait_events", "redo_log"],
    "db2": ["connections", "bufferpool", "tablespace", "lock_waits"],
    "clickhouse": ["queries", "merges", "replicated_lag", "memory"],
    "kafka": ["consumer_lag", "broker_active", "controller", "isr_shrink", "under_replicated"],
    "rabbitmq": ["queue_depth", "consumers", "memory", "disk_free", "connections"],
    "jvm": ["gc_pause", "heap_usage", "thread_pool", "class_loading"],
    "nginx": ["active_connections", "request_rate", "connection_backlog"],
    "kubernetes": ["pod_restart", "cpu_limit", "memory_limit", "pvc_usage"],
    "operational": ["exporter_health", "config_reload"],
    "platform": ["threshold_metric_count", "recording_rule_health", "scrape_success"],
}

# Dependency suggestions (for rule-pack-selector)
DEPENDENCIES = {
    "mariadb": {"suggests": ["kubernetes"], "reason": {"en": "Container resource alerts complement DB monitoring", "zh": "容器資源告警補充 DB 監控"}},
    "postgresql": {"suggests": ["kubernetes"], "reason": {"en": "Container resource alerts complement DB monitoring", "zh": "容器資源告警補充 DB 監控"}},
    "redis": {"suggests": ["kubernetes"], "reason": {"en": "Container resource alerts complement DB monitoring", "zh": "容器資源告警補充 DB 監控"}},
    "mongodb": {"suggests": ["kubernetes"], "reason": {"en": "Container resource alerts complement DB monitoring", "zh": "容器資源告警補充 DB 監控"}},
    "elasticsearch": {"suggests": ["kubernetes", "jvm"], "reason": {"en": "ES runs on JVM; K8s monitors container resources", "zh": "ES 運行在 JVM 上；K8s 監控容器資源"}},
    "oracle": {"suggests": ["kubernetes"], "reason": {"en": "Container resource alerts complement DB monitoring", "zh": "容器資源告警補充 DB 監控"}},
    "db2": {"suggests": ["kubernetes"], "reason": {"en": "Container resource alerts complement DB monitoring", "zh": "容器資源告警補充 DB 監控"}},
    "clickhouse": {"suggests": ["kubernetes"], "reason": {"en": "Container resource alerts complement DB monitoring", "zh": "容器資源告警補充 DB 監控"}},
    "kafka": {"suggests": ["kubernetes", "jvm"], "reason": {"en": "Kafka brokers run on JVM; K8s monitors pods", "zh": "Kafka broker 運行在 JVM 上；K8s 監控 Pod"}},
    "rabbitmq": {"suggests": ["kubernetes"], "reason": {"en": "Container resource alerts complement MQ monitoring", "zh": "容器資源告警補充 MQ 監控"}},
    "jvm": {"suggests": ["kubernetes"], "reason": {"en": "JVM apps typically run in K8s pods", "zh": "JVM 應用通常運行在 K8s Pod 中"}},
    "nginx": {"suggests": ["kubernetes"], "reason": {"en": "Ingress/proxy pods benefit from K8s resource alerts", "zh": "Ingress/proxy Pod 受益於 K8s 資源告警"}},
}

# Required packs (always included, cannot be deselected)
REQUIRED_PACKS = {"operational", "platform"}

# Default-on packs (selected by default for new tenants)
DEFAULT_ON_PACKS = {"kubernetes", "mariadb"}

# Canonical order for display
PACK_ORDER = [
    "mariadb", "postgresql", "redis", "mongodb",
    "elasticsearch", "oracle", "db2", "clickhouse",
    "kafka", "rabbitmq", "jvm", "nginx",
    "kubernetes", "operational", "platform",
]


# ---------------------------------------------------------------------------
# Build platform-data.json
# ---------------------------------------------------------------------------
def build_platform_data() -> dict:
    """Build the complete platform-data.json structure."""
    rule_counts = gather_rule_counts()
    scaffold_packs = load_scaffold_rule_packs()

    rule_packs = {}
    for key in PACK_ORDER:
        counts = rule_counts.get(key, {"recording": 0, "alert": 0})
        scaffold = scaffold_packs.get(key, {})

        pack_data = {
            "label": PACK_LABELS.get(key, key),
            "category": PACK_CATEGORY.get(key, "unknown"),
            "exporter": EXPORTER_SHORT.get(key, key),
            "configMap": f"prometheus-rules-{key}",
            "recordingRules": counts["recording"],
            "alertRules": counts["alert"],
            "required": key in REQUIRED_PACKS,
            "defaultOn": key in DEFAULT_ON_PACKS,
            "metrics": METRICS_COVERAGE.get(key, []),
        }

        # Enrich from scaffold_tenant.py if available
        if scaffold:
            pack_data["display"] = scaffold.get("display", pack_data["label"])
            pack_data["exporterFull"] = scaffold.get("exporter", pack_data["exporter"])

            # Extract default threshold keys
            defaults = scaffold.get("defaults", {})
            if defaults:
                pack_data["defaults"] = {
                    k: {
                        "value": v["value"],
                        "unit": v.get("unit", ""),
                        "desc": v.get("desc", ""),
                    }
                    for k, v in defaults.items()
                }

        # Dependencies
        if key in DEPENDENCIES:
            pack_data["dependencies"] = DEPENDENCIES[key]

        rule_packs[key] = pack_data

    # Totals
    total_rec = sum(rule_counts.get(k, {}).get("recording", 0) for k in PACK_ORDER)
    total_alert = sum(rule_counts.get(k, {}).get("alert", 0) for k in PACK_ORDER)

    return {
        "_comment": "Auto-generated by generate_platform_data.py — DO NOT EDIT",
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generator": "scripts/tools/generate_platform_data.py",
        "packOrder": PACK_ORDER,
        "rulePacks": rule_packs,
        "categories": CATEGORIES,
        "totals": {
            "packs": len(PACK_ORDER),
            "recordingRules": total_rec,
            "alertRules": total_alert,
            "total": total_rec + total_alert,
        },
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    """CLI entry point: 共用平台資料產生器."""
    parser = argparse.ArgumentParser(
        description="Generate docs/assets/platform-data.json from source YAML",
    )
    parser.add_argument("--check", action="store_true",
                        help="CI mode: exit 1 if output is outdated")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print JSON to stdout without writing file")
    args = parser.parse_args()

    data = build_platform_data()
    # For --check comparison, use a stable timestamp
    if args.check:
        data.pop("generated", None)

    content = json.dumps(data, indent=2, ensure_ascii=False) + "\n"

    if args.dry_run:
        print(content)
        return

    if args.check:
        if not OUTPUT_PATH.exists():
            print(f"❌ {OUTPUT_PATH.relative_to(REPO_ROOT)} does not exist. "
                  f"Run without --check first.")
            sys.exit(1)
        existing = json.loads(OUTPUT_PATH.read_text(encoding="utf-8"))
        existing.pop("generated", None)
        existing_str = json.dumps(existing, indent=2, ensure_ascii=False) + "\n"
        if existing_str != content:
            print(f"❌ {OUTPUT_PATH.relative_to(REPO_ROOT)} is outdated. "
                  f"Run `make platform-data` to update.")
            sys.exit(1)
        else:
            print(f"✅ {OUTPUT_PATH.relative_to(REPO_ROOT)} is up to date.")
        return

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(content, encoding="utf-8")
    os.chmod(OUTPUT_PATH,
             stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)
    print(f"✅ Generated {OUTPUT_PATH.relative_to(REPO_ROOT)}")
    print(f"   {data['totals']['packs']} packs, "
          f"{data['totals']['recordingRules']} recording rules, "
          f"{data['totals']['alertRules']} alert rules")


if __name__ == "__main__":
    main()
