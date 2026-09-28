---
title: "Interactive Glossary"
version: v2.9.0
lang: en
related: [schema-explorer, wizard, onboarding-checklist]
dependencies: [
  "_common/components/EmptyState.jsx"
]
---

import React, { useState } from 'react';
// PR-portal-8: shared empty-state replaces inline "No terms" div.
// TRK-230f: ESM import — esbuild bundles it natively (TD-030z retired the jsx-loader transform).
import { EmptyState } from './_common/components/EmptyState.jsx';

const t = window.__t || ((zh, en) => en);

const GLOSSARY = [
  { term: 'Rule Pack', category: 'Core', def: 'A pre-built bundle of Prometheus recording rules and alert rules for a specific technology (e.g., MariaDB, Redis). You pick the ones you need — no PromQL required.', related: ['Recording Rule', 'Alert Rule', 'Projected Volume'], tryLink: '../assets/jsx-loader.html?component=../rule-pack-selector.jsx' },
  { term: 'Threshold', category: 'Core', def: 'A numeric limit (like "80% CPU") that triggers an alert. Each tenant sets their own values in simple YAML — just a key-value pair.', related: ['Tenant', 'Three-State Mode'], tryLink: '../assets/jsx-loader.html?component=../threshold-calculator.jsx' },
  { term: 'Tenant', category: 'Core', def: 'A team or namespace that owns a set of services. Each tenant has isolated config, thresholds, and alert routing. Multi-tenant isolation is the core architecture principle.', related: ['Threshold', 'Routing'], tryLink: '../assets/jsx-loader.html?component=../playground.jsx' },
  { term: 'Three-State Mode', category: 'Core', def: 'Every threshold key has three states: a custom value (you set it), omitted (you inherit the platform default from _defaults.yaml, if it sets one), or "disable" (no alert for that key).', related: ['Threshold', 'Silent Mode', 'Maintenance Mode'] },
  { term: 'Recording Rule', category: 'Prometheus', def: 'A Prometheus rule that pre-computes and stores metric calculations for faster queries. Rule Packs include these automatically — you never write them manually.', related: ['Rule Pack', 'Alert Rule'] },
  { term: 'Alert Rule', category: 'Prometheus', def: 'A Prometheus rule that fires an alert when its condition holds. Rule Packs ship them ready-made; your tenant YAML only supplies the threshold values they compare against. Rules of your own go in _custom_alerts.', related: ['Rule Pack', 'Recording Rule', 'Severity Dedup'] },
  { term: 'Severity Dedup', category: 'Architecture', def: 'When a Critical alert fires, the matching Warning alert is suppressed via an Alertmanager inhibit rule, so you only see the most severe one. On by default; set _severity_dedup to "disable" to turn it off.', related: ['Alert Rule', 'Inhibit Rule'], tryLink: '../assets/jsx-loader.html?component=../alert-simulator.jsx' },
  { term: 'threshold-exporter', category: 'Component', def: 'The core Go binary that reads tenant YAML configs and exports Prometheus metrics. Runs with HA replicas, SHA-256 hot-reload, and directory scanning.', related: ['Rule Pack', 'Threshold', 'Projected Volume'] },
  { term: 'Projected Volume', category: 'Kubernetes', def: 'A Kubernetes volume that combines multiple ConfigMaps into a single mount point. The plain-manifest deployment uses it to mount the Rule Pack ConfigMaps into Prometheus; operator mode ships Rule Packs as PrometheusRule resources instead.', related: ['Rule Pack', 'ConfigMap'] },
  { term: 'ConfigMap', category: 'Kubernetes', def: 'A Kubernetes object that stores non-confidential configuration data as key-value pairs. Each Rule Pack is its own ConfigMap; tenant configs share one (threshold-config), one key per tenant.', related: ['Projected Volume', 'Tenant'] },
  { term: 'Silent Mode', category: 'Operations', def: 'Suppresses alert notifications for a tenant while alerts keep firing and are recorded in TSDB. Accepted values: see the _silent_mode entry in the Schema Explorer.', related: ['Three-State Mode', 'Maintenance Mode', 'Sentinel Alert'] },
  { term: 'Maintenance Mode', category: 'Operations', def: 'Suppresses alerts during planned maintenance. One-time (with expires) filters alerts out in PromQL, so nothing fires; recurring windows (cron schedule) become Alertmanager silences.', related: ['Silent Mode', 'Three-State Mode', 'Recurring Maintenance'] },
  { term: 'Sentinel Alert', category: 'Architecture', def: 'An alert on a flag metric (0 or 1) that threshold-exporter exports, such as user_silent_mode. While it fires, an Alertmanager inhibit rule holds back the tenant notifications it covers. Silent mode and severity dedup work this way; maintenance mode does not (it filters in PromQL).', related: ['Silent Mode', 'Severity Dedup', 'Inhibit Rule'] },
  { term: 'Inhibit Rule', category: 'Alertmanager', def: 'An Alertmanager rule that suppresses one alert when another is firing. Used for severity dedup (critical suppresses warning) and sentinel-based mode switching.', related: ['Severity Dedup', 'Sentinel Alert'] },
  { term: 'Routing', category: 'Alertmanager', def: 'The mechanism that sends alerts to the right receiver (Slack, webhook, email, etc.) based on tenant configuration, customizable per tenant.', related: ['Receiver', 'Enforced Routing'] },
  { term: 'Receiver', category: 'Alertmanager', def: 'The destination for alert notifications. Supported types: webhook, email, slack, teams, rocketchat, pagerduty. Configured per-tenant in _routing.receiver.', related: ['Routing'] },
  { term: 'Enforced Routing', category: 'Alertmanager', def: 'Optional platform-level routing (_routing_enforced) that also sends a tenant\'s alerts to the NOC/platform team. Pairs with dual-perspective annotations (platform_summary for NOC, summary for tenant).', related: ['Routing', 'Dual-Perspective Annotation'] },
  { term: 'Dual-Perspective Annotation', category: 'Architecture', def: 'Rule Pack alerts carry two summaries: platform_summary (NOC-oriented, infrastructure context) and summary (tenant-oriented, business context). Used with enforced routing.', related: ['Enforced Routing'] },
  { term: 'group_left', category: 'PromQL', def: 'A Prometheus vector matching operator used to join threshold metrics with info metrics. Enables dynamic runbook URL, owner, and tier injection without hardcoding.', related: ['Recording Rule', 'Dynamic Runbook Injection'] },
  { term: 'Dynamic Runbook Injection', category: 'Architecture', def: 'threshold-exporter exports tenant metadata (runbook_url, owner, tier) as the tenant_metadata_info metric, and Rule Pack alerts join it in via group_left. A tenant sets its runbook link in YAML without touching any rule.', related: ['group_left'] },
  { term: 'Cardinality Guard', category: 'Architecture', def: 'Per-tenant cap on exported metrics: 500 by default, set by max_metrics_per_tenant in the root _defaults.yaml. Metrics over the cap are dropped, an ERROR is logged, and the TenantMetricsOverLimit alert fires.', related: ['Threshold', 'threshold-exporter'] },
  { term: 'Hot-Reload', category: 'Architecture', def: 'threshold-exporter checks its config files on a timer and reloads when their SHA-256 hash changes. Config updates need no restart.', related: ['threshold-exporter'] },
  { term: 'Recurring Maintenance', category: 'Operations', def: 'Cron-based maintenance windows that automatically create Alertmanager silences. Configured in _state_maintenance.recurring[] with a cron expression and duration; the da-tools maintenance-scheduler CronJob creates the silences.', related: ['Maintenance Mode'] },
  { term: 'da-tools', category: 'Tooling', def: 'The CLI toolkit for Dynamic Alerting. Its commands cover scaffolding, validation, migration, diagnostics, benchmarking, and more. Available as Docker image or direct install.', tryLink: '../assets/jsx-loader.html?component=../cli-playground.jsx' },
];

const CATEGORIES = [...new Set(GLOSSARY.map(g => g.category))];

export default function GlossaryPage() {
  const [search, setSearch] = useState('');
  const [selectedCategory, setSelectedCategory] = useState('all');
  const [expanded, setExpanded] = useState(new Set());

  const toggle = (term) => {
    setExpanded(prev => {
      const next = new Set(prev);
      if (next.has(term)) next.delete(term); else next.add(term);
      return next;
    });
  };

  const filtered = GLOSSARY.filter(g => {
    const q = search.toLowerCase();
    if (q && !g.term.toLowerCase().includes(q) && !g.def.toLowerCase().includes(q)) return false;
    if (selectedCategory !== 'all' && g.category !== selectedCategory) return false;
    return true;
  });

  return (
    <div className="min-h-screen bg-gradient-to-br from-slate-50 to-slate-100 p-8">
      <div className="max-w-4xl mx-auto">
        <div className="mb-8">
          <h1 className="text-3xl font-bold text-slate-900 mb-2">{t('互動式術語表', 'Interactive Glossary')}</h1>
          <p className="text-slate-600">{t(`${GLOSSARY.length} 個平台術語，可搜尋、可篩選`, `${GLOSSARY.length} platform terms — searchable and filterable`)}</p>
        </div>

        <div className="flex flex-col sm:flex-row gap-3 mb-6">
          <input
            type="text"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder={t('搜尋術語...', 'Search terms...')}
            className="flex-1 px-4 py-2 rounded-lg border border-slate-200 text-sm focus:outline-none focus:ring-2 focus:ring-blue-400 bg-white"
          />
          <select
            value={selectedCategory}
            onChange={(e) => setSelectedCategory(e.target.value)}
            aria-label={t('依分類篩選', 'Filter by category')}
            className="px-3 py-2 rounded-lg border border-slate-200 text-sm bg-white focus:outline-none focus:ring-2 focus:ring-blue-400"
          >
            <option value="all">{t('所有分類', 'All Categories')}</option>
            {CATEGORIES.map(c => <option key={c} value={c}>{c}</option>)}
          </select>
        </div>

        <div className="text-xs text-slate-500 mb-4">{filtered.length} / {GLOSSARY.length} {t('術語', 'terms')}</div>

        <div className="space-y-2">
          {filtered.map(g => (
            <div key={g.term} className="bg-white rounded-xl border border-slate-200 overflow-hidden">
              <button
                onClick={() => toggle(g.term)}
                className="w-full text-left p-4 flex items-center justify-between hover:bg-slate-50 transition-colors"
              >
                <div className="flex items-center gap-3">
                  <span className="text-xs font-medium px-2 py-0.5 rounded bg-slate-100 text-slate-500">{g.category}</span>
                  <span className="font-semibold text-slate-900">{g.term}</span>
                </div>
                <span className="text-slate-400 text-sm">{expanded.has(g.term) ? '▲' : '▼'}</span>
              </button>
              {expanded.has(g.term) && (
                <div className="px-4 pb-4 border-t border-slate-100 pt-3">
                  <p className="text-sm text-slate-700 leading-relaxed mb-3">{g.def}</p>
                  {g.related && g.related.length > 0 && (
                    <div className="mb-3">
                      <span className="text-xs font-semibold text-slate-500">{t('相關', 'Related')}: </span>
                      {g.related.map(r => (
                        <button
                          key={r}
                          onClick={() => { setSearch(r); setSelectedCategory('all'); }}
                          className="text-xs text-blue-600 hover:underline mr-2"
                        >{r}</button>
                      ))}
                    </div>
                  )}
                  {g.tryLink && (
                    <a
                      href={g.tryLink}
                      className="inline-block text-xs px-3 py-1.5 bg-blue-600 text-white rounded-lg hover:bg-blue-700 transition-colors"
                    >
                      {t('試試看 →', 'Try it →')}
                    </a>
                  )}
                </div>
              )}
            </div>
          ))}
        </div>

        {filtered.length === 0 && (
          <EmptyState
            icon="📖"
            title={t('沒有符合的術語', 'No terms match your search.')}
          />
        )}
      </div>
    </div>
  );
}
