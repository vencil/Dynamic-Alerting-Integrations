---
title: "da-tools CLI Playground - command catalog + network modes"
purpose: |
  Data module extracted from cli-playground.jsx: the da-tools COMMANDS
  catalog (args/flags/preview per command) and the NETWORK_MODES presets.
  Both build user-facing labels via window.__t at module load (same
  live-global-with-fallback idiom as the rest of the portal). Split out so
  the ~300-LOC catalog no longer bloats the component render file.
---

const t = window.__t || ((zh, en) => en);

const COMMANDS = {
  'check-alert': {
    label: 'check-alert',
    description: t('查詢特定租戶的告警狀態', 'Query alert status for a specific tenant'),
    category: t('Prometheus API 工具', 'Prometheus API Tools'),
    popular: true,
    preview: `$ da-tools check-alert HighMemoryUsage db-a

Alert:    HighMemoryUsage
Tenant:   db-a
State:    firing
Severity: warning
Value:    87.3% (threshold: 80%)
Since:    2026-03-14T02:15:00Z (12m ago)
Labels:   {tenant="db-a", severity="warning", namespace="db-a"}`,
    args: [
      { name: 'alert_name', label: t('告警名稱', 'Alert Name'), required: true, placeholder: 'e.g., HighMemoryUsage' },
      { name: 'tenant', label: t('租戶 ID', 'Tenant ID'), required: true, placeholder: 'e.g., db-a' }
    ],
    flags: [
      { name: '--prometheus', label: t('Prometheus URL', 'Prometheus URL'), required: true, placeholder: 'http://localhost:9090' }
    ]
  },
  'diagnose': {
    label: 'diagnose',
    description: t('單租戶綜合健康檢查', 'Single-tenant comprehensive health check'),
    category: t('Prometheus API 工具', 'Prometheus API Tools'),
    popular: true,
    preview: `$ da-tools diagnose --tenant db-a --prometheus http://localhost:9090

╔══════════════════════════════════════╗
║  Tenant Health Report: db-a         ║
╚══════════════════════════════════════╝

Thresholds:  12 configured (3 critical)
Rule Packs:  mariadb, kubernetes (2 active)
Alerts:      1 firing, 0 pending
Routing:     webhook → https://webhook.example.com
Mode:        normal (no silence/maintenance)

✓ All recording rules producing data
✓ Threshold metrics exported (12/12)
⚠ 1 alert firing: MariaDBHighConnections (87% > 80%)`,
    args: [],
    flags: [
      { name: '--tenant', label: t('租戶 ID', 'Tenant ID'), required: true, placeholder: 'e.g., db-a' },
      { name: '--prometheus', label: t('Prometheus URL', 'Prometheus URL'), required: false, placeholder: 'http://localhost:9090' },
      { name: '--config-dir', label: t('配置目錄', 'Config Directory'), required: false, placeholder: '/etc/config' },
      { name: '--namespace', label: t('Kubernetes 命名空間', 'Kubernetes Namespace'), required: false, placeholder: 'monitoring' }
    ]
  },
  'batch-diagnose': {
    label: 'batch-diagnose',
    description: t('並行檢查所有租戶的健康狀況', 'Parallel health check for all tenants'),
    category: t('Prometheus API 工具', 'Prometheus API Tools'),
    args: [],
    flags: [
      { name: '--prometheus', label: t('Prometheus URL', 'Prometheus URL'), required: true, placeholder: 'http://localhost:9090' },
      { name: '--tenants', label: t('租戶列表', 'Tenant List'), required: false, placeholder: 'db-a,db-b,cache' },
      { name: '--workers', label: t('工作線程', 'Worker Threads'), required: false, placeholder: '4' }
    ]
  },
  'baseline': {
    label: 'baseline',
    description: t('觀察指標、計算統計資訊 (p50/p90/p95/p99/max)、建議閾值', 'Observe metrics, calculate stats (p50/p90/p95/p99/max), suggest thresholds'),
    category: t('Prometheus API 工具', 'Prometheus API Tools'),
    args: [],
    flags: [
      { name: '--tenant', label: t('租戶 ID', 'Tenant ID'), required: true, placeholder: 'e.g., db-a' },
      { name: '--prometheus', label: t('Prometheus URL', 'Prometheus URL'), required: true, placeholder: 'http://localhost:9090' },
      { name: '--duration', label: t('觀察時間長度 (秒)', 'Observation Duration (s)'), required: false, placeholder: '3600' }
    ]
  },
  'validate': {
    label: 'validate',
    description: t('影子監控驗證：比較舊記錄規則與新記錄規則', 'Shadow Monitoring validation: compare old vs new recording rules'),
    category: t('Prometheus API 工具', 'Prometheus API Tools'),
    args: [],
    flags: [
      { name: '--prometheus', label: t('Prometheus URL', 'Prometheus URL'), required: true, placeholder: 'http://localhost:9090' },
      { name: '--watch', label: t('監視模式', 'Watch Mode'), required: false, type: 'checkbox' },
      { name: '--interval', label: t('檢查間隔 (秒)', 'Check Interval (s)'), required: false, placeholder: '60' }
    ]
  },
  'cutover': {
    label: 'cutover',
    description: t('影子監控一鍵切換：禁用舊規則、啟用新規則、驗證健康狀況', 'Shadow Monitoring one-click switch: disable old, enable new, verify health'),
    category: t('Prometheus API 工具', 'Prometheus API Tools'),
    args: [],
    flags: [
      { name: '--tenant', label: t('租戶 ID', 'Tenant ID'), required: true, placeholder: 'e.g., db-a' },
      { name: '--prometheus', label: t('Prometheus URL', 'Prometheus URL'), required: true, placeholder: 'http://localhost:9090' },
      { name: '--readiness-json', label: t('就緒 JSON 文件', 'Readiness JSON File'), required: false, placeholder: '/tmp/readiness.json' }
    ]
  },
  'scaffold': {
    label: 'scaffold',
    description: t('生成新租戶配置', 'Generate new tenant configuration'),
    category: t('文件系統工具', 'Filesystem Tools'),
    popular: true,
    preview: `$ da-tools scaffold --tenant my-app --db mariadb,redis

Scaffolding tenant configuration...

Created:
  conf.d/my-app.yaml       (MariaDB + Redis thresholds)
  conf.d/_defaults.yaml     (updated with new tenant)

Summary:
  Tenant:     my-app
  Databases:  mariadb (8 thresholds), redis (6 thresholds)
  Routing:    not configured (add _routing to enable)

Next steps:
  1. Edit conf.d/my-app.yaml to customize thresholds
  2. Run: da-tools validate-config --config-dir conf.d/
  3. Deploy: kubectl apply -k .`,
    args: [],
    flags: [
      { name: '--non-interactive', label: t('非互動模式', 'Non-Interactive Mode'), required: false, type: 'checkbox' },
      { name: '--tenant', label: t('租戶 ID', 'Tenant ID'), required: false, placeholder: 'e.g., db-c' },
      { name: '--db', label: t('數據庫類型', 'Database Type'), required: false, placeholder: 'mysql,postgres,redis' }
    ]
  },
  'migrate': {
    label: 'migrate',
    description: t('將傳統 Prometheus 規則轉換為動態格式', 'Convert traditional Prometheus rules to dynamic format'),
    category: t('文件系統工具', 'Filesystem Tools'),
    args: [
      { name: 'input', label: t('輸入規則文件', 'Input Rules File'), required: true, placeholder: '/path/to/rules.yaml' }
    ],
    flags: [
      { name: '--output', label: t('輸出目錄', 'Output Directory'), required: false, placeholder: '/tmp/migrated' },
      { name: '--dry-run', label: t('試運行', 'Dry Run'), required: false, type: 'checkbox' },
      { name: '--triage', label: t('分類模式', 'Triage Mode'), required: false, type: 'checkbox' }
    ]
  },
  'validate-config': {
    label: 'validate-config',
    description: t('一站式配置驗證：YAML + schema + routing + policy + versions', 'One-stop config validation: YAML + schema + routing + policy + versions'),
    category: t('文件系統工具', 'Filesystem Tools'),
    popular: true,
    preview: `$ da-tools validate-config --config-dir conf.d/ --ci

Running validation suite...

[✓] YAML syntax          3/3 files valid
[✓] Schema validation    2 tenants, 0 unknown keys
[✓] Threshold format     18 thresholds, all numeric strings
[✓] Routing validation   2 receivers configured
[✓] Duration guardrails  group_wait, repeat_interval in range
[✓] Version consistency  v2.7.0

All checks passed (6/6). Exit code: 0`,
    args: [],
    flags: [
      { name: '--config-dir', label: t('配置目錄', 'Config Directory'), required: true, placeholder: '/etc/config' },
      { name: '--policy', label: t('Webhook 域政策', 'Webhook Domain Policy'), required: false, placeholder: '*.example.com' },
      { name: '--ci', label: t('CI 模式 (exit codes)', 'CI Mode (exit codes)'), required: false, type: 'checkbox' }
    ]
  },
  'generate-routes': {
    label: 'generate-routes',
    description: t('從租戶 YAML 生成 Alertmanager routes + receivers + inhibit', 'Generate Alertmanager routes + receivers + inhibit from tenant YAML'),
    category: t('配置生成', 'Configuration Generation'),
    args: [],
    flags: [
      { name: '--config-dir', label: t('配置目錄', 'Config Directory'), required: true, placeholder: '/etc/config' },
      { name: '--output', label: t('輸出文件', 'Output File'), required: false, placeholder: '/tmp/routes.yaml' },
      { name: '--output-configmap', label: t('輸出為 ConfigMap', 'Output as ConfigMap'), required: false, type: 'checkbox' }
    ]
  },
  'patch-config': {
    label: 'patch-config',
    description: t('部分更新 ConfigMap，包含預覽 (--diff) 和應用', 'Partial ConfigMap update with preview (--diff) and apply'),
    category: t('配置生成', 'Configuration Generation'),
    args: [],
    flags: [
      { name: '--namespace', label: t('Kubernetes 命名空間', 'Kubernetes Namespace'), required: true, placeholder: 'monitoring' },
      { name: '--configmap', label: t('ConfigMap 名稱', 'ConfigMap Name'), required: true, placeholder: 'alertmanager-config' },
      { name: '--dry-run', label: t('試運行 / 差異預覽', 'Dry Run / Diff Preview'), required: false, type: 'checkbox' }
    ]
  },
  'explain-route': {
    label: 'explain-route',
    description: t('路由合併管線除錯器：四層展開 + 設定檔擴展 (ADR-007)', 'Routing merge pipeline debugger: four-layer expansion + profile (ADR-007)'),
    category: t('文件系統工具', 'Filesystem Tools'),
    popular: true,
    preview: `$ da-tools explain-route --config-dir conf.d/ --tenant db-a

Tenant: db-a
  Layer 1 (_routing_defaults): group_wait=30s, repeat_interval=4h
  Layer 2 (routing_profiles → "standard-webhook"): receiver_type=webhook
  Layer 3 (tenant _routing): webhook_url=https://hooks.example.com/db-a
  Layer 4 (_routing_enforced): noc_webhook_url=https://noc.example.com

  Final: webhook → https://hooks.example.com/db-a (+ NOC copy)`,
    args: [],
    flags: [
      { name: '--config-dir', label: t('配置目錄', 'Config Directory'), required: true, placeholder: 'conf.d/' },
      { name: '--tenant', label: t('租戶 ID（可多次指定）', 'Tenant ID (repeatable)'), required: false, placeholder: 'e.g., db-a' },
      { name: '--show-profile-expansion', label: t('顯示設定檔展開', 'Show Profile Expansion'), required: false, type: 'checkbox' },
      { name: '--trace', label: t('五步路由追蹤模擬', 'Five-step route tracing simulation'), required: false, type: 'checkbox' },
      { name: '--alertname', label: t('追蹤用告警名稱', 'Alert name for trace'), required: false, placeholder: 'HighMemoryUsage' },
      { name: '--severity', label: t('追蹤用嚴重度', 'Severity for trace'), required: false, placeholder: 'warning' },
      { name: '--json', label: t('JSON 輸出', 'JSON Output'), required: false, type: 'checkbox' }
    ]
  },
  'discover-mappings': {
    label: 'discover-mappings',
    description: t('自動發現 1:N 實例-租戶映射 (ADR-006)', 'Auto-discover 1:N instance-tenant mappings (ADR-006)'),
    category: t('Prometheus API 工具', 'Prometheus API Tools'),
    preview: `$ da-tools discover-mappings --endpoint http://mariadb-exporter:9104/metrics

Scraping http://mariadb-exporter:9104/metrics ...

Detected DB type: mariadb
Partition labels found:
  schema       12 values  (score: 0.85)  ★ recommended
  tablespace    4 values  (score: 0.62)

Mapping draft (YAML):
  instance: mariadb-exporter:9104
  db_type: mariadb
  partition_label: schema
  partitions:
    - app_db
    - user_db
    - analytics_db
    ...`,
    args: [],
    flags: [
      { name: '--endpoint', label: t('Exporter /metrics URL', 'Exporter /metrics URL'), required: false, placeholder: 'http://exporter:9104/metrics' },
      { name: '--prometheus', label: t('Prometheus API URL', 'Prometheus API URL'), required: false, placeholder: 'http://localhost:9090' },
      { name: '--instance', label: t('Instance 標籤', 'Instance Label'), required: false, placeholder: 'exporter:9104' },
      { name: '--job', label: t('Job 標籤', 'Job Label'), required: false, placeholder: 'mariadb' },
      { name: '-o', label: t('輸出檔案', 'Output File'), required: false, placeholder: 'mapping-draft.yaml' },
      { name: '--json', label: t('JSON 輸出', 'JSON Output'), required: false, type: 'checkbox' }
    ]
  },
  'drift-detect': {
    label: 'drift-detect',
    description: t('跨叢集配置漂移偵測：目錄級 SHA-256 比對', 'Cross-cluster config drift detection: directory-level SHA-256 comparison'),
    category: t('文件系統工具', 'Filesystem Tools'),
    args: [],
    flags: [
      { name: '--dirs', label: t('比對目錄列表', 'Directory List'), required: true, placeholder: 'cluster-a/conf.d,cluster-b/conf.d' },
      { name: '--labels', label: t('叢集標籤', 'Cluster Labels'), required: false, placeholder: 'edge-1,edge-2' },
      { name: '--ci', label: t('CI 模式', 'CI Mode'), required: false, type: 'checkbox' }
    ]
  },
  'shadow-verify': {
    label: 'shadow-verify',
    description: t('Shadow Monitoring 就緒度與收斂性三階段驗證', 'Shadow Monitoring readiness & convergence 3-phase verification'),
    category: t('Prometheus API 工具', 'Prometheus API Tools'),
    args: [],
    flags: [
      { name: '--mapping', label: t('指標映射檔', 'Metric Mapping File'), required: true, placeholder: 'mapping.yaml' },
      { name: '--prometheus', label: t('Prometheus URL', 'Prometheus URL'), required: false, placeholder: 'http://localhost:9090' },
      { name: '--report-csv', label: t('CSV 報告輸出', 'CSV Report Output'), required: false, placeholder: 'report.csv' },
      { name: '--readiness-json', label: t('就緒 JSON 輸出', 'Readiness JSON Output'), required: false, placeholder: 'readiness.json' }
    ]
  },
  'alert-quality': {
    label: 'alert-quality',
    description: t('告警品質評估：計算 MTTA/MTTR/SNR 等指標', 'Alert quality scoring: calculate MTTA/MTTR/SNR metrics'),
    category: t('Prometheus API 工具', 'Prometheus API Tools'),
    args: [],
    flags: [
      { name: '--prometheus', label: t('Prometheus URL', 'Prometheus URL'), required: true, placeholder: 'http://localhost:9090' },
      { name: '--tenant', label: t('租戶 ID', 'Tenant ID'), required: false, placeholder: 'db-a' },
      { name: '--lookback', label: t('回溯時間', 'Lookback Duration'), required: false, placeholder: '7d' },
      { name: '--json', label: t('JSON 輸出', 'JSON Output'), required: false, type: 'checkbox' }
    ]
  },
  'evaluate-policy': {
    label: 'evaluate-policy',
    description: t('Policy-as-Code 評估：宣告式 DSL 驗證 routing + threshold', 'Policy-as-Code evaluation: declarative DSL for routing + threshold validation'),
    category: t('文件系統工具', 'Filesystem Tools'),
    args: [],
    flags: [
      { name: '--config-dir', label: t('配置目錄', 'Config Directory'), required: true, placeholder: 'conf.d/' },
      { name: '--policy', label: t('策略檔案', 'Policy File'), required: true, placeholder: 'policy.yaml' },
      { name: '--ci', label: t('CI 模式', 'CI Mode'), required: false, type: 'checkbox' },
      { name: '--json', label: t('JSON 輸出', 'JSON Output'), required: false, type: 'checkbox' }
    ]
  },
  'byo-check': {
    label: 'byo-check',
    description: t('BYO Prometheus/Alertmanager 整合前檢驗證', 'BYO Prometheus/Alertmanager integration pre-check'),
    category: t('Prometheus API 工具', 'Prometheus API Tools'),
    args: [
      { name: 'target', label: t('檢查目標', 'Check Target'), required: true, placeholder: 'prometheus | alertmanager | all' }
    ],
    flags: [
      { name: '--prometheus', label: t('Prometheus URL', 'Prometheus URL'), required: false, placeholder: 'http://localhost:9090' },
      { name: '--alertmanager', label: t('Alertmanager URL', 'Alertmanager URL'), required: false, placeholder: 'http://localhost:9093' },
      { name: '--json', label: t('JSON 輸出', 'JSON Output'), required: false, type: 'checkbox' }
    ]
  }
};

const NETWORK_MODES = {
  'k8s': {
    label: t('K8s 內部 (svc.cluster.local)', 'K8s Internal (svc.cluster.local)'),
    prometheus: 'http://prometheus.monitoring.svc.cluster.local:9090',
    network: ''
  },
  'docker-desktop': {
    label: t('Docker Desktop (host.docker.internal)', 'Docker Desktop (host.docker.internal)'),
    prometheus: 'http://host.docker.internal:9090',
    network: ''
  },
  'linux': {
    label: t('Linux Docker (--network=host)', 'Linux Docker (--network=host)'),
    prometheus: 'http://localhost:9090',
    network: '--network=host'
  }
};

export { COMMANDS, NETWORK_MODES };
