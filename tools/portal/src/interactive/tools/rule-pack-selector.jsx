---
title: "Rule Pack Selector"
tags: [prometheus, rule packs, config gen]
audience: ["platform-engineer", "domain-expert"]
version: v2.9.0
lang: en
related: [rule-pack-detail, dependency-graph, threshold-calculator]
---

import React, { useState, useCallback } from 'react';
import { Copy, ChevronDown } from 'lucide-react';
import { useCopyToClipboard } from './_common/hooks/useCopyToClipboard.js';
import { RULE_PACK_DATA, PACK_ORDER } from './_common/data/rule-packs.js';

const t = window.__t || ((zh, en) => en);

// --- Shared platform data (from platform-data.json via jsx-loader) ---
// Still used for the `categories` slice, which is not part of the per-pack
// catalog. Pack data itself now comes from the canonical accessor below.
const __PD = window.__PLATFORM_DATA || {};

// Rule Pack catalog from the canonical accessor (canonicalize PR-3). Online
// this resolves to window.__PLATFORM_DATA.rulePacks; offline to the baked-in
// mirror in _common/data/rule-packs.js — one catalog, drift-gated against
// platform-data.json. This file used to bake its own count table, which had
// drifted to a v2.7.0 snapshot. Deriving the "(Always included)" suffix here
// (rather than baking it into the offline labels) also makes it translate
// offline, which the old hard-coded English fallback labels did not.
const RULE_PACKS = (() => {
  const packs = {};
  for (const key of PACK_ORDER) {
    const p = RULE_PACK_DATA[key];
    if (!p) continue;
    packs[key] = {
      configMap: p.configMap,
      label: p.required ? `${p.label} (${t('始終包含', 'Always included')})` : p.label,
      recordingRules: p.recordingRules,
      alertRules: p.alertRules,
      category: p.category,
      ...(p.required && { required: true }),
    };
  }
  return packs;
})();

const CATEGORIES = (() => {
  if (__PD.categories) {
    const cats = {};
    const lang = window.__DA_LANG || 'en';
    for (const [key, v] of Object.entries(__PD.categories)) {
      cats[key] = v[lang] || v.en;
    }
    return cats;
  }
  return { database: 'Databases', messaging: 'Messaging', runtime: 'Runtime Environments', webserver: 'Web Servers', infrastructure: 'Infrastructure' };
})();

// Dependency hints, also from the canonical accessor. The old offline fallback
// carried only 4 of the 12 packs that declare dependencies (and English-only
// reasons), so offline users silently lost the hints for redis / mongodb /
// oracle / db2 / clickhouse / rabbitmq / jvm / nginx.
const DEPENDENCIES = (() => {
  const deps = {};
  const lang = window.__DA_LANG || 'en';
  for (const [key, p] of Object.entries(RULE_PACK_DATA)) {
    if (p.dependencies) {
      deps[key] = {
        suggests: p.dependencies.suggests,
        reason: typeof p.dependencies.reason === 'object' ? (p.dependencies.reason[lang] || p.dependencies.reason.en) : p.dependencies.reason,
      };
    }
  }
  return deps;
})();

export default function RulePackSelector() {
  // Initialize from flow state if available (cross-step data passing)
  const flowInit = (window.__FLOW_STATE && window.__FLOW_STATE.selectedPacks)
    ? new Set(window.__FLOW_STATE.selectedPacks) : new Set();
  const [selected, setSelected] = useState(flowInit);
  const [expandedPacks, setExpandedPacks] = useState(new Set());
  const { copied, copy } = useCopyToClipboard();

  const toggleSelection = useCallback((packKey) => {
    const newSelected = new Set(selected);
    if (newSelected.has(packKey)) {
      newSelected.delete(packKey);
    } else {
      newSelected.add(packKey);
    }
    setSelected(newSelected);
    // Persist to flow state for cross-step data passing
    if (window.__flowSave) window.__flowSave({ selectedPacks: Array.from(newSelected) });
  }, [selected]);

  const toggleExpandPack = useCallback((packKey) => {
    const newExpanded = new Set(expandedPacks);
    if (newExpanded.has(packKey)) {
      newExpanded.delete(packKey);
    } else {
      newExpanded.add(packKey);
    }
    setExpandedPacks(newExpanded);
  }, [expandedPacks]);

  // Calculate statistics
  const selectedPacks = Array.from(selected).map(key => RULE_PACKS[key]);
  const alwaysIncludedPacks = ['operational', 'platform'].map(key => RULE_PACKS[key]);
  const allActivePacks = [...selectedPacks, ...alwaysIncludedPacks];

  const totalRecordingRules = allActivePacks.reduce((sum, pack) => sum + pack.recordingRules, 0);
  const totalAlertRules = allActivePacks.reduce((sum, pack) => sum + pack.alertRules, 0);
  const totalRulePacks = allActivePacks.length;

  // Generate YAML snippets
  const generateProjectedVolume = () => {
    const configMapNames = Array.from(selected).map(key => RULE_PACKS[key].configMap)
      .concat(['prometheus-rules-operational', 'prometheus-rules-platform'])
      .sort();

    return `  - name: prometheus-rule-volumes
    projected:
      sources:
${configMapNames.map(cm => `        - configMap:
            name: ${cm}
            items:
              - key: rules
                path: ${cm}.yaml`).join('\n')}`;
  };

  const generatePrometheusRuleFiles = () => {
    const configMapNames = Array.from(selected).map(key => RULE_PACKS[key].configMap)
      .concat(['prometheus-rules-operational', 'prometheus-rules-platform'])
      .sort();

    return configMapNames.map(cm => `      - /etc/prometheus/rules/${cm}.yaml`)
      .join('\n');
  };

  const generateHelmValues = () => {
    const enabled = Array.from(selected).sort();
    const disabled = Object.keys(RULE_PACKS).filter(k => !selected.has(k) && !RULE_PACKS[k].required).sort();
    const lines = ['rulePacks:'];
    enabled.forEach(k => lines.push(`  ${k}: { enabled: true }`));
    disabled.forEach(k => lines.push(`  ${k}: { enabled: false }`));
    return lines.join('\n');
  };

  const yamlOutput = `# Prometheus Rule Packs Configuration
# Auto-generated from Rule Pack Selector

# Add to prometheus-deployment.yaml spec.template.spec.volumes:
${generateProjectedVolume()}

# Add to prometheus.yml rule_files:
rule_files:
${generatePrometheusRuleFiles()}

# ── Helm values-override.yaml ──
# Copy below into your values-override.yaml for helm install:
${generateHelmValues()}`;

  const copyToClipboard = () => copy(yamlOutput);

  const groupedPacks = Object.entries(CATEGORIES).reduce((acc, [category, label]) => {
    const packs = Object.entries(RULE_PACKS)
      .filter(([_, pack]) => pack.category === category && !pack.required)
      .map(([key, pack]) => ({ key, ...pack }));
    if (packs.length > 0) {
      acc[label] = packs;
    }
    return acc;
  }, {});

  return (
    <div className="min-h-screen bg-gradient-to-br from-slate-50 to-slate-100 p-8">
      <div className="max-w-6xl mx-auto">
        {/* Header */}
        <div className="mb-8">
          <h1 className="text-4xl font-bold text-slate-900 mb-2">{t('Rule Pack 選擇器', 'Rule Pack Selector')}</h1>
          <p className="text-lg text-slate-600">{t('為您的基礎設施配置 Prometheus 監控', 'Configure Prometheus monitoring for your infrastructure')}</p>
        </div>

        <div className="grid grid-cols-1 lg:grid-cols-3 gap-8">
          {/* Selector Panel */}
          <div className="lg:col-span-2">
            <div className="bg-white rounded-lg shadow-lg p-6">
              <h2 className="text-2xl font-semibold text-slate-900 mb-6">{t('選擇服務', 'Select Services')}</h2>
              <p className="text-slate-600 mb-6">{t('選擇您要監控的服務和資料庫：', 'Choose which services and databases you monitor:')}</p>

              {/* Always Included Banner */}
              <div className="mb-8 p-4 bg-blue-50 border border-blue-200 rounded-lg">
                <p className="text-sm font-semibold text-blue-900">{t('始終包含', 'Always Included')}</p>
                <p className="text-sm text-blue-700">{t('Operational 和 Platform Rule Pack 始終包含（基礎設施必需）', 'Operational and Platform rule packs are always included (infrastructure essentials)')}</p>
              </div>

              {/* Dependency Hints */}
              {(() => {
                const hints = [];
                selected.forEach(key => {
                  const dep = DEPENDENCIES[key];
                  if (dep) {
                    dep.suggests.forEach(s => {
                      if (!selected.has(s) && !hints.some(h => h.pack === s)) {
                        hints.push({ pack: s, label: RULE_PACKS[s].label, from: RULE_PACKS[key].label, reason: dep.reason });
                      }
                    });
                  }
                });
                return hints.length > 0 ? (
                  <div className="mb-8 p-4 bg-amber-50 border border-amber-200 rounded-lg">
                    <p className="text-sm font-semibold text-amber-900 mb-2">{t('建議的 Rule Packs', 'Suggested Packs')}</p>
                    {hints.map(h => (
                      <div key={h.pack} className="flex items-center justify-between text-sm text-amber-800 py-1">
                        <span>
                          <strong>{h.label}</strong> — {h.reason}
                        </span>
                        <button
                          onClick={() => toggleSelection(h.pack)}
                          className="ml-3 px-2 py-0.5 text-xs font-medium bg-amber-200 text-amber-900 rounded hover:bg-amber-300 transition-colors"
                        >
                          {t('+ 新增', '+ Add')}
                        </button>
                      </div>
                    ))}
                  </div>
                ) : null;
              })()}

              {/* Service Categories */}
              <div className="space-y-8">
                {Object.entries(groupedPacks).map(([categoryLabel, packs]) => (
                  <div key={categoryLabel}>
                    <h3 className="text-sm font-semibold text-slate-700 uppercase tracking-wide mb-4">{categoryLabel}</h3>
                    <div className="space-y-3">
                      {packs.map(pack => (
                        <div key={pack.key} className="flex items-start gap-3">
                          <input
                            type="checkbox"
                            id={pack.key}
                            checked={selected.has(pack.key)}
                            onChange={() => toggleSelection(pack.key)}
                            className="w-5 h-5 mt-0.5 text-blue-600 rounded border-slate-300 focus:ring-2 focus:ring-blue-500 cursor-pointer"
                          />
                          <label htmlFor={pack.key} className="flex-1 cursor-pointer">
                            <div className="flex items-center justify-between">
                              <span className="font-medium text-slate-900">{pack.label}</span>
                              <button
                                onClick={() => toggleExpandPack(pack.key)}
                                className="text-slate-500 hover:text-slate-700 transition-colors"
                                aria-label={t('顯示詳情', 'Show details')}
                              >
                                <ChevronDown
                                  size={16}
                                  className={'transition-transform duration-200 ' + (expandedPacks.has(pack.key) ? 'rotate-180' : '')}
                                />
                              </button>
                            </div>
                            {expandedPacks.has(pack.key) && (
                              <div className="mt-2 text-sm text-slate-600 bg-slate-50 p-3 rounded">
                                <div className="flex gap-4">
                                  <span>📋 {t('記錄規則', 'Recording')}: <strong>{pack.recordingRules}</strong></span>
                                  <span>🚨 {t('告警規則', 'Alerts')}: <strong>{pack.alertRules}</strong></span>
                                </div>
                                <div className="mt-2 text-xs text-slate-500">
                                  {t('ConfigMap', 'ConfigMap')}: <code className="bg-slate-100 px-2 py-1 rounded">{pack.configMap}</code>
                                </div>
                              </div>
                            )}
                          </label>
                        </div>
                      ))}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          </div>

          {/* Summary & Output Panel */}
          <div className="lg:col-span-1">
            <div className="sticky top-8 space-y-4">
              {/* Statistics Card */}
              <div className="bg-white rounded-lg shadow-lg p-6">
                <h3 className="text-lg font-semibold text-slate-900 mb-4">{t('摘要', 'Summary')}</h3>
                <div className="space-y-3">
                  <div className="flex justify-between items-center">
                    <span className="text-slate-600">{t('Rule Packs 數量', 'Rule Packs')}:</span>
                    <span className="text-2xl font-bold text-blue-600">{totalRulePacks}</span>
                  </div>
                  <div className="flex justify-between items-center">
                    <span className="text-slate-600">{t('記錄規則', 'Recording Rules')}:</span>
                    <span className="text-2xl font-bold text-green-600">{totalRecordingRules}</span>
                  </div>
                  <div className="flex justify-between items-center">
                    <span className="text-slate-600">{t('告警規則', 'Alert Rules')}:</span>
                    <span className="text-2xl font-bold text-red-600">{totalAlertRules}</span>
                  </div>
                </div>

                {/* Rule Pack List */}
                {allActivePacks.length > 0 && (
                  <div className="mt-6 pt-6 border-t border-slate-200">
                    <p className="text-xs font-semibold text-slate-700 uppercase mb-3">{t('啟用的 Rule Packs', 'Active Packs')}</p>
                    <div className="space-y-1">
                      {allActivePacks.map(pack => (
                        <div key={pack.configMap} className="flex justify-between text-xs">
                          <span className="text-slate-600">{pack.label}</span>
                          <span className="text-slate-500">
                            {pack.recordingRules}R + {pack.alertRules}A
                          </span>
                        </div>
                      ))}
                    </div>
                  </div>
                )}
              </div>

              {/* YAML Output Card */}
              <div className="bg-white rounded-lg shadow-lg p-6">
                <h3 className="text-lg font-semibold text-slate-900 mb-4">{t('YAML 配置', 'YAML Configuration')}</h3>
                <div className="relative">
                  <pre className="bg-slate-900 text-slate-100 p-4 rounded text-xs overflow-x-auto max-h-64 overflow-y-auto">
{yamlOutput}
                  </pre>
                  <button
                    onClick={copyToClipboard}
                    className={`absolute top-2 right-2 p-2 rounded transition-colors ${
                      copied
                        ? 'bg-green-500 text-white'
                        : 'bg-slate-700 text-slate-200 hover:bg-slate-600'
                    }`}
                    title={t('複製到剪貼簿', 'Copy to clipboard')}
                  >
                    <Copy size={16} />
                  </button>
                </div>
                {copied && (
                  <p className="mt-2 text-sm text-green-600 font-medium"><span aria-hidden="true">✓</span> {t('已複製到剪貼簿', 'Copied to clipboard')}</p>
                )}
                <p className="mt-4 text-xs text-slate-600">
                  {t('將此配置應用到您的 Prometheus 部署並重啟服務。', 'Apply this configuration to your Prometheus deployment and restart the service.')}
                </p>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
