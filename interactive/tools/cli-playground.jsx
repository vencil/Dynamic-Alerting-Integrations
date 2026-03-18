---
title: "da-tools CLI Playground"
tags: [cli, da-tools, docker]
audience: ["platform-engineer"]
version: v2.2.0
lang: en
related: [wizard, onboarding-checklist, glossary]
---

import React, { useState, useCallback } from 'react';
import { Copy, RefreshCw } from 'lucide-react';

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
[✓] Version consistency  v2.1.0

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

// Build initial state for a command's args/flags
function initCommandState(cmdKey) {
  const cmd = COMMANDS[cmdKey];
  const a = {};
  const f = {};
  cmd.args.forEach(arg => { a[arg.name] = ''; });
  cmd.flags.forEach(flag => { f[flag.name] = flag.type === 'checkbox' ? false : ''; });
  return { args: a, flags: f };
}

function readHashCmd() {
  try {
    const p = new URLSearchParams(window.location.hash.slice(1));
    const cmd = p.get('cmd');
    return (cmd && COMMANDS[cmd]) ? cmd : 'check-alert';
  } catch(e) { return 'check-alert'; }
}

export default function CLIPlayground() {
  const initialCmd = readHashCmd();
  const initial = initCommandState(initialCmd);
  const [selectedCommand, setSelectedCommand] = useState(initialCmd);
  const [isDocker, setIsDocker] = useState(true);
  const [networkMode, setNetworkMode] = useState('linux');
  const [args, setArgs] = useState(initial.args);
  const [flags, setFlags] = useState(initial.flags);
  const [copied, setCopied] = useState(false);
  const [searchFilter, setSearchFilter] = useState('');
  const [showPopularOnly, setShowPopularOnly] = useState(false);

  const command = COMMANDS[selectedCommand];
  const network = NETWORK_MODES[networkMode];

  // Initialize args/flags when command changes
  const handleCommandChange = (cmdKey) => {
    setSelectedCommand(cmdKey);
    const state = initCommandState(cmdKey);
    setArgs(state.args);
    setFlags(state.flags);
    window.history.replaceState(null, '', '#cmd=' + cmdKey);
  };

  const updateArg = (name, value) => {
    setArgs(prev => ({ ...prev, [name]: value }));
  };

  const updateFlag = (name, value) => {
    setFlags(prev => ({ ...prev, [name]: value }));
  };

  // Build the command string
  const buildCommand = () => {
    let cmd = '';

    if (isDocker) {
      cmd = 'docker run --rm ';
      if (network.network) cmd += network.network + ' ';
      cmd += `-e PROMETHEUS_URL=${network.prometheus} `;
      cmd += 'ghcr.io/vencil/da-tools:v2.1.0 ';
    } else {
      cmd = 'da-tools ';
    }

    cmd += selectedCommand;

    // Add positional arguments
    command.args.forEach(arg => {
      const value = args[arg.name];
      if (value) {
        cmd += ` ${value}`;
      }
    });

    // Add flags
    command.flags.forEach(flag => {
      const value = flags[flag.name];
      if (flag.type === 'checkbox') {
        if (value) cmd += ` ${flag.name}`;
      } else if (value) {
        // Skip Prometheus URL for docker mode (passed via env var)
        if (isDocker && flag.name === '--prometheus') return;
        cmd += ` ${flag.name} ${value}`;
      }
    });

    return cmd;
  };

  const copyCommand = () => {
    navigator.clipboard.writeText(buildCommand());
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  const commandsByCategory = {};
  Object.entries(COMMANDS).forEach(([key, cmd]) => {
    // Apply search filter
    const q = searchFilter.toLowerCase();
    if (q && !cmd.label.toLowerCase().includes(q) && !cmd.description.toLowerCase().includes(q) && !cmd.category.toLowerCase().includes(q)) return;
    // Apply popular filter
    if (showPopularOnly && !cmd.popular) return;
    if (!commandsByCategory[cmd.category]) {
      commandsByCategory[cmd.category] = [];
    }
    commandsByCategory[cmd.category].push({ key, ...cmd });
  });

  const requiredFlagsEmpty = command.flags
    .filter(f => f.required && f.type !== 'checkbox')
    .some(f => !flags[f.name]);
  const requiredArgsEmpty = command.args
    .filter(a => a.required)
    .some(a => !args[a.name]);

  return (
    <div className="min-h-screen bg-gradient-to-br from-slate-50 to-slate-100 p-8">
      <div className="max-w-6xl mx-auto">
        {/* Header */}
        <div className="mb-8">
          <h1 className="text-4xl font-bold text-slate-900 mb-2">{t('da-tools CLI 遊樂場', 'da-tools CLI Playground')}</h1>
          <p className="text-lg text-slate-600">{t('使用視覺介面建立和複製 da-tools 命令', 'Build and copy da-tools commands with a visual interface')}</p>
        </div>

        <div className="grid grid-cols-1 lg:grid-cols-3 gap-8">
          {/* Command Selector */}
          <div className="lg:col-span-2">
            <div className="bg-white rounded-lg shadow-lg p-6 space-y-6">
              {/* Execution Mode Toggle */}
              <div>
                <h3 className="text-sm font-semibold text-slate-900 mb-3">{t('執行模式', 'Execution Mode')}</h3>
                <div className="flex gap-3">
                  <label className="flex items-center gap-2 cursor-pointer">
                    <input
                      type="radio"
                      checked={isDocker}
                      onChange={() => setIsDocker(true)}
                      className="w-4 h-4"
                    />
                    <span className="text-slate-700">{t('Docker 容器', 'Docker Container')}</span>
                  </label>
                  <label className="flex items-center gap-2 cursor-pointer">
                    <input
                      type="radio"
                      checked={!isDocker}
                      onChange={() => setIsDocker(false)}
                      className="w-4 h-4"
                    />
                    <span className="text-slate-700">{t('直接 CLI', 'Direct CLI')}</span>
                  </label>
                </div>
              </div>

              {/* Network Mode (Docker only) */}
              {isDocker && (
                <div>
                  <h3 className="text-sm font-semibold text-slate-900 mb-3">{t('網路配置', 'Network Configuration')}</h3>
                  <select
                    value={networkMode}
                    onChange={(e) => setNetworkMode(e.target.value)}
                    className="w-full px-3 py-2 border border-slate-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500 text-slate-900"
                  >
                    {Object.entries(NETWORK_MODES).map(([key, mode]) => (
                      <option key={key} value={key}>{mode.label}</option>
                    ))}
                  </select>
                  <p className="text-xs text-slate-500 mt-2">
                    {t('Prometheus:', 'Prometheus:')} <code className="bg-slate-100 px-1 rounded">{network.prometheus}</code>
                  </p>
                </div>
              )}

              {/* Command Selection */}
              <div>
                <h3 className="text-sm font-semibold text-slate-900 mb-3">{t('選擇命令', 'Select Command')}</h3>
                <div className="flex gap-2 mb-3">
                  <input
                    type="text"
                    value={searchFilter}
                    onChange={(e) => setSearchFilter(e.target.value)}
                    placeholder={t('搜尋命令...', 'Search commands...')}
                    className="flex-1 px-3 py-2 border border-slate-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500 text-slate-900 text-sm"
                  />
                  <button
                    onClick={() => setShowPopularOnly(!showPopularOnly)}
                    className={`px-3 py-2 rounded-lg text-xs font-medium transition-colors whitespace-nowrap ${
                      showPopularOnly ? 'bg-amber-100 text-amber-800 border border-amber-300' : 'bg-slate-100 text-slate-600 border border-slate-300 hover:bg-slate-200'
                    }`}
                  >
                    ★ {t('熱門', 'Popular')}
                  </button>
                </div>
                {Object.keys(commandsByCategory).length === 0 && (
                  <p className="text-sm text-slate-500 py-4 text-center">{t('找不到與您的搜尋相符的命令。', 'No commands match your search.')}</p>
                )}
                <div className="space-y-2">
                  {Object.entries(commandsByCategory).map(([category, cmds]) => (
                    <div key={category}>
                      <p className="text-xs font-semibold text-slate-600 uppercase tracking-wide mb-2">{category}</p>
                      <div className="space-y-1 mb-4">
                        {cmds.map(cmd => (
                          <button
                            key={cmd.key}
                            onClick={() => handleCommandChange(cmd.key)}
                            className={`w-full text-left px-3 py-2 rounded text-sm transition-colors flex items-center gap-2 ${
                              selectedCommand === cmd.key
                                ? 'bg-blue-600 text-white font-medium'
                                : 'bg-slate-100 text-slate-900 hover:bg-slate-200'
                            }`}
                          >
                            <span className="flex-1">{cmd.label}</span>
                            {cmd.popular && <span className="text-amber-500 text-xs" title={t('常用命令', 'Commonly used')}>★</span>}
                          </button>
                        ))}
                      </div>
                    </div>
                  ))}
                </div>
              </div>

              {/* Command Description */}
              <div className="p-4 bg-blue-50 border border-blue-200 rounded-lg">
                <p className="text-sm text-blue-900">{command.description}</p>
              </div>

              {/* Arguments */}
              {command.args.length > 0 && (
                <div>
                  <h3 className="text-sm font-semibold text-slate-900 mb-3">{t('引數', 'Arguments')}</h3>
                  <div className="space-y-3">
                    {command.args.map(arg => (
                      <div key={arg.name}>
                        <label className="text-xs font-medium text-slate-700 block mb-1">
                          {arg.label} {arg.required && <span className="text-red-600">*</span>}
                        </label>
                        <input
                          type="text"
                          value={args[arg.name] || ''}
                          onChange={(e) => updateArg(arg.name, e.target.value)}
                          placeholder={arg.placeholder}
                          className="w-full px-3 py-2 border border-slate-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500 text-slate-900 text-sm"
                        />
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {/* Flags */}
              {command.flags.length > 0 && (
                <div>
                  <h3 className="text-sm font-semibold text-slate-900 mb-3">{t('選項', 'Options')}</h3>
                  <div className="space-y-3">
                    {command.flags.map(flag => (
                      <div key={flag.name}>
                        {flag.type === 'checkbox' ? (
                          <label className="flex items-center gap-2 cursor-pointer">
                            <input
                              type="checkbox"
                              checked={flags[flag.name] || false}
                              onChange={(e) => updateFlag(flag.name, e.target.checked)}
                              className="w-4 h-4 rounded"
                            />
                            <span className="text-sm text-slate-700">{flag.label}</span>
                          </label>
                        ) : (
                          <>
                            <label className="text-xs font-medium text-slate-700 block mb-1">
                              {flag.label} {flag.required && <span className="text-red-600">*</span>}
                            </label>
                            <input
                              type="text"
                              value={flags[flag.name] || ''}
                              onChange={(e) => updateFlag(flag.name, e.target.value)}
                              placeholder={flag.placeholder}
                              className="w-full px-3 py-2 border border-slate-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500 text-slate-900 text-sm"
                            />
                          </>
                        )}
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </div>
          </div>

          {/* Command Output & Summary */}
          <div className="lg:col-span-1">
            <div className="sticky top-8 space-y-4">
              {/* Command Output */}
              <div className="bg-white rounded-lg shadow-lg p-6">
                <h3 className="text-lg font-semibold text-slate-900 mb-4">{t('命令', 'Command')}</h3>
                <div className="relative">
                  <pre className="bg-slate-900 text-slate-100 p-4 rounded text-xs overflow-x-auto break-words whitespace-pre-wrap max-h-64 overflow-y-auto font-mono">
                    {buildCommand()}
                  </pre>
                  <button
                    onClick={copyCommand}
                    disabled={requiredArgsEmpty || requiredFlagsEmpty}
                    className={`absolute top-2 right-2 p-2 rounded transition-colors ${
                      copied
                        ? 'bg-green-500 text-white'
                        : 'bg-slate-700 text-slate-200 hover:bg-slate-600 disabled:opacity-50 disabled:cursor-not-allowed'
                    }`}
                    title={t('複製到剪貼板', 'Copy to clipboard')}
                  >
                    <Copy size={16} />
                  </button>
                </div>
                {copied && (
                  <p className="mt-2 text-sm text-green-600 font-medium">✓ {t('已複製到剪貼板', 'Copied to clipboard')}</p>
                )}
                {(requiredArgsEmpty || requiredFlagsEmpty) && (
                  <p className="mt-2 text-xs text-amber-600">{t('填寫必填欄位以啟用複製', 'Fill required fields to enable copy')}</p>
                )}
              </div>

              {/* Sample Output Preview */}
              {command.preview && (
                <div className="bg-white rounded-lg shadow-lg p-6">
                  <h3 className="text-sm font-semibold text-slate-900 mb-3 flex items-center gap-2">
                    <span className="text-green-500">▶</span> {t('範例輸出', 'Sample Output')}
                  </h3>
                  <pre className="bg-slate-900 text-green-400 p-4 rounded text-xs overflow-x-auto whitespace-pre-wrap max-h-56 overflow-y-auto font-mono leading-relaxed">
                    {command.preview}
                  </pre>
                  <p className="text-xs text-slate-400 mt-2 italic">{t('模擬輸出 — 實際結果取決於您的環境。', 'Simulated output — actual results depend on your environment.')}</p>
                </div>
              )}

              {/* Environment Info */}
              <div className="bg-white rounded-lg shadow-lg p-6 text-sm">
                <h3 className="font-semibold text-slate-900 mb-3">{t('環境', 'Environment')}</h3>
                <div className="space-y-2 text-slate-600 text-xs">
                  <div>
                    <span className="font-medium text-slate-900">{t('模式:', 'Mode:')}</span> {isDocker ? t('Docker 容器', 'Docker Container') : t('直接 CLI', 'Direct CLI')}
                  </div>
                  {isDocker && (
                    <>
                      <div>
                        <span className="font-medium text-slate-900">{t('映像:', 'Image:')}</span> ghcr.io/vencil/da-tools:v2.1.0
                      </div>
                      <div>
                        <span className="font-medium text-slate-900">{t('網路:', 'Network:')}</span> {network.label}
                      </div>
                    </>
                  )}
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
