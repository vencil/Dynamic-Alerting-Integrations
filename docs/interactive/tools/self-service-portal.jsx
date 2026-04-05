---
title: "Tenant Self-Service Portal"
tags: [self-service, validation, routing, alerts, tenant]
audience: ["platform-engineer", "domain-expert", "tenant"]
version: v2.4.0
lang: en
related: [playground, config-lint, alert-simulator, schema-explorer]
dependencies: [portal-shared.jsx, YamlValidatorTab.jsx, AlertPreviewTab.jsx, RoutingTraceTab.jsx]
---

import React, { useState } from 'react';

const t = window.__t || ((zh, en) => en);

/* ── Import tab components from dependency-loaded modules ── */
const YamlValidatorTab = window.__YamlValidatorTab;
const AlertPreviewTab = window.__AlertPreviewTab;
const RoutingTraceTab = window.__RoutingTraceTab;

/* ── Main Portal Component ── */
const TABS = [
  { id: 'validate', label: () => t('YAML 驗證', 'YAML Validation'), icon: '🔍' },
  { id: 'alerts', label: () => t('告警預覽', 'Alert Preview'), icon: '🔔' },
  { id: 'routing', label: () => t('路由追蹤', 'Routing Trace'), icon: '🌐' },
];

export default function SelfServicePortal() {
  const [activeTab, setActiveTab] = useState('validate');

  return (
    <div className="max-w-4xl mx-auto">
      <div className="mb-6">
        <h1 className="text-2xl font-bold text-gray-900">
          {t('租戶自助入口', 'Tenant Self-Service Portal')}
        </h1>
        <p className="text-gray-600 mt-1">
          {t('驗證配置、預覽告警、追蹤路由 — 無需 CLI 或部署。支援 15 個 Rule Pack、四層路由合併 (ADR-007)、Severity Dedup。',
             'Validate configs, preview alerts, trace routing — no CLI or deployment needed. Supports 15 Rule Packs, four-layer routing merge (ADR-007), severity dedup.')}
        </p>
      </div>

      {/* Tabs */}
      <div className="flex gap-1 bg-gray-100 p-1 rounded-lg mb-6">
        {TABS.map(tab => (
          <button
            key={tab.id}
            onClick={() => setActiveTab(tab.id)}
            className={`flex-1 px-3 py-2 rounded-md text-sm font-medium transition-colors ${
              activeTab === tab.id
                ? 'bg-white text-blue-600 shadow-sm'
                : 'text-gray-600 hover:text-gray-800'
            }`}
          >
            <span className="mr-1">{tab.icon}</span>
            {tab.label()}
          </button>
        ))}
      </div>

      {/* Tab content */}
      <div className="bg-white rounded-lg border p-6">
        {activeTab === 'validate' && <YamlValidatorTab />}
        {activeTab === 'alerts' && <AlertPreviewTab />}
        {activeTab === 'routing' && <RoutingTraceTab />}
      </div>

      {/* Footer info */}
      <div className="mt-6 p-4 bg-blue-50 rounded-lg border border-blue-100">
        <h4 className="text-sm font-medium text-blue-800 mb-2">
          {t('提示', 'Tips')}
        </h4>
        <ul className="text-sm text-blue-700 space-y-1">
          <li>{t('• 此工具在瀏覽器端執行，YAML 不會送往任何伺服器。',
                 '• This tool runs entirely in your browser — YAML is never sent to any server.')}</li>
          <li>{t('• 工具自動載入 platform-data.json 中的 15 個 Rule Pack metric 定義。',
                 '• Tool auto-loads 15 Rule Pack metric definitions from platform-data.json.')}</li>
          <li>{t('• 完整驗證請使用 CLI: da-tools validate-config --config-dir conf.d/',
                 '• For full validation use CLI: da-tools validate-config --config-dir conf.d/')}</li>
          <li>{t('• Policy-as-Code 策略需透過 CLI 評估: da-tools evaluate-policy --config-dir conf.d/',
                 '• Policy-as-Code evaluation via CLI: da-tools evaluate-policy --config-dir conf.d/')}</li>
        </ul>
      </div>
    </div>
  );
}
