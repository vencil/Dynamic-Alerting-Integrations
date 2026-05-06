---
title: "Operator Setup Wizard — command + config generators"
purpose: |
  Pure functions that build the artifacts the wizard ships back to
  the user: `da-tools operator-generate` command line, the optional
  `migrate-to-operator --dry-run` migration command, and a sample
  AlertmanagerConfig CRD YAML for preview.

  Pre-PR-portal-4 these were inline at the top of operator-setup-
  wizard.jsx. Splitting them out drops 75 LOC from the orchestrator
  and lets the StepReview component (also extracted in PR-portal-4)
  pull from the same canonical implementation.

  Public API:
    window.__validateTenantName(name)               RFC 1123 char check
    window.__generateOperatorCommand(config)        build da-tools CLI
    window.__generateMigrationCommand(config)       null unless dual-stack
    window.__generateAlertmanagerConfigPreview(c,i) sample CRD for tenant i
    window.__getReceiverConfig(type, secretName)    YAML fragment per receiver

  Closure deps: none. Pure functions; receive config as arg.
---

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

window.__validateTenantName = validateTenantName;
window.__generateOperatorCommand = generateOperatorCommand;
window.__generateMigrationCommand = generateMigrationCommand;
window.__generateAlertmanagerConfigPreview = generateAlertmanagerConfigPreview;
window.__getReceiverConfig = getReceiverConfig;

// TD-030e: ESM exports. Removed in TD-030z.
// <!-- jsx-loader-compat: ignore -->
export { validateTenantName, generateOperatorCommand, generateMigrationCommand, generateAlertmanagerConfigPreview, getReceiverConfig };
