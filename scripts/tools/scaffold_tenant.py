#!/usr/bin/env python3
"""
scaffold_tenant.py — Interactive tenant config generator for Dynamic Alerting.

Generates:
  1. <tenant>.yaml         — Tenant threshold overrides (conf.d/ format)
  2. _defaults.yaml        — Platform defaults (optional, if starting fresh)
  3. scaffold-report.txt   — Summary with rule pack & Helm deployment instructions

Usage:
  python3 scripts/tools/scaffold_tenant.py
  python3 scripts/tools/scaffold_tenant.py --tenant db-c --db mariadb,redis -o output/
  python3 scripts/tools/scaffold_tenant.py --non-interactive --tenant db-c --db mariadb
  python3 scripts/tools/scaffold_tenant.py --tenant db-c --db mariadb --namespaces ns1,ns2,ns3
"""
import argparse
import os
import sys
import textwrap

import yaml

import sys as _sys
_sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lib_python import VALID_RESERVED_KEYS, VALID_RESERVED_PREFIXES, read_onboard_hints  # noqa: E402

# ============================================================
# Rule Pack catalog — metric keys, defaults, descriptions
# ============================================================
RULE_PACKS = {
    "kubernetes": {
        "display": "Kubernetes (cAdvisor + KSM)",
        "exporter": "cAdvisor + kube-state-metrics",
        "default_on": True,
        "rule_pack_file": "rule-packs/rule-pack-kubernetes.yaml",
        "defaults": {
            "container_cpu": {"value": 80, "unit": "%", "desc": "Container CPU % of limit (weakest link)"},
            "container_memory": {"value": 85, "unit": "%", "desc": "Container memory % of limit (weakest link)"},
        },
        "state_filters": {
            "container_crashloop": {
                "reasons": ["CrashLoopBackOff"],
                "severity": "critical",
                "desc": "Detect CrashLoopBackOff containers",
            },
            "container_imagepull": {
                "reasons": ["ImagePullBackOff", "InvalidImageName"],
                "severity": "warning",
                "desc": "Detect ImagePullBackOff containers",
            },
            "maintenance": {
                "reasons": [],
                "severity": "info",
                "default_state": "disable",
                "desc": "Maintenance mode (suppresses all alerts)",
            },
        },
    },
    "postgresql": {
        "display": "PostgreSQL (postgres_exporter)",
        "exporter": "prometheus-community/postgres_exporter",
        "default_on": False,
        "rule_pack_file": "rule-packs/rule-pack-postgresql.yaml",
        "defaults": {
            "pg_connections": {"value": 80, "unit": "% of max_connections", "desc": "Connection usage % warning"},
            "pg_replication_lag": {"value": 30, "unit": "seconds", "desc": "Replication lag warning"},
        },
        "optional_overrides": {
            "pg_connections_critical": {"value": 90, "unit": "% of max_connections", "desc": "Critical tier connections"},
            "pg_replication_lag_critical": {"value": 60, "unit": "seconds", "desc": "Critical tier replication lag"},
        },
        "dimensional_example": {
            "pg_stat_activity_count{datname=\"orders\"}": "50",
        },
    },
    "mariadb": {
        "display": "MariaDB / MySQL (mysqld_exporter)",
        "exporter": "prom/mysqld-exporter or Percona",
        "default_on": True,
        "rule_pack_file": "rule-packs/rule-pack-mariadb.yaml",
        "defaults": {
            "mysql_connections": {"value": 80, "unit": "count", "desc": "Max threads_connected warning"},
            "mysql_cpu": {"value": 80, "unit": "threads/s", "desc": "threads_running rate5m warning"},
        },
        "optional_overrides": {
            "mysql_connections_critical": {"value": 120, "unit": "count", "desc": "Critical tier (Scenario D)"},
            "mysql_cpu_critical": {"value": 120, "unit": "threads/s", "desc": "Critical tier CPU"},
        },
    },
    "redis": {
        "display": "Redis (oliver006/redis_exporter)",
        "exporter": "oliver006/redis_exporter",
        "default_on": False,
        "rule_pack_file": "rule-packs/rule-pack-redis.yaml",
        "defaults": {
            "redis_memory_used_bytes": {"value": 4294967296, "unit": "bytes (4GB)", "desc": "Memory usage warning"},
            "redis_connected_clients": {"value": 200, "unit": "count", "desc": "Connected clients warning"},
        },
        "optional_overrides": {
            "redis_evicted_keys_total": {"value": 100, "unit": "keys/s", "desc": "Key eviction rate"},
            "redis_keyspace_misses_ratio": {"value": 0.3, "unit": "ratio", "desc": "Cache miss ratio (30%)"},
        },
        "dimensional_example": {
            "redis_queue_length{queue=\"order-processing\"}": "100",
            "redis_db_keys{db=\"db0\"}": "1000000",
        },
    },
    "mongodb": {
        "display": "MongoDB (percona/mongodb_exporter)",
        "exporter": "percona/mongodb_exporter",
        "default_on": False,
        "rule_pack_file": "rule-packs/rule-pack-mongodb.yaml",
        "defaults": {
            "mongodb_connections_current": {"value": 300, "unit": "count", "desc": "Current connections warning"},
            "mongodb_repl_lag_seconds": {"value": 10, "unit": "seconds", "desc": "Replication lag warning"},
        },
        "optional_overrides": {
            "mongodb_opcounters_total": {"value": 10000, "unit": "ops/s", "desc": "Total operations rate"},
        },
        "dimensional_example": {
            "mongodb_op_latency{database=\"orders\"}": "50",
        },
    },
    "elasticsearch": {
        "display": "Elasticsearch (elasticsearch_exporter)",
        "exporter": "justwatchcom/elasticsearch_exporter",
        "default_on": False,
        "rule_pack_file": "rule-packs/rule-pack-elasticsearch.yaml",
        "defaults": {
            "es_jvm_memory_used_percent": {"value": 85, "unit": "%", "desc": "JVM heap usage warning"},
            "es_filesystem_free_percent": {"value": 15, "unit": "%", "desc": "Disk free space warning"},
        },
        "optional_overrides": {
            "es_cluster_health": {"value": 1, "unit": "0=green,1=yellow,2=red", "desc": "Cluster health threshold"},
        },
        "dimensional_example": {
            "es_index_doc_count{index=\"logs-prod\"}": "50000000",
        },
    },
    "oracle": {
        "display": "Oracle Database (oracledb_exporter)",
        "exporter": "iamseth/oracledb_exporter",
        "default_on": False,
        "rule_pack_file": "rule-packs/rule-pack-oracle.yaml",
        "defaults": {
            "oracle_sessions_active": {"value": 200, "unit": "count", "desc": "Active sessions warning"},
            "oracle_tablespace_used_percent": {"value": 85, "unit": "%", "desc": "Tablespace usage warning"},
        },
        "optional_overrides": {
            "oracle_wait_time_rate": {"value": 50, "unit": "s/s", "desc": "Wait time rate (5m)"},
            "oracle_process_count": {"value": 300, "unit": "count", "desc": "Active processes warning"},
            "oracle_pga_allocated_bytes": {"value": 4294967296, "unit": "bytes (4GB)", "desc": "PGA allocation warning"},
        },
        "dimensional_example": {
            "oracle_tablespace_used_percent{tablespace_name=~\"USERS|DATA.*\"}": "90",
            "oracle_tablespace_used_percent{tablespace_name=\"SYSTEM\"}": "95:critical",
        },
    },
    "db2": {
        "display": "IBM DB2 (ibm_db2_exporter)",
        "exporter": "ibm_db2_exporter (community)",
        "default_on": False,
        "rule_pack_file": "rule-packs/rule-pack-db2.yaml",
        "defaults": {
            "db2_connections_active": {"value": 200, "unit": "count", "desc": "Active connections warning"},
            "db2_bufferpool_hit_ratio": {"value": 0.95, "unit": "ratio", "desc": "Bufferpool hit ratio warning"},
        },
        "optional_overrides": {
            "db2_log_usage_percent": {"value": 70, "unit": "%", "desc": "Transaction log usage warning"},
            "db2_deadlock_rate": {"value": 5, "unit": "count/s", "desc": "Deadlock rate (5m)"},
            "db2_tablespace_used_percent": {"value": 85, "unit": "%", "desc": "Tablespace usage warning"},
        },
        "dimensional_example": {
            "db2_bufferpool_hit_ratio{bufferpool_name=~\"IBMDEFAULT.*|BP_USER.*\"}": "0.90",
            "db2_tablespace_used_percent{tablespace_name=~\"USERSPACE.*\"}": "80",
        },
    },
    "clickhouse": {
        "display": "ClickHouse (built-in /metrics)",
        "exporter": "ClickHouse built-in Prometheus endpoint",
        "default_on": False,
        "rule_pack_file": "rule-packs/rule-pack-clickhouse.yaml",
        "defaults": {
            "clickhouse_queries_rate": {"value": 500, "unit": "qps", "desc": "Query rate warning (5m)"},
            "clickhouse_active_connections": {"value": 200, "unit": "count", "desc": "Active TCP connections warning"},
        },
        "optional_overrides": {
            "clickhouse_max_part_count": {"value": 300, "unit": "count", "desc": "Max part count per partition"},
            "clickhouse_replication_queue": {"value": 50, "unit": "count", "desc": "Replication queue size"},
            "clickhouse_memory_tracking_bytes": {"value": 8589934592, "unit": "bytes (8GB)", "desc": "Memory tracking warning"},
        },
        "dimensional_example": {
            "clickhouse_max_part_count{database=~\"prod_.*\"}": "200",
        },
    },
    "kafka": {
        "display": "Apache Kafka (danielqsj/kafka-exporter)",
        "exporter": "danielqsj/kafka-exporter",
        "default_on": False,
        "rule_pack_file": "rule-packs/rule-pack-kafka.yaml",
        "defaults": {
            "kafka_consumer_lag": {"value": 1000, "unit": "messages", "desc": "Consumer lag warning"},
            "kafka_under_replicated_partitions": {"value": 0, "unit": "count", "desc": "Under-replicated partitions (should be 0)"},
            "kafka_broker_count": {"value": 3, "unit": "count", "desc": "Minimum broker count warning"},
            "kafka_active_controllers": {"value": 1, "unit": "count", "desc": "Minimum active controllers (should be 1)"},
            "kafka_request_rate": {"value": 10000, "unit": "msg/s", "desc": "Message rate warning (5m)"},
        },
        "optional_overrides": {
            "kafka_consumer_lag_critical": {"value": 10000, "unit": "messages", "desc": "Consumer lag critical"},
            "kafka_under_replicated_partitions_critical": {"value": 1, "unit": "count", "desc": "Under-replicated critical"},
            "kafka_request_rate_critical": {"value": 50000, "unit": "msg/s", "desc": "Message rate critical"},
        },
        "dimensional_example": {
            "kafka_consumer_lag{group=\"order-processing\"}": "5000",
            "kafka_consumer_lag{group=\"analytics\"}": "50000",
        },
    },
    "rabbitmq": {
        "display": "RabbitMQ (kbudde/rabbitmq_exporter)",
        "exporter": "kbudde/rabbitmq_exporter or built-in Prometheus plugin",
        "default_on": False,
        "rule_pack_file": "rule-packs/rule-pack-rabbitmq.yaml",
        "defaults": {
            "rabbitmq_queue_messages": {"value": 100000, "unit": "messages", "desc": "Queue depth warning"},
            "rabbitmq_node_mem_percent": {"value": 80, "unit": "%", "desc": "Node memory usage warning"},
            "rabbitmq_connections": {"value": 1000, "unit": "count", "desc": "Connection count warning"},
            "rabbitmq_consumers": {"value": 5, "unit": "count", "desc": "Minimum consumer count warning"},
            "rabbitmq_unacked_messages": {"value": 10000, "unit": "messages", "desc": "Unacknowledged messages warning"},
        },
        "optional_overrides": {
            "rabbitmq_queue_messages_critical": {"value": 500000, "unit": "messages", "desc": "Queue depth critical"},
            "rabbitmq_node_mem_percent_critical": {"value": 95, "unit": "%", "desc": "Node memory critical"},
        },
        "dimensional_example": {
            "rabbitmq_queue_messages{queue=\"payment-events\"}": "50000",
            "rabbitmq_queue_messages{queue=\"bulk-processing\"}": "500000",
        },
    },
    # v1.12.0: JVM Rule Pack
    "jvm": {
        "display": "JVM (jmx_exporter / Micrometer)",
        "exporter": "prometheus/jmx_exporter or Spring Boot Actuator",
        "default_on": False,
        "rule_pack_file": "rule-packs/rule-pack-jvm.yaml",
        "defaults": {
            "jvm_gc_pause": {"value": 0.5, "unit": "seconds/5m", "desc": "GC pause duration rate warning"},
            "jvm_memory": {"value": 80, "unit": "%", "desc": "Heap memory usage warning"},
            "jvm_threads": {"value": 500, "unit": "count", "desc": "Active thread count warning"},
        },
        "optional_overrides": {
            "jvm_gc_pause_critical": {"value": 1.0, "unit": "seconds/5m", "desc": "GC pause critical"},
            "jvm_memory_critical": {"value": 95, "unit": "%", "desc": "Heap memory critical"},
            "jvm_threads_critical": {"value": 800, "unit": "count", "desc": "Thread count critical"},
        },
    },
    # v1.12.0: Nginx Rule Pack
    "nginx": {
        "display": "Nginx (nginx-prometheus-exporter)",
        "exporter": "nginxinc/nginx-prometheus-exporter",
        "default_on": False,
        "rule_pack_file": "rule-packs/rule-pack-nginx.yaml",
        "defaults": {
            "nginx_connections": {"value": 1000, "unit": "count", "desc": "Active connections warning"},
            "nginx_request_rate": {"value": 5000, "unit": "req/s", "desc": "Request rate warning"},
            "nginx_waiting": {"value": 200, "unit": "count", "desc": "Waiting connections (backlog) warning"},
        },
        "optional_overrides": {
            "nginx_connections_critical": {"value": 2000, "unit": "count", "desc": "Active connections critical"},
            "nginx_request_rate_critical": {"value": 10000, "unit": "req/s", "desc": "Request rate critical"},
            "nginx_waiting_critical": {"value": 500, "unit": "count", "desc": "Waiting connections critical"},
        },
    },
}


def prompt_choice(question, options, default=None):
    """Interactive single-choice prompt."""
    print(f"\n{question}")
    for i, (key, label) in enumerate(options, 1):
        marker = " (default)" if key == default else ""
        print(f"  {i}. {label}{marker}")
    while True:
        raw = input(f"選擇 [1-{len(options)}]: ").strip()
        if not raw and default:
            return default
        try:
            idx = int(raw)
            if 1 <= idx <= len(options):
                return options[idx - 1][0]
        except ValueError:
            pass
        print("  無效選擇，請重試。")


def prompt_multi(question, options):
    """Interactive multi-choice prompt. Returns list of selected keys."""
    print(f"\n{question}")
    for i, (key, label) in enumerate(options, 1):
        print(f"  {i}. {label}")
    print(f"  輸入數字 (逗號分隔，例如 1,3)，或 'all' 全選:")
    while True:
        raw = input("選擇: ").strip()
        if raw.lower() == "all":
            return [k for k, _ in options]
        try:
            indices = [int(x.strip()) for x in raw.split(",")]
            if all(1 <= i <= len(options) for i in indices):
                return [options[i - 1][0] for i in indices]
        except ValueError:
            pass
        print("  無效選擇，請重試。")


def prompt_value(metric, info, current=None):
    """Prompt for a threshold value. Returns string or None to skip."""
    default_val = current if current else info["value"]
    raw = input(f"  {metric} [{info['desc']}] ({default_val} {info['unit']}): ").strip()
    if not raw:
        return str(default_val)
    if raw.lower() in ("skip", "disable"):
        return raw if raw.lower() == "disable" else None
    return raw


def generate_profile(profile_name, selected_dbs, tier="prod"):
    """Generate a _profiles.yaml skeleton with metric defaults for selected DBs.

    Args:
        profile_name: Profile name (e.g., "standard-mariadb-prod")
        selected_dbs: List of DB type keys (e.g., ["kubernetes", "mariadb"])
        tier: Environment tier hint for threshold adjustment
              ("prod" = conservative, "staging" = relaxed)

    Returns:
        dict: {"profiles": {profile_name: {metric_key: value, ...}}}
    """
    profile = {}
    tier_factor = 1.0 if tier == "prod" else 1.2  # staging: 20% more relaxed

    for db in selected_dbs:
        pack = RULE_PACKS.get(db)
        if not pack:
            continue
        for key, info in pack.get("defaults", {}).items():
            value = info["value"]
            if isinstance(value, (int, float)) and tier_factor != 1.0:
                value = type(info["value"])(value * tier_factor)
            profile[key] = value

        # Include optional overrides (critical tiers) as commented hints
        # By placing them with actual values, the profile becomes a
        # complete "standard" baseline that tenants only override selectively
        for key, info in pack.get("optional_overrides", {}).items():
            profile[key] = info["value"]

    return {"profiles": {profile_name: profile}}


def generate_defaults(selected_dbs):
    """Generate _defaults.yaml content."""
    defaults = {}
    state_filters = {}

    # Always include kubernetes defaults
    k8s = RULE_PACKS["kubernetes"]
    for key, info in k8s["defaults"].items():
        defaults[key] = info["value"]
    state_filters = {
        k: {kk: vv for kk, vv in v.items() if kk != "desc"}
        for k, v in k8s["state_filters"].items()
    }

    # Add DB-specific defaults
    for db in selected_dbs:
        pack = RULE_PACKS.get(db)
        if pack and "defaults" in pack:
            for key, info in pack["defaults"].items():
                defaults[key] = info["value"]

    return {"defaults": defaults, "state_filters": state_filters}


def generate_tenant(tenant_name, selected_dbs, interactive=False):
    """Generate tenant YAML content."""
    tenant_config = {}

    for db in selected_dbs:
        pack = RULE_PACKS.get(db)
        if not pack:
            continue

        # Add default metric overrides
        for key, info in pack.get("defaults", {}).items():
            if interactive:
                val = prompt_value(key, info)
                if val and val != "skip":
                    tenant_config[key] = val
            # Non-interactive: skip defaults (will inherit from _defaults.yaml)

        # Add optional overrides
        for key, info in pack.get("optional_overrides", {}).items():
            if interactive:
                val = prompt_value(key, info)
                if val and val != "skip":
                    tenant_config[key] = val

    # Always add maintenance state control (disabled by default)
    # v1.7.0: supports structured format with expires for auto-deactivation
    if interactive:
        enable_maint = input("\n  啟用維護模式? (y/N): ").strip().lower()
        if enable_maint == "y":
            expires_str = input("  設定到期時間 (ISO 8601, 如 2026-04-01T00:00:00Z, 空白=無期限): ").strip()
            if expires_str:
                reason_str = input("  原因 (選填): ").strip()
                maint_obj = {"target": "enable", "expires": expires_str}
                if reason_str:
                    maint_obj["reason"] = reason_str
                tenant_config["_state_maintenance"] = maint_obj
            else:
                tenant_config["_state_maintenance"] = "enable"

    # Silent mode: alerts fire (TSDB records) but notifications suppressed
    # v1.7.0: supports structured format with expires for auto-deactivation
    if interactive:
        print("\n  靜音模式 (Silent Mode):")
        print("    1. Normal — 不靜音 (預設)")
        print("    2. Warning — 只靜音 warning 通知")
        print("    3. Critical — 只靜音 critical 通知")
        print("    4. All — 靜音所有通知")
        silent_choice = input("  選擇 [1-4] (預設 1): ").strip()
        silent_map = {"2": "warning", "3": "critical", "4": "all"}
        if silent_choice in silent_map:
            expires_str = input("  設定到期時間 (ISO 8601, 如 2026-04-01T00:00:00Z, 空白=無期限): ").strip()
            if expires_str:
                reason_str = input("  原因 (選填): ").strip()
                silent_obj = {"target": silent_map[silent_choice], "expires": expires_str}
                if reason_str:
                    silent_obj["reason"] = reason_str
                tenant_config["_silent_mode"] = silent_obj
            else:
                tenant_config["_silent_mode"] = silent_map[silent_choice]

    # Severity dedup: control warning↔critical notification deduplication
    if interactive:
        print("\n  嚴重度去重 (Severity Dedup):")
        print("    1. Enable — critical 觸發時壓制 warning 通知 (預設)")
        print("    2. Disable — warning 和 critical 通知都發送")
        dedup_choice = input("  選擇 [1-2] (預設 1): ").strip()
        if dedup_choice == "2":
            tenant_config["_severity_dedup"] = "disable"

    # Alert routing: tenant-managed notification destination
    if interactive:
        print("\n  告警路由 (Alert Routing):")
        print("    設定通知目的地，空白跳過使用平台預設")
        print("    支援類型: webhook | email | slack | teams | rocketchat | pagerduty")
        receiver_type = input("  Receiver type (預設 webhook): ").strip().lower() or "webhook"
        receiver_obj = None
        if receiver_type == "webhook":
            url = input("  Webhook URL: ").strip()
            if url:
                receiver_obj = {"type": "webhook", "url": url}
        elif receiver_type == "email":
            to = input("  Email to (逗號分隔): ").strip()
            smarthost = input("  SMTP smarthost (例如 smtp.example.com:587): ").strip()
            if to and smarthost:
                receiver_obj = {"type": "email", "to": [t.strip() for t in to.split(",")],
                                "smarthost": smarthost}
        elif receiver_type == "slack":
            api_url = input("  Slack API URL: ").strip()
            if api_url:
                receiver_obj = {"type": "slack", "api_url": api_url}
                channel = input("  Channel (例如 #alerts，選填): ").strip()
                if channel:
                    receiver_obj["channel"] = channel
        elif receiver_type == "teams":
            webhook_url = input("  Teams Webhook URL: ").strip()
            if webhook_url:
                receiver_obj = {"type": "teams", "webhook_url": webhook_url}
        elif receiver_type == "rocketchat":
            url = input("  Rocket.Chat Webhook URL: ").strip()
            if url:
                receiver_obj = {"type": "rocketchat", "url": url}
        elif receiver_type == "pagerduty":
            service_key = input("  PagerDuty Service Key: ").strip()
            if service_key:
                receiver_obj = {"type": "pagerduty", "service_key": service_key}
        else:
            print(f"  WARN: unknown type '{receiver_type}', skipping routing")

        if receiver_obj:
            routing = {"receiver": receiver_obj}

            group_by = input("  Group by labels (逗號分隔，預設 alertname,tenant): ").strip()
            routing["group_by"] = ([g.strip() for g in group_by.split(",")]
                                   if group_by else ["alertname", "tenant"])
            routing["group_wait"] = input("  Group wait (預設 30s，範圍 5s-5m): ").strip() or "30s"
            routing["group_interval"] = input("  Group interval (預設 5m，範圍 5s-5m): ").strip() or "5m"
            routing["repeat_interval"] = input("  Repeat interval (預設 4h，範圍 1m-72h): ").strip() or "4h"

            tenant_config["_routing"] = routing

    return {"tenants": {tenant_name: tenant_config}} if tenant_config else {"tenants": {tenant_name: {}}}


def generate_report(tenant_name, selected_dbs, output_dir, namespaces=None):
    """Generate scaffold report with deployment instructions."""
    lines = [
        f"# Scaffold Report — {tenant_name}",
        f"# Generated by scaffold_tenant.py",
        "",
        "## 生成檔案",
        f"  - {output_dir}/{tenant_name}.yaml (tenant 閾值設定)",
        f"  - {output_dir}/_defaults.yaml (平台預設值)",
    ]

    if namespaces:
        lines.append(f"  - {output_dir}/relabel_configs-{tenant_name}.yaml (Prometheus relabel snippet)")

    lines.extend([
        "",
        "## Rule Packs (已預載於平台)",
        "  所有核心 Rule Packs (包含自我監控) 已透過 Projected Volume 預載於平台中。",
        "  未部署 exporter 的 pack 不會產生 metrics，alert 不會誤觸發。",
        "",
    ])

    for db in selected_dbs:
        pack = RULE_PACKS.get(db)
        if pack:
            lines.append(f"  ✅ {pack['display']} — 已預載")

    # Helm deployment command (no rule pack overlays needed)
    lines.extend(["", "## 部署指令", ""])

    lines.append("```bash")
    lines.append("# 部署/更新 threshold-exporter (Rule Packs 已內建，無需額外 -f)")
    lines.append("helm upgrade --install threshold-exporter ./components/threshold-exporter \\")
    lines.append("  -n monitoring \\")
    lines.append("  -f environments/local/threshold-exporter.yaml")
    lines.append("```")

    # Prometheus relabel_configs (if namespaces provided)
    if namespaces:
        lines.extend([
            "",
            "## Prometheus N:1 Tenant Mapping",
            "",
            f"Relabel configs have been generated for namespace mapping: {namespaces}",
            "",
            "```bash",
            "# Add to your Prometheus scrape_configs[] section:",
            f"cat {output_dir}/relabel_configs-{tenant_name}.yaml",
            "```",
            "",
            "Then apply to scrape_configs in your Prometheus configuration.",
        ])

    # ConfigMap patching
    lines.extend([
        "",
        "## 掛載 Tenant Config",
        "",
        "```bash",
        "# 方法 1: 直接複製到 conf.d/",
        f"cp {output_dir}/{tenant_name}.yaml components/threshold-exporter/config/conf.d/",
        "",
        "# 方法 2: 用 patch_config.py 動態更新",
        f"python3 scripts/tools/patch_config.py {tenant_name} <metric_key> <value>",
        "```",
        "",
        "## 驗證",
        "",
        "```bash",
        f"python3 scripts/tools/diagnose.py {tenant_name}",
        f"python3 scripts/tools/check_alert.py MariaDBHighConnections {tenant_name}",
        "```",
    ])

    return "\n".join(lines)


def write_outputs(output_dir, tenant_name, defaults_data, tenant_data, report, relabel_snippet=None):
    """Write all output files."""
    os.makedirs(output_dir, exist_ok=True)

    # Write _defaults.yaml
    defaults_path = os.path.join(output_dir, "_defaults.yaml")
    with open(defaults_path, "w", encoding="utf-8") as f:
        f.write("# _defaults.yaml — Platform-managed global settings\n")
        f.write("# Generated by scaffold_tenant.py\n")
        yaml.safe_dump(defaults_data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    os.chmod(defaults_path, 0o600)
    print(f"  📄 {defaults_path}")

    # Write tenant yaml
    tenant_path = os.path.join(output_dir, f"{tenant_name}.yaml")
    with open(tenant_path, "w", encoding="utf-8") as f:
        f.write(f"# {tenant_name}.yaml — Tenant-managed thresholds\n")
        f.write("# Generated by scaffold_tenant.py\n")
        f.write("# 三態: 數值=Custom, 省略=Default, \"disable\"=停用\n")
        yaml.safe_dump(tenant_data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    os.chmod(tenant_path, 0o600)
    print(f"  📄 {tenant_path}")

    # Write relabel_configs if provided
    if relabel_snippet:
        relabel_path = os.path.join(output_dir, f"relabel_configs-{tenant_name}.yaml")
        with open(relabel_path, "w", encoding="utf-8") as f:
            f.write(relabel_snippet)
        os.chmod(relabel_path, 0o600)
        print(f"  📄 {relabel_path}")

    # Write report
    report_path = os.path.join(output_dir, "scaffold-report.txt")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report)
    os.chmod(report_path, 0o600)
    print(f"  📄 {report_path}")


def print_catalog():
    """Print the supported exporter catalog."""
    print("\n╔══════════════════════════════════════════════════════════════╗")
    print("║          Dynamic Alerting — Supported Exporters            ║")
    print("╠══════════════════════════════════════════════════════════════╣")
    for key, pack in RULE_PACKS.items():
        metrics = ", ".join(pack.get("defaults", {}).keys())
        print(f"║ [預載] {pack['display']:<45}║")
        print(f"║   Exporter: {pack['exporter']:<44}║")
        print(f"║   Metrics:  {metrics:<44}║")
        print(f"║   Rule Pack: {pack['rule_pack_file']:<43}║")
        print("╠══════════════════════════════════════════════════════════════╣")
    print("╚══════════════════════════════════════════════════════════════╝")


def generate_relabel_snippet(tenant, namespaces, tenant_label="tenant"):
    """
    Generate Prometheus relabel_configs snippet for N:1 tenant mapping.

    Args:
        tenant: Tenant name
        namespaces: List of K8s namespace names (or comma-separated string)
        tenant_label: Label name to set (default: "tenant")

    Returns:
        YAML string with relabel_configs section
    """
    # Parse namespaces if string
    if isinstance(namespaces, str):
        ns_list = [ns.strip() for ns in namespaces.split(",") if ns.strip()]
    else:
        ns_list = list(namespaces)

    if not ns_list:
        return ""

    # Build regex from namespace list
    ns_regex = "|".join(ns_list)

    relabel_config = {
        "relabel_configs": [
            {
                "source_labels": ["__meta_kubernetes_namespace"],
                "action": "keep",
                "regex": ns_regex,
            },
            {
                "source_labels": ["__meta_kubernetes_namespace"],
                "regex": ns_regex,
                "target_label": tenant_label,
                "replacement": tenant,
            },
        ]
    }

    # Generate YAML with comment header
    yaml_lines = [
        "# Prometheus relabel_configs for N:1 tenant mapping",
        "# Add to scrape_configs[].relabel_configs",
        "",
    ]
    yaml_lines.append(yaml.dump(relabel_config, default_flow_style=False, allow_unicode=True, sort_keys=False).rstrip())

    return "\n".join(yaml_lines)


def run_interactive(output_dir):
    """Full interactive mode."""
    print("=" * 60)
    print("  scaffold_tenant.py — 互動式 Tenant Config 產生器")
    print("=" * 60)

    # Step 1: Tenant name
    tenant_name = input("\n📛 Tenant namespace (例如 db-c): ").strip()
    if not tenant_name:
        print("錯誤: Tenant name 不可為空")
        sys.exit(1)

    # Step 2: Select DB types
    db_options = [(k, v["display"]) for k, v in RULE_PACKS.items() if k != "kubernetes"]
    selected_dbs = prompt_multi(
        "📦 選擇要監控的 DB 類型:",
        db_options,
    )
    # Always include kubernetes
    selected_dbs = ["kubernetes"] + selected_dbs

    print(f"\n已選擇: {', '.join(selected_dbs)}")

    # Step 3: Configure thresholds
    customize = input("\n🔧 自訂閾值? (y=逐項設定 / N=使用預設值): ").strip().lower()
    interactive_thresholds = customize == "y"

    # Step 4: N:1 tenant mapping (optional)
    namespaces = None
    ns_input = input("\n📍 K8s 命名空間 (逗號分隔，例如 ns1,ns2,ns3，空白跳過): ").strip()
    if ns_input:
        namespaces = ns_input

    # Step 5: Profile assignment (optional, v1.12.0)
    profile_name = None
    profile_input = input("\n🏷️  使用 Tenant Profile? (輸入 profile 名稱，空白跳過): ").strip()
    if profile_input:
        profile_name = profile_input

    # Generate
    print("\n⚙️  正在生成...")
    defaults_data = generate_defaults(selected_dbs)
    tenant_data = generate_tenant(tenant_name, selected_dbs, interactive=interactive_thresholds)

    # Add _profile if specified (v1.12.0)
    if profile_name:
        tenant_data["tenants"][tenant_name]["_profile"] = profile_name

    # Add _namespaces metadata if provided
    if namespaces:
        tenant_data["tenants"][tenant_name]["_namespaces"] = [ns.strip() for ns in namespaces.split(",")]

    relabel_snippet = generate_relabel_snippet(tenant_name, namespaces) if namespaces else None
    report = generate_report(tenant_name, selected_dbs, output_dir, namespaces=namespaces)

    # Write
    print(f"\n📁 輸出至 {output_dir}/")
    write_outputs(output_dir, tenant_name, defaults_data, tenant_data, report, relabel_snippet=relabel_snippet)

    # Summary
    print("\n" + "=" * 60)
    print("✅ Tenant config 生成完畢！")
    print("=" * 60)

    print("\n  所有核心 Rule Packs (包含自我監控) 已透過 Projected Volume 預載於平台，無需額外掛載。")
    print(f"\n詳見 {output_dir}/scaffold-report.txt")


def run_non_interactive(args):
    """Non-interactive mode with CLI args."""
    tenant_name = args.tenant
    selected_dbs = ["kubernetes"] + [db.strip() for db in args.db.split(",")]
    output_dir = args.output_dir

    # Validate DB choices
    for db in selected_dbs:
        if db not in RULE_PACKS:
            print(f"錯誤: 不支援的 DB 類型 '{db}'")
            print(f"支援的類型: {', '.join(RULE_PACKS.keys())}")
            sys.exit(1)

    print(f"⚙️  生成 {tenant_name} config (DBs: {', '.join(selected_dbs)})...")
    defaults_data = generate_defaults(selected_dbs)
    tenant_data = generate_tenant(tenant_name, selected_dbs, interactive=False)

    # Apply --profile if specified (v1.12.0)
    profile_name = getattr(args, "profile", None)
    if profile_name:
        tenant_data["tenants"][tenant_name]["_profile"] = profile_name

    # Apply --silent-mode if specified
    silent_mode = getattr(args, "silent_mode", None)
    if silent_mode and silent_mode != "disable":
        tenant_data["tenants"][tenant_name]["_silent_mode"] = silent_mode

    # Apply --severity-dedup if specified (only write if not default)
    severity_dedup = getattr(args, "severity_dedup", "enable")
    if severity_dedup == "disable":
        tenant_data["tenants"][tenant_name]["_severity_dedup"] = "disable"

    # Apply --routing-receiver if specified
    # Platform defaults are applied for timing params when not explicitly provided,
    # ensuring generated config is complete for generate_alertmanager_routes.py
    routing_receiver = getattr(args, "routing_receiver", None)
    if routing_receiver:
        receiver_type = getattr(args, "routing_receiver_type", "webhook")
        receiver_obj = {"type": receiver_type}
        if receiver_type == "webhook":
            receiver_obj["url"] = routing_receiver
        elif receiver_type == "email":
            receiver_obj["to"] = [t.strip() for t in routing_receiver.split(",")]
            receiver_obj["smarthost"] = getattr(args, "routing_smarthost", None) or "localhost:25"
        elif receiver_type == "slack":
            receiver_obj["api_url"] = routing_receiver
        elif receiver_type == "teams":
            receiver_obj["webhook_url"] = routing_receiver
        elif receiver_type == "rocketchat":
            receiver_obj["url"] = routing_receiver
        elif receiver_type == "pagerduty":
            receiver_obj["service_key"] = routing_receiver
        routing = {"receiver": receiver_obj}

        # group_by: use explicit value or platform default
        routing_group_by = getattr(args, "routing_group_by", None)
        routing["group_by"] = (
            [g.strip() for g in routing_group_by.split(",")]
            if routing_group_by
            else ["alertname", "tenant"]
        )

        # Timing params: use explicit value or platform default
        routing["group_wait"] = getattr(args, "routing_group_wait", None) or "30s"
        routing["group_interval"] = getattr(args, "routing_group_interval", None) or "5m"
        routing["repeat_interval"] = getattr(args, "routing_repeat_interval", None) or "4h"

        tenant_data["tenants"][tenant_name]["_routing"] = routing

    # Apply --namespaces if specified
    namespaces = getattr(args, "namespaces", None)
    relabel_snippet = None
    if namespaces:
        ns_list = [ns.strip() for ns in namespaces.split(",")]
        tenant_data["tenants"][tenant_name]["_namespaces"] = ns_list
        relabel_snippet = generate_relabel_snippet(tenant_name, namespaces)

    report = generate_report(tenant_name, selected_dbs, output_dir, namespaces=namespaces)

    print(f"\n📁 輸出至 {output_dir}/")
    write_outputs(output_dir, tenant_name, defaults_data, tenant_data, report, relabel_snippet=relabel_snippet)

    print("\n✅ 完成 (所有核心 Rule Packs (包含自我監控) 已透過 Projected Volume 預載於平台，無需額外掛載)")


def run_from_onboard(args):
    """Auto-scaffold tenants from onboard-hints.json.

    Reads hints produced by onboard_platform.py and generates config
    for each tenant with pre-filled DB types and routing hints.
    """
    hints = read_onboard_hints(args.from_onboard)
    if not hints:
        print(f"ERROR: Cannot read onboard hints: {args.from_onboard}", file=sys.stderr)
        sys.exit(1)

    tenants = hints.get("tenants", [])
    if not tenants:
        print("No tenants found in onboard hints.", file=sys.stderr)
        sys.exit(1)

    output_dir = args.output_dir
    print(f"Auto-scaffolding {len(tenants)} tenant(s) from onboard hints...")

    for tenant_name in tenants:
        # Resolve DB types from hints, fallback to kubernetes only
        db_list = hints.get("db_types", {}).get(tenant_name, [])
        selected_dbs = ["kubernetes"] + [db for db in db_list if db in RULE_PACKS]

        print(f"\n  {tenant_name}: DBs={', '.join(selected_dbs)}")
        defaults_data = generate_defaults(selected_dbs)
        tenant_data = generate_tenant(tenant_name, selected_dbs, interactive=False)

        # Apply routing hints if available
        routing_hint = hints.get("routing_hints", {}).get(tenant_name, {})
        if routing_hint and routing_hint.get("receiver_type"):
            # Routing hints provide structure but receivers need actual URLs
            # which onboard can extract from Alertmanager config
            routing = {}
            if routing_hint.get("group_wait"):
                routing["group_wait"] = routing_hint["group_wait"]
            if routing_hint.get("group_interval"):
                routing["group_interval"] = routing_hint["group_interval"]
            if routing_hint.get("repeat_interval"):
                routing["repeat_interval"] = routing_hint["repeat_interval"]
            if routing:
                tenant_data["tenants"][tenant_name].setdefault("_routing", {}).update(routing)

        report = generate_report(tenant_name, selected_dbs, output_dir)
        write_outputs(output_dir, tenant_name, defaults_data, tenant_data, report)

    print(f"\n  Scaffolded {len(tenants)} tenants to {output_dir}/")


def main():
    parser = argparse.ArgumentParser(
        description="Interactive tenant config generator for Dynamic Alerting",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""
            Examples:
              %(prog)s                                                      # 互動模式
              %(prog)s --catalog                                            # 顯示支援的 exporter 清單
              %(prog)s --tenant db-c --db mariadb,redis                     # 非互動模式
              %(prog)s --tenant db-c --db mariadb -o out/                   # 指定輸出目錄
              %(prog)s --tenant db-c --db mariadb --namespaces ns1,ns2,ns3  # 含 N:1 tenant mapping
        """),
    )
    parser.add_argument("--tenant", help="Tenant namespace name (e.g., db-c)")
    parser.add_argument("--db", help="Comma-separated DB types (mariadb,redis,mongodb,elasticsearch)")
    parser.add_argument("-o", "--output-dir", default="scaffold_output", help="Output directory (default: scaffold_output)")
    parser.add_argument("--catalog", action="store_true", help="顯示支援的 exporter 清單")
    parser.add_argument("--non-interactive", action="store_true", help="Skip interactive prompts (requires --tenant and --db)")
    parser.add_argument("--namespaces",
                        help="Comma-separated K8s namespace names for N:1 tenant mapping (e.g., ns1,ns2,ns3)")
    parser.add_argument("--silent-mode", choices=["warning", "critical", "all", "disable"],
                        help="Silent mode: alerts fire (TSDB records) but notifications suppressed")
    parser.add_argument("--severity-dedup", choices=["enable", "disable"], default="enable",
                        help="Severity dedup: suppress warning notification when critical fires (default: enable)")
    parser.add_argument("--routing-receiver",
                        help="Alert routing receiver address (URL or email list depending on type)")
    parser.add_argument("--routing-receiver-type", default="webhook",
                        choices=["webhook", "email", "slack", "teams", "rocketchat", "pagerduty"],
                        help="Receiver type (default: webhook)")
    parser.add_argument("--routing-smarthost",
                        help="SMTP smarthost for email receiver (e.g., smtp.example.com:587)")
    parser.add_argument("--routing-group-by",
                        help="Comma-separated group_by labels (e.g., alertname,severity)")
    parser.add_argument("--routing-group-wait",
                        help="Group wait duration (e.g., 30s, range: 5s-5m)")
    parser.add_argument("--routing-group-interval",
                        help="Group interval duration (e.g., 5m, range: 5s-5m)")
    parser.add_argument("--routing-repeat-interval",
                        help="Repeat interval duration (e.g., 4h, range: 1m-72h)")
    parser.add_argument("--profile",
                        help="Tenant profile name (e.g., standard-mariadb-prod). "
                             "When set, tenant YAML will reference the named profile "
                             "and only contain explicit overrides.")
    parser.add_argument("--from-onboard",
                        help="Path to onboard-hints.json (auto-scaffold from onboard results)")
    parser.add_argument("--generate-profile",
                        help="Generate a _profiles.yaml skeleton. Value is profile name "
                             "(e.g., standard-mariadb-prod). Requires --db.")
    parser.add_argument("--tier", choices=["prod", "staging"], default="prod",
                        help="Environment tier for profile thresholds "
                             "(prod=conservative, staging=relaxed 20%%). Default: prod")

    args = parser.parse_args()

    if args.catalog:
        print_catalog()
        sys.exit(0)

    if args.generate_profile:
        if not args.db:
            print("錯誤: --generate-profile 需要 --db 參數", file=sys.stderr)
            sys.exit(1)
        selected_dbs = ["kubernetes"] + [db.strip() for db in args.db.split(",")]
        for db in selected_dbs:
            if db not in RULE_PACKS:
                print(f"錯誤: 不支援的 DB 類型 '{db}'", file=sys.stderr)
                sys.exit(1)
        profile_data = generate_profile(args.generate_profile, selected_dbs,
                                        tier=args.tier)
        output_dir = args.output_dir
        os.makedirs(output_dir, exist_ok=True)
        profiles_path = os.path.join(output_dir, "_profiles.yaml")
        with open(profiles_path, "w", encoding="utf-8") as f:
            f.write("# _profiles.yaml — Tenant Profile definitions\n")
            f.write("# Generated by scaffold_tenant.py --generate-profile\n")
            f.write("# Four-layer inheritance: Defaults → Rule Pack → Profile → Tenant\n")
            yaml.safe_dump(profile_data, f, default_flow_style=False,
                           allow_unicode=True, sort_keys=False)
        os.chmod(profiles_path, 0o600)
        print(f"✅ Profile skeleton generated: {profiles_path}")
        print(f"   Profile: {args.generate_profile} (tier={args.tier})")
        print(f"   DBs: {', '.join(selected_dbs)}")
        print(f"   Metrics: {len(profile_data['profiles'][args.generate_profile])} keys")
        sys.exit(0)

    if args.from_onboard:
        run_from_onboard(args)
    elif args.non_interactive or (args.tenant and args.db):
        if not args.tenant or not args.db:
            print("錯誤: --non-interactive 模式需要 --tenant 和 --db 參數")
            sys.exit(1)
        run_non_interactive(args)
    else:
        run_interactive(args.output_dir)


if __name__ == "__main__":
    main()
