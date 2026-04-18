---
title: "Operator Setup Wizard"
tags: [operator, prometheus, crd, migration, setup, wizard]
audience: [platform-engineer, sre, devops]
version: v2.7.0
lang: en
related: [deployment-wizard, cicd-setup-wizard, config-lint]
dependencies: []
---

import React, { useState, useMemo, useCallback } from 'react';

const t = window.__t || ((zh, en) => en);

/* ── Step definitions ── */
const STEPS = [
  { id: 'environment', label: () => t('環境偵測', 'Environment Detection') },
  { id: 'crd-config', label: () => t('CRD 配置', 'CRD Configuration') },
  { id: 'receiver', label: () => t('Receiver 設定', 'Receiver Setup') },
  { id: 'tenants', label: () => t('Tenant 選擇', 'Tenant Selection') },
  { id: 'review', label: () => t('產出 & 檢視', 'Review & Generate') },
];

/* ── Demo tenants ── */
const DEMO_TENANTS = [
  'db-a', 'db-b', 'web-prod', 'cache-staging',
  'analytics-dev', 'kafka-prod', 'redis-cache'
];

/* ── Operator versions ── */
const OPERATOR_VERSIONS = [
  { id: 'v0.65+', label: 'v0.65+ (Latest)', apiVersion: 'v1beta1', recommended: true },
  { id: 'v0.50-v0.64', label: 'v0.50 to v0.64', apiVersion: 'v1', recommended: false },
  { id: 'older', label: 'Older (v0.49)', apiVersion: 'v1alpha1', recommended: false },
];

/* ── Cluster types ── */
const CLUSTER_TYPES = [
  { id: 'kind', label: 'Kind (Local)', icon: '💻' },
  { id: 'eks', label: 'EKS (AWS)', icon: '🔶' },
  { id: 'gke', label: 'GKE (Google Cloud)', icon: '☁️' },
  { id: 'aks', label: 'AKS (Azure)', icon: '🟦' },
  { id: 'onprem', label: 'On-Premises', icon: '🏢' },
  { id: 'other', label: 'Other', icon: '❓' },
];

/* ── Receiver types ── */
const RECEIVER_TYPES = [
  { id: 'slack', label: 'Slack', icon: '💬', defaultPort: 80 },
  { id: 'pagerduty', label: 'PagerDuty', icon: '📱', defaultPort: 443 },
  { id: 'email', label: 'Email (SMTP)', icon: '📧', defaultPort: 587 },
  { id: 'teams', label: 'Microsoft Teams', icon: '🟦', defaultPort: 443 },
  { id: 'opsgenie', label: 'OpsGenie', icon: '🚨', defaultPort: 443 },
  { id: 'webhook', label: 'Custom Webhook', icon: '🪝', defaultPort: 443 },
];

/* ── Rule deployment modes ── */
const RULE_MODES = [
  {
    id: 'operator',
    label: () => t('純 Operator', 'Pure Operator'),
    desc: () => t('所有規則透過 PrometheusRule CRD 部署', 'All rules via PrometheusRule CRD'),
    riskLevel: 'low'
  },
  {
    id: 'configmap',
    label: () => t('純 ConfigMap', 'Pure ConfigMap'),
    desc: () => t('所有規則保留在 ConfigMap（無 Operator）', 'All rules in ConfigMap (no Operator)'),
    riskLevel: 'low'
  },
  {
    id: 'dual-stack',
    label: () => t('雙堆棧 (遷移用)', 'Dual-Stack (Migration)'),
    desc: () => t('ConfigMap + PrometheusRule CRD 並行運行，支援漸進遷移', 'Both ConfigMap & PrometheusRule, gradual migration'),
    riskLevel: 'medium'
  },
];

/* ── Helper functions ── */

function validateTenantName(name) {
  // RFC 1123: alphanumeric and hyphen, must start/end with alphanumeric
  return /^[a-z0-9]([a-z0-9-]*[a-z0-9])?$/.test(name);
}

function generateOperatorCommand(config) {
  const parts = [
    'da-tools operator-generate',
    `--crd-version=${config.crdVersion || 'v1beta1'}`,
    `--namespace=${config.namespace || 'monitoring'}`,
    `--rule-mode=${config.ruleMode || 'operator'}`,
    `--receiver-type=${config.receiverType}`,
    `--receiver-secret=${config.receiverSecret}`,
  ];

  if (config.selectedTenants && config.selectedTenants.length > 0) {
    parts.push(`--tenants=${config.selectedTenants.join(',')}`);
  }

  if (config.operatorVersion) {
    parts.push(`--operator-version=${config.operatorVersion}`);
  }

  return parts.join(' \\');
}

function generateMigrationCommand(config) {
  if (config.ruleMode !== 'dual-stack') return null;

  return `da-tools migrate-to-operator \\
  --namespace=${config.namespace || 'monitoring'} \\
  --tenants=${config.selectedTenants.join(',')} \\
  --dry-run`;
}

function generateAlertmanagerConfigPreview(config, tenantIdx = 0) {
  const tenant = config.selectedTenants[tenantIdx];
  if (!tenant) return '';

  let yaml = `apiVersion: monitoring.coreos.com/v1alpha1
kind: AlertmanagerConfig
metadata:
  name: ${tenant}-alertmanager-config
  namespace: monitoring
spec:
  route:
    groupBy: ['alertname', 'cluster']
    groupWait: 30s
    groupInterval: 5m
    repeatInterval: 4h
    receiver: '${config.receiverType}'
  receivers:
    - name: '${config.receiverType}'
      ${config.receiverType}Configs:
        - ${getReceiverConfig(config.receiverType, config.receiverSecret)}
`;
  return yaml;
}

function getReceiverConfig(receiverType, secretName) {
  const configs = {
    slack: `apiUrl: '{{ index .Values.secrets "${secretName}" "webhook_url" }}'`,
    pagerduty: `serviceKey: '{{ index .Values.secrets "${secretName}" "service_key" }}'`,
    email: `smarthost: 'smtp.example.com:587'
        authUsername: '{{ index .Values.secrets "${secretName}" "username" }}'
        authPassword: '{{ index .Values.secrets "${secretName}" "password" }}'`,
    teams: `webhookUrl: '{{ index .Values.secrets "${secretName}" "webhook_url" }}'`,
    opsgenie: `apiKey: '{{ index .Values.secrets "${secretName}" "api_key" }}'`,
    webhook: `url: '{{ index .Values.secrets "${secretName}" "webhook_url" }}'`,
  };
  return configs[receiverType] || 'url: "https://example.com/webhook"';
}

/* ── Step Components ── */

function StepEnvironment({ config, onChange, helpOpen, setHelpOpen }) {
  const updateConfig = useCallback((updates) => {
    onChange({ ...config, ...updates });
  }, [config, onChange]);

  return (
    <div className="space-y-4">
      <div className="flex items-start justify-between">
        <div>
          <h3 style={{ fontSize: 'var(--da-font-size-lg)', fontWeight: 'var(--da-font-weight-bold)', marginBottom: 'var(--da-space-2)' }}>
            {t('第一步：環境偵測', 'Step 1: Environment Detection')}
          </h3>
          <p style={{ fontSize: 'var(--da-font-size-sm)', color: 'var(--da-color-muted)', marginBottom: 'var(--da-space-4)' }}>
            {t('告訴我們你的 Kubernetes 環境和 Prometheus Operator 設置。', 'Tell us about your Kubernetes environment and Prometheus Operator setup.')}
          </p>
        </div>
        <button
          onClick={() => setHelpOpen(!helpOpen)}
          aria-label={t('顯示幫助資訊', 'Show help information')}
          aria-expanded={helpOpen}
          style={{
            background: 'none',
            border: 'none',
            fontSize: 'var(--da-font-size-lg)',
            cursor: 'pointer',
            padding: 'var(--da-space-2)',
          }}
        >
          ?
        </button>
      </div>

      {helpOpen && (
        <div style={{
          backgroundColor: 'var(--da-color-info-soft)',
          border: '1px solid var(--da-color-info)',
          borderRadius: 'var(--da-radius-md)',
          padding: 'var(--da-space-3)',
          fontSize: 'var(--da-font-size-sm)',
          color: 'var(--da-color-fg)',
        }}>
          <p style={{ marginBottom: 'var(--da-space-2)' }}>
            <strong>{t('為什麼需要這個資訊？', 'Why do we need this?')}</strong>
          </p>
          <ul style={{ marginLeft: 'var(--da-space-4)', listStyleType: 'disc', lineHeight: '1.6' }}>
            <li>{t('Operator 版本決定了 CRD API 版本（v1beta1 vs v1 vs v1alpha1）', 'Operator version determines CRD API version (v1beta1 vs v1 vs v1alpha1)')}</li>
            <li>{t('cluster 類型影響 RBAC 和網路配置', 'Cluster type affects RBAC and networking config')}</li>
            <li>{t('kube-prometheus-stack 安裝狀態決定遷移路徑', 'kube-prometheus-stack installation status determines migration path')}</li>
          </ul>
          <p style={{ marginTop: 'var(--da-space-3)', fontSize: 'var(--da-font-size-xs)', color: 'var(--da-color-muted)' }}>
            {t('詳見: docs/adr/operator-integration.md', 'See: docs/adr/operator-integration.md')}
          </p>
        </div>
      )}

      {/* kube-prometheus-stack installed? */}
      <div style={{
        padding: 'var(--da-space-4)',
        border: '1px solid var(--da-color-surface-border)',
        borderRadius: 'var(--da-radius-md)',
        backgroundColor: 'var(--da-color-surface)',
      }}>
        <label style={{ display: 'block', marginBottom: 'var(--da-space-3)', fontWeight: 'var(--da-font-weight-semibold)', color: 'var(--da-color-fg)' }}>
          {t('已安裝 kube-prometheus-stack？', 'Do you have kube-prometheus-stack installed?')}
        </label>
        <div style={{ display: 'flex', gap: 'var(--da-space-2)', flexWrap: 'wrap' }}>
          {['yes', 'no', 'unsure'].map(opt => (
            <button
              key={opt}
              onClick={() => updateConfig({ kubePromInstalled: opt })}
              style={{
                padding: 'var(--da-space-2) var(--da-space-3)',
                borderRadius: 'var(--da-radius-md)',
                border: config.kubePromInstalled === opt ? '2px solid var(--da-color-accent)' : '1px solid var(--da-color-surface-border)',
                backgroundColor: config.kubePromInstalled === opt ? 'var(--da-color-accent-soft)' : 'var(--da-color-bg)',
                color: 'var(--da-color-fg)',
                fontWeight: config.kubePromInstalled === opt ? 'var(--da-font-weight-semibold)' : 'normal',
                cursor: 'pointer',
                fontSize: 'var(--da-font-size-sm)',
              }}
            >
              {t(['是', '否', '不確定'][['yes', 'no', 'unsure'].indexOf(opt)], ['Yes', 'No', 'Unsure'][['yes', 'no', 'unsure'].indexOf(opt)])}
            </button>
          ))}
        </div>
      </div>

      {/* Operator version */}
      <div style={{
        padding: 'var(--da-space-4)',
        border: '1px solid var(--da-color-surface-border)',
        borderRadius: 'var(--da-radius-md)',
        backgroundColor: 'var(--da-color-surface)',
      }}>
        <label style={{ display: 'block', marginBottom: 'var(--da-space-3)', fontWeight: 'var(--da-font-weight-semibold)', color: 'var(--da-color-fg)' }}>
          {t('Prometheus Operator 版本', 'Prometheus Operator Version')}
        </label>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(200px, 1fr))', gap: 'var(--da-space-2)' }}>
          {OPERATOR_VERSIONS.map(ver => (
            <button
              key={ver.id}
              onClick={() => updateConfig({ operatorVersion: ver.id, crdVersion: ver.apiVersion })}
              style={{
                padding: 'var(--da-space-3)',
                border: config.operatorVersion === ver.id ? '2px solid var(--da-color-accent)' : '1px solid var(--da-color-surface-border)',
                borderRadius: 'var(--da-radius-md)',
                backgroundColor: config.operatorVersion === ver.id ? 'var(--da-color-accent-soft)' : 'var(--da-color-bg)',
                color: 'var(--da-color-fg)',
                fontWeight: 'var(--da-font-weight-semibold)',
                cursor: 'pointer',
                fontSize: 'var(--da-font-size-sm)',
                textAlign: 'left',
              }}
            >
              {ver.label}
              {ver.recommended && <span style={{ display: 'block', fontSize: 'var(--da-font-size-xs)', color: 'var(--da-color-success)', marginTop: 'var(--da-space-1)' }}>✓ {t('推薦', 'Recommended')}</span>}
            </button>
          ))}
        </div>
        <details style={{ marginTop: 'var(--da-space-3)', fontSize: 'var(--da-font-size-xs)', color: 'var(--da-color-muted)' }}>
          <summary style={{ cursor: 'pointer', fontWeight: 'var(--da-font-weight-semibold)', marginBottom: 'var(--da-space-1)' }}>
            {t('如何檢查我的版本？', 'How to check my version?')}
          </summary>
          <pre style={{ backgroundColor: 'var(--da-color-bg)', padding: 'var(--da-space-2)', borderRadius: 'var(--da-radius-sm)', fontSize: 'var(--da-font-size-xs)', overflow: 'auto', marginTop: 'var(--da-space-2)' }}>
{t(`kubectl get deployment -n monitoring prometheus-operator -o jsonpath='{.spec.template.spec.containers[0].image}'`, `kubectl get deployment -n monitoring prometheus-operator -o jsonpath='{.spec.template.spec.containers[0].image}'`)}
          </pre>
        </details>
      </div>

      {/* Cluster type */}
      <div style={{
        padding: 'var(--da-space-4)',
        border: '1px solid var(--da-color-surface-border)',
        borderRadius: 'var(--da-radius-md)',
        backgroundColor: 'var(--da-color-surface)',
      }}>
        <label style={{ display: 'block', marginBottom: 'var(--da-space-3)', fontWeight: 'var(--da-font-weight-semibold)', color: 'var(--da-color-fg)' }}>
          {t('Cluster 類型', 'Cluster Type')}
        </label>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(150px, 1fr))', gap: 'var(--da-space-2)' }}>
          {CLUSTER_TYPES.map(ct => (
            <button
              key={ct.id}
              onClick={() => updateConfig({ clusterType: ct.id })}
              style={{
                padding: 'var(--da-space-3)',
                border: config.clusterType === ct.id ? '2px solid var(--da-color-accent)' : '1px solid var(--da-color-surface-border)',
                borderRadius: 'var(--da-radius-md)',
                backgroundColor: config.clusterType === ct.id ? 'var(--da-color-accent-soft)' : 'var(--da-color-bg)',
                color: 'var(--da-color-fg)',
                cursor: 'pointer',
                fontSize: 'var(--da-font-size-sm)',
                textAlign: 'center',
              }}
            >
              <div style={{ fontSize: '1.5em' }}>{ct.icon}</div>
              <div style={{ fontWeight: 'var(--da-font-weight-semibold)', marginTop: 'var(--da-space-1)' }}>{ct.label}</div>
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}

function StepCRDConfig({ config, onChange, helpOpen, setHelpOpen }) {
  const updateConfig = useCallback((updates) => {
    onChange({ ...config, ...updates });
  }, [config, onChange]);

  return (
    <div className="space-y-4">
      <div className="flex items-start justify-between">
        <div>
          <h3 style={{ fontSize: 'var(--da-font-size-lg)', fontWeight: 'var(--da-font-weight-bold)', marginBottom: 'var(--da-space-2)' }}>
            {t('第二步：CRD 配置', 'Step 2: CRD Configuration')}
          </h3>
          <p style={{ fontSize: 'var(--da-font-size-sm)', color: 'var(--da-color-muted)', marginBottom: 'var(--da-space-4)' }}>
            {t('設定 PrometheusRule CRD 部署參數和遷移策略。', 'Configure PrometheusRule CRD deployment parameters and migration strategy.')}
          </p>
        </div>
        <button
          onClick={() => setHelpOpen(!helpOpen)}
          aria-label={t('顯示幫助資訊', 'Show help information')}
          aria-expanded={helpOpen}
          style={{
            background: 'none',
            border: 'none',
            fontSize: 'var(--da-font-size-lg)',
            cursor: 'pointer',
            padding: 'var(--da-space-2)',
          }}
        >
          ?
        </button>
      </div>

      {helpOpen && (
        <div style={{
          backgroundColor: 'var(--da-color-warning-soft)',
          border: '1px solid var(--da-color-warning)',
          borderRadius: 'var(--da-radius-md)',
          padding: 'var(--da-space-3)',
          fontSize: 'var(--da-font-size-sm)',
          color: 'var(--da-color-fg)',
        }}>
          <p style={{ marginBottom: 'var(--da-space-2)' }}>
            <strong>{t('常見陷阱', 'Common Pitfalls')}</strong>
          </p>
          <ul style={{ marginLeft: 'var(--da-space-4)', listStyleType: 'disc', lineHeight: '1.6' }}>
            <li>{t('dual-stack 模式需要仔細測試，避免 duplicate 規則', 'dual-stack mode requires careful testing to avoid duplicate rules')}</li>
            <li>{t('Namespace 必須已存在，Operator 需要對應 RBAC', 'Namespace must exist, Operator needs corresponding RBAC')}</li>
            <li>{t('API 版本必須與 Operator 相容', 'API version must be compatible with Operator')}</li>
          </ul>
        </div>
      )}

      {/* CRD API Version */}
      <div style={{
        padding: 'var(--da-space-4)',
        border: '1px solid var(--da-color-surface-border)',
        borderRadius: 'var(--da-radius-md)',
        backgroundColor: 'var(--da-color-surface)',
      }}>
        <label style={{ display: 'block', marginBottom: 'var(--da-space-3)', fontWeight: 'var(--da-font-weight-semibold)', color: 'var(--da-color-fg)' }}>
          {t('CRD API 版本 (自動)', 'CRD API Version (Auto)')}
        </label>
        <div style={{
          padding: 'var(--da-space-2) var(--da-space-3)',
          backgroundColor: 'var(--da-color-bg)',
          borderRadius: 'var(--da-radius-md)',
          fontFamily: 'monospace',
          fontSize: 'var(--da-font-size-sm)',
          color: 'var(--da-color-muted)',
        }}>
          monitoring.coreos.com/{config.crdVersion || 'v1beta1'}
        </div>
      </div>

      {/* Namespace */}
      <div style={{
        padding: 'var(--da-space-4)',
        border: '1px solid var(--da-color-surface-border)',
        borderRadius: 'var(--da-radius-md)',
        backgroundColor: 'var(--da-color-surface)',
      }}>
        <label style={{ display: 'block', marginBottom: 'var(--da-space-2)', fontWeight: 'var(--da-font-weight-semibold)', color: 'var(--da-color-fg)' }}>
          {t('Namespace', 'Namespace')}
        </label>
        <input
          type="text"
          value={config.namespace || 'monitoring'}
          onChange={(e) => updateConfig({ namespace: e.target.value })}
          style={{
            width: '100%',
            padding: 'var(--da-space-2) var(--da-space-3)',
            border: '1px solid var(--da-color-surface-border)',
            borderRadius: 'var(--da-radius-md)',
            fontSize: 'var(--da-font-size-sm)',
            backgroundColor: 'var(--da-color-bg)',
            color: 'var(--da-color-fg)',
            boxSizing: 'border-box',
          }}
          placeholder="monitoring"
        />
        <p style={{ fontSize: 'var(--da-font-size-xs)', color: 'var(--da-color-muted)', marginTop: 'var(--da-space-1)' }}>
          {t('預設: monitoring。Operator 和 tenant 規則將部署至此。', 'Default: monitoring. Operator and tenant rules will be deployed here.')}
        </p>
      </div>

      {/* Rule deployment mode */}
      <div style={{
        padding: 'var(--da-space-4)',
        border: '1px solid var(--da-color-surface-border)',
        borderRadius: 'var(--da-radius-md)',
        backgroundColor: 'var(--da-color-surface)',
      }}>
        <label style={{ display: 'block', marginBottom: 'var(--da-space-3)', fontWeight: 'var(--da-font-weight-semibold)', color: 'var(--da-color-fg)' }}>
          {t('規則部署模式', 'Rule Deployment Mode')}
        </label>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--da-space-2)' }}>
          {RULE_MODES.map(mode => (
            <button
              key={mode.id}
              onClick={() => updateConfig({ ruleMode: mode.id })}
              style={{
                padding: 'var(--da-space-3)',
                border: config.ruleMode === mode.id ? '2px solid var(--da-color-accent)' : '1px solid var(--da-color-surface-border)',
                borderRadius: 'var(--da-radius-md)',
                backgroundColor: config.ruleMode === mode.id ? 'var(--da-color-accent-soft)' : 'var(--da-color-bg)',
                cursor: 'pointer',
                textAlign: 'left',
              }}
            >
              <div style={{ fontWeight: 'var(--da-font-weight-semibold)', color: 'var(--da-color-fg)' }}>
                {mode.label()}
              </div>
              <div style={{ fontSize: 'var(--da-font-size-xs)', color: 'var(--da-color-muted)', marginTop: 'var(--da-space-1)' }}>
                {mode.desc()}
              </div>
              {mode.riskLevel === 'medium' && (
                <div style={{ fontSize: 'var(--da-font-size-xs)', color: 'var(--da-color-warning)', marginTop: 'var(--da-space-1)' }}>
                  ⚠️ {t('需要仔細測試', 'Requires careful testing')}
                </div>
              )}
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}

function StepReceiver({ config, onChange, helpOpen, setHelpOpen }) {
  const updateConfig = useCallback((updates) => {
    onChange({ ...config, ...updates });
  }, [config, onChange]);

  return (
    <div className="space-y-4">
      <div className="flex items-start justify-between">
        <div>
          <h3 style={{ fontSize: 'var(--da-font-size-lg)', fontWeight: 'var(--da-font-weight-bold)', marginBottom: 'var(--da-space-2)' }}>
            {t('第三步：Receiver 設定', 'Step 3: Receiver Setup')}
          </h3>
          <p style={{ fontSize: 'var(--da-font-size-sm)', color: 'var(--da-color-muted)', marginBottom: 'var(--da-space-4)' }}>
            {t('選擇告警接收器並配置認證 Secret。', 'Select alert receiver and configure authentication secret.')}
          </p>
        </div>
        <button
          onClick={() => setHelpOpen(!helpOpen)}
          aria-label={t('顯示幫助資訊', 'Show help information')}
          aria-expanded={helpOpen}
          style={{
            background: 'none',
            border: 'none',
            fontSize: 'var(--da-font-size-lg)',
            cursor: 'pointer',
            padding: 'var(--da-space-2)',
          }}
        >
          ?
        </button>
      </div>

      {helpOpen && (
        <div style={{
          backgroundColor: 'var(--da-color-info-soft)',
          border: '1px solid var(--da-color-info)',
          borderRadius: 'var(--da-radius-md)',
          padding: 'var(--da-space-3)',
          fontSize: 'var(--da-font-size-sm)',
          color: 'var(--da-color-fg)',
        }}>
          <p style={{ marginBottom: 'var(--da-space-2)' }}>
            <strong>{t('Secret 管理最佳做法', 'Secret Management Best Practices')}</strong>
          </p>
          <ul style={{ marginLeft: 'var(--da-space-4)', listStyleType: 'disc', lineHeight: '1.6' }}>
            <li>{t('Secret 應儲存在 Kubernetes Secret 或 external secrets operator', 'Store secrets in Kubernetes Secret or external secrets operator')}</li>
            <li>{t('永遠不要在 config 檔案中存放明文認證資訊', 'Never store credentials in plaintext in config files')}</li>
            <li>{t('使用 RBAC 限制 Secret 存取權限', 'Restrict Secret access with RBAC')}</li>
          </ul>
        </div>
      )}

      {/* Receiver type */}
      <div style={{
        padding: 'var(--da-space-4)',
        border: '1px solid var(--da-color-surface-border)',
        borderRadius: 'var(--da-radius-md)',
        backgroundColor: 'var(--da-color-surface)',
      }}>
        <label style={{ display: 'block', marginBottom: 'var(--da-space-3)', fontWeight: 'var(--da-font-weight-semibold)', color: 'var(--da-color-fg)' }}>
          {t('Receiver 類型', 'Receiver Type')}
        </label>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(140px, 1fr))', gap: 'var(--da-space-2)' }}>
          {RECEIVER_TYPES.map(rt => (
            <button
              key={rt.id}
              onClick={() => updateConfig({ receiverType: rt.id, receiverSecret: `${rt.id}-credentials` })}
              style={{
                padding: 'var(--da-space-3)',
                border: config.receiverType === rt.id ? '2px solid var(--da-color-accent)' : '1px solid var(--da-color-surface-border)',
                borderRadius: 'var(--da-radius-md)',
                backgroundColor: config.receiverType === rt.id ? 'var(--da-color-accent-soft)' : 'var(--da-color-bg)',
                cursor: 'pointer',
                textAlign: 'center',
              }}
            >
              <div style={{ fontSize: '1.5em' }}>{rt.icon}</div>
              <div style={{ fontWeight: 'var(--da-font-weight-semibold)', marginTop: 'var(--da-space-1)', fontSize: 'var(--da-font-size-sm)' }}>
                {rt.label}
              </div>
            </button>
          ))}
        </div>
      </div>

      {/* Secret name */}
      <div style={{
        padding: 'var(--da-space-4)',
        border: '1px solid var(--da-color-surface-border)',
        borderRadius: 'var(--da-radius-md)',
        backgroundColor: 'var(--da-color-surface)',
      }}>
        <label style={{ display: 'block', marginBottom: 'var(--da-space-2)', fontWeight: 'var(--da-font-weight-semibold)', color: 'var(--da-color-fg)' }}>
          {t('Secret 名稱', 'Secret Name')}
        </label>
        <input
          type="text"
          value={config.receiverSecret || ''}
          onChange={(e) => updateConfig({ receiverSecret: e.target.value })}
          style={{
            width: '100%',
            padding: 'var(--da-space-2) var(--da-space-3)',
            border: '1px solid var(--da-color-surface-border)',
            borderRadius: 'var(--da-radius-md)',
            fontSize: 'var(--da-font-size-sm)',
            backgroundColor: 'var(--da-color-bg)',
            color: 'var(--da-color-fg)',
            boxSizing: 'border-box',
          }}
          placeholder="e.g., slack-webhook-secret"
        />
        <p style={{ fontSize: 'var(--da-font-size-xs)', color: 'var(--da-color-muted)', marginTop: 'var(--da-space-1)' }}>
          {t('Kubernetes Secret 的名稱，必須包含認證資訊。', 'Kubernetes Secret name containing credentials.')}
        </p>
      </div>

      {/* Secret creation hint */}
      <div style={{
        padding: 'var(--da-space-3)',
        backgroundColor: 'var(--da-color-info-soft)',
        border: '1px solid var(--da-color-info)',
        borderRadius: 'var(--da-radius-md)',
        fontSize: 'var(--da-font-size-xs)',
        color: 'var(--da-color-fg)',
      }}>
        <p style={{ fontWeight: 'var(--da-font-weight-semibold)', marginBottom: 'var(--da-space-2)' }}>
          {t('建立 Secret 的範例命令', 'Example command to create secret')}
        </p>
        <pre style={{
          backgroundColor: 'var(--da-color-bg)',
          padding: 'var(--da-space-2)',
          borderRadius: 'var(--da-radius-sm)',
          overflow: 'auto',
          fontSize: 'var(--da-font-size-xs)',
        }}>
{`kubectl create secret generic ${config.receiverSecret} \\
  --from-literal=webhook_url='YOUR_WEBHOOK_URL' \\
  -n monitoring`}
        </pre>
      </div>
    </div>
  );
}

function StepTenants({ config, onChange, helpOpen, setHelpOpen }) {
  const [customTenant, setCustomTenant] = useState('');

  const updateConfig = useCallback((updates) => {
    onChange({ ...config, ...updates });
  }, [config, onChange]);

  const addCustomTenant = useCallback(() => {
    if (customTenant.trim() && validateTenantName(customTenant.trim())) {
      const tenants = Array.from(new Set([...(config.selectedTenants || []), customTenant.trim()]));
      updateConfig({ selectedTenants: tenants });
      setCustomTenant('');
    }
  }, [customTenant, config, updateConfig]);

  const toggleTenant = useCallback((tenant) => {
    const selected = config.selectedTenants || [];
    const updated = selected.includes(tenant)
      ? selected.filter(t => t !== tenant)
      : [...selected, tenant];
    updateConfig({ selectedTenants: updated });
  }, [config, updateConfig]);

  return (
    <div className="space-y-4">
      <div className="flex items-start justify-between">
        <div>
          <h3 style={{ fontSize: 'var(--da-font-size-lg)', fontWeight: 'var(--da-font-weight-bold)', marginBottom: 'var(--da-space-2)' }}>
            {t('第四步：Tenant 選擇', 'Step 4: Tenant Selection')}
          </h3>
          <p style={{ fontSize: 'var(--da-font-size-sm)', color: 'var(--da-color-muted)', marginBottom: 'var(--da-space-4)' }}>
            {t('選擇要遷移至 Operator 的 tenant，或輸入自訂 tenant 名稱。', 'Select tenants to migrate to Operator, or enter custom tenant names.')}
          </p>
        </div>
        <button
          onClick={() => setHelpOpen(!helpOpen)}
          aria-label={t('顯示幫助資訊', 'Show help information')}
          aria-expanded={helpOpen}
          style={{
            background: 'none',
            border: 'none',
            fontSize: 'var(--da-font-size-lg)',
            cursor: 'pointer',
            padding: 'var(--da-space-2)',
          }}
        >
          ?
        </button>
      </div>

      {helpOpen && (
        <div style={{
          backgroundColor: 'var(--da-color-success-soft)',
          border: '1px solid var(--da-color-success)',
          borderRadius: 'var(--da-radius-md)',
          padding: 'var(--da-space-3)',
          fontSize: 'var(--da-font-size-sm)',
          color: 'var(--da-color-fg)',
        }}>
          <p style={{ marginBottom: 'var(--da-space-2)' }}>
            <strong>{t('Tenant 命名規則', 'Tenant Naming Convention')}</strong>
          </p>
          <p style={{ marginBottom: 'var(--da-space-2)', fontSize: 'var(--da-font-size-xs)' }}>
            {t('RFC 1123: 小寫字母、數字、連字號，必須以字母或數字開頭和結尾', 'RFC 1123: lowercase alphanumeric and hyphens, must start/end with alphanumeric')}
          </p>
          <p style={{ fontSize: 'var(--da-font-size-xs)' }}>
            {t('有效例子: db-a, web-prod, cache-staging-01', 'Valid examples: db-a, web-prod, cache-staging-01')}
          </p>
        </div>
      )}

      {/* Demo tenants */}
      <div style={{
        padding: 'var(--da-space-4)',
        border: '1px solid var(--da-color-surface-border)',
        borderRadius: 'var(--da-radius-md)',
        backgroundColor: 'var(--da-color-surface)',
      }}>
        <label style={{ display: 'block', marginBottom: 'var(--da-space-3)', fontWeight: 'var(--da-font-weight-semibold)', color: 'var(--da-color-fg)' }}>
          {t('示範 Tenant', 'Demo Tenants')}
        </label>
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 'var(--da-space-2)' }}>
          {DEMO_TENANTS.map(tenant => (
            <button
              key={tenant}
              onClick={() => toggleTenant(tenant)}
              style={{
                padding: 'var(--da-space-2) var(--da-space-3)',
                borderRadius: 'var(--da-radius-md)',
                border: (config.selectedTenants || []).includes(tenant) ? '2px solid var(--da-color-accent)' : '1px solid var(--da-color-surface-border)',
                backgroundColor: (config.selectedTenants || []).includes(tenant) ? 'var(--da-color-accent-soft)' : 'var(--da-color-bg)',
                color: 'var(--da-color-fg)',
                fontWeight: (config.selectedTenants || []).includes(tenant) ? 'var(--da-font-weight-semibold)' : 'normal',
                cursor: 'pointer',
                fontSize: 'var(--da-font-size-sm)',
              }}
            >
              {tenant}
            </button>
          ))}
        </div>
      </div>

      {/* Custom tenant input */}
      <div style={{
        padding: 'var(--da-space-4)',
        border: '1px solid var(--da-color-surface-border)',
        borderRadius: 'var(--da-radius-md)',
        backgroundColor: 'var(--da-color-surface)',
      }}>
        <label style={{ display: 'block', marginBottom: 'var(--da-space-2)', fontWeight: 'var(--da-font-weight-semibold)', color: 'var(--da-color-fg)' }}>
          {t('輸入自訂 Tenant 名稱', 'Enter Custom Tenant Name')}
        </label>
        <div style={{ display: 'flex', gap: 'var(--da-space-2)' }}>
          <input
            type="text"
            value={customTenant}
            onChange={(e) => setCustomTenant(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && addCustomTenant()}
            style={{
              flex: 1,
              padding: 'var(--da-space-2) var(--da-space-3)',
              border: '1px solid var(--da-color-surface-border)',
              borderRadius: 'var(--da-radius-md)',
              fontSize: 'var(--da-font-size-sm)',
              backgroundColor: 'var(--da-color-bg)',
              color: 'var(--da-color-fg)',
              boxSizing: 'border-box',
            }}
            placeholder="e.g., my-prod-tenant"
          />
          <button
            onClick={addCustomTenant}
            disabled={!customTenant.trim() || !validateTenantName(customTenant.trim())}
            style={{
              padding: 'var(--da-space-2) var(--da-space-4)',
              backgroundColor: !customTenant.trim() || !validateTenantName(customTenant.trim()) ? 'var(--da-color-muted)' : 'var(--da-color-accent)',
              color: 'white',
              border: 'none',
              borderRadius: 'var(--da-radius-md)',
              fontWeight: 'var(--da-font-weight-semibold)',
              cursor: !customTenant.trim() || !validateTenantName(customTenant.trim()) ? 'not-allowed' : 'pointer',
              fontSize: 'var(--da-font-size-sm)',
            }}
          >
            {t('加入', 'Add')}
          </button>
        </div>
        {customTenant.trim() && !validateTenantName(customTenant.trim()) && (
          <p style={{ fontSize: 'var(--da-font-size-xs)', color: 'var(--da-color-danger)', marginTop: 'var(--da-space-1)' }}>
            ✗ {t('無效的 tenant 名稱。必須符合 RFC 1123', 'Invalid tenant name. Must comply with RFC 1123')}
          </p>
        )}
      </div>

      {/* Selected tenants */}
      {(config.selectedTenants || []).length > 0 && (
        <div style={{
          padding: 'var(--da-space-3)',
          backgroundColor: 'var(--da-color-success-soft)',
          border: '1px solid var(--da-color-success)',
          borderRadius: 'var(--da-radius-md)',
          fontSize: 'var(--da-font-size-sm)',
          color: 'var(--da-color-fg)',
        }}>
          <p style={{ fontWeight: 'var(--da-font-weight-semibold)', marginBottom: 'var(--da-space-1)' }}>
            {t('已選擇 ', 'Selected ')}{config.selectedTenants.length}{t(' 個 Tenant', ' tenant(s)')}
          </p>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 'var(--da-space-1)' }}>
            {config.selectedTenants.map(t => (
              <span key={t} style={{
                display: 'inline-flex',
                alignItems: 'center',
                gap: 'var(--da-space-1)',
                padding: 'var(--da-space-1) var(--da-space-2)',
                backgroundColor: 'var(--da-color-bg)',
                borderRadius: 'var(--da-radius-sm)',
                fontSize: 'var(--da-font-size-xs)',
              }}>
                {t}
                <button
                  onClick={() => toggleTenant(t)}
                  style={{
                    background: 'none',
                    border: 'none',
                    cursor: 'pointer',
                    fontSize: 'var(--da-font-size-sm)',
                    color: 'var(--da-color-danger)',
                    padding: 0,
                  }}
                >
                  ×
                </button>
              </span>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function StepReview({ config }) {
  const [activeTab, setActiveTab] = useState('command');
  const selectedTenants = config.selectedTenants || [];

  const copyToClipboard = useCallback((text, label) => {
    navigator.clipboard.writeText(text);
    alert(t(`已複製${label}`, `Copied ${label}`));
  }, []);

  const generatedCommand = generateOperatorCommand(config);
  const migrationCommand = generateMigrationCommand(config);
  const firstTenantConfig = generateAlertmanagerConfigPreview(config, 0);

  return (
    <div className="space-y-4">
      <div>
        <h3 style={{ fontSize: 'var(--da-font-size-lg)', fontWeight: 'var(--da-font-weight-bold)', marginBottom: 'var(--da-space-2)' }}>
          {t('第五步：產出與檢視', 'Step 5: Review & Generate')}
        </h3>
        <p style={{ fontSize: 'var(--da-font-size-sm)', color: 'var(--da-color-muted)', marginBottom: 'var(--da-space-4)' }}>
          {t('檢查生成的命令和 CRD 配置。複製並在你的環境中執行。', 'Review generated commands and CRD configs. Copy and run in your environment.')}
        </p>
      </div>

      {/* Configuration Summary */}
      <div style={{
        padding: 'var(--da-space-4)',
        backgroundColor: 'var(--da-color-info-soft)',
        border: '1px solid var(--da-color-info)',
        borderRadius: 'var(--da-radius-md)',
      }}>
        <h4 style={{ fontWeight: 'var(--da-font-weight-semibold)', marginBottom: 'var(--da-space-2)', color: 'var(--da-color-fg)' }}>
          {t('配置摘要', 'Configuration Summary')}
        </h4>
        <div style={{
          display: 'grid',
          gridTemplateColumns: 'repeat(auto-fit, minmax(200px, 1fr))',
          gap: 'var(--da-space-2)',
          fontSize: 'var(--da-font-size-sm)',
          color: 'var(--da-color-fg)',
        }}>
          <div><strong>{t('Operator 版本: ', 'Operator Version: ')}</strong>{config.operatorVersion || 'N/A'}</div>
          <div><strong>{t('CRD API: ', 'CRD API: ')}</strong>monitoring.coreos.com/{config.crdVersion || 'v1'}</div>
          <div><strong>{t('Namespace: ', 'Namespace: ')}</strong>{config.namespace || 'monitoring'}</div>
          <div><strong>{t('部署模式: ', 'Rule Mode: ')}</strong>{config.ruleMode || 'N/A'}</div>
          <div><strong>{t('Receiver: ', 'Receiver: ')}</strong>{config.receiverType || 'N/A'}</div>
          <div><strong>{t('Tenant 數: ', 'Tenant Count: ')}</strong>{selectedTenants.length}</div>
        </div>
      </div>

      {/* Tabs */}
      <div style={{ display: 'flex', gap: 'var(--da-space-1)', borderBottom: '1px solid var(--da-color-surface-border)' }}>
        {['command', 'migration', 'alertmanager', 'checklist'].map(tab => (
          <button
            key={tab}
            onClick={() => setActiveTab(tab)}
            style={{
              padding: 'var(--da-space-2) var(--da-space-3)',
              borderBottom: activeTab === tab ? '2px solid var(--da-color-accent)' : 'none',
              background: 'none',
              border: 'none',
              cursor: 'pointer',
              fontWeight: activeTab === tab ? 'var(--da-font-weight-semibold)' : 'normal',
              color: activeTab === tab ? 'var(--da-color-accent)' : 'var(--da-color-muted)',
              fontSize: 'var(--da-font-size-sm)',
            }}
          >
            {tab === 'command' && t('Operator 命令', 'Operator Command')}
            {tab === 'migration' && t('遷移命令', 'Migration Command')}
            {tab === 'alertmanager' && t('AlertmanagerConfig', 'AlertmanagerConfig')}
            {tab === 'checklist' && t('檢查清單', 'Checklist')}
          </button>
        ))}
      </div>

      {/* Tab Content */}
      <div style={{
        padding: 'var(--da-space-4)',
        backgroundColor: 'var(--da-color-surface)',
        border: '1px solid var(--da-color-surface-border)',
        borderRadius: 'var(--da-radius-md)',
      }}>
        {activeTab === 'command' && (
          <div className="space-y-3">
            <pre style={{
              backgroundColor: 'var(--da-color-bg)',
              padding: 'var(--da-space-3)',
              borderRadius: 'var(--da-radius-sm)',
              overflow: 'auto',
              fontSize: 'var(--da-font-size-xs)',
              color: 'var(--da-color-fg)',
              fontFamily: 'monospace',
              whiteSpace: 'pre-wrap',
              wordBreak: 'break-word',
            }}>
              {generatedCommand}
            </pre>
            <button
              onClick={() => copyToClipboard(generatedCommand, t('命令', 'command'))}
              style={{
                padding: 'var(--da-space-2) var(--da-space-3)',
                backgroundColor: 'var(--da-color-accent)',
                color: 'white',
                border: 'none',
                borderRadius: 'var(--da-radius-md)',
                fontWeight: 'var(--da-font-weight-semibold)',
                cursor: 'pointer',
                fontSize: 'var(--da-font-size-sm)',
              }}
            >
              📋 {t('複製命令', 'Copy Command')}
            </button>
          </div>
        )}

        {activeTab === 'migration' && (
          <div className="space-y-3">
            {migrationCommand ? (
              <>
                <pre style={{
                  backgroundColor: 'var(--da-color-bg)',
                  padding: 'var(--da-space-3)',
                  borderRadius: 'var(--da-radius-sm)',
                  overflow: 'auto',
                  fontSize: 'var(--da-font-size-xs)',
                  color: 'var(--da-color-fg)',
                  fontFamily: 'monospace',
                  whiteSpace: 'pre-wrap',
                  wordBreak: 'break-word',
                }}>
                  {migrationCommand}
                </pre>
                <button
                  onClick={() => copyToClipboard(migrationCommand, t('遷移命令', 'migration command'))}
                  style={{
                    padding: 'var(--da-space-2) var(--da-space-3)',
                    backgroundColor: 'var(--da-color-accent)',
                    color: 'white',
                    border: 'none',
                    borderRadius: 'var(--da-radius-md)',
                    fontWeight: 'var(--da-font-weight-semibold)',
                    cursor: 'pointer',
                    fontSize: 'var(--da-font-size-sm)',
                  }}
                >
                  📋 {t('複製遷移命令', 'Copy Migration Command')}
                </button>
              </>
            ) : (
              <p style={{ color: 'var(--da-color-muted)', fontSize: 'var(--da-font-size-sm)' }}>
                {t('遷移命令只在雙堆棧模式下生成', 'Migration commands only generated for dual-stack mode')}
              </p>
            )}
          </div>
        )}

        {activeTab === 'alertmanager' && (
          <div className="space-y-3">
            <p style={{ fontSize: 'var(--da-font-size-xs)', color: 'var(--da-color-muted)', marginBottom: 'var(--da-space-2)' }}>
              {t('第 1 個 Tenant 的 AlertmanagerConfig 預覽:', 'AlertmanagerConfig preview for tenant 1:')}
            </p>
            <pre style={{
              backgroundColor: 'var(--da-color-bg)',
              padding: 'var(--da-space-3)',
              borderRadius: 'var(--da-radius-sm)',
              overflow: 'auto',
              fontSize: 'var(--da-font-size-xs)',
              color: 'var(--da-color-fg)',
              fontFamily: 'monospace',
              whiteSpace: 'pre-wrap',
              wordBreak: 'break-word',
              maxHeight: '300px',
            }}>
              {firstTenantConfig || '(No data)'}
            </pre>
            <button
              onClick={() => copyToClipboard(firstTenantConfig, t('AlertmanagerConfig', 'AlertmanagerConfig'))}
              style={{
                padding: 'var(--da-space-2) var(--da-space-3)',
                backgroundColor: 'var(--da-color-accent)',
                color: 'white',
                border: 'none',
                borderRadius: 'var(--da-radius-md)',
                fontWeight: 'var(--da-font-weight-semibold)',
                cursor: 'pointer',
                fontSize: 'var(--da-font-size-sm)',
              }}
            >
              📋 {t('複製 YAML', 'Copy YAML')}
            </button>
          </div>
        )}

        {activeTab === 'checklist' && (
          <div style={{ fontSize: 'var(--da-font-size-sm)', color: 'var(--da-color-fg)' }}>
            <ul style={{ listStyleType: 'none', padding: 0, lineHeight: '1.8' }}>
              <li style={{ marginBottom: 'var(--da-space-2)' }}>☐ {t('確認 Prometheus Operator 已安裝且版本正確', 'Confirm Prometheus Operator is installed with correct version')}</li>
              <li style={{ marginBottom: 'var(--da-space-2)' }}>☐ {t('在 monitoring namespace 建立 Secret（認證資訊）', 'Create Secret in monitoring namespace with credentials')}</li>
              <li style={{ marginBottom: 'var(--da-space-2)' }}>☐ {t('執行 operator-generate 命令產生 PrometheusRule CRD', 'Run operator-generate command to produce PrometheusRule CRDs')}</li>
              <li style={{ marginBottom: 'var(--da-space-2)' }}>☐ {t('套用生成的 YAML 到 Kubernetes 叢集', 'Apply generated YAML to Kubernetes cluster')}</li>
              <li style={{ marginBottom: 'var(--da-space-2)' }}>☐ {t('驗證 PrometheusRule 資源已建立：kubectl get prometheusrules', 'Verify PrometheusRules created: kubectl get prometheusrules')}</li>
              <li style={{ marginBottom: 'var(--da-space-2)' }}>☐ {t('檢查 Prometheus targets 和 Rule evaluation 狀態', 'Check Prometheus targets and Rule evaluation status')}</li>
              {config.ruleMode === 'dual-stack' && (
                <>
                  <li style={{ marginBottom: 'var(--da-space-2)' }}>☐ {t('測試 duplicate 規則檢測（警告/錯誤）', 'Test duplicate rule detection (warnings/errors)')}</li>
                  <li style={{ marginBottom: 'var(--da-space-2)' }}>☐ {t('逐步從 ConfigMap 遷移到 PrometheusRule CRD', 'Gradually migrate from ConfigMap to PrometheusRule CRD')}</li>
                  <li style={{ marginBottom: 'var(--da-space-2)' }}>☐ {t('完全遷移後，移除 ConfigMap 中的規則', 'Remove rules from ConfigMap after complete migration')}</li>
                </>
              )}
              <li style={{ marginBottom: 'var(--da-space-2)' }}>☐ {t('監控告警是否正確路由到 receiver', 'Monitor if alerts route correctly to receiver')}</li>
              <li>☐ {t('根據文件更新 CHANGELOG 和部署說明', 'Update CHANGELOG and deployment docs')}</li>
            </ul>
          </div>
        )}
      </div>

      {/* Footer note */}
      <div style={{
        padding: 'var(--da-space-3)',
        backgroundColor: 'var(--da-color-warning-soft)',
        border: '1px solid var(--da-color-warning)',
        borderRadius: 'var(--da-radius-md)',
        fontSize: 'var(--da-font-size-xs)',
        color: 'var(--da-color-fg)',
        lineHeight: '1.6',
      }}>
        <p style={{ fontWeight: 'var(--da-font-weight-semibold)', marginBottom: 'var(--da-space-1)' }}>
          ⚠️ {t('重要提示', 'Important Notes')}
        </p>
        <ul style={{ marginLeft: 'var(--da-space-4)', listStyleType: 'disc' }}>
          <li>{t('在生產環境中執行之前，務必在 staging 環境測試', 'Always test in staging before running in production')}</li>
          <li>{t('備份現有的 rule 配置', 'Back up existing rule configurations')}</li>
          <li>{t('檢查 RBAC 和 Secret 訪問權限', 'Verify RBAC and Secret access permissions')}</li>
          <li>{t('詳見: docs/adr/operator-integration.md', 'See: docs/adr/operator-integration.md')}</li>
        </ul>
      </div>
    </div>
  );
}

/* ── Main Component ── */

export default function OperatorSetupWizard() {
  const [currentStep, setCurrentStep] = useState(0);
  const [config, setConfig] = useState({});
  const [helpOpen, setHelpOpen] = useState(false);

  const canProceed = useMemo(() => {
    const step = STEPS[currentStep];
    if (step.id === 'environment') {
      return config.kubePromInstalled && config.operatorVersion && config.clusterType;
    }
    if (step.id === 'crd-config') {
      return config.namespace && config.ruleMode;
    }
    if (step.id === 'receiver') {
      return config.receiverType && config.receiverSecret;
    }
    if (step.id === 'tenants') {
      return config.selectedTenants && config.selectedTenants.length > 0;
    }
    return true;
  }, [currentStep, config]);

  const handleReset = useCallback(() => {
    if (confirm(t('確定要重置所有設定？', 'Reset all settings?'))) {
      setConfig({});
      setCurrentStep(0);
      setHelpOpen(false);
    }
  }, []);

  const stepContent = {
    environment: <StepEnvironment config={config} onChange={setConfig} helpOpen={helpOpen} setHelpOpen={setHelpOpen} />,
    'crd-config': <StepCRDConfig config={config} onChange={setConfig} helpOpen={helpOpen} setHelpOpen={setHelpOpen} />,
    receiver: <StepReceiver config={config} onChange={setConfig} helpOpen={helpOpen} setHelpOpen={setHelpOpen} />,
    tenants: <StepTenants config={config} onChange={setConfig} helpOpen={helpOpen} setHelpOpen={setHelpOpen} />,
    review: <StepReview config={config} />,
  };

  return (
    <main role="main" className="wizard" style={{
      minHeight: '100vh',
      background: 'var(--da-color-bg)',
      padding: 'var(--da-space-8)',
    }}>
      <div style={{ maxWidth: '900px', margin: '0 auto' }}>
        {/* Header */}
        <div style={{ marginBottom: 'var(--da-space-8)' }}>
          <h1 style={{
            fontSize: 'var(--da-font-size-3xl)',
            fontWeight: 'var(--da-font-weight-bold)',
            color: 'var(--da-color-fg)',
            marginBottom: 'var(--da-space-2)',
          }}>
            {t('Operator 設定精靈', 'Operator Setup Wizard')}
          </h1>
          <p style={{ fontSize: 'var(--da-font-size-base)', color: 'var(--da-color-muted)' }}>
            {t('五步流程引導設定 Prometheus Operator 整合和告警路由。', 'Five-step guided setup for Prometheus Operator integration and alert routing.')}
          </p>
        </div>

        {/* Progress Stepper */}
        <div style={{ marginBottom: 'var(--da-space-8)' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', gap: 'var(--da-space-1)', marginBottom: 'var(--da-space-2)' }}>
            {STEPS.map((step, idx) => (
              <button
                key={step.id}
                onClick={() => setCurrentStep(idx)}
                data-testid={`step-${step.id}`}
                className="wizard-step"
                aria-current={idx === currentStep ? 'step' : undefined}
                aria-label={`${t('步驟', 'Step')} ${idx + 1}: ${step.label()}`}
                style={{
                  flex: 1,
                  padding: 'var(--da-space-2)',
                  borderRadius: 'var(--da-radius-md)',
                  fontWeight: 'var(--da-font-weight-semibold)',
                  fontSize: 'var(--da-font-size-xs-sm)',
                  border: 'none',
                  cursor: 'pointer',
                  transition: 'all 200ms ease',
                  backgroundColor: idx === currentStep
                    ? 'var(--da-color-accent)'
                    : idx < currentStep
                    ? 'var(--da-color-success-soft)'
                    : 'var(--da-color-surface)',
                  color: idx === currentStep
                    ? 'white'
                    : idx < currentStep
                    ? 'var(--da-color-success)'
                    : 'var(--da-color-muted)',
                }}
              >
                {idx < currentStep && '✓ '}{step.label()}
              </button>
            ))}
          </div>
          <div style={{
            width: '100%',
            height: '4px',
            backgroundColor: 'var(--da-color-surface)',
            borderRadius: 'var(--da-radius-full)',
            overflow: 'hidden',
          }}>
            <div
              style={{
                height: '100%',
                backgroundColor: 'var(--da-color-accent)',
                width: `${((currentStep + 1) / STEPS.length) * 100}%`,
                transition: 'width 300ms ease',
              }}
            />
          </div>
        </div>

        {/* Step Content */}
        <div style={{
          backgroundColor: 'var(--da-color-surface)',
          borderRadius: 'var(--da-radius-lg)',
          boxShadow: 'var(--da-shadow-subtle)',
          padding: 'var(--da-space-6)',
          marginBottom: 'var(--da-space-6)',
        }}>
          {stepContent[STEPS[currentStep].id]}
        </div>

        {/* Navigation */}
        <div style={{ display: 'flex', justifyContent: 'space-between', gap: 'var(--da-space-3)' }}>
          <button
            onClick={handleReset}
            style={{
              padding: 'var(--da-space-2) var(--da-space-4)',
              backgroundColor: 'var(--da-color-surface)',
              color: 'var(--da-color-fg)',
              border: '1px solid var(--da-color-surface-border)',
              borderRadius: 'var(--da-radius-md)',
              fontWeight: 'var(--da-font-weight-semibold)',
              fontSize: 'var(--da-font-size-sm)',
              cursor: 'pointer',
            }}
          >
            🔄 {t('重置', 'Reset')}
          </button>

          <div style={{ display: 'flex', gap: 'var(--da-space-2)' }}>
            <button
              onClick={() => setCurrentStep(Math.max(0, currentStep - 1))}
              disabled={currentStep === 0}
              style={{
                padding: 'var(--da-space-2) var(--da-space-4)',
                backgroundColor: currentStep === 0 ? 'var(--da-color-surface)' : 'var(--da-color-surface)',
                color: currentStep === 0 ? 'var(--da-color-muted)' : 'var(--da-color-fg)',
                border: '1px solid var(--da-color-surface-border)',
                borderRadius: 'var(--da-radius-md)',
                fontWeight: 'var(--da-font-weight-semibold)',
                fontSize: 'var(--da-font-size-sm)',
                cursor: currentStep === 0 ? 'not-allowed' : 'pointer',
                opacity: currentStep === 0 ? 0.5 : 1,
              }}
            >
              ← {t('上一步', 'Back')}
            </button>
            <button
              onClick={() => setCurrentStep(Math.min(STEPS.length - 1, currentStep + 1))}
              disabled={!canProceed || currentStep === STEPS.length - 1}
              style={{
                padding: 'var(--da-space-2) var(--da-space-4)',
                backgroundColor: !canProceed || currentStep === STEPS.length - 1 ? 'var(--da-color-muted)' : 'var(--da-color-accent)',
                color: 'white',
                border: 'none',
                borderRadius: 'var(--da-radius-md)',
                fontWeight: 'var(--da-font-weight-semibold)',
                fontSize: 'var(--da-font-size-sm)',
                cursor: !canProceed || currentStep === STEPS.length - 1 ? 'not-allowed' : 'pointer',
                opacity: !canProceed || currentStep === STEPS.length - 1 ? 0.5 : 1,
              }}
            >
              {currentStep === STEPS.length - 1 ? t('完成', 'Done') : t('下一步', 'Next')} →
            </button>
          </div>
        </div>

        {/* Helpful tip */}
        <div style={{
          padding: 'var(--da-space-4)',
          backgroundColor: 'var(--da-color-info-soft)',
          border: '1px solid var(--da-color-info)',
          borderRadius: 'var(--da-radius-md)',
          fontSize: 'var(--da-font-size-sm)',
          color: 'var(--da-color-fg)',
        }}>
          💡 {t('提示：本精靈會產生 da-tools 命令和 Kubernetes CRD YAML。複製指令到你的 CI/CD 流程中執行。詳見文件。', 'Tip: This wizard generates da-tools commands and Kubernetes CRD YAML. Copy commands to your CI/CD pipeline. See docs for details.')}
        </div>
      </div>
    </main>
  );
}
