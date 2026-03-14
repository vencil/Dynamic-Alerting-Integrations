---
title: "Onboarding Checklist Generator"
tags: [onboarding, interactive, tools]
audience: [tenant, platform-engineer, domain-expert]
version: v2.0.0-preview.2
lang: en
related: [wizard, architecture-quiz, glossary]
---

import React, { useState, useMemo } from 'react';

const t = window.__t || ((zh, en) => en);

const REPO_BASE = "https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main";

const ROLES = [
  { id: 'platform', label: 'Platform Engineer', icon: '⚙️' },
  { id: 'domain', label: 'Domain Expert (DBA)', icon: '🗄️' },
  { id: 'tenant', label: 'Tenant Team', icon: '👥' },
];

const CHECKLISTS = {
  platform: {
    title: t('平台工程師上線清單', 'Platform Engineer Onboarding'),
    phases: [
      {
        name: t('準備階段', 'Phase 1 — Prerequisites'),
        icon: '📋',
        steps: [
          { text: t('確認 Kubernetes 叢集可用（Kind / EKS / GKE）', 'Confirm Kubernetes cluster is available (Kind / EKS / GKE)'), doc: null },
          { text: t('安裝 Helm 3.x 和 kubectl', 'Install Helm 3.x and kubectl'), doc: null },
          { text: t('安裝 da-tools CLI（Docker 或 pip）', 'Install da-tools CLI (Docker or pip)'), doc: 'docs/cli-reference.md' },
          { text: t('閱讀架構概覽', 'Read the Architecture Overview'), doc: 'docs/architecture-and-design.md' },
          { text: t('閱讀平台工程師快速入門', 'Read Platform Engineer Quick Start'), doc: 'docs/getting-started/for-platform-engineers.md' },
        ],
      },
      {
        name: t('部署階段', 'Phase 2 — Deploy'),
        icon: '🚀',
        steps: [
          { text: t('使用 Helm 部署 threshold-exporter', 'Deploy threshold-exporter via Helm'), doc: 'docs/getting-started/for-platform-engineers.md' },
          { text: t('配置 Prometheus Projected Volume 掛載 Rule Packs', 'Configure Prometheus Projected Volume for Rule Packs'), doc: 'docs/architecture-and-design.md' },
          { text: t('部署 Alertmanager 並啟用 configmap-reload sidecar', 'Deploy Alertmanager with configmap-reload sidecar'), doc: 'docs/byo-alertmanager-integration.md' },
          { text: t('設定 _defaults.yaml 全域預設值', 'Set up _defaults.yaml global defaults'), doc: null },
          { text: t('執行 da-tools scaffold 建立第一個 tenant', 'Run da-tools scaffold to create the first tenant'), doc: 'docs/migration-guide.md' },
        ],
      },
      {
        name: t('驗證階段', 'Phase 3 — Validate'),
        icon: '✅',
        steps: [
          { text: t('用 da-tools validate 驗證配置', 'Run da-tools validate to check config'), doc: 'docs/cli-reference.md' },
          { text: t('確認 Prometheus targets 健康', 'Verify Prometheus targets are healthy'), doc: 'docs/troubleshooting.md' },
          { text: t('確認 threshold-exporter metrics 輸出正確', 'Confirm threshold-exporter metrics output'), doc: 'docs/benchmarks.md' },
          { text: t('觸發測試告警並確認路由正確', 'Trigger a test alert and verify routing'), doc: 'docs/scenarios/alert-routing-split.md' },
          { text: t('跑一次 baseline benchmark', 'Run a baseline benchmark'), doc: 'docs/benchmarks.md' },
        ],
      },
      {
        name: t('生產就緒', 'Phase 4 — Production Ready'),
        icon: '🏁',
        steps: [
          { text: t('設定 GitOps CI/CD pipeline', 'Set up GitOps CI/CD pipeline'), doc: 'docs/gitops-deployment.md' },
          { text: t('配置 governance & security（RBAC、webhook allowlist）', 'Configure governance & security (RBAC, webhook allowlist)'), doc: 'docs/governance-security.md' },
          { text: t('建立 config_diff CI 檢查', 'Set up config_diff CI check'), doc: 'docs/gitops-deployment.md' },
          { text: t('文件化內部運維 SOP', 'Document internal operations SOP'), doc: 'docs/internal/testing-playbook.md' },
          { text: t('通知 tenant 團隊開始上線', 'Notify tenant teams to begin onboarding'), doc: 'docs/getting-started/for-tenants.md' },
        ],
      },
    ],
  },
  domain: {
    title: t('資料庫專家上線清單', 'Domain Expert (DBA) Onboarding'),
    phases: [
      {
        name: t('學習階段', 'Phase 1 — Learn'),
        icon: '📚',
        steps: [
          { text: t('閱讀 Domain Expert 快速入門', 'Read Domain Expert Quick Start'), doc: 'docs/getting-started/for-domain-experts.md' },
          { text: t('理解三層 Rule Pack 架構（Recording → Alert → Threshold）', 'Understand 3-layer Rule Pack architecture (Recording → Alert → Threshold)'), doc: 'docs/architecture-and-design.md' },
          { text: t('瀏覽 Alert Reference 了解所有告警定義', 'Browse Alert Reference for all alert definitions'), doc: 'docs/rule-packs/ALERT-REFERENCE.md' },
          { text: t('理解三態模式（Custom / Default / Disable）', 'Understand three-state mode (Custom / Default / Disable)'), doc: 'docs/architecture-and-design.md' },
        ],
      },
      {
        name: t('分析階段', 'Phase 2 — Analyze'),
        icon: '📊',
        steps: [
          { text: t('確認你的資料庫對應哪個 Rule Pack', 'Identify which Rule Pack matches your database'), doc: 'docs/rule-packs/README.md' },
          { text: t('收集當前工作負載的 p50 / p90 / p99 數據', 'Collect current workload p50 / p90 / p99 statistics'), doc: null },
          { text: t('使用 Threshold Calculator 計算建議閾值', 'Use Threshold Calculator to compute recommended thresholds'), doc: null, tool: '../assets/jsx-loader.html?component=../threshold-calculator.jsx' },
          { text: t('與平台團隊確認 exporter 已部署', 'Confirm with platform team that exporters are deployed'), doc: null },
        ],
      },
      {
        name: t('配置階段', 'Phase 3 — Configure'),
        icon: '⚙️',
        steps: [
          { text: t('建立 tenant YAML 並設定閾值', 'Create tenant YAML with thresholds'), doc: 'docs/getting-started/for-tenants.md' },
          { text: t('設定 warning 和 critical 兩層嚴重度', 'Set warning and critical severity levels'), doc: 'docs/architecture-and-design.md' },
          { text: t('用 YAML Validator 驗證配置', 'Validate config with YAML Validator'), doc: null, tool: '../assets/jsx-loader.html?component=../playground.jsx' },
          { text: t('（選用）配置排程式閾值', '(Optional) Configure scheduled thresholds'), doc: 'docs/scenarios/advanced-scenarios.md' },
          { text: t('（選用）配置 regex 維度閾值', '(Optional) Configure regex dimension thresholds'), doc: 'docs/scenarios/advanced-scenarios.md' },
        ],
      },
      {
        name: t('驗證階段', 'Phase 4 — Verify'),
        icon: '✅',
        steps: [
          { text: t('部署配置並確認 metrics 輸出', 'Deploy config and confirm metrics output'), doc: null },
          { text: t('觸發測試告警確認閾值正確', 'Trigger test alert to verify thresholds'), doc: 'docs/troubleshooting.md' },
          { text: t('監控一週並根據實際數據微調', 'Monitor for one week and fine-tune based on real data'), doc: null },
          { text: t('文件化閾值選擇理由供團隊參考', 'Document threshold rationale for team reference'), doc: null },
        ],
      },
    ],
  },
  tenant: {
    title: t('Tenant 團隊上線清單', 'Tenant Team Onboarding'),
    phases: [
      {
        name: t('了解階段', 'Phase 1 — Understand'),
        icon: '📖',
        steps: [
          { text: t('閱讀 Tenant 快速入門（30 秒版）', 'Read Tenant Quick Start (30-second version)'), doc: 'docs/getting-started/for-tenants.md' },
          { text: t('了解你會收到哪些告警', 'Understand which alerts you will receive'), doc: 'docs/rule-packs/ALERT-REFERENCE.md' },
          { text: t('了解三態模式（改值 / 用預設 / 關閉）', 'Understand three-state mode (custom / default / disable)'), doc: 'docs/architecture-and-design.md' },
        ],
      },
      {
        name: t('設定階段', 'Phase 2 — Set Up'),
        icon: '🔧',
        steps: [
          { text: t('向平台團隊申請 tenant namespace', 'Request tenant namespace from platform team'), doc: 'docs/scenarios/tenant-lifecycle.md' },
          { text: t('選擇需要的 Rule Pack 組合', 'Select the Rule Packs for your stack'), doc: null, tool: '../assets/jsx-loader.html?component=../rule-pack-selector.jsx' },
          { text: t('建立 tenant YAML 配置檔', 'Create tenant YAML config file'), doc: 'docs/getting-started/for-tenants.md' },
          { text: t('設定告警路由（Slack / Email / Webhook / PagerDuty）', 'Configure alert routing (Slack / Email / Webhook / PagerDuty)'), doc: 'docs/scenarios/alert-routing-split.md' },
          { text: t('用 YAML Validator 驗證配置', 'Validate config with YAML Validator'), doc: null, tool: '../assets/jsx-loader.html?component=../playground.jsx' },
        ],
      },
      {
        name: t('上線階段', 'Phase 3 — Go Live'),
        icon: '🚀',
        steps: [
          { text: t('提交 PR 讓平台團隊 review', 'Submit PR for platform team review'), doc: 'docs/gitops-deployment.md' },
          { text: t('確認告警送達正確頻道', 'Verify alerts arrive at correct channel'), doc: 'docs/troubleshooting.md' },
          { text: t('測試 maintenance mode 功能', 'Test maintenance mode functionality'), doc: 'docs/scenarios/tenant-lifecycle.md' },
          { text: t('分享 troubleshooting guide 給團隊成員', 'Share troubleshooting guide with team members'), doc: 'docs/troubleshooting.md' },
        ],
      },
    ],
  },
};

function ChecklistItem({ step, checked, onToggle }) {
  return (
    <label className={`flex items-start gap-3 p-3 rounded-lg cursor-pointer transition-colors ${checked ? 'bg-green-50' : 'hover:bg-slate-50'}`}>
      <input
        type="checkbox"
        checked={checked}
        onChange={onToggle}
        className="mt-0.5 w-4 h-4 rounded border-slate-300 text-green-600 focus:ring-green-500 cursor-pointer"
      />
      <div className="flex-1">
        <span className={`text-sm ${checked ? 'line-through text-slate-400' : 'text-slate-800'}`}>
          {step.text}
        </span>
        <div className="flex gap-2 mt-1">
          {step.doc && (
            <a href={`${REPO_BASE}/${step.doc}`} target="_blank" rel="noopener noreferrer"
              onClick={(e) => e.stopPropagation()}
              className="text-xs text-blue-600 hover:underline">
              {t('文件', 'Docs')} →
            </a>
          )}
          {step.tool && (
            <a href={step.tool} target="_blank" rel="noopener noreferrer"
              onClick={(e) => e.stopPropagation()}
              className="text-xs text-purple-600 hover:underline">
              {t('工具', 'Tool')} →
            </a>
          )}
        </div>
      </div>
    </label>
  );
}

export default function OnboardingChecklist() {
  const [role, setRole] = useState(null);
  const [checked, setChecked] = useState({});

  const checklist = role ? CHECKLISTS[role] : null;

  const totalSteps = useMemo(() => {
    if (!checklist) return 0;
    return checklist.phases.reduce((s, p) => s + p.steps.length, 0);
  }, [role]);

  const checkedCount = useMemo(() => {
    return Object.values(checked).filter(Boolean).length;
  }, [checked]);

  const toggleCheck = (phaseIdx, stepIdx) => {
    const key = `${phaseIdx}-${stepIdx}`;
    setChecked(prev => ({ ...prev, [key]: !prev[key] }));
  };

  const resetChecklist = () => {
    setChecked({});
  };

  const selectRole = (r) => {
    setRole(r);
    setChecked({});
  };

  // Generate a printable text version
  const generateText = () => {
    if (!checklist) return '';
    const lines = [`# ${checklist.title}`, `# Generated: ${new Date().toISOString().split('T')[0]}`, ''];
    checklist.phases.forEach((phase, pi) => {
      lines.push(`## ${phase.name}`);
      phase.steps.forEach((step, si) => {
        const mark = checked[`${pi}-${si}`] ? 'x' : ' ';
        lines.push(`- [${mark}] ${step.text}`);
        if (step.doc) lines.push(`      Doc: ${REPO_BASE}/${step.doc}`);
      });
      lines.push('');
    });
    lines.push(`Progress: ${checkedCount}/${totalSteps}`);
    return lines.join('\n');
  };

  const copyChecklist = () => {
    navigator.clipboard.writeText(generateText());
  };

  const progressPct = totalSteps > 0 ? Math.round((checkedCount / totalSteps) * 100) : 0;

  return (
    <div className="min-h-screen bg-gradient-to-br from-slate-50 to-slate-100 p-8">
      <div className="max-w-4xl mx-auto">
        <div className="mb-8">
          <h1 className="text-3xl font-bold text-slate-900 mb-2">{t('上線檢查清單', 'Onboarding Checklist Generator')}</h1>
          <p className="text-slate-600">{t('選擇你的角色，取得從零到完全運作的所有步驟', 'Pick your role and get every step from zero to fully operational')}</p>
        </div>

        {/* Role Selection */}
        {!role ? (
          <div className="space-y-6">
            <h2 className="text-lg font-semibold text-slate-800">{t('你的角色是？', 'What is your role?')}</h2>
            <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
              {ROLES.map(r => (
                <button
                  key={r.id}
                  onClick={() => selectRole(r.id)}
                  className="bg-white rounded-xl border border-slate-200 p-6 text-left hover:border-blue-500 hover:shadow-md transition-all"
                >
                  <span className="text-3xl block mb-3">{r.icon}</span>
                  <div className="font-semibold text-slate-900 mb-1">{r.label}</div>
                </button>
              ))}
            </div>
          </div>
        ) : (
          <>
            {/* Header with role + progress */}
            <div className="bg-white rounded-xl shadow-sm border border-slate-200 p-6 mb-6">
              <div className="flex items-center justify-between mb-4">
                <div className="flex items-center gap-3">
                  <span className="text-2xl">{ROLES.find(r => r.id === role)?.icon}</span>
                  <div>
                    <h2 className="text-lg font-bold text-slate-900">{checklist.title}</h2>
                    <p className="text-xs text-slate-500">{t('勾選完成的步驟追蹤進度', 'Check off steps to track your progress')}</p>
                  </div>
                </div>
                <div className="flex gap-2">
                  <button onClick={resetChecklist} className="text-xs text-slate-500 hover:text-slate-700 px-3 py-1.5 rounded-lg border border-slate-200 hover:border-slate-300">
                    {t('重置', 'Reset')}
                  </button>
                  <button onClick={() => { setRole(null); setChecked({}); }} className="text-xs text-slate-500 hover:text-slate-700 px-3 py-1.5 rounded-lg border border-slate-200 hover:border-slate-300">
                    {t('換角色', 'Change Role')}
                  </button>
                </div>
              </div>

              {/* Progress bar */}
              <div className="flex items-center gap-4">
                <div className="flex-1 h-3 bg-slate-100 rounded-full overflow-hidden">
                  <div
                    className={`h-full rounded-full transition-all duration-500 ${progressPct === 100 ? 'bg-green-500' : 'bg-blue-500'}`}
                    style={{ width: `${progressPct}%` }}
                  />
                </div>
                <span className={`text-sm font-bold ${progressPct === 100 ? 'text-green-600' : 'text-slate-600'}`}>
                  {checkedCount}/{totalSteps} ({progressPct}%)
                </span>
              </div>

              {progressPct === 100 && (
                <div className="mt-4 p-3 bg-green-50 border border-green-200 rounded-lg text-sm text-green-800 font-medium text-center">
                  🎉 {t('恭喜！所有步驟已完成！', 'Congratulations! All steps complete!')}
                </div>
              )}
            </div>

            {/* Phases */}
            <div className="space-y-6">
              {checklist.phases.map((phase, pi) => {
                const phaseChecked = phase.steps.filter((_, si) => checked[`${pi}-${si}`]).length;
                const phaseComplete = phaseChecked === phase.steps.length;
                return (
                  <div key={pi} className={`bg-white rounded-xl shadow-sm border ${phaseComplete ? 'border-green-200' : 'border-slate-200'} overflow-hidden`}>
                    <div className={`px-6 py-4 flex items-center justify-between ${phaseComplete ? 'bg-green-50' : 'bg-slate-50'} border-b border-slate-100`}>
                      <div className="flex items-center gap-3">
                        <span className="text-xl">{phase.icon}</span>
                        <h3 className="font-semibold text-slate-900">{phase.name}</h3>
                      </div>
                      <span className={`text-xs font-bold px-2 py-1 rounded-full ${phaseComplete ? 'bg-green-200 text-green-800' : 'bg-slate-200 text-slate-600'}`}>
                        {phaseChecked}/{phase.steps.length}
                      </span>
                    </div>
                    <div className="p-4 space-y-1">
                      {phase.steps.map((step, si) => (
                        <ChecklistItem
                          key={si}
                          step={step}
                          checked={!!checked[`${pi}-${si}`]}
                          onToggle={() => toggleCheck(pi, si)}
                        />
                      ))}
                    </div>
                  </div>
                );
              })}
            </div>

            {/* Export */}
            <div className="mt-6 flex gap-3">
              <button
                onClick={copyChecklist}
                className="flex-1 px-4 py-3 bg-slate-700 text-white rounded-lg text-sm font-medium hover:bg-slate-600 transition-colors"
              >
                {t('複製為 Markdown', 'Copy as Markdown')}
              </button>
              <button
                onClick={() => window.print()}
                className="flex-1 px-4 py-3 bg-blue-600 text-white rounded-lg text-sm font-medium hover:bg-blue-700 transition-colors"
              >
                {t('列印', 'Print')}
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
