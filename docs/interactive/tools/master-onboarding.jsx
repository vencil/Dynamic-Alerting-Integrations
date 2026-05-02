---
title: "Master Onboarding — Dual Entry"
tags: [onboarding, dual-entry, dispatcher, import, greenfield, c-3]
audience: [platform-engineer, sre, tenant]
version: v2.7.0
lang: en
related: [cicd-setup-wizard, deployment-wizard, onboarding-checklist, tenant-manager, alert-simulator]
---

import React, { useState, useMemo } from 'react';

/* ── i18n + repo helpers ───────────────────────────────────────────── */
const t = window.__t || ((zh, en) => en);
const REPO_BASE = "https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main";
const docUrl = (relative) => `${REPO_BASE}/${relative}`;

/* ── Journey definitions (S#90 / C-3 PR-1 MVP) ────────────────────────
 *
 * Two onboarding entry points per planning §C-3:
 *   - Import Journey (existing assets): Migration Toolkit → C-8 parser →
 *     C-9 profile builder → C-10 batch PR → C-12 dangling-defaults guard
 *   - Wizard Journey (greenfield): CI/CD Setup Wizard → Deployment Wizard
 *     → [alert-builder, routing-trace TBD v2.8.x]
 *
 * Honest scope (PR-1 MVP):
 *   - Dispatcher UI + step-by-step pointers to existing tools/docs.
 *   - Does NOT build alert-builder / routing-trace wizards (deferred).
 *   - Does NOT implement architecture-aligned shared profile-extract
 *     UI (R7 constraint applies to backend code path, already covered
 *     by C-9 PR-3).
 * ─────────────────────────────────────────────────────────────────── */

const IMPORT_STEPS = [
  {
    id: 'install',
    title: () => t('1. 安裝 Migration Toolkit', '1. Install Migration Toolkit'),
    desc: () => t(
      '從 GitHub Release 下載 da-tools binary（或拉 Docker image / air-gapped tar）。三種交付形態說明見安裝指南。',
      'Download da-tools binary from GitHub Release (or Docker image / air-gapped tar). See installation guide for the three delivery modes.'
    ),
    cta: () => t('查看安裝指南', 'View installation guide'),
    href: docUrl('docs/migration-toolkit-installation.md'),
    cmd: '# verify cosign signature (recommended for cloud-native)\nmake verify-release VERSION=tools/v2.8.0',
  },
  {
    id: 'parser',
    title: () => t('2. 解析現有 PrometheusRule', '2. Parse existing PrometheusRule'),
    desc: () => t(
      'C-8 parser 把 PromQL / MetricsQL rule YAML 轉成中介 JSON，標 dialect / prom_portable / vm_only_functions 等欄位。可選 --fail-on-non-portable 嚴格模式。',
      'C-8 parser converts PromQL / MetricsQL rule YAML to intermediate JSON, tagging dialect / prom_portable / vm_only_functions. Optional --fail-on-non-portable strict mode.'
    ),
    cta: () => t('CLI Reference', 'CLI Reference'),
    href: docUrl('docs/cli-reference.md'),
    cmd: 'da-tools parser import \\\n  --input prom-rules.yaml \\\n  --output parsed.json \\\n  --validate-strict-prom',
  },
  {
    id: 'profile',
    title: () => t('3. 聚類建議 Profile', '3. Cluster proposals into Profile'),
    desc: () => t(
      'C-9 Profile Builder 把 parser 輸出聚類成 _defaults.yaml 候選；接受 / 拒絕 / 編輯每組建議；輸出 conf.d/ 目錄結構（Profile-as-Directory-Default, ADR-019）。',
      'C-9 Profile Builder clusters parser output into _defaults.yaml proposals; accept / reject / edit each group; emits conf.d/ directory shape (Profile-as-Directory-Default, ADR-019).'
    ),
    cta: () => t('ADR-019', 'ADR-019'),
    href: docUrl('docs/adr/019-profile-as-directory-default.md'),
    cmd: 'da-tools profile build \\\n  --input parsed.json \\\n  --decisions decisions.yaml \\\n  --enable-fuzzy   # opt-in duration-equivalence',
  },
  {
    id: 'batchpr',
    title: () => t('4. 開 Batch PR 進客戶 repo', '4. Open Batch PRs into customer repo'),
    desc: () => t(
      'C-10 Batch PR Pipeline：先 [Base Infrastructure PR] 帶所有 _defaults.yaml；個別 tenant PR 標 Blocked by: #base。--chunk-by domain / region / count=N。--dry-run 出 plan。',
      'C-10 Batch PR Pipeline: [Base Infrastructure PR] first carries all _defaults.yaml; tenant PRs tagged Blocked by: #base. --chunk-by domain / region / count=N. --dry-run outputs plan.'
    ),
    cta: () => t('Migration Playbook', 'Migration Playbook'),
    href: docUrl('docs/scenarios/incremental-migration-playbook.md'),
    cmd: 'da-tools batch-pr apply \\\n  --profile profile.yaml \\\n  --targets tenants.yaml \\\n  --chunk-by domain \\\n  --dry-run',
  },
  {
    id: 'guard',
    title: () => t('5. Guard 驗證 Base PR', '5. Guard validates Base PR'),
    desc: () => t(
      'C-12 Dangling Defaults Guard 自動 PR-time 驗證：(i) Schema validator (ii) ADR-017/018 routing guardrails (iii) Cardinality guard。任一 tenant orphan / cycle / cardinality 超標 → block merge。',
      'C-12 Dangling Defaults Guard auto-runs at PR time: (i) Schema validator (ii) ADR-017/018 routing guardrails (iii) Cardinality guard. Any orphan tenant / route cycle / cardinality breach → block merge.'
    ),
    cta: () => t('查看 guard workflow', 'View guard workflow'),
    href: `${REPO_BASE}/.github/workflows/guard-defaults-impact.yml`,
    cmd: '# guard runs automatically on PRs touching **/_defaults.yaml\n# manual trigger:\nda-tools guard defaults-impact --pr <number>',
  },
];

const WIZARD_STEPS = [
  {
    id: 'cicd',
    title: () => t('1. CI/CD 平台設定', '1. CI/CD Platform Setup'),
    desc: () => t(
      '4 步互動精靈，選擇 GitHub Actions / GitLab CI、部署模式（Kustomize / Helm / ArgoCD）、Rule Packs，產出 da-tools init 命令 + workflow YAML。',
      '4-step interactive wizard: pick GitHub Actions / GitLab CI, deployment mode (Kustomize / Helm / ArgoCD), Rule Packs; outputs da-tools init command + workflow YAML.'
    ),
    cta: () => t('開啟 CI/CD Setup Wizard', 'Open CI/CD Setup Wizard'),
    href: 'cicd-setup-wizard.html',
    internal: true,
  },
  {
    id: 'deploy',
    title: () => t('2. 部署 threshold-exporter', '2. Deploy threshold-exporter'),
    desc: () => t(
      'Deployment Wizard：選擇 Kubernetes 叢集模式、Prometheus 整合方式、reload sidecar，產出 Helm values 或 Kustomize patch。',
      'Deployment Wizard: pick Kubernetes cluster mode, Prometheus integration, reload sidecar; outputs Helm values or Kustomize patch.'
    ),
    cta: () => t('開啟 Deployment Wizard', 'Open Deployment Wizard'),
    href: 'deployment-wizard.html',
    internal: true,
  },
  {
    id: 'alerts',
    title: () => t('3. 建立告警規則', '3. Build alert rules'),
    desc: () => t(
      '4 步互動精靈：identity / expression / severity / review YAML。輸出 PrometheusRule snippet 可直接貼入 rule-packs/ 或餵給 da-tools alert-create。',
      '4-step interactive wizard: identity / expression / severity / review YAML. Outputs PrometheusRule snippet — paste into rule-packs/ or feed da-tools alert-create.'
    ),
    cta: () => t('開啟 Alert Builder', 'Open Alert Builder'),
    href: 'alert-builder.html',
    internal: true,
  },
  {
    id: 'routing',
    title: () => t('4. 路由追蹤', '4. Routing Trace'),
    desc: () => t(
      '4 步路由模擬器：定義樣本告警 + 預設路由 + 子路由 → 看哪個 receiver 收到。教 label-match + tree-walk 概念；inhibit / timing simulation 用 alert-simulator 與 amtool。',
      '4-step routing simulator: define sample alert + default route + child routes → see which receiver gets it. Teaches label-match + tree-walk concept; inhibit / timing simulation lives in alert-simulator + amtool.'
    ),
    cta: () => t('開啟 Routing Trace', 'Open Routing Trace'),
    href: 'routing-trace.html',
    internal: true,
  },
  {
    id: 'verify',
    title: () => t('5. 驗證與管理 tenants', '5. Verify & manage tenants'),
    desc: () => t(
      '兩條動線匯流：用 Tenant Manager 看 effective config / source_hash / merged_hash；改完 tenant.yaml 走 Git PR 流程。',
      'Both journeys converge here: use Tenant Manager to inspect effective config / source_hash / merged_hash; tenant.yaml edits flow via Git PR.'
    ),
    cta: () => t('開啟 Tenant Manager', 'Open Tenant Manager'),
    href: 'tenant-manager.html',
    internal: true,
  },
];

/* ── Step component (shared between journeys) ────────────────────── */
function Step({ step, index, total }) {
  const deferredBadge = step.deferred ? (
    <span className="ml-2 px-2 py-0.5 text-xs rounded-full bg-[color:var(--da-color-warning-soft)] text-[color:var(--da-color-warning)]">
      {t('規劃中', 'Planned')}
    </span>
  ) : null;

  return (
    <div className="border border-[color:var(--da-color-surface-border)] rounded-lg p-5 bg-[color:var(--da-color-surface)]">
      <div className="flex items-baseline justify-between mb-2">
        <h4 className="text-base font-semibold text-[color:var(--da-color-fg)]">
          {step.title()}{deferredBadge}
        </h4>
        <span className="text-xs text-[color:var(--da-color-muted)]">
          {index + 1} / {total}
        </span>
      </div>
      <p className="text-sm text-[color:var(--da-color-muted)] mb-3 leading-relaxed">
        {step.desc()}
      </p>
      {step.cmd && (
        <pre className="text-xs bg-[color:var(--da-color-toast-bg)] text-[color:var(--da-color-toast-fg)] p-3 rounded mb-3 overflow-x-auto">
{step.cmd}
        </pre>
      )}
      <a
        href={step.href}
        target={step.internal ? undefined : '_blank'}
        rel={step.internal ? undefined : 'noopener noreferrer'}
        className="inline-flex items-center text-sm font-medium text-[color:var(--da-color-accent)] hover:text-[color:var(--da-color-accent-hover)] underline decoration-dotted underline-offset-4"
      >
        {step.cta()} →
      </a>
    </div>
  );
}

/* ── Journey card (entry-point selection) ────────────────────────── */
function JourneyCard({ icon, title, subtitle, bullets, ctaLabel, onSelect, dataTestId }) {
  return (
    <button
      type="button"
      onClick={onSelect}
      data-testid={dataTestId}
      className="text-left p-6 rounded-xl border-2 border-[color:var(--da-color-surface-border)] bg-[color:var(--da-color-surface)] hover:border-[color:var(--da-color-accent)] hover:shadow-md transition-all focus:outline-none focus:ring-2 focus:ring-[color:var(--da-color-focus-ring)]"
    >
      <div className="text-4xl mb-3">{icon}</div>
      <h3 className="text-xl font-bold text-[color:var(--da-color-fg)] mb-1">{title}</h3>
      <p className="text-sm text-[color:var(--da-color-muted)] mb-4">{subtitle}</p>
      <ul className="text-sm text-[color:var(--da-color-fg)] space-y-1.5 mb-5 list-disc pl-5">
        {bullets.map((b, i) => <li key={i}>{b}</li>)}
      </ul>
      <span className="inline-flex items-center text-sm font-semibold text-[color:var(--da-color-accent)]">
        {ctaLabel} →
      </span>
    </button>
  );
}

/* ── Main component ───────────────────────────────────────────────── */
export default function MasterOnboarding() {
  // 'choose' | 'import' | 'wizard'
  const [journey, setJourney] = useState('choose');

  const steps = useMemo(() => {
    if (journey === 'import') return IMPORT_STEPS;
    if (journey === 'wizard') return WIZARD_STEPS;
    return [];
  }, [journey]);

  return (
    <div className="max-w-4xl mx-auto p-6 space-y-6">
      {/* Header */}
      <div>
        <h1 className="text-2xl font-bold text-[color:var(--da-color-fg)] mb-2">
          {t('入門總覽 — 雙路徑分岔', 'Master Onboarding — Dual Entry')}
        </h1>
        <p className="text-sm text-[color:var(--da-color-muted)]">
          {t(
            '兩條 onboarding 動線在第一步分岔，最後在 Tenant Manager 匯流。',
            'Two onboarding paths split at step 1 and converge at Tenant Manager.'
          )}
        </p>
      </div>

      {/* Step 0: Journey choice */}
      {journey === 'choose' && (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-5">
          <JourneyCard
            icon="📥"
            title={t('Import Journey', 'Import Journey')}
            subtitle={t(
              '已有 PrometheusRule / Alertmanager 設定要遷入',
              'Existing PrometheusRule / Alertmanager configs to migrate'
            )}
            bullets={[
              t('安裝 Migration Toolkit (binary / Docker / air-gapped tar)', 'Install Migration Toolkit (binary / Docker / air-gapped tar)'),
              t('Parser → Profile → Batch PR → Guard 五步管線', 'Parser → Profile → Batch PR → Guard 5-step pipeline'),
              t('適合：巨型客戶、有既有資產、conf.d/ 大批次', 'For: large customers with existing assets, big-batch conf.d/'),
            ]}
            ctaLabel={t('開始 Import 路徑', 'Start Import Path')}
            onSelect={() => setJourney('import')}
            dataTestId="onboarding-card-import"
          />
          <JourneyCard
            icon="🌱"
            title={t('Wizard Journey', 'Wizard Journey')}
            subtitle={t(
              '從零開始，透過互動精靈一步步建構',
              'Greenfield setup via interactive wizards'
            )}
            bullets={[
              t('CI/CD Setup → Deployment → Alert / Routing 四個 wizard', 'CI/CD Setup → Deployment → Alert / Routing 4 wizards'),
              t('產出 da-tools init 命令 + Helm values + workflow YAML', 'Generates da-tools init + Helm values + workflow YAML'),
              t('適合：新客戶、單 tenant、greenfield 部署', 'For: new customers, single-tenant, greenfield deployment'),
            ]}
            ctaLabel={t('開始 Wizard 路徑', 'Start Wizard Path')}
            onSelect={() => setJourney('wizard')}
            dataTestId="onboarding-card-wizard"
          />
        </div>
      )}

      {/* Step list (Import or Wizard) */}
      {journey !== 'choose' && (
        <>
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2">
              <span className="text-sm font-medium text-[color:var(--da-color-fg)]">
                {journey === 'import'
                  ? t('Import 路徑（既有資產遷入）', 'Import Path (existing assets)')
                  : t('Wizard 路徑（從零建構）', 'Wizard Path (greenfield)')}
              </span>
              <span className="text-xs text-[color:var(--da-color-muted)]">
                · {steps.length} {t('步', 'steps')}
              </span>
            </div>
            <button
              type="button"
              onClick={() => setJourney('choose')}
              data-testid="onboarding-back"
              className="text-sm text-[color:var(--da-color-muted)] hover:text-[color:var(--da-color-fg)] underline"
            >
              ← {t('回到選擇', 'Back to choice')}
            </button>
          </div>
          <div className="space-y-4">
            {steps.map((step, index) => (
              <Step key={step.id} step={step} index={index} total={steps.length} />
            ))}
          </div>
          <div className="border-t border-[color:var(--da-color-surface-border)] pt-4">
            <p className="text-sm text-[color:var(--da-color-muted)]">
              {t('完成後 → ', 'When done → ')}
              <a
                href="tenant-manager.html"
                className="font-medium text-[color:var(--da-color-accent)] hover:text-[color:var(--da-color-accent-hover)] underline decoration-dotted underline-offset-4"
              >
                {t('開啟 Tenant Manager 驗證', 'Open Tenant Manager to verify')}
              </a>
              {t(' — 兩條路徑在這裡匯流。', ' — both journeys converge here.')}
            </p>
          </div>
        </>
      )}
    </div>
  );
}
