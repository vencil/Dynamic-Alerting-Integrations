---
title: "YAML Validator Tab"
tags: [self-service, validation, internal]
audience: ["platform-engineer", "tenant"]
version: v2.7.0
lang: en
---

import React, { useState, useMemo, useCallback, useEffect } from 'react';
// Direct ESM imports (dev-rules §S6) — the previous module-scope
// destructure of `window.__portalShared` crashed the whole bundle at
// load time: no module in the graph imported portal-shared.jsx, so
// the producer never ran.
import { getAllMetricKeys } from './_common/data/rule-packs.js';
import { parseYaml } from './_common/validation/yaml-parser.js';
import { generateSampleYaml, validateConfig } from './_common/sim/alert-engine.js';
import { MetricAutocomplete, RulePackSelector } from './portal-shared.jsx';

const t = window.__t || ((zh, en) => en);
const REPO_BASE = "https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main";

function YamlValidatorTab() {
  const [selectedPacks, setSelectedPacks] = useState(['mariadb', 'kubernetes']);
  const [yaml, setYaml] = useState('');
  const [result, setResult] = useState(null);

  // Generate initial YAML from selected packs
  useEffect(() => {
    if (!yaml) {
      setYaml(generateSampleYaml(selectedPacks, false));
    }
  }, []);

  const allMetrics = useMemo(() => getAllMetricKeys(selectedPacks), [selectedPacks]);

  // Saturation `_critical` educational hint (display-only, never blocks
  // validation): scan the textarea for `<base>_critical:` keys whose base
  // metric is classed `saturation` in platform-data / fallback catalog.
  const saturationCriticalHits = useMemo(() => {
    const saturationKeys = new Set(
      allMetrics.filter((m) => m.metricClass === 'saturation').map((m) => m.key));
    if (saturationKeys.size === 0) return [];
    const hits = [];
    const seen = new Set();
    for (const line of yaml.split('\n')) {
      if (line.trim().startsWith('#')) continue;
      const m = line.match(/^\s*([A-Za-z0-9_]+)_critical\s*:/);
      if (m && saturationKeys.has(m[1]) && !seen.has(m[1])) {
        seen.add(m[1]);
        hits.push(`${m[1]}_critical`);
      }
    }
    return hits;
  }, [allMetrics, yaml]);

  const validate = useCallback(() => {
    const { config, errors } = parseYaml(yaml);
    const validation = validateConfig(config, selectedPacks);
    setResult({ config, parseErrors: errors, ...validation });
  }, [yaml, selectedPacks]);

  const handleInsertMetric = useCallback((m) => {
    const line = `${m.key}: "${m.value}"  # ${m.desc || ''}`;
    setYaml(prev => {
      const lines = prev.split('\n');
      // Insert before first _ key or at end of metric section
      let insertIdx = lines.length;
      for (let i = 0; i < lines.length; i++) {
        const trimmed = lines[i].trim();
        if (trimmed.startsWith('_') && !trimmed.startsWith('#')) {
          insertIdx = i;
          break;
        }
      }
      lines.splice(insertIdx, 0, line);
      return lines.join('\n');
    });
  }, []);

  const issueIcon = (level) => {
    if (level === 'error') return '🔴';
    if (level === 'warning') return '🟡';
    return '🔵';
  };

  return (
    <div>
      <h3 className="text-lg font-semibold mb-3">
        {t('YAML 驗證', 'YAML Validation')}
      </h3>
      <p className="text-sm text-gray-600 mb-4">
        {t('選擇 Rule Pack，自動帶入 metric key。貼入或編輯 tenant YAML，即時檢查 schema、routing、policy 問題。',
           'Select Rule Packs for metric key suggestions. Paste or edit tenant YAML to check schema, routing, and policy issues.')}
      </p>

      {/* Rule Pack Selector */}
      <div className="mb-4 p-3 bg-gray-50 rounded-lg border">
        <div className="text-sm font-medium text-gray-700 mb-2">
          {t('選擇 Rule Pack', 'Select Rule Packs')}
          <span className="ml-2 text-gray-400 text-xs font-normal">
            {t(`已選 ${selectedPacks.length} 個`, `${selectedPacks.length} selected`)}
          </span>
        </div>
        <RulePackSelector selected={selectedPacks} onChange={setSelectedPacks} />
      </div>

      {/* Metric autocomplete */}
      <div className="mb-3">
        <div className="text-sm font-medium text-gray-700 mb-1">
          {t('插入 Metric Key', 'Insert Metric Key')}
        </div>
        <MetricAutocomplete allMetrics={allMetrics} onInsert={handleInsertMetric} />
      </div>

      {/* Quick actions */}
      <div className="flex gap-2 mb-2">
        <button
          onClick={() => setYaml(generateSampleYaml(selectedPacks, false))}
          className="text-xs px-2 py-1 bg-gray-200 hover:bg-gray-300 rounded"
        >{t('產生範例：直接 routing', 'Generate: Direct routing')}</button>
        <button
          onClick={() => setYaml(generateSampleYaml(selectedPacks, true))}
          className="text-xs px-2 py-1 bg-gray-200 hover:bg-gray-300 rounded"
        >{t('產生範例：Routing Profile', 'Generate: Routing Profile')}</button>
      </div>

      {saturationCriticalHits.length > 0 && (
        <p
          className="text-xs mt-1 mb-2 pl-2 border-l-2 border-[color:var(--da-color-warning)] text-[color:var(--da-color-warning-text)]"
          data-testid="saturation-critical-hint"
        >
          <code className="font-mono">{saturationCriticalHits.join(', ')}</code>
          {' — '}
          {t('這是飽和類指標的 critical 層：飽和屬容量訊號，建議先確認有配對的症狀告警（如慢查詢），或考慮 warning＋容量規劃——詳見',
             'This sets a critical tier on a saturation metric — saturation is a capacity signal; confirm a paired symptom alert (e.g. slow queries) exists first, or consider warning + capacity planning. See ')}
          <a
            href={`${REPO_BASE}/docs/alerting-design-fundamentals.md`}
            target="_blank"
            rel="noopener noreferrer"
            className="underline"
          >{t('〈告警該響之前〉', "'Before the Alert Fires'")}</a>
          {t('。', '.')}
        </p>
      )}

      <textarea
        value={yaml}
        onChange={(e) => setYaml(e.target.value)}
        className="w-full h-64 font-mono text-sm p-3 border rounded-lg bg-gray-50 focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
        placeholder={t('貼入 tenant YAML...', 'Paste tenant YAML...')}
      />

      <button
        onClick={validate}
        className="mt-3 px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 transition-colors"
      >
        {t('驗證', 'Validate')}
      </button>

      {result && (
        <div className="mt-4 space-y-2">
          {result.issues.length === 0 ? (
            <div className="p-3 bg-green-50 border border-green-200 rounded-lg text-green-800">
              <><span aria-hidden="true">✓</span> {t('所有檢查通過', 'All checks passed')}</>
            </div>
          ) : (
            result.issues.map((issue, i) => (
              <div key={i} className={`p-3 rounded-lg border ${
                issue.level === 'error' ? 'bg-red-50 border-red-200 text-red-800' :
                issue.level === 'warning' ? 'bg-yellow-50 border-yellow-200 text-yellow-800' :
                'bg-blue-50 border-blue-200 text-blue-800'
              }`}>
                <span className="mr-2">{issueIcon(issue.level)}</span>
                <code className="text-xs font-mono bg-white bg-opacity-50 px-1 rounded">{issue.field}</code>
                <span className="ml-2">{issue.msg}</span>
              </div>
            ))
          )}

          <div className="mt-3 p-3 bg-gray-50 rounded-lg">
            <span className="text-sm font-medium text-gray-700">
              {t(`共 ${result.issues.filter(i => i.level === 'error').length} 錯誤、` +
                 `${result.issues.filter(i => i.level === 'warning').length} 警告、` +
                 `${result.issues.filter(i => i.level === 'info').length} 建議`,
                 `${result.issues.filter(i => i.level === 'error').length} errors, ` +
                 `${result.issues.filter(i => i.level === 'warning').length} warnings, ` +
                 `${result.issues.filter(i => i.level === 'info').length} info`)}
            </span>
          </div>
        </div>
      )}
    </div>
  );
}

// <!-- jsx-loader-compat: ignore -->
export { YamlValidatorTab };
