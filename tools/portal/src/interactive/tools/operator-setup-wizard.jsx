---
title: "Operator Setup Wizard"
version: v2.9.0
lang: en
related: [deployment-wizard, cicd-setup-wizard, config-lint]
dependencies: [
  "operator-setup-wizard/fixtures/wizard-defaults.js",
  "operator-setup-wizard/utils/generators.js",
  "_common/hooks/useCopyToClipboard.js",
  "operator-setup-wizard/components/StepReview.jsx",
  "_common/components/ErrorBoundary.jsx"
]
---

import React, { useState, useMemo, useCallback } from 'react';

// TRK-230e: ESM imports; esbuild bundles them natively into the dist
// file (TD-030z retired the old jsx-loader import-transform path).
import { OSW_STEPS as STEPS, OSW_OPERATOR_VERSIONS as OPERATOR_VERSIONS, OSW_CLUSTER_TYPES as CLUSTER_TYPES, OSW_RULE_MODES as RULE_MODES } from './operator-setup-wizard/fixtures/wizard-defaults.js';
import { validateTenantName } from './operator-setup-wizard/utils/generators.js';
import { StepReview } from './operator-setup-wizard/components/StepReview.jsx';
// PR-portal-11: per-step boundary so a render error in step N
// doesn't crash the wizard chrome — user can still hit "Back" and
// recover state from step N-1.
import { ErrorBoundary } from './_common/components/ErrorBoundary.jsx';

const t = window.__t || ((zh, en) => en);

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
              {ver.recommended && <span style={{ display: 'block', fontSize: 'var(--da-font-size-xs)', color: 'var(--da-color-success)', marginTop: 'var(--da-space-1)' }}><span aria-hidden="true">✓</span> {t('推薦', 'Recommended')}</span>}
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
          monitoring.coreos.com/v1
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
                <div style={{ fontSize: 'var(--da-font-size-xs)', color: 'var(--da-color-warning-text)', marginTop: 'var(--da-space-1)' }}>
                  <span aria-hidden="true">⚠</span>️ {t('需要仔細測試', 'Requires careful testing')}
                </div>
              )}
            </button>
          ))}
        </div>
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
    review: <StepReview config={config} />,
  };

  return (
    <main role="main" className="wizard" style={{
      minHeight: '100vh',
      background: 'var(--da-color-bg)',
      padding: 'var(--da-space-8)',
    }}>
      <div style={{ maxWidth: '900px' /* token-exempt: page content max width */, margin: '0 auto' }}>
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
            {t('三步流程引導設定 Prometheus Operator 整合；告警路由由 generate-routes 產出，交給 Alertmanager 的 configSecret。', 'Three-step guided setup for Prometheus Operator integration; alert routing comes from generate-routes, handed to the Alertmanager as its configSecret.')}
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
                {idx < currentStep && <span aria-hidden="true">✓ </span>}{step.label()}
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
          {/* PR-portal-11: per-step boundary keyed by step id so a
              fresh boundary mounts on each step transition. */}
          <ErrorBoundary
            key={STEPS[currentStep].id}
            scope={'operator-setup-wizard/step/' + STEPS[currentStep].id}
          >
            {stepContent[STEPS[currentStep].id]}
          </ErrorBoundary>
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
