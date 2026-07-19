---
title: "Platform Health Dashboard"
tags: [health, dashboard, monitoring, overview, operations]
audience: ["platform-engineer"]
version: v2.7.0
lang: en
related: [health-dashboard, self-service-portal, alert-simulator]
dependencies: [
  "platform-health/fixtures/platform-data.js",
  "platform-health/components/StatusDot.jsx",
  "platform-health/components/ComponentHealth.jsx",
  "platform-health/components/TenantOverview.jsx",
  "platform-health/components/RulePackDistribution.jsx",
  "platform-health/components/ReloadTimeline.jsx"
]
---

import React from 'react';

// TRK-230 (Option C): orchestrator composes its sub-components via ESM
// imports. The browser loads the esbuild dist bundle (TD-030z removed the
// legacy jsx-loader relative-import transform + dependencies-walk), so
// these ESM imports are what's load-bearing at runtime.
import { StatusDot } from './platform-health/components/StatusDot.jsx';
import { ComponentHealth } from './platform-health/components/ComponentHealth.jsx';
import { TenantOverview } from './platform-health/components/TenantOverview.jsx';
import { RulePackDistribution } from './platform-health/components/RulePackDistribution.jsx';
import { ReloadTimeline } from './platform-health/components/ReloadTimeline.jsx';

const t = window.__t || ((zh, en) => en);

/* ── Main Dashboard ── */
export default function PlatformHealth() {
  return (
    <div className="max-w-5xl mx-auto">
      <div className="mb-6">
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-2xl font-bold text-[color:var(--da-color-fg)]">
              {t('平台健康儀表板', 'Platform Health Dashboard')}
            </h1>
            <p className="text-[color:var(--da-color-muted)] mt-1">
              {t('平台元件狀態、Tenant 概覽、Rule Pack 分佈、最近事件 — 一眼掌握全局。',
                 'Component status, tenant overview, Rule Pack distribution, recent events — at a glance.')}
            </p>
          </div>
          <div className="text-right text-xs text-[color:var(--da-color-muted)]">
            <div>{t('模擬資料', 'Simulated Data')}</div>
            <div>{t('生產環境連接 Prometheus API', 'Production connects to Prometheus API')}</div>
          </div>
        </div>

        {/* Top-level status banner */}
        <div className="mt-4 p-3 bg-[color:var(--da-color-success-soft)] rounded-lg border border-[color:var(--da-color-success)] flex items-center gap-3">
          <StatusDot status="healthy" />
          <span className="text-sm font-semibold text-[color:var(--da-color-success)]">
            {t('平台運行正常', 'Platform Operational')}
          </span>
          <span className="text-xs text-[color:var(--da-color-muted)] ml-auto">
            {t('所有元件健康 · 5 Tenant · 16 Rule Pack · 301 Rules',
               'All components healthy · 5 Tenants · 16 Rule Packs · 301 Rules')}
          </span>
        </div>
      </div>

      <div className="space-y-6">
        <ComponentHealth />
        <TenantOverview />
        <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
          <RulePackDistribution />
          <ReloadTimeline />
        </div>
      </div>

      {/* Footer */}
      <div className="mt-6 p-4 bg-[color:var(--da-color-accent-soft)] rounded-lg border border-[color:var(--da-color-accent-border-soft)]">
        <h4 className="text-sm font-medium text-[color:var(--da-color-accent)] mb-2">{t('提示', 'Tips')}</h4>
        <ul className="text-sm text-[color:var(--da-color-accent)] space-y-1">
          <li>{t('• 此儀表板使用模擬資料。生產環境中透過 da-tools diagnose 和 batch-diagnose 取得即時資料。',
                 '• This dashboard uses simulated data. In production, use da-tools diagnose and batch-diagnose for live data.')}</li>
          <li>{t('• 配置重載由 SHA-256 hash 比對觸發 — threshold-exporter 每 15s 檢查一次。',
                 '• Config reloads are triggered by SHA-256 hash comparison — threshold-exporter checks every 15s.')}</li>
          <li>{t('• Cardinality 上限 500 per-tenant，超過會自動截斷並記錄 ERROR。',
                 '• Cardinality limit is 500 per-tenant. Exceeding triggers auto-truncation with ERROR log.')}</li>
        </ul>
      </div>
    </div>
  );
}
