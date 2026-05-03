---
title: "Operator Setup Wizard — Step 5: Review & Generate"
purpose: |
  Final wizard step. Renders configuration summary + 4 tabbed views
  (operator command / migration command / AlertmanagerConfig preview /
  manual checklist) with copy-to-clipboard buttons.

  Pre-PR-portal-4 lived inline as the largest function in operator-
  setup-wizard.jsx (~240 LOC, lines 815-1053). Extracted to drop the
  orchestrator under the 1000-LOC mark and to let future PRs polish
  this step without touching the wizard control flow.

  Props:
    config  the wizard's accumulated config object built across steps
            1-4 (operatorVersion / crdVersion / namespace / ruleMode /
            receiverType / receiverSecret / selectedTenants).

  Closure deps: window.__t, window.__generateOperatorCommand,
  window.__generateMigrationCommand,
  window.__generateAlertmanagerConfigPreview. The 3 generators are
  registered by `_common/.../utils/generators.js` (PR-portal-4
  sibling extract); orchestrator front-matter dependencies block
  loads them before this component.
---

const { useState, useCallback } = React;

const t = window.__t || ((zh, en) => en);
const generateOperatorCommand = window.__generateOperatorCommand;
const generateMigrationCommand = window.__generateMigrationCommand;
const generateAlertmanagerConfigPreview = window.__generateAlertmanagerConfigPreview;

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

window.__StepReview = StepReview;
