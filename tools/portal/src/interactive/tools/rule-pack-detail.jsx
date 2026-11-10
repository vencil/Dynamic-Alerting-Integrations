---
title: "Rule Pack Detail Viewer"
version: v2.9.0
lang: en
related: [rule-pack-matrix, rule-pack-selector, runbook-viewer]
---

import React, { useState } from 'react';
import { RULE_PACK_DATA, PACK_ORDER, CATEGORY_LABELS } from './_common/data/rule-packs.js';

const t = window.__t || ((zh, en) => en);

// #3681: this page no longer carries a hand-copied list of recording rules and
// alerts — that copy drifted (missing records, exprs without the maintenance
// clause, wrong counts, four packs absent) and no gate could see it. Every
// number here comes from the generated platform data (RULE_PACK_DATA); the
// rules themselves are read where they are generated: ALERT-REFERENCE (every
// alert, with its trigger and suggested action) and the pack's own YAML.
const REPO_BASE = 'https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main';

const PACK_KEYS = [
  ...PACK_ORDER.filter(key => RULE_PACK_DATA[key]),
  ...Object.keys(RULE_PACK_DATA).filter(key => !PACK_ORDER.includes(key)),
];

// Packs ALERT-REFERENCE does not cover: the platform pack's alerts are the
// platform's own, not tenant-facing. Pinned to the generated reference by
// tests/shared/test_portal_rule_pack_detail_links.py, both ways.
const NOT_IN_ALERT_REFERENCE = ['platform'];

function alertReferenceUrl() {
  // The portal's language is window.__DA_LANG (jsx-loader); <html lang> is fixed.
  return `${REPO_BASE}/rule-packs/ALERT-REFERENCE${window.__DA_LANG === 'zh' ? '' : '.en'}.md`;
}

// Read pack from URL hash
function getPackFromHash() {
  try {
    const params = new URLSearchParams(window.location.hash.slice(1));
    return params.get('pack') || null;
  } catch { return null; }
}

function categoryLabel(category) {
  const label = CATEGORY_LABELS[category];
  return label ? label() : category;
}

export default function RulePackDetail() {
  const initialPack = getPackFromHash();
  const [selectedPack, setSelectedPack] = useState(
    initialPack && RULE_PACK_DATA[initialPack] ? initialPack : PACK_KEYS[0]);

  const pack = RULE_PACK_DATA[selectedPack];

  const handlePackChange = (key) => {
    setSelectedPack(key);
    window.history.replaceState(null, '', '#pack=' + key);
  };

  return (
    <div className="min-h-screen bg-gradient-to-br from-gray-50 to-gray-100 p-8">
      <div className="max-w-5xl mx-auto">
        <div className="mb-8">
          <h1 className="text-3xl font-bold text-gray-900 mb-2">{t('Rule Pack 詳情', 'Rule Pack Details')}</h1>
          <p className="text-gray-600">{t('每個 Rule Pack 的規模與規則來源；規則本身請看生成的告警參考與 Rule Pack YAML',
            'Size and sources of each pack; the rules themselves are in the generated alert reference and the pack YAML')}</p>
        </div>

        <div className="flex gap-3 flex-wrap mb-8">
          {PACK_KEYS.map(key => (
            <button
              key={key}
              onClick={() => handlePackChange(key)}
              aria-pressed={selectedPack === key}
              className={`px-4 py-2 rounded-lg text-sm font-medium transition-colors ${
                selectedPack === key
                  ? 'bg-blue-600 text-white shadow-md'
                  : 'bg-white text-gray-700 border border-gray-200 hover:border-blue-300'
              }`}
            >
              {RULE_PACK_DATA[key].label || key}
            </button>
          ))}
        </div>

        {pack && (
          <div className="bg-white rounded-xl shadow-sm border border-gray-200 p-6 mb-6">
            <div className="flex items-start justify-between flex-wrap gap-4">
              <div>
                <h2 className="text-2xl font-bold text-gray-900">{pack.label || selectedPack}</h2>
                <div className="mt-2 text-xs text-gray-500">
                  {t('分類', 'Category')}: <span className="font-medium">{categoryLabel(pack.category)}</span>
                  {pack.exporter && (<>
                    {' · '}{t('匯出器', 'Exporter')}: <code className="bg-gray-100 px-2 py-0.5 rounded">{pack.exporter}</code>
                  </>)}
                </div>
              </div>
              <div className="flex gap-4 text-center">
                <div className="bg-blue-50 px-4 py-2 rounded-lg">
                  <div className="text-xl font-bold text-blue-600">{pack.recordingRules ?? '—'}</div>
                  <div className="text-xs text-gray-500">{t('記錄規則', 'Recording')}</div>
                </div>
                <div className="bg-red-50 px-4 py-2 rounded-lg">
                  <div className="text-xl font-bold text-red-600">{pack.alertRules ?? '—'}</div>
                  <div className="text-xs text-gray-500">{t('告警規則', 'Alerts')}</div>
                </div>
              </div>
            </div>
            <div className="mt-6 flex flex-wrap gap-3">
              {!NOT_IN_ALERT_REFERENCE.includes(selectedPack) && (
                <a href={alertReferenceUrl()} target="_blank" rel="noopener noreferrer"
                   className="px-4 py-2 rounded-lg text-sm font-medium bg-blue-600 text-white hover:bg-blue-700">
                  {t('告警參考（觸發條件與建議操作）', 'Alert reference (triggers and suggested actions)')}
                </a>
              )}
              <a href={`${REPO_BASE}/rule-packs/rule-pack-${selectedPack}.yaml`} target="_blank" rel="noopener noreferrer"
                 className="px-4 py-2 rounded-lg text-sm font-medium bg-white text-gray-700 border border-gray-200 hover:border-blue-300">
                {t('Rule Pack YAML（完整 PromQL）', 'Rule Pack YAML (full PromQL)')}
              </a>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
