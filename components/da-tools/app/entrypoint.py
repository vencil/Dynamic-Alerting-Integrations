#!/usr/bin/env python3
import os

def _build_help_text(lang):
    """Build help text in the specified language.

    Args:
        lang: 'zh' (Chinese) or 'en' (English)

    Returns:
        Help text string
    """
    if lang == 'zh':
        return """da-tools — 動態告警 CLI 工具包

統一的可攜式驗證和遷移工具入口。
為平台工程師和 SRE 設計，無需克隆完整倉庫即可驗證整合。

用法:
    da-tools <command> [options]

命令 (Prometheus API — 可攜式):
    check-alert       查詢租戶的告警觸發狀態
    diagnose          租戶健康檢查 (配置 + 指標 + 告警狀態)
    batch-diagnose    多租戶並行健康報告 (轉換後)
    baseline          觀察指標並建議閾值
    validate          比較舊規則 vs 新規則 (影子監控)
    backtest          根據歷史資料回測閾值變更
    cutover           一鍵影子監控轉換 (§7.1 所有步驟)
    blind-spot        掃描叢集目標並查找未監控實例
    maintenance-scheduler  評估週期性維護並建立 AM 靜默規則
    alert-quality     警報品質評估 (震盪/閒置/延遲/壓制分析)
    alert-correlate   告警關聯分析 (時間窗口聚類 + 根因推斷)

命令 (配置生成 — 讀取租戶 YAML):
    generate-routes   租戶 YAML → Alertmanager route/receiver/inhibit 片段
    patch-config      修補 threshold-config ConfigMap (含 --diff 預覽)
    analyze-gaps      Rule Pack 間隙分析 (用於自訂規則)

命令 (文件系統 — 離線):
    migrate           轉換遺留 Prometheus 規則為動態格式
    scaffold          互動式生成租戶配置
    offboard          租戶配置預檢和移除
    deprecate         跨配置標記指標為已禁用
    lint              針對治理禁止列表驗證自訂規則
    onboard           分析現有 Alertmanager/Prometheus 配置以供遷移
    validate-config   一站式配置驗證 (YAML + schema + routing + policy)
    config-diff       GitOps PR 審查的目錄級配置差異
    drift-detect      跨叢集配置漂移偵測 (目錄級 SHA-256 比對)
    evaluate-policy   Policy-as-Code 策略評估 (宣告式 DSL)
    opa-evaluate      OPA Rego 策略評估橋接 (OPA 整合)
    cardinality-forecast  基數預測 (線性回歸趨勢分析)
    test-notification 多通道通知連通性測試 (驗證 receiver 可達性)
    threshold-recommend 閾值推薦引擎 (基於歷史 P50/P95/P99)
    explain-route     路由合併管線除錯器 (四層展開 + 設定檔擴展)
    byo-check         BYO Alertmanager 整合前檢 (端點 + 配置驗證)
    federation-check  Prometheus Federation 健康檢查
    grafana-import    Grafana Dashboard JSON 匯入
    shadow-verify     Shadow Monitoring 雙軌比對驗證
    discover-mappings 自動發現 1:N 實例-租戶映射 (掃描 exporter /metrics)
    init              在客戶 repo 初始化 Dynamic Alerting 整合骨架 (CI/CD + conf.d + Kustomize)
    config-history    配置快照與歷史追蹤 (snapshot / log / diff / show)
    gitops-check      GitOps Native Mode 就緒度驗證 (repo / local / sidecar)

命令 (Operator-Native — CRD 產生與驗證):
    operator-generate 產出 PrometheusRule / AlertmanagerConfig / ServiceMonitor CRD YAML
    operator-check    驗證 Operator CRD 部署狀態 (5 項檢查 + 診斷報告)
    migrate-to-operator ConfigMap 格式遷移至 Operator 原生 CRD (含遷移清單與預檢)

命令 (Federation — 多叢集):
    rule-pack-split   Rule Pack 分層拆分 (edge Part 1 + central Part 2+3)

全域環境變數:
    PROMETHEUS_URL    預設 Prometheus 端點 (--prometheus 的後備)
    DA_LANG           設定 CLI 語言 (zh/en，優先於 LC_ALL/LANG)"""
    else:
        return """da-tools — Dynamic Alerting CLI Toolkit

Unified entrypoint for portable verification and migration tools.
Designed for Platform Engineers and SREs to validate integrations
without cloning the full repository.

Usage:
    da-tools <command> [options]

Commands (Prometheus API — portable):
    check-alert       Query alert firing status for a tenant
    diagnose          Tenant health check (config + metric + alert status)
    batch-diagnose    Multi-tenant parallel health report (post-cutover)
    baseline          Observe metrics and recommend thresholds
    validate          Compare old vs new recording rules (Shadow Monitoring)
    backtest          Backtest threshold changes against historical data
    cutover           One-command Shadow Monitoring cutover (§7.1 all steps)
    blind-spot        Scan cluster targets and find unmonitored instances
    maintenance-scheduler  Evaluate recurring maintenance and create AM silences
    alert-quality     Alert quality scoring (noise/stale/latency/suppression)
    alert-correlate   Alert correlation analysis (time-window clustering + root cause)

Commands (Config Generation — reads tenant YAML):
    generate-routes   Tenant YAML → Alertmanager route/receiver/inhibit fragment
    patch-config      Patch threshold-config ConfigMap (with --diff preview)
    analyze-gaps      Rule Pack gap analysis for custom rules

Commands (File System — offline):
    migrate           Convert legacy Prometheus rules to dynamic format
    scaffold          Generate tenant configuration interactively
    offboard          Pre-check and remove a tenant configuration
    deprecate         Mark metrics as disabled across configs
    lint              Validate custom rules against governance deny-list
    onboard           Analyze existing Alertmanager/Prometheus configs for migration
    validate-config   One-stop config validation (YAML + schema + routing + policy)
    config-diff       Directory-level config diff for GitOps PR review
    drift-detect      Cross-cluster config drift detection (directory-level SHA-256)
    evaluate-policy   Policy-as-Code evaluation (declarative DSL)
    opa-evaluate      OPA Rego policy evaluation bridge (OPA integration)
    cardinality-forecast  Cardinality forecasting (linear regression trend)
    test-notification Multi-channel notification connectivity testing
    threshold-recommend Threshold recommendation engine (historical P50/P95/P99)
    explain-route     Routing merge pipeline debugger (four-layer expansion + profile)
    byo-check         BYO Alertmanager pre-integration check (endpoint + config)
    federation-check  Prometheus Federation health check
    grafana-import    Grafana Dashboard JSON import
    shadow-verify     Shadow Monitoring dual-rail comparison
    discover-mappings Auto-discover 1:N instance-tenant mappings (scrape exporter /metrics)
    init              Bootstrap Dynamic Alerting integration in your repo (CI/CD + conf.d + Kustomize)
    config-history    Config snapshot & history tracker (snapshot / log / diff / show)
    gitops-check      GitOps Native Mode readiness validation (repo / local / sidecar)

Commands (Operator-Native — CRD generation & validation):
    operator-generate Generate PrometheusRule / AlertmanagerConfig / ServiceMonitor CRD YAML
    operator-check    Validate Operator CRD deployment status (5 checks + diagnostic report)
    migrate-to-operator Migrate ConfigMap format to Operator native CRD (migration checklist + pre-check)

Commands (Federation — multi-cluster):
    rule-pack-split   Rule Pack stratification (edge Part 1 + central Parts 2+3)

Global environment variables:
    PROMETHEUS_URL    Default Prometheus endpoint (fallback for --prometheus)
    DA_LANG           Set CLI language (zh/en, takes precedence over LC_ALL/LANG)"""

import os
import sys
import importlib.util


def detect_cli_lang():
    """Detect CLI language from LANG/LC_ALL/DA_LANG environment variable.

    Returns 'zh' or 'en'. Default: 'en'.
    """
    for var in ('DA_LANG', 'LC_ALL', 'LANG'):
        val = os.environ.get(var, '')
        if val.startswith('zh'):
            return 'zh'
        if val.startswith('en'):
            return 'en'
    return 'en'


TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
_LANG = detect_cli_lang()

# Map subcommand names to script filenames
COMMAND_MAP = {
    # Group A: Prometheus API only (portable)
    "check-alert": "check_alert.py",
    "baseline": "baseline_discovery.py",
    "diagnose": "diagnose.py",
    "validate": "validate_migration.py",
    "batch-diagnose": "batch_diagnose.py",
    "backtest": "backtest_threshold.py",
    "cutover": "cutover_tenant.py",
    "blind-spot": "blind_spot_discovery.py",
    "maintenance-scheduler": "maintenance_scheduler.py",
    "alert-quality": "alert_quality.py",
    "alert-correlate": "alert_correlate.py",
    # Group B: Prometheus + file system (config generation)
    "generate-routes": "generate_alertmanager_routes.py",
    "drift-detect": "drift_detect.py",
    # Group C: File system only (offline)
    "migrate": "migrate_rule.py",
    "scaffold": "scaffold_tenant.py",
    "offboard": "offboard_tenant.py",
    "deprecate": "deprecate_rule.py",
    "lint": "lint_custom_rules.py",
    "onboard": "onboard_platform.py",
    "validate-config": "validate_config.py",
    "analyze-gaps": "analyze_rule_pack_gaps.py",
    "patch-config": "patch_config.py",
    "config-diff": "config_diff.py",
    "evaluate-policy": "policy_engine.py",
    "cardinality-forecast": "cardinality_forecasting.py",
    "test-notification": "notification_tester.py",
    "threshold-recommend": "threshold_recommend.py",
    "explain-route": "explain_route.py",
    "byo-check": "byo_check.py",
    "federation-check": "federation_check.py",
    "grafana-import": "grafana_import.py",
    "shadow-verify": "shadow_verify.py",
    "discover-mappings": "discover_instance_mappings.py",
    "init": "init_project.py",
    "config-history": "config_history.py",
    "gitops-check": "gitops_check.py",
    # Group D: Operator-native (CRD generation + validation)
    "operator-generate": "operator_generate.py",
    "operator-check": "operator_check.py",
    "migrate-to-operator": "migrate_to_operator.py",
    # Group E: Federation (multi-cluster)
    "rule-pack-split": "generate_rule_pack_split.py",
    # Group F: Policy (OPA/Rego)
    "opa-evaluate": "policy_opa_bridge.py",
}

# Commands that accept --prometheus flag (inject env var fallback)
PROMETHEUS_COMMANDS = {"check-alert", "baseline", "diagnose", "validate",
                       "batch-diagnose", "backtest", "cutover", "blind-spot",
                       "alert-quality", "alert-correlate",
                       "cardinality-forecast", "threshold-recommend",
                       "discover-mappings"}


def print_usage():
    """Print help message in detected language."""
    print(_build_help_text(_LANG))
    print()
    if _LANG == 'zh':
        print("範例:")
        print("  da-tools check-alert MariaDBHighConnections db-a --prometheus http://prometheus:9090")
        print("  da-tools baseline --tenant db-a --prometheus http://prometheus:9090")
        print("  da-tools validate --mapping mapping.csv --prometheus http://prometheus:9090")
        print("  da-tools migrate legacy-rules.yml --dry-run --triage")
        print("  da-tools scaffold --tenant db-c --db mariadb,redis --non-interactive")
        print("  da-tools lint /path/to/custom-rules/ --ci")
        print("  da-tools onboard --alertmanager-config alertmanager.yaml --tenant-label organization")
        print("  da-tools validate-config --config-dir conf.d/")
        print("  da-tools init --ci both --tenants db-a,db-b --rule-packs mariadb,redis")
        print()
        print("環境變數:")
        print("  PROMETHEUS_URL   Prometheus 預設端點 (未指定 --prometheus 時使用)")
        print("  DA_LANG          設定 CLI 語言 (zh/en)")
    else:
        print("Examples:")
        print("  da-tools check-alert MariaDBHighConnections db-a --prometheus http://prometheus:9090")
        print("  da-tools baseline --tenant db-a --prometheus http://prometheus:9090")
        print("  da-tools validate --mapping mapping.csv --prometheus http://prometheus:9090")
        print("  da-tools migrate legacy-rules.yml --dry-run --triage")
        print("  da-tools scaffold --tenant db-c --db mariadb,redis --non-interactive")
        print("  da-tools lint /path/to/custom-rules/ --ci")
        print("  da-tools onboard --alertmanager-config alertmanager.yaml --tenant-label organization")
        print("  da-tools validate-config --config-dir conf.d/")
        print("  da-tools init --ci both --tenants db-a,db-b --rule-packs mariadb,redis")
        print()
        print("Environment:")
        print("  PROMETHEUS_URL   Default Prometheus endpoint (used when --prometheus is omitted)")
        print("  DA_LANG          Set CLI language (zh/en)")
    sys.exit(0)


def inject_prometheus_env(args):
    """If --prometheus is not in args, inject PROMETHEUS_URL env var as default."""
    if "--prometheus" not in args:
        prom_url = os.environ.get("PROMETHEUS_URL")
        if prom_url:
            args.extend(["--prometheus", prom_url])
    return args


def run_tool(script_name, args):
    """Load and execute a tool script by rewriting sys.argv."""
    script_path = os.path.join(TOOLS_DIR, script_name)

    if not os.path.isfile(script_path):
        print(f"Error: Tool script not found: {script_path}", file=sys.stderr)
        sys.exit(1)

    # Rewrite sys.argv so argparse in each tool sees correct arguments
    sys.argv = [script_name] + args

    # Load and execute the script as __main__
    spec = importlib.util.spec_from_file_location("__main__", script_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help", "help"):
        print_usage()

    command = sys.argv[1]
    args = sys.argv[2:]

    if command == "--version":
        version_file = os.path.join(TOOLS_DIR, "VERSION")
        if os.path.isfile(version_file):
            with open(version_file, encoding="utf-8") as f:
                print(f"da-tools {f.read().strip()}")
        else:
            print("da-tools (dev)")
        sys.exit(0)

    # Handle --help for the main entrypoint
    if command in ("-h", "--help", "help"):
        print_usage()

    if command not in COMMAND_MAP:
        if _LANG == 'zh':
            print(f"錯誤: 未知命令 '{command}'", file=sys.stderr)
            print(f"可用命令: {', '.join(sorted(COMMAND_MAP.keys()))}", file=sys.stderr)
            print("執行 'da-tools --help' 以查看用法。", file=sys.stderr)
        else:
            print(f"Error: Unknown command '{command}'", file=sys.stderr)
            print(f"Available commands: {', '.join(sorted(COMMAND_MAP.keys()))}", file=sys.stderr)
            print("Run 'da-tools --help' for usage.", file=sys.stderr)
        sys.exit(1)

    # Inject PROMETHEUS_URL for applicable commands
    if command in PROMETHEUS_COMMANDS:
        args = inject_prometheus_env(args)

    run_tool(COMMAND_MAP[command], args)


if __name__ == "__main__":
    main()
