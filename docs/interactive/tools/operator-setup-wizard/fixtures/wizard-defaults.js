---
title: "Operator Setup Wizard — Default catalogs"
purpose: |
  Static data tables for the 5-step Operator Setup Wizard: step
  metadata, demo tenant list, supported Operator versions, cluster
  types, receiver types, and rule deployment modes.

  Pre-PR-portal-4 these were inline at the top of operator-setup-
  wizard.jsx. Splitting them out drops 60 LOC from the orchestrator
  and lets the deployment-wizard / cicd-setup-wizard reuse the
  CLUSTER_TYPES + RECEIVER_TYPES tables in a follow-up.

  Public API:
    window.__OSW_STEPS                ordered step metadata
    window.__OSW_DEMO_TENANTS         pre-baked demo tenant ids
    window.__OSW_OPERATOR_VERSIONS    Prometheus Operator version matrix
    window.__OSW_CLUSTER_TYPES        K8s cluster type catalog
    window.__OSW_RECEIVER_TYPES       Alertmanager receiver type catalog
    window.__OSW_RULE_MODES           rule deployment mode catalog

  Closure deps: reads window.__t at consumer call time.
---

const t = window.__t || ((zh, en) => en);

const OSW_STEPS = [
  { id: 'environment', label: () => t('環境偵測', 'Environment Detection') },
  { id: 'crd-config', label: () => t('CRD 配置', 'CRD Configuration') },
  { id: 'receiver', label: () => t('Receiver 設定', 'Receiver Setup') },
  { id: 'tenants', label: () => t('Tenant 選擇', 'Tenant Selection') },
  { id: 'review', label: () => t('產出 & 檢視', 'Review & Generate') },
];

const OSW_DEMO_TENANTS = [
  'db-a', 'db-b', 'web-prod', 'cache-staging',
  'analytics-dev', 'kafka-prod', 'redis-cache'
];

const OSW_OPERATOR_VERSIONS = [
  { id: 'v0.65+', label: 'v0.65+ (Latest)', apiVersion: 'v1beta1', recommended: true },
  { id: 'v0.50-v0.64', label: 'v0.50 to v0.64', apiVersion: 'v1', recommended: false },
  { id: 'older', label: 'Older (v0.49)', apiVersion: 'v1alpha1', recommended: false },
];

const OSW_CLUSTER_TYPES = [
  { id: 'kind', label: 'Kind (Local)', icon: '💻' },
  { id: 'eks', label: 'EKS (AWS)', icon: '🔶' },
  { id: 'gke', label: 'GKE (Google Cloud)', icon: '☁️' },
  { id: 'aks', label: 'AKS (Azure)', icon: '🟦' },
  { id: 'onprem', label: 'On-Premises', icon: '🏢' },
  { id: 'other', label: 'Other', icon: '❓' },
];

const OSW_RECEIVER_TYPES = [
  { id: 'slack', label: 'Slack', icon: '💬', defaultPort: 80 },
  { id: 'pagerduty', label: 'PagerDuty', icon: '📱', defaultPort: 443 },
  { id: 'email', label: 'Email (SMTP)', icon: '📧', defaultPort: 587 },
  { id: 'teams', label: 'Microsoft Teams', icon: '🟦', defaultPort: 443 },
  { id: 'opsgenie', label: 'OpsGenie', icon: '🚨', defaultPort: 443 },
  { id: 'webhook', label: 'Custom Webhook', icon: '🪝', defaultPort: 443 },
];

const OSW_RULE_MODES = [
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

window.__OSW_STEPS = OSW_STEPS;
window.__OSW_DEMO_TENANTS = OSW_DEMO_TENANTS;
window.__OSW_OPERATOR_VERSIONS = OSW_OPERATOR_VERSIONS;
window.__OSW_CLUSTER_TYPES = OSW_CLUSTER_TYPES;
window.__OSW_RECEIVER_TYPES = OSW_RECEIVER_TYPES;
window.__OSW_RULE_MODES = OSW_RULE_MODES;
