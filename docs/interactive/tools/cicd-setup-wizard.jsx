---
title: "CI/CD Setup Wizard"
tags: [cicd, gitops, setup, wizard, adoption]
audience: ["platform-engineer"]
version: v2.7.0
lang: en
related: [self-service-portal, template-gallery, onboarding-checklist]
dependencies: [
  "cicd-setup-wizard/fixtures/wizard-defaults.js",
  "cicd-setup-wizard/utils/generators.js"
]
---

import React, { useState, useMemo } from 'react';

const t = window.__t || ((zh, en) => en);

// PR-portal-10: data + helpers extracted to sibling subdirectory
// (mirrors operator-setup-wizard PR-portal-4 pattern). Step
// components remain inline; future PR can extract them.
const STEPS = window.__CICD_STEPS;
const RULE_PACKS = window.__CICD_RULE_PACKS;
const CI_OPTIONS = window.__CICD_CI_OPTIONS;
const DEPLOY_OPTIONS = window.__CICD_DEPLOY_OPTIONS;

const generateInitCommand = window.__cicdGenerateInitCommand;
const generateDockerCommand = window.__cicdGenerateDockerCommand;
const generateFileTree = window.__cicdGenerateFileTree;
const generateGitHubActionsPreview = window.__cicdGenerateGitHubActionsPreview;

/* ── Step components ── */

function StepCI({ config, onChange }) {
  return (
    <div>
      <h3 className="text-lg font-semibold mb-2">{t('選擇 CI/CD 平台', 'Choose CI/CD Platform')}</h3>
      <p className="text-sm text-[color:var(--da-color-muted)] mb-4">
        {t('da-tools init 會為你產生對應的 pipeline 配置檔。', 'da-tools init will generate the corresponding pipeline config files.')}
      </p>
      <div className="grid grid-cols-1 gap-3" role="radiogroup" aria-label={t('CI/CD 平台選擇', 'CI/CD platform selection')}>
        {CI_OPTIONS.map(opt => (
          <button
            key={opt.id}
            onClick={() => onChange({ ...config, ci: opt.id })}
            role="radio"
            aria-checked={config.ci === opt.id}
            className={`p-4 rounded-lg border-2 text-left transition-all ${
              config.ci === opt.id
                ? 'border-[color:var(--da-color-accent)] bg-[color:var(--da-color-accent-soft)]'
                : 'border-[color:var(--da-color-surface-border)] hover:border-[color:var(--da-color-card-hover-border)]'
            }`}
          >
            <div className="flex items-center gap-3">
              <span className="text-2xl" aria-hidden="true">{opt.icon}</span>
              <div>
                <div className="font-medium text-[color:var(--da-color-fg)]">{opt.label}</div>
                <div className="text-xs text-[color:var(--da-color-muted)]">{opt.desc()}</div>
              </div>
            </div>
          </button>
        ))}
      </div>
    </div>
  );
}

function StepDeploy({ config, onChange }) {
  return (
    <div>
      <h3 className="text-lg font-semibold mb-2">{t('選擇部署方式', 'Choose Deployment Mode')}</h3>
      <p className="text-sm text-[color:var(--da-color-muted)] mb-4">
        {t('從 YAML 配置到 Kubernetes ConfigMap 的部署路徑。', 'The deployment path from YAML configs to Kubernetes ConfigMap.')}
      </p>
      <div className="grid grid-cols-1 gap-3" role="radiogroup" aria-label={t('部署方式選擇', 'Deployment mode selection')}>
        {DEPLOY_OPTIONS.map(opt => (
          <button
            key={opt.id}
            onClick={() => onChange({ ...config, deploy: opt.id })}
            role="radio"
            aria-checked={config.deploy === opt.id}
            className={`p-4 rounded-lg border-2 text-left transition-all ${
              config.deploy === opt.id
                ? 'border-[color:var(--da-color-accent)] bg-[color:var(--da-color-accent-soft)]'
                : 'border-[color:var(--da-color-surface-border)] hover:border-[color:var(--da-color-card-hover-border)]'
            }`}
          >
            <div className="flex items-center gap-3">
              <span className="text-2xl" aria-hidden="true">{opt.icon}</span>
              <div>
                <div className="font-medium text-[color:var(--da-color-fg)]">{opt.label}</div>
                <div className="text-xs text-[color:var(--da-color-muted)]">{opt.desc()}</div>
              </div>
            </div>
          </button>
        ))}
      </div>
    </div>
  );
}

function StepPacks({ config, onChange }) {
  const categories = useMemo(() => {
    const groups = {};
    for (const p of RULE_PACKS) {
      if (!groups[p.category]) groups[p.category] = [];
      groups[p.category].push(p);
    }
    return groups;
  }, []);

  const catLabels = {
    database: () => t('資料庫', 'Databases'),
    messaging: () => t('訊息佇列', 'Messaging'),
    runtime: () => t('運行環境', 'Runtime'),
    webserver: () => t('網頁伺服器', 'Web Servers'),
    infrastructure: () => t('基礎設施', 'Infrastructure'),
  };

  const toggle = (id) => {
    const next = config.packs.includes(id)
      ? config.packs.filter(x => x !== id)
      : [...config.packs, id];
    onChange({ ...config, packs: next });
  };

  return (
    <div>
      <h3 className="text-lg font-semibold mb-2">{t('選擇 Rule Pack', 'Select Rule Packs')}</h3>
      <p className="text-sm text-[color:var(--da-color-muted)] mb-4">
        {t('選擇你需要監控的技術。operational 和 platform pack 自動啟用。', 'Select technologies to monitor. Operational and platform packs are auto-enabled.')}
      </p>
      <div className="space-y-4">
        {Object.entries(categories).map(([cat, packs]) => (
          <div key={cat}>
            <div className="text-xs font-medium text-[color:var(--da-color-muted)] uppercase tracking-wide mb-2" id={`pack-cat-${cat}`}>
              {catLabels[cat] ? catLabels[cat]() : cat}
            </div>
            <div className="grid grid-cols-2 gap-2" role="group" aria-labelledby={`pack-cat-${cat}`}>
              {packs.map(p => {
                const isSelected = config.packs.includes(p.id);
                return (
                  <button
                    key={p.id}
                    onClick={() => toggle(p.id)}
                    aria-pressed={isSelected}
                    className={`p-3 rounded-lg border text-left text-sm transition-all ${
                      isSelected
                        ? 'border-[color:var(--da-color-accent)] bg-[color:var(--da-color-accent-soft)]'
                        : 'border-[color:var(--da-color-surface-border)] hover:border-[color:var(--da-color-card-hover-border)]'
                    }`}
                  >
                    <span className="mr-2" aria-hidden="true">{p.icon}</span>
                    <span className={isSelected ? 'font-medium text-[color:var(--da-color-fg)]' : 'text-[color:var(--da-color-fg)]'}>{p.label}</span>
                    {isSelected && <span className="ml-1 text-[color:var(--da-color-accent)]" aria-hidden="true">✓</span>}
                  </button>
                );
              })}
            </div>
          </div>
        ))}
      </div>
      <div
        className="mt-3 p-2 bg-[color:var(--da-color-surface-hover)] rounded text-xs text-[color:var(--da-color-muted)]"
        role="status"
        aria-live="polite"
      >
        {t(`已選 ${config.packs.length} 個 Rule Pack（+ operational、platform 自動啟用 = ${config.packs.length + 2} 個）`,
           `${config.packs.length} selected (+ operational, platform auto-enabled = ${config.packs.length + 2} total)`)}
      </div>
    </div>
  );
}

function StepTenants({ config, onChange }) {
  const [input, setInput] = useState('');

  const addTenant = () => {
    const name = input.trim().toLowerCase().replace(/[^a-z0-9-]/g, '-');
    if (name && !config.tenants.includes(name)) {
      onChange({ ...config, tenants: [...config.tenants, name] });
      setInput('');
    }
  };

  const removeTenant = (name) => {
    onChange({ ...config, tenants: config.tenants.filter(t => t !== name) });
  };

  return (
    <div>
      <h3 className="text-lg font-semibold mb-2">{t('設定 Tenant', 'Configure Tenants')}</h3>
      <p className="text-sm text-[color:var(--da-color-muted)] mb-4">
        {t('每個 tenant 代表一組獨立的閾值配置。例如 prod-mariadb, staging-redis。',
           'Each tenant represents an independent threshold config set. e.g. prod-mariadb, staging-redis.')}
      </p>

      <div className="flex gap-2 mb-4">
        <label htmlFor="cicd-tenant-input" className="sr-only">{t('Tenant 名稱', 'Tenant name')}</label>
        <input
          id="cicd-tenant-input"
          type="text"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && addTenant()}
          placeholder={t('輸入 tenant 名稱...', 'Enter tenant name...')}
          aria-label={t('Tenant 名稱', 'Tenant name')}
          className="flex-1 px-3 py-2 border border-[color:var(--da-color-surface-border)] rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-[color:var(--da-color-focus-ring)]"
        />
        <button
          onClick={addTenant}
          disabled={!input.trim()}
          className="px-4 py-2 bg-[color:var(--da-color-accent)] text-white rounded-lg text-sm hover:bg-[color:var(--da-color-accent-hover)] disabled:opacity-50"
        >
          {t('新增', 'Add')}
        </button>
      </div>

      {/* Quick-add suggestions */}
      <div className="flex flex-wrap gap-1.5 mb-4" role="group" aria-label={t('快速新增 tenant', 'Quick add tenants')}>
        <span className="text-xs text-[color:var(--da-color-muted)] self-center">{t('快速新增：', 'Quick add:')}</span>
        {['prod-mariadb', 'prod-redis', 'staging-app', 'dev-testing', 'prod-kafka'].map(name => (
          <button
            key={name}
            onClick={() => {
              if (!config.tenants.includes(name)) {
                onChange({ ...config, tenants: [...config.tenants, name] });
              }
            }}
            disabled={config.tenants.includes(name)}
            aria-label={t(`快速新增 ${name}`, `Quick add ${name}`)}
            className="text-xs px-2 py-1 bg-[color:var(--da-color-tag-bg)] hover:bg-[color:var(--da-color-surface-hover)] rounded disabled:opacity-30"
          >
            + {name}
          </button>
        ))}
      </div>

      {config.tenants.length > 0 && (
        <div className="space-y-1" role="list" aria-label={t('已新增的 tenant', 'Added tenants')}>
          {config.tenants.map(name => (
            <div key={name} role="listitem" className="flex items-center justify-between p-2 bg-[color:var(--da-color-surface-hover)] rounded">
              <code className="text-sm font-mono text-[color:var(--da-color-fg)]">{name}</code>
              <button
                onClick={() => removeTenant(name)}
                aria-label={t(`移除 tenant ${name}`, `Remove tenant ${name}`)}
                className="text-xs text-[color:var(--da-color-error)] hover:opacity-80"
              >
                {t('移除', 'Remove')}
              </button>
            </div>
          ))}
        </div>
      )}

      {config.tenants.length === 0 && (
        <div
          className="text-sm text-[color:var(--da-color-muted)] text-center py-4"
          role="status"
          aria-live="polite"
        >
          {t('尚未新增 tenant', 'No tenants added yet')}
        </div>
      )}
    </div>
  );
}

function StepReview({ config }) {
  const [copiedCmd, setCopiedCmd] = useState(false);
  const [copiedDocker, setCopiedDocker] = useState(false);
  const [showPipeline, setShowPipeline] = useState(false);

  const initCmd = generateInitCommand(config);
  const dockerCmd = generateDockerCommand(config);
  const fileTree = generateFileTree(config);

  const isComplete = config.ci && config.deploy && config.packs.length > 0 && config.tenants.length > 0;

  const copy = (text, setter) => {
    navigator.clipboard.writeText(text);
    setter(true);
    setTimeout(() => setter(false), 2000);
  };

  return (
    <div>
      <h3 className="text-lg font-semibold mb-2">{t('檢視配置', 'Review Configuration')}</h3>

      {!isComplete && (
        <div
          className="p-3 bg-[color:var(--da-color-warning-soft)] border border-[color:var(--da-color-warning)]/30 rounded-lg text-sm text-[color:var(--da-color-warning)] mb-4"
          role="alert"
          aria-live="polite"
        >
          {t('請完成所有步驟後再產生命令。', 'Please complete all steps before generating commands.')}
        </div>
      )}

      {/* Summary */}
      <div
        className="grid grid-cols-2 gap-3 mb-4"
        role="status"
        aria-live="polite"
        aria-atomic="true"
        aria-label={t('配置摘要', 'Configuration summary')}
      >
        <div className="p-3 bg-[color:var(--da-color-surface-hover)] rounded-lg">
          <div className="text-xs text-[color:var(--da-color-muted)]">{t('CI/CD 平台', 'CI/CD Platform')}</div>
          <div className="font-medium text-sm text-[color:var(--da-color-fg)]">{CI_OPTIONS.find(o => o.id === config.ci)?.label || '-'}</div>
        </div>
        <div className="p-3 bg-[color:var(--da-color-surface-hover)] rounded-lg">
          <div className="text-xs text-[color:var(--da-color-muted)]">{t('部署方式', 'Deployment')}</div>
          <div className="font-medium text-sm text-[color:var(--da-color-fg)]">{DEPLOY_OPTIONS.find(o => o.id === config.deploy)?.label || '-'}</div>
        </div>
        <div className="p-3 bg-[color:var(--da-color-surface-hover)] rounded-lg">
          <div className="text-xs text-[color:var(--da-color-muted)]">Rule Packs</div>
          <div className="font-medium text-sm text-[color:var(--da-color-fg)]">{config.packs.length} + 2 {t('自動', 'auto')}</div>
        </div>
        <div className="p-3 bg-[color:var(--da-color-surface-hover)] rounded-lg">
          <div className="text-xs text-[color:var(--da-color-muted)]">Tenants</div>
          <div className="font-medium text-sm text-[color:var(--da-color-fg)]">{config.tenants.length}</div>
        </div>
      </div>

      {isComplete && (
        <>
          {/* da-tools init command */}
          <div className="mb-4">
            <div className="flex items-center justify-between mb-1">
              <span className="text-sm font-medium text-[color:var(--da-color-fg)]">da-tools init {t('命令', 'command')}</span>
              <button
                onClick={() => copy(initCmd, setCopiedCmd)}
                aria-label={t('複製 da-tools init 命令', 'Copy da-tools init command')}
                className={`text-xs px-2 py-1 rounded ${copiedCmd ? 'bg-[color:var(--da-color-success)] text-white' : 'bg-[color:var(--da-color-tag-bg)] hover:bg-[color:var(--da-color-surface-hover)]'}`}
              >
                {copiedCmd ? '✓' : t('複製', 'Copy')}
              </button>
            </div>
            <pre className="bg-[color:var(--da-color-hero-bg)] text-[color:var(--da-color-success)] p-4 rounded-lg text-xs font-mono overflow-x-auto">
              {initCmd}
            </pre>
          </div>

          {/* Docker run command */}
          <div className="mb-4">
            <div className="flex items-center justify-between mb-1">
              <span className="text-sm font-medium text-[color:var(--da-color-fg)]">{t('Docker 一鍵執行', 'Docker one-liner')}</span>
              <button
                onClick={() => copy(dockerCmd, setCopiedDocker)}
                aria-label={t('複製 Docker 命令', 'Copy Docker command')}
                className={`text-xs px-2 py-1 rounded ${copiedDocker ? 'bg-[color:var(--da-color-success)] text-white' : 'bg-[color:var(--da-color-tag-bg)] hover:bg-[color:var(--da-color-surface-hover)]'}`}
              >
                {copiedDocker ? '✓' : t('複製', 'Copy')}
              </button>
            </div>
            <pre className="bg-[color:var(--da-color-hero-bg)] text-[color:var(--da-color-hero-accent)] p-4 rounded-lg text-xs font-mono overflow-x-auto">
              {dockerCmd}
            </pre>
          </div>

          {/* File tree */}
          <div className="mb-4">
            <span className="text-sm font-medium text-[color:var(--da-color-fg)]">{t('產生的檔案結構', 'Generated file structure')}</span>
            <pre className="mt-1 bg-[color:var(--da-color-surface-hover)] p-4 rounded-lg text-xs font-mono border border-[color:var(--da-color-surface-border)] text-[color:var(--da-color-fg)]">
              {fileTree}
            </pre>
          </div>

          {/* Pipeline preview toggle */}
          <button
            onClick={() => setShowPipeline(!showPipeline)}
            aria-expanded={showPipeline}
            className="text-sm text-[color:var(--da-color-accent)] hover:text-[color:var(--da-color-accent-hover)] mb-2"
          >
            {showPipeline
              ? t('▾ 收起 Pipeline 預覽', '▾ Hide pipeline preview')
              : t('▸ 展開 Pipeline 預覽', '▸ Show pipeline preview')}
          </button>
          {showPipeline && (config.ci === 'github' || config.ci === 'both') && (
            <div className="mb-4">
              <span className="text-xs font-medium text-[color:var(--da-color-muted)]">.github/workflows/dynamic-alerting.yaml</span>
              <pre className="mt-1 bg-[color:var(--da-color-hero-bg)] text-[color:var(--da-color-hero-fg)] p-4 rounded-lg text-xs font-mono overflow-x-auto max-h-64 overflow-y-auto">
                {generateGitHubActionsPreview(config)}
              </pre>
            </div>
          )}

          {/* Next steps */}
          <div className="mt-4 p-4 bg-[color:var(--da-color-info-soft)] rounded-lg border border-[color:var(--da-color-info)]/30">
            <h4 className="text-sm font-medium text-[color:var(--da-color-accent-hover)] mb-2">{t('下一步', 'Next Steps')}</h4>
            <ol className="text-sm text-[color:var(--da-color-fg)] space-y-1 list-decimal list-inside">
              <li>{t('在你的 repo 根目錄執行上方的 Docker 命令', 'Run the Docker command above in your repo root')}</li>
              <li>{t('編輯 conf.d/ 中的 tenant YAML，調整閾值', 'Edit tenant YAML in conf.d/, adjust thresholds')}</li>
              <li>{t('git commit → CI 自動執行 Validate + Generate', 'git commit → CI auto-runs Validate + Generate')}</li>
              <li>{t('PR 審核通過後手動觸發 Apply（或 ArgoCD 自動同步）', 'After PR approval, manually trigger Apply (or ArgoCD auto-syncs)')}</li>
            </ol>
          </div>
        </>
      )}
    </div>
  );
}

/* ── Main Wizard Component ── */
export default function CICDSetupWizard() {
  const [step, setStep] = useState(0);
  const [config, setConfig] = useState({
    ci: 'github',
    deploy: 'kustomize',
    packs: ['mariadb', 'kubernetes'],
    tenants: [],
  });

  const canNext = useMemo(() => {
    switch (step) {
      case 0: return !!config.ci;
      case 1: return !!config.deploy;
      case 2: return config.packs.length > 0;
      case 3: return config.tenants.length > 0;
      default: return false;
    }
  }, [step, config]);

  return (
    <div className="max-w-3xl mx-auto">
      <div className="mb-6">
        <h1 className="text-2xl font-bold text-[color:var(--da-color-fg)]">
          {t('CI/CD 導入精靈', 'CI/CD Setup Wizard')}
        </h1>
        <p className="text-[color:var(--da-color-muted)] mt-1">
          {t('四步產出完整的 da-tools init 命令和 CI/CD 配置 — 從零到部署。',
             'Four steps to generate your complete da-tools init command and CI/CD config — from zero to deployment.')}
        </p>
      </div>

      {/* Step indicator */}
      <div
        className="flex items-center mb-6 bg-[color:var(--da-color-surface-hover)] rounded-lg p-2"
        role="list"
        aria-label={t('CI/CD 設定步驟', 'CI/CD setup steps')}
      >
        {STEPS.map((s, i) => (
          <React.Fragment key={s.id}>
            {i > 0 && <div className={`flex-1 h-0.5 ${i <= step ? 'bg-[color:var(--da-color-accent)]' : 'bg-[color:var(--da-color-surface-border)]'}`} aria-hidden="true" />}
            <button
              onClick={() => setStep(i)}
              role="listitem"
              aria-current={i === step ? 'step' : undefined}
              className={`flex items-center gap-1.5 px-2.5 py-1.5 rounded-md text-xs font-medium transition-colors ${
                i === step ? 'bg-[color:var(--da-color-accent)] text-white' :
                i < step ? 'text-[color:var(--da-color-accent)] hover:bg-[color:var(--da-color-accent-soft)]' : 'text-[color:var(--da-color-muted)]'
              }`}
            >
              <span className={`w-5 h-5 rounded-full flex items-center justify-center text-xs ${
                i < step ? 'bg-[color:var(--da-color-accent-soft)] text-[color:var(--da-color-accent)]' :
                i === step ? 'bg-[color:var(--da-color-surface)] text-[color:var(--da-color-accent)]' : 'bg-[color:var(--da-color-tag-bg)] text-[color:var(--da-color-tag-fg)]'
              }`} aria-hidden="true">
                {i < step ? '✓' : i + 1}
              </span>
              <span className="hidden sm:inline">{s.label()}</span>
            </button>
          </React.Fragment>
        ))}
      </div>

      {/* Step content */}
      <div
        className="bg-[color:var(--da-color-surface)] rounded-lg border border-[color:var(--da-color-surface-border)] p-6 mb-4"
        role="region"
        aria-label={t(`步驟 ${step + 1} 內容`, `Step ${step + 1} content`)}
      >
        {step === 0 && <StepCI config={config} onChange={setConfig} />}
        {step === 1 && <StepDeploy config={config} onChange={setConfig} />}
        {step === 2 && <StepPacks config={config} onChange={setConfig} />}
        {step === 3 && <StepTenants config={config} onChange={setConfig} />}
        {step === 4 && <StepReview config={config} />}
      </div>

      {/* Navigation */}
      <div className="flex justify-between">
        <button
          onClick={() => setStep(Math.max(0, step - 1))}
          disabled={step === 0}
          className="px-4 py-2 text-sm font-medium text-[color:var(--da-color-fg)] bg-[color:var(--da-color-tag-bg)] rounded-lg hover:bg-[color:var(--da-color-surface-hover)] disabled:opacity-30"
        >
          {t('上一步', 'Back')}
        </button>
        {step < STEPS.length - 1 ? (
          <button
            onClick={() => setStep(step + 1)}
            disabled={!canNext}
            className="px-4 py-2 text-sm font-medium text-white bg-[color:var(--da-color-accent)] rounded-lg hover:bg-[color:var(--da-color-accent-hover)] disabled:opacity-50"
          >
            {t('下一步', 'Next')}
          </button>
        ) : (
          <div className="text-sm text-[color:var(--da-color-muted)]">
            {t('複製上方命令開始執行', 'Copy the command above to get started')}
          </div>
        )}
      </div>
    </div>
  );
}
