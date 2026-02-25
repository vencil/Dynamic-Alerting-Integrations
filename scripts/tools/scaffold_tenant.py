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
"""
import argparse
import os
import sys
import textwrap

import yaml

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


def generate_tenant(tenant_name, selected_dbs, overrides, interactive=False):
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
    if interactive:
        enable_maint = input("\n  啟用維護模式? (y/N): ").strip().lower()
        if enable_maint == "y":
            tenant_config["_state_maintenance"] = "enable"

    return {"tenants": {tenant_name: tenant_config}} if tenant_config else {"tenants": {tenant_name: {}}}


def generate_report(tenant_name, selected_dbs, output_dir):
    """Generate scaffold report with deployment instructions."""
    lines = [
        f"# Scaffold Report — {tenant_name}",
        f"# Generated by scaffold_tenant.py",
        "",
        "## 生成檔案",
        f"  - {output_dir}/{tenant_name}.yaml (tenant 閾值設定)",
        f"  - {output_dir}/_defaults.yaml (平台預設值)",
        "",
        "## Rule Packs (已預載於平台)",
        "  所有 5 個 Rule Pack 已預載於 Prometheus ConfigMap 中。",
        "  未部署 exporter 的 pack 不會產生 metrics，alert 不會誤觸發。",
        "",
    ]

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


def write_outputs(output_dir, tenant_name, defaults_data, tenant_data, report):
    """Write all output files."""
    os.makedirs(output_dir, exist_ok=True)

    # Write _defaults.yaml
    defaults_path = os.path.join(output_dir, "_defaults.yaml")
    with open(defaults_path, "w") as f:
        f.write("# _defaults.yaml — Platform-managed global settings\n")
        f.write("# Generated by scaffold_tenant.py\n")
        yaml.safe_dump(defaults_data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    print(f"  📄 {defaults_path}")

    # Write tenant yaml
    tenant_path = os.path.join(output_dir, f"{tenant_name}.yaml")
    with open(tenant_path, "w") as f:
        f.write(f"# {tenant_name}.yaml — Tenant-managed thresholds\n")
        f.write("# Generated by scaffold_tenant.py\n")
        f.write("# 三態: 數值=Custom, 省略=Default, \"disable\"=停用\n")
        yaml.safe_dump(tenant_data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    print(f"  📄 {tenant_path}")

    # Write report
    report_path = os.path.join(output_dir, "scaffold-report.txt")
    with open(report_path, "w") as f:
        f.write(report)
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

    # Generate
    print("\n⚙️  正在生成...")
    defaults_data = generate_defaults(selected_dbs)
    tenant_data = generate_tenant(tenant_name, selected_dbs, {}, interactive=interactive_thresholds)
    report = generate_report(tenant_name, selected_dbs, output_dir)

    # Write
    print(f"\n📁 輸出至 {output_dir}/")
    write_outputs(output_dir, tenant_name, defaults_data, tenant_data, report)

    # Summary
    print("\n" + "=" * 60)
    print("✅ Tenant config 生成完畢！")
    print("=" * 60)

    print("\n  所有 Rule Packs 已預載於 Prometheus，無需額外掛載。")
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
    tenant_data = generate_tenant(tenant_name, selected_dbs, {}, interactive=False)
    report = generate_report(tenant_name, selected_dbs, output_dir)

    print(f"\n📁 輸出至 {output_dir}/")
    write_outputs(output_dir, tenant_name, defaults_data, tenant_data, report)

    print("\n✅ 完成 (所有 Rule Packs 已預載於 Prometheus，無需額外掛載)")


def main():
    parser = argparse.ArgumentParser(
        description="Interactive tenant config generator for Dynamic Alerting",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""
            Examples:
              %(prog)s                                    # 互動模式
              %(prog)s --catalog                          # 顯示支援的 exporter 清單
              %(prog)s --tenant db-c --db mariadb,redis   # 非互動模式
              %(prog)s --tenant db-c --db mariadb -o out/ # 指定輸出目錄
        """),
    )
    parser.add_argument("--tenant", help="Tenant namespace name (e.g., db-c)")
    parser.add_argument("--db", help="Comma-separated DB types (mariadb,redis,mongodb,elasticsearch)")
    parser.add_argument("-o", "--output-dir", default="scaffold_output", help="Output directory (default: scaffold_output)")
    parser.add_argument("--catalog", action="store_true", help="顯示支援的 exporter 清單")
    parser.add_argument("--non-interactive", action="store_true", help="Skip interactive prompts (requires --tenant and --db)")

    args = parser.parse_args()

    if args.catalog:
        print_catalog()
        sys.exit(0)

    if args.non_interactive or (args.tenant and args.db):
        if not args.tenant or not args.db:
            print("錯誤: --non-interactive 模式需要 --tenant 和 --db 參數")
            sys.exit(1)
        run_non_interactive(args)
    else:
        run_interactive(args.output_dir)


if __name__ == "__main__":
    main()
