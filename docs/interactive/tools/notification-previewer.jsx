---
title: "Notification Template Editor"
tags: [notification, template, editing, export, receiver, validation]
audience: ["platform-engineer", "tenant"]
version: v2.7.0
lang: en
related: [self-service-portal, alert-simulator, template-gallery]
---

import React, { useState, useMemo, useCallback, useRef, useEffect } from 'react';

const t = window.__t || ((zh, en) => en);

/* ── Design token styles ── */
const styles = {
  container: {
    maxWidth: '1400px',
    margin: '0 auto',
    padding: 'var(--da-space-4)',
    fontFamily: 'var(--da-font-family)',
    color: 'var(--da-color-fg)',
    backgroundColor: 'var(--da-color-bg)',
    transition: 'all var(--da-transition-base)',
  },
  header: {
    marginBottom: 'var(--da-space-6)',
  },
  title: {
    fontSize: 'var(--da-font-size-2xl)',
    fontWeight: 'var(--da-font-weight-bold)',
    color: 'var(--da-color-fg)',
    margin: '0 0 var(--da-space-2) 0',
  },
  subtitle: {
    fontSize: 'var(--da-font-size-base)',
    color: 'var(--da-color-muted)',
    margin: 0,
  },
  gridLayout: {
    display: 'grid',
    gridTemplateColumns: '1fr 1.2fr 1fr',
    gap: 'var(--da-space-6)',
    '@media (max-width: 1200px)': {
      gridTemplateColumns: '1fr',
    },
  },
  section: {
    backgroundColor: 'var(--da-color-surface)',
    border: `1px solid var(--da-color-surface-border)`,
    borderRadius: 'var(--da-radius-lg)',
    padding: 'var(--da-space-4)',
    boxShadow: 'var(--da-shadow-subtle)',
  },
  sectionTitle: {
    fontSize: 'var(--da-font-size-md)',
    fontWeight: 'var(--da-font-weight-semibold)',
    color: 'var(--da-color-fg)',
    marginBottom: 'var(--da-space-3)',
    display: 'flex',
    alignItems: 'center',
    gap: 'var(--da-space-2)',
  },
  space: {
    marginBottom: 'var(--da-space-4)',
  },
  spaceSmall: {
    marginBottom: 'var(--da-space-2)',
  },
  label: {
    display: 'block',
    fontSize: 'var(--da-font-size-sm)',
    fontWeight: 'var(--da-font-weight-semibold)',
    color: 'var(--da-color-fg)',
    marginBottom: 'var(--da-space-2)',
  },
  button: {
    padding: `var(--da-space-2) var(--da-space-3)`,
    fontSize: 'var(--da-font-size-sm)',
    fontWeight: 'var(--da-font-weight-medium)',
    border: 'none',
    borderRadius: 'var(--da-radius-md)',
    cursor: 'pointer',
    transition: 'all var(--da-transition-fast)',
    display: 'inline-flex',
    alignItems: 'center',
    gap: 'var(--da-space-2)',
  },
  buttonPrimary: {
    backgroundColor: 'var(--da-color-accent)',
    color: 'var(--da-color-accent-fg)',
    border: 'none',
  },
  buttonPrimaryHover: {
    backgroundColor: 'var(--da-color-accent-hover)',
    boxShadow: 'var(--da-shadow-hover)',
  },
  buttonSecondary: {
    backgroundColor: 'transparent',
    color: 'var(--da-color-accent)',
    border: `1px solid var(--da-color-accent)`,
  },
  buttonSecondaryHover: {
    backgroundColor: 'var(--da-color-accent-soft)',
  },
  buttonSmall: {
    width: '100%',
    padding: `var(--da-space-2) var(--da-space-3)`,
    marginBottom: 'var(--da-space-1)',
    textAlign: 'left',
    borderRadius: 'var(--da-radius-md)',
    fontSize: 'var(--da-font-size-sm)',
    transition: 'all var(--da-transition-fast)',
    border: `1px solid var(--da-color-surface-border)`,
    backgroundColor: 'var(--da-color-surface)',
    color: 'var(--da-color-fg)',
    cursor: 'pointer',
  },
  buttonSmallActive: {
    border: `2px solid var(--da-color-accent)`,
    backgroundColor: 'var(--da-color-accent-soft)',
  },
  input: {
    width: '100%',
    padding: `var(--da-space-2) var(--da-space-3)`,
    fontSize: 'var(--da-font-size-base)',
    fontFamily: 'var(--da-font-family)',
    border: `1px solid var(--da-color-surface-border)`,
    borderRadius: 'var(--da-radius-md)',
    backgroundColor: 'var(--da-color-surface)',
    color: 'var(--da-color-fg)',
    transition: 'border-color var(--da-transition-fast)',
    boxSizing: 'border-box',
  },
  inputFocus: {
    borderColor: 'var(--da-color-accent)',
    outline: 'none',
    boxShadow: `0 0 0 2px var(--da-color-focus-ring)`,
  },
  textarea: {
    width: '100%',
    padding: `var(--da-space-3) var(--da-space-3)`,
    fontSize: 'var(--da-font-size-sm)',
    fontFamily: 'var(--da-font-mono)',
    border: `1px solid var(--da-color-surface-border)`,
    borderRadius: 'var(--da-radius-md)',
    backgroundColor: 'var(--da-color-surface)',
    color: 'var(--da-color-fg)',
    lineHeight: 'var(--da-line-height-base)',
    minHeight: '120px',
    resize: 'vertical',
    boxSizing: 'border-box',
  },
  card: {
    backgroundColor: 'var(--da-color-card-bg)',
    border: `1px solid var(--da-color-card-border)`,
    borderRadius: 'var(--da-radius-lg)',
    padding: 'var(--da-space-4)',
    boxShadow: 'var(--da-shadow-subtle)',
    transition: 'all var(--da-transition-fast)',
  },
  cardHover: {
    borderColor: 'var(--da-color-card-hover-border)',
    boxShadow: 'var(--da-shadow-hover)',
  },
  badge: {
    display: 'inline-block',
    padding: `var(--da-space-1) var(--da-space-2)`,
    fontSize: 'var(--da-font-size-xs)',
    fontWeight: 'var(--da-font-weight-semibold)',
    backgroundColor: 'var(--da-color-tag-bg)',
    color: 'var(--da-color-tag-fg)',
    borderRadius: 'var(--da-radius-sm)',
  },
  badgeSuccess: {
    backgroundColor: 'var(--da-color-success-soft)',
    color: 'var(--da-color-success)',
  },
  badgeWarning: {
    backgroundColor: 'var(--da-color-warning-soft)',
    color: 'var(--da-color-warning)',
  },
  badgeError: {
    backgroundColor: 'var(--da-color-error-soft)',
    color: 'var(--da-color-error)',
  },
  badgeInfo: {
    backgroundColor: 'var(--da-color-info-soft)',
    color: 'var(--da-color-info)',
  },
  tagContainer: {
    display: 'flex',
    flexWrap: 'wrap',
    gap: 'var(--da-space-2)',
    marginBottom: 'var(--da-space-2)',
  },
  table: {
    width: '100%',
    borderCollapse: 'collapse',
    fontSize: 'var(--da-font-size-sm)',
  },
  tableRow: {
    borderBottom: `1px solid var(--da-color-surface-border)`,
  },
  tableCell: {
    padding: `var(--da-space-2) var(--da-space-3)`,
    textAlign: 'left',
  },
  tableCellHeader: {
    fontWeight: 'var(--da-font-weight-semibold)',
    backgroundColor: 'var(--da-color-surface-hover)',
    color: 'var(--da-color-fg)',
  },
  mono: {
    fontFamily: 'var(--da-font-mono)',
    fontSize: 'var(--da-font-size-sm)',
    padding: `var(--da-space-1) var(--da-space-2)`,
    backgroundColor: 'var(--da-color-surface-hover)',
    borderRadius: 'var(--da-radius-sm)',
    color: 'var(--da-color-fg)',
  },
  alertSuccess: {
    backgroundColor: 'var(--da-color-success-soft)',
    border: `1px solid var(--da-color-success)`,
    color: 'var(--da-color-success)',
  },
  alertWarning: {
    backgroundColor: 'var(--da-color-warning-soft)',
    border: `1px solid var(--da-color-warning)`,
    color: 'var(--da-color-warning)',
  },
  alertError: {
    backgroundColor: 'var(--da-color-error-soft)',
    border: `1px solid var(--da-color-error)`,
    color: 'var(--da-color-error)',
  },
  alertInfo: {
    backgroundColor: 'var(--da-color-info-soft)',
    border: `1px solid var(--da-color-info)`,
    color: 'var(--da-color-info)',
  },
  alertBox: {
    padding: 'var(--da-space-3)',
    borderRadius: 'var(--da-radius-md)',
    marginBottom: 'var(--da-space-4)',
    fontSize: 'var(--da-font-size-sm)',
  },
  previewBox: {
    backgroundColor: 'var(--da-color-surface-hover)',
    border: `1px solid var(--da-color-surface-border)`,
    borderRadius: 'var(--da-radius-md)',
    padding: 'var(--da-space-4)',
    minHeight: '200px',
    maxHeight: '400px',
    overflowY: 'auto',
  },
  codeBlock: {
    backgroundColor: 'var(--da-color-surface)',
    border: `1px solid var(--da-color-surface-border)`,
    borderRadius: 'var(--da-radius-md)',
    padding: 'var(--da-space-3)',
    fontFamily: 'var(--da-font-mono)',
    fontSize: 'var(--da-font-size-sm)',
    overflow: 'auto',
    maxHeight: '300px',
  },
};

/* ── Receiver types and their templates ── */
const RECEIVER_TYPES = {
  slack: {
    label: 'Slack',
    icon: '💬',
    defaultTemplate: {
      titleTemplate: '{{ .GroupLabels.alertname }}',
      bodyTemplate: '*Severity:* {{ .GroupLabels.severity }}\n*Tenant:* {{ .GroupLabels.tenant }}\n{{ .GroupAnnotations.summary }}',
      customLabels: {},
    },
    fieldLimits: { bodyTemplate: 3000 },
  },
  webhook: {
    label: 'Webhook (Generic)',
    icon: '🔗',
    defaultTemplate: {
      titleTemplate: '{{ .GroupLabels.alertname }}',
      bodyTemplate: '{{ toJson .Alerts }}',
      customLabels: {},
    },
    fieldLimits: {},
  },
  email: {
    label: 'Email',
    icon: '📧',
    defaultTemplate: {
      titleTemplate: '[{{ .GroupLabels.severity }}] {{ .GroupLabels.alertname }}',
      bodyTemplate: 'Tenant: {{ .GroupLabels.tenant }}\n\n{{ .GroupAnnotations.summary }}\n\nRunbook: {{ .GroupAnnotations.runbook_url }}',
      customLabels: {},
    },
    fieldLimits: {},
  },
  pagerduty: {
    label: 'PagerDuty',
    icon: '🚨',
    defaultTemplate: {
      titleTemplate: '[{{ .GroupLabels.severity | upper }}] {{ .GroupLabels.alertname }}',
      bodyTemplate: '{{ .GroupAnnotations.platform_summary }}',
      customLabels: { severity: 'critical' },
    },
    fieldLimits: {},
  },
  teams: {
    label: 'Microsoft Teams',
    icon: '🟦',
    defaultTemplate: {
      titleTemplate: '{{ .GroupLabels.alertname }}',
      bodyTemplate: '*Severity:* {{ .GroupLabels.severity }}\n*Tenant:* {{ .GroupLabels.tenant }}\n{{ .GroupAnnotations.summary }}',
      customLabels: {},
    },
    fieldLimits: {},
  },
  rocketchat: {
    label: 'Rocket.Chat',
    icon: '🚀',
    defaultTemplate: {
      titleTemplate: '{{ .GroupLabels.alertname }}',
      bodyTemplate: 'Severity: {{ .GroupLabels.severity }}\nTenant: {{ .GroupLabels.tenant }}\n{{ .GroupAnnotations.summary }}',
      customLabels: {},
    },
    fieldLimits: {},
  },
  opsgenie: {
    label: 'OpsGenie',
    icon: '🔔',
    defaultTemplate: {
      titleTemplate: '{{ .GroupLabels.alertname }}',
      bodyTemplate: '{{ .GroupAnnotations.summary }}',
      customLabels: { priority: 'P1' },
    },
    fieldLimits: {},
  },
};

const TEMPLATE_VARIABLES = [
  { name: '{{ .GroupLabels.alertname }}', desc: 'Alert rule name' },
  { name: '{{ .GroupLabels.severity }}', desc: 'Severity level' },
  { name: '{{ .GroupLabels.tenant }}', desc: 'Tenant ID' },
  { name: '{{ .GroupAnnotations.summary }}', desc: 'Alert summary (English)' },
  { name: '{{ .GroupAnnotations.summary_zh }}', desc: 'Alert summary (Chinese)' },
  { name: '{{ .GroupAnnotations.platform_summary }}', desc: 'Platform NOC view' },
  { name: '{{ .GroupAnnotations.runbook_url }}', desc: 'Runbook URL' },
  { name: '{{ .CommonLabels.rule_pack }}', desc: 'Rule pack name' },
  { name: '{{ .Status }}', desc: 'Alert status (firing/resolved)' },
  { name: '{{ .Alerts.Firing | len }}', desc: 'Number of firing alerts' },
  { name: '{{ toJson .Alerts }}', desc: 'Full alert payload as JSON' },
];

const TEMPLATE_PRESETS = {
  slack: [
    {
      name: t('詳細 (全欄位)', 'Detailed (all fields)'),
      template: {
        titleTemplate: '{{ .GroupLabels.alertname }}',
        bodyTemplate: '*Severity:* {{ .GroupLabels.severity }}\n*Tenant:* {{ .GroupLabels.tenant }}\n*Rule Pack:* {{ .CommonLabels.rule_pack }}\n\n{{ .GroupAnnotations.summary }}',
        customLabels: {},
      },
    },
    {
      name: t('簡潔 (嚴重度優先)', 'Compact (critical only)'),
      template: {
        titleTemplate: '[{{ .GroupLabels.severity | upper }}] {{ .GroupLabels.alertname }}',
        bodyTemplate: '{{ .GroupAnnotations.summary }}',
        customLabels: {},
      },
    },
    {
      name: t('雙語 (ZH+EN)', 'Bilingual (ZH+EN)'),
      template: {
        titleTemplate: '{{ .GroupLabels.alertname }}',
        bodyTemplate: '*English:* {{ .GroupAnnotations.summary }}\n*中文:* {{ .GroupAnnotations.summary_zh }}',
        customLabels: {},
      },
    },
  ],
  email: [
    {
      name: t('詳細 (全欄位)', 'Detailed (all fields)'),
      template: {
        titleTemplate: '[{{ .GroupLabels.severity }}] {{ .GroupLabels.alertname }} — {{ .GroupLabels.tenant }}',
        bodyTemplate: 'Severity: {{ .GroupLabels.severity }}\nTenant: {{ .GroupLabels.tenant }}\nRule Pack: {{ .CommonLabels.rule_pack }}\n\n{{ .GroupAnnotations.summary }}\n\nRunbook: {{ .GroupAnnotations.runbook_url }}',
        customLabels: {},
      },
    },
    {
      name: t('簡潔 (嚴重度優先)', 'Compact (critical only)'),
      template: {
        titleTemplate: '[{{ .GroupLabels.severity | upper }}] {{ .GroupLabels.alertname }}',
        bodyTemplate: '{{ .GroupAnnotations.summary }}',
        customLabels: {},
      },
    },
  ],
  webhook: [
    {
      name: t('完整 JSON 有效負載', 'Full JSON payload'),
      template: {
        titleTemplate: '{{ .GroupLabels.alertname }}',
        bodyTemplate: '{{ toJson .Alerts }}',
        customLabels: {},
      },
    },
  ],
};

/* ── Sample alert data ── */
const SAMPLE_ALERTS = [
  {
    GroupLabels: { alertname: 'MariaDBHighConnections', severity: 'warning', tenant: 'prod-mariadb' },
    CommonLabels: { rule_pack: 'mariadb' },
    GroupAnnotations: {
      summary: 'MySQL connections at 165 (threshold: 150)',
      summary_zh: 'MySQL 連線數達到 165（閾值：150）',
      platform_summary: '[prod-mariadb] MariaDB connections warning — 165/150',
      runbook_url: 'https://runbooks.example.com/mariadb-connections',
    },
    Status: 'firing',
    Alerts: [
      {
        Status: 'firing',
        Labels: { alertname: 'MariaDBHighConnections', severity: 'warning', tenant: 'prod-mariadb' },
        Annotations: { summary: 'MySQL connections at 165 (threshold: 150)', summary_zh: 'MySQL 連線數達到 165（閾值：150）' },
        StartsAt: new Date().toISOString(),
      },
    ],
  },
];

/* ── Template variable validator ── */
function validateTemplate(template) {
  const braceRegex = /\{\{|\}\}/g;
  let openCount = 0;
  let errors = [];
  let lastPos = 0;

  let match;
  const regex = /\{\{|\}\}/g;
  while ((match = regex.exec(template)) !== null) {
    if (match[0] === '{{') openCount++;
    else openCount--;

    if (openCount < 0) {
      errors.push({
        line: template.substring(0, match.index).split('\n').length,
        msg: t('未配對的 }}', 'Unmatched }}'),
      });
      openCount = 0;
    }
  }

  if (openCount > 0) {
    errors.push({ line: 1, msg: t('未關閉的 {{', 'Unclosed {{') });
  }

  return errors;
}

function extractVariables(template) {
  const regex = /\{\{\.[\w\.]+\}\}/g;
  const found = new Set();
  let match;
  while ((match = regex.exec(template)) !== null) {
    found.add(match[0]);
  }
  return Array.from(found);
}

/* ── Template preview renderer ── */
function renderTemplatePreview(template, alertData) {
  try {
    let result = template;

    // Simple Go template variable substitution
    const vars = extractVariables(template);
    vars.forEach(varName => {
      const path = varName.slice(2, -2); // Remove {{ and }}
      const keys = path.split('.');
      let value = alertData;
      for (let key of keys) {
        if (key) value = value[key] || `[${key}?]`;
      }
      result = result.replace(new RegExp(varName.replace(/\./g, '\\.'), 'g'), value);
    });

    return result;
  } catch (e) {
    return t('[無法預覽 - 範本錯誤]', '[Preview failed - template error]');
  }
}

/* ── Export functionality ── */
function generateYAML(receiverType, template) {
  const yaml = `# Notification Template for ${RECEIVER_TYPES[receiverType].label}
# Auto-generated by Notification Template Editor

${receiverType}:
  # Title/Subject template
  titleTemplate: |
    ${template.titleTemplate}

  # Body/Message template
  bodyTemplate: |
    ${template.bodyTemplate.split('\n').join('\n    ')}

  # Custom labels (if applicable)
  customLabels:
${Object.entries(template.customLabels).map(([k, v]) => `    ${k}: ${v}`).join('\n')}
`;
  return yaml;
}

function generateJSON(receiverType, template) {
  return JSON.stringify(
    {
      receiver_type: receiverType,
      receiver_label: RECEIVER_TYPES[receiverType].label,
      template: {
        title_template: template.titleTemplate,
        body_template: template.bodyTemplate,
        custom_labels: template.customLabels,
      },
      generated_at: new Date().toISOString(),
    },
    null,
    2
  );
}

/* ── Template Editor Panel ── */
function TemplateEditorPanel({ receiverType, template, onTemplateChange }) {
  const [showVariables, setShowVariables] = useState(false);
  const titleErrors = useMemo(() => validateTemplate(template.titleTemplate), [template.titleTemplate]);
  const bodyErrors = useMemo(() => validateTemplate(template.bodyTemplate), [template.bodyTemplate]);
  const usedVariables = useMemo(() => extractVariables(template.bodyTemplate), [template.bodyTemplate]);

  return (
    <div style={styles.space}>
      <div style={styles.sectionTitle}>
        {t('範本編輯', 'Template Editor')}
      </div>

      {/* Title template */}
      <div style={styles.spaceSmall}>
        <label style={styles.label}>{t('標題範本', 'Title Template')}</label>
        <textarea
          style={styles.textarea}
          value={template.titleTemplate}
          onChange={(e) => onTemplateChange({ ...template, titleTemplate: e.target.value })}
          placeholder="e.g., {{ .GroupLabels.alertname }}"
          aria-label={t('標題範本輸入', 'Title template input')}
        />
        {titleErrors.length > 0 && (
          <div style={{ ...styles.alertBox, ...styles.alertError }}>
            {titleErrors.map((err, i) => <div key={i}>⚠️ {err.msg}</div>)}
          </div>
        )}
      </div>

      {/* Body template */}
      <div style={styles.spaceSmall}>
        <label style={styles.label}>{t('內容範本', 'Body Template')}</label>
        <textarea
          style={styles.textarea}
          value={template.bodyTemplate}
          onChange={(e) => onTemplateChange({ ...template, bodyTemplate: e.target.value })}
          placeholder={`e.g., Severity: {{ .GroupLabels.severity }}\nTenant: {{ .GroupLabels.tenant }}`}
          aria-label={t('內容範本輸入', 'Body template input')}
        />
        {bodyErrors.length > 0 && (
          <div style={{ ...styles.alertBox, ...styles.alertError }}>
            {bodyErrors.map((err, i) => <div key={i}>⚠️ {err.msg}</div>)}
          </div>
        )}
        <div style={{ ...styles.alertBox, ...styles.alertInfo, marginTop: 'var(--da-space-2)' }}>
          ℹ️ {t('使用的變數：', 'Variables used:')} {usedVariables.length > 0 ? usedVariables.join(', ') : t('無', 'None')}
        </div>
      </div>

      {/* Variable autocomplete */}
      <button
        onClick={() => setShowVariables(!showVariables)}
        style={{ ...styles.button, ...styles.buttonSecondary, width: '100%', marginBottom: 'var(--da-space-3)' }}
        aria-label={t('顯示範本變數', 'Show template variables')}
      >
        {showVariables ? '▾' : '▸'} {t('可用變數', 'Available Variables')}
      </button>

      {showVariables && (
        <div style={{ ...styles.card, marginBottom: 'var(--da-space-3)' }}>
          <table style={styles.table}>
            <thead>
              <tr style={styles.tableRow}>
                <th style={{ ...styles.tableCell, ...styles.tableCellHeader }}>{t('變數', 'Variable')}</th>
                <th style={{ ...styles.tableCell, ...styles.tableCellHeader }}>{t('說明', 'Description')}</th>
              </tr>
            </thead>
            <tbody>
              {TEMPLATE_VARIABLES.map((v, i) => (
                <tr key={i} style={styles.tableRow}>
                  <td style={styles.tableCell}>
                    <code style={styles.mono}>{v.name}</code>
                  </td>
                  <td style={styles.tableCell}>{v.desc}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* Custom labels */}
      <div style={styles.spaceSmall}>
        <label style={styles.label}>{t('自訂標籤 (JSON)', 'Custom Labels (JSON)')}</label>
        <textarea
          style={styles.textarea}
          value={JSON.stringify(template.customLabels, null, 2)}
          onChange={(e) => {
            try {
              onTemplateChange({ ...template, customLabels: JSON.parse(e.target.value) });
            } catch (err) {
              // Keep existing on parse error
            }
          }}
          placeholder='{"key": "value"}'
          aria-label={t('自訂標籤輸入', 'Custom labels input')}
        />
      </div>
    </div>
  );
}

/* ── Export Panel ── */
function ExportPanel({ receiverType, template }) {
  const [copyFeedback, setCopyFeedback] = useState('');
  const yamlRef = useRef();
  const jsonRef = useRef();

  const handleCopy = (format) => {
    const content = format === 'yaml' ? generateYAML(receiverType, template) : generateJSON(receiverType, template);
    navigator.clipboard.writeText(content).then(() => {
      setCopyFeedback(t('已複製！', 'Copied!'));
      setTimeout(() => setCopyFeedback(''), 2000);
    });
  };

  return (
    <div style={styles.space}>
      <div style={styles.sectionTitle}>
        {t('匯出', 'Export')}
      </div>

      <div style={{ marginBottom: 'var(--da-space-3)' }}>
        <label style={styles.label}>YAML</label>
        <div style={styles.codeBlock}>
          <pre style={{ margin: 0, whiteSpace: 'pre-wrap', wordWrap: 'break-word' }}>
            {generateYAML(receiverType, template)}
          </pre>
        </div>
        <button
          onClick={() => handleCopy('yaml')}
          style={{ ...styles.button, ...styles.buttonPrimary, width: '100%', marginTop: 'var(--da-space-2)' }}
          aria-label={t('複製 YAML', 'Copy YAML')}
        >
          📋 {t('複製 YAML', 'Copy YAML')} {copyFeedback === t('已複製！', 'Copied!') && copyFeedback}
        </button>
      </div>

      <div style={{ marginBottom: 'var(--da-space-3)' }}>
        <label style={styles.label}>JSON</label>
        <div style={styles.codeBlock}>
          <pre style={{ margin: 0, whiteSpace: 'pre-wrap', wordWrap: 'break-word' }}>
            {generateJSON(receiverType, template)}
          </pre>
        </div>
        <button
          onClick={() => handleCopy('json')}
          style={{ ...styles.button, ...styles.buttonPrimary, width: '100%', marginTop: 'var(--da-space-2)' }}
          aria-label={t('複製 JSON', 'Copy JSON')}
        >
          📋 {t('複製 JSON', 'Copy JSON')} {copyFeedback === t('已複製！', 'Copied!') && copyFeedback}
        </button>
      </div>
    </div>
  );
}

/* ── Template Gallery ── */
function TemplateGallery({ receiverType, onSelect }) {
  const presets = TEMPLATE_PRESETS[receiverType] || [];

  if (presets.length === 0) return null;

  return (
    <div style={styles.space}>
      <div style={styles.sectionTitle}>
        {t('預設範本', 'Template Presets')}
      </div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--da-space-2)' }}>
        {presets.map((preset, i) => (
          <button
            key={i}
            onClick={() => onSelect(preset.template)}
            style={{
              ...styles.buttonSmall,
              textAlign: 'left',
              padding: 'var(--da-space-3)',
            }}
            aria-label={t('選擇預設範本：', 'Select preset:') + preset.name}
          >
            📋 {preset.name}
          </button>
        ))}
      </div>
    </div>
  );
}

/* ── Live Preview ── */
function LivePreview({ receiverType, template }) {
  const alertData = SAMPLE_ALERTS[0];
  const titlePreview = renderTemplatePreview(template.titleTemplate, alertData);
  const bodyPreview = renderTemplatePreview(template.bodyTemplate, alertData);
  const limit = RECEIVER_TYPES[receiverType].fieldLimits?.bodyTemplate;
  const charCount = template.bodyTemplate.length;
  const isNearLimit = limit && charCount > limit * 0.9;
  const isOverLimit = limit && charCount > limit;

  return (
    <div style={styles.space}>
      <div style={styles.sectionTitle}>
        {t('即時預覽', 'Live Preview')} ({RECEIVER_TYPES[receiverType].icon})
      </div>

      <div style={{ marginBottom: 'var(--da-space-3)' }}>
        <div style={{ fontSize: 'var(--da-font-size-xs)', color: 'var(--da-color-muted)', marginBottom: 'var(--da-space-1)' }}>
          {t('標題', 'Title')}
        </div>
        <div
          style={{
            ...styles.previewBox,
            fontWeight: 'var(--da-font-weight-semibold)',
            fontSize: 'var(--da-font-size-md)',
            padding: 'var(--da-space-3)',
          }}
          aria-live="polite"
          tabIndex={0}
          aria-label={t('通知標題預覽', 'Notification title preview')}
        >
          {titlePreview || t('（無標題）', '(no title)')}
        </div>
      </div>

      <div style={styles.spaceSmall}>
        <div style={{ fontSize: 'var(--da-font-size-xs)', color: 'var(--da-color-muted)', marginBottom: 'var(--da-space-1)' }}>
          {t('內容', 'Body')}
          {limit && (
            <span
              style={{
                marginLeft: 'var(--da-space-2)',
                color: isOverLimit ? 'var(--da-color-error)' : isNearLimit ? 'var(--da-color-warning)' : 'var(--da-color-success)',
              }}
            >
              ({charCount}/{limit})
            </span>
          )}
        </div>
        <div
          style={{
            ...styles.previewBox,
            whiteSpace: 'pre-wrap',
            wordWrap: 'break-word',
            fontFamily: 'var(--da-font-mono)',
            fontSize: 'var(--da-font-size-sm)',
          }}
          aria-live="polite"
          tabIndex={0}
          aria-label={t('通知內容預覽', 'Notification body preview')}
        >
          {bodyPreview || t('（無內容）', '(no body)')}
        </div>
        {isOverLimit && (
          <div style={{ ...styles.alertBox, ...styles.alertError, marginTop: 'var(--da-space-2)' }}>
            ⚠️ {t('超過字數限制！', 'Exceeds character limit!')} ({charCount}/{limit})
          </div>
        )}
        {isNearLimit && !isOverLimit && (
          <div style={{ ...styles.alertBox, ...styles.alertWarning, marginTop: 'var(--da-space-2)' }}>
            ⚠️ {t('接近字數限制', 'Approaching character limit')} ({charCount}/{limit})
          </div>
        )}
      </div>
    </div>
  );
}

/* ── Main Component ── */
export default function NotificationTemplateEditor() {
  const [receiverType, setReceiverType] = useState('slack');
  const [template, setTemplate] = useState(RECEIVER_TYPES.slack.defaultTemplate);

  const handleReceiverTypeChange = (type) => {
    setReceiverType(type);
    setTemplate(RECEIVER_TYPES[type].defaultTemplate);
  };

  const handleTemplateSelect = (selectedTemplate) => {
    setTemplate(selectedTemplate);
  };

  return (
    <div style={styles.container}>
      {/* Header */}
      <div style={styles.header}>
        <h1 style={styles.title}>
          {t('通知範本編輯器', 'Notification Template Editor')}
        </h1>
        <p style={styles.subtitle}>
          {t(
            '建立和預覽不同 receiver 類型的告警通知範本 — 包括 Slack、Email、PagerDuty、Teams、Webhook。',
            'Create and preview notification templates for different receiver types — Slack, Email, PagerDuty, Teams, Webhook.'
          )}
        </p>
      </div>

      {/* Receiver Type Selector */}
      <div style={{ ...styles.section, marginBottom: 'var(--da-space-6)' }}>
        <div style={styles.sectionTitle}>
          {t('選擇 Receiver 類型', 'Select Receiver Type')}
        </div>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(120px, 1fr))', gap: 'var(--da-space-2)' }}>
          {Object.entries(RECEIVER_TYPES).map(([id, r]) => (
            <button
              key={id}
              onClick={() => handleReceiverTypeChange(id)}
              style={{
                ...styles.buttonSmall,
                ...(receiverType === id ? styles.buttonSmallActive : {}),
              }}
              aria-pressed={receiverType === id}
              aria-label={t('選擇 Receiver：', 'Select receiver:') + r.label}
            >
              {r.icon} {r.label}
            </button>
          ))}
        </div>
      </div>

      {/* Three-column layout */}
      <div
        style={{
          display: 'grid',
          gridTemplateColumns: 'minmax(300px, 1fr) minmax(350px, 1.2fr) minmax(300px, 1fr)',
          gap: 'var(--da-space-6)',
          '@media (max-width: 1200px)': {
            gridTemplateColumns: '1fr',
          },
        }}
      >
        {/* Left: Templates & Presets */}
        <div style={styles.section}>
          <TemplateGallery receiverType={receiverType} onSelect={handleTemplateSelect} />
        </div>

        {/* Center: Editor */}
        <div style={styles.section}>
          <TemplateEditorPanel
            receiverType={receiverType}
            template={template}
            onTemplateChange={setTemplate}
          />
        </div>

        {/* Right: Preview & Export */}
        <div style={styles.section}>
          <LivePreview receiverType={receiverType} template={template} />
          <ExportPanel receiverType={receiverType} template={template} />
        </div>
      </div>

      {/* Footer */}
      <div style={{ ...styles.section, ...styles.alertInfo, marginTop: 'var(--da-space-6)' }}>
        <h4 style={{ marginTop: 0, marginBottom: 'var(--da-space-2)', color: 'var(--da-color-info)' }}>
          {t('提示', 'Tips')}
        </h4>
        <ul style={{ margin: 0, paddingLeft: 'var(--da-space-4)', fontSize: 'var(--da-font-size-sm)' }}>
          <li>{t('使用 Go 範本語法 — {{ .GroupLabels.fieldname }} 格式。', 'Use Go template syntax — {{ .GroupLabels.fieldname }} format.')}</li>
          <li>{t('實際通知格式由 Alertmanager 決定 — 此編輯器為概念性展示。', 'Actual format is determined by Alertmanager — this editor is conceptual.')}</li>
          <li>{t('使用預設範本快速開始，或建立自訂範本以滿足特定需求。', 'Use presets to get started quickly, or create custom templates for specific needs.')}</li>
          <li>{t('使用 da-tools test-notification 進行真實通知測試。', 'Use da-tools test-notification for real notification testing.')}</li>
        </ul>
      </div>
    </div>
  );
}
