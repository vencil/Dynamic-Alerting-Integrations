---
title: "Config Template Gallery"
tags: [templates, examples, stacks]
audience: [tenant, "platform-engineer"]
version: v2.3.0
lang: en
related: [playground, rule-pack-selector, threshold-calculator]
---

import React, { useState, useMemo, useEffect } from 'react';

const t = window.__t || ((zh, en) => en);

/* ── Fallback pack list (used if JSON fetch fails) ── */
const FALLBACK_PACKS = [
  { id: 'mariadb', label: 'MariaDB' },
  { id: 'postgresql', label: 'PostgreSQL' },
  { id: 'redis', label: 'Redis' },
  { id: 'mongodb', label: 'MongoDB' },
  { id: 'elasticsearch', label: 'Elasticsearch' },
  { id: 'oracle', label: 'Oracle' },
  { id: 'db2', label: 'DB2' },
  { id: 'clickhouse', label: 'ClickHouse' },
  { id: 'kafka', label: 'Kafka' },
  { id: 'rabbitmq', label: 'RabbitMQ' },
  { id: 'jvm', label: 'JVM' },
  { id: 'nginx', label: 'Nginx' },
  { id: 'kubernetes', label: 'Kubernetes' },
];

/* Convert bilingual JSON object { zh, en } to localized string */
function tText(obj) {
  if (!obj) return '';
  if (typeof obj === 'string') return obj;
  return t(obj.zh || '', obj.en || '');
}

/* Transform raw JSON template into runtime format with t() accessors */
function hydrateTemplate(raw) {
  return { ...raw, name: () => tText(raw.name), desc: () => tText(raw.desc) };
}

/* ── Pack badge colors ── */
const PACK_COLORS = {
  mariadb: 'bg-blue-100 text-blue-700',
  postgresql: 'bg-indigo-100 text-indigo-700',
  redis: 'bg-red-100 text-red-700',
  mongodb: 'bg-green-100 text-green-700',
  elasticsearch: 'bg-purple-100 text-purple-700',
  oracle: 'bg-rose-100 text-rose-700',
  db2: 'bg-sky-100 text-sky-700',
  clickhouse: 'bg-orange-100 text-orange-700',
  kafka: 'bg-amber-100 text-amber-700',
  rabbitmq: 'bg-lime-100 text-lime-700',
  jvm: 'bg-teal-100 text-teal-700',
  nginx: 'bg-emerald-100 text-emerald-700',
  kubernetes: 'bg-cyan-100 text-cyan-700',
};

const PackBadge = ({ id, allPacks }) => {
  const pack = allPacks.find(p => p.id === id);
  if (!pack) return null;
  return (
    <span className={`inline-block px-2 py-0.5 text-xs font-medium rounded ${PACK_COLORS[id] || 'bg-gray-100 text-gray-700'}`}>
      {pack.label}
    </span>
  );
};

export default function TemplateGallery() {
  const [selected, setSelected] = useState(null);
  const [copiedId, setCopiedId] = useState(null);
  const [filter, setFilter] = useState('');
  const [packFilter, setPackFilter] = useState(null);
  const [viewMode, setViewMode] = useState('scenarios'); // 'scenarios' | 'quickstart' | 'all'
  const [templateData, setTemplateData] = useState(null);
  const [loadError, setLoadError] = useState(null);

  /* ── Load template data from external JSON ── */
  useEffect(() => {
    fetch('../../assets/template-data.json')
      .then(r => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json(); })
      .then(data => setTemplateData(data))
      .catch(err => {
        console.error('[TemplateGallery] Failed to load template-data.json:', err);
        setLoadError(err.message);
      });
  }, []);

  /* Derive ALL_PACKS and TEMPLATES from loaded data */
  const ALL_PACKS = useMemo(() => {
    return (templateData && templateData.packs) || FALLBACK_PACKS;
  }, [templateData]);

  const TEMPLATES = useMemo(() => {
    if (!templateData || !templateData.templates) return [];
    return templateData.templates.map(hydrateTemplate);
  }, [templateData]);

  const filtered = useMemo(() => {
    return TEMPLATES.filter(tpl => {
      // View mode filter — use category field from JSON
      if (viewMode === 'scenarios' && tpl.category !== 'scenario') return false;
      if (viewMode === 'quickstart' && tpl.category !== 'quickstart') return false;

      // Pack filter
      if (packFilter && !tpl.packs.includes(packFilter)) return false;

      // Text search
      if (!filter) return true;
      const q = filter.toLowerCase();
      return tpl.name().toLowerCase().includes(q) ||
             tpl.desc().toLowerCase().includes(q) ||
             tpl.packs.some(p => p.toLowerCase().includes(q));
    });
  }, [filter, packFilter, viewMode, TEMPLATES]);

  const copyYaml = (tpl) => {
    navigator.clipboard.writeText(tpl.yaml);
    setCopiedId(tpl.id);
    setTimeout(() => setCopiedId(null), 2000);
  };

  // Coverage stats
  const coveredPacks = useMemo(() => {
    const set = new Set();
    TEMPLATES.forEach(tpl => tpl.packs.forEach(p => set.add(p)));
    return set;
  }, [TEMPLATES]);

  /* ── Loading state ── */
  if (!templateData && !loadError) {
    return (
      <div className="min-h-screen bg-gradient-to-br from-slate-50 to-slate-100 p-8 flex items-center justify-center">
        <div className="text-center">
          <div className="inline-block w-8 h-8 border-4 border-blue-200 border-t-blue-600 rounded-full animate-spin mb-4" />
          <p className="text-slate-500 text-sm">{t('載入模板資料...', 'Loading template data...')}</p>
        </div>
      </div>
    );
  }

  /* ── Error state ── */
  if (loadError && TEMPLATES.length === 0) {
    return (
      <div className="min-h-screen bg-gradient-to-br from-slate-50 to-slate-100 p-8 flex items-center justify-center">
        <div className="text-center bg-red-50 border border-red-200 rounded-xl p-6 max-w-md">
          <p className="text-red-700 font-medium mb-2">{t('載入失敗', 'Failed to Load')}</p>
          <p className="text-red-600 text-sm">{t('無法載入 template-data.json', 'Could not load template-data.json')}: {loadError}</p>
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-gradient-to-br from-slate-50 to-slate-100 p-8">
      <div className="max-w-5xl mx-auto">
        <div className="mb-6">
          <h1 className="text-3xl font-bold text-slate-900 mb-2">{t('配置模板庫', 'Config Template Gallery')}</h1>
          <p className="text-slate-600">
            {t(`${TEMPLATES.length} 個模板覆蓋全部 ${coveredPacks.size} 個 Rule Pack — 場景模板多 Pack 組合，快速入門模板單 Pack 開箱即用`,
               `${TEMPLATES.length} templates covering all ${coveredPacks.size} Rule Packs — scenario templates combine multiple packs, quick-start templates for single-pack setup`)}
          </p>
        </div>

        {/* View mode toggle */}
        <div className="flex gap-1 bg-white p-1 rounded-lg border mb-4">
          {[
            { id: 'all', label: () => t('全部', 'All') },
            { id: 'scenarios', label: () => t('場景模板', 'Scenarios') },
            { id: 'quickstart', label: () => t('快速入門', 'Quick Start') },
          ].map(mode => (
            <button
              key={mode.id}
              onClick={() => setViewMode(mode.id)}
              className={`flex-1 px-3 py-1.5 rounded-md text-sm font-medium transition-colors ${
                viewMode === mode.id ? 'bg-blue-600 text-white' : 'text-slate-600 hover:bg-slate-100'
              }`}
            >
              {mode.label()}
            </button>
          ))}
        </div>

        {/* Pack filter chips */}
        <div className="flex flex-wrap gap-1.5 mb-4">
          <button
            onClick={() => setPackFilter(null)}
            className={`text-xs px-2.5 py-1 rounded-full border transition-colors ${
              !packFilter ? 'bg-blue-100 text-blue-800 border-blue-300' : 'bg-white text-gray-600 border-gray-200 hover:bg-gray-50'
            }`}
          >
            {t('全部 Pack', 'All Packs')}
          </button>
          {ALL_PACKS.map(p => (
            <button
              key={p.id}
              onClick={() => setPackFilter(packFilter === p.id ? null : p.id)}
              className={`text-xs px-2.5 py-1 rounded-full border transition-colors ${
                packFilter === p.id
                  ? (PACK_COLORS[p.id] || 'bg-blue-100 text-blue-800') + ' border-current'
                  : 'bg-white text-gray-600 border-gray-200 hover:bg-gray-50'
              }`}
            >
              {p.label}
            </button>
          ))}
        </div>

        <input
          type="text"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          placeholder={t('搜尋模板或技術棧...', 'Search templates or stack...')}
          className="w-full px-4 py-3 rounded-xl border border-slate-200 text-sm focus:outline-none focus:ring-2 focus:ring-blue-400 mb-6 bg-white"
        />

        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          {filtered.map(tpl => (
            <div
              key={tpl.id}
              className={`bg-white rounded-xl border transition-all hover:shadow-md cursor-pointer ${
                selected === tpl.id ? 'border-blue-500 shadow-md' : 'border-slate-200'
              }`}
              onClick={() => setSelected(selected === tpl.id ? null : tpl.id)}
            >
              <div className="p-5">
                <div className="flex items-start gap-3 mb-3">
                  <span className="text-2xl">{tpl.icon}</span>
                  <div className="flex-1">
                    <h3 className="font-semibold text-slate-900">{tpl.name()}</h3>
                    <p className="text-xs text-slate-500 mt-1">{tpl.desc()}</p>
                  </div>
                </div>
                <div className="flex flex-wrap gap-1.5">
                  {tpl.packs.map(p => <PackBadge key={p} id={p} allPacks={ALL_PACKS} />)}
                </div>
              </div>
              {selected === tpl.id && (
                <div className="border-t border-slate-100 p-5">
                  <pre className="bg-slate-900 text-slate-100 p-4 rounded-lg text-xs overflow-x-auto font-mono max-h-56 overflow-y-auto mb-3">
                    {tpl.yaml}
                  </pre>
                  <div className="flex gap-2">
                    <button
                      onClick={(e) => { e.stopPropagation(); copyYaml(tpl); }}
                      className={`flex-1 px-3 py-2 rounded-lg text-xs font-medium transition-colors ${
                        copiedId === tpl.id
                          ? 'bg-green-600 text-white'
                          : 'bg-slate-200 text-slate-700 hover:bg-slate-300'
                      }`}
                    >
                      {copiedId === tpl.id ? t('✓ 已複製', '✓ Copied') : t('複製 YAML', 'Copy YAML')}
                    </button>
                  </div>
                </div>
              )}
            </div>
          ))}
        </div>

        {filtered.length === 0 && (
          <div className="text-center text-slate-400 py-12">
            {t('沒有符合的模板', 'No templates match your search.')}
          </div>
        )}

        {/* Coverage summary */}
        <div className="mt-8 p-4 bg-white rounded-xl border">
          <h4 className="text-sm font-medium text-slate-700 mb-2">
            {t('Rule Pack 覆蓋率', 'Rule Pack Coverage')}
          </h4>
          <div className="flex flex-wrap gap-1.5">
            {ALL_PACKS.map(p => (
              <span
                key={p.id}
                className={`text-xs px-2 py-0.5 rounded ${
                  coveredPacks.has(p.id) ? PACK_COLORS[p.id] || 'bg-green-100 text-green-700' : 'bg-gray-100 text-gray-400'
                }`}
              >
                {coveredPacks.has(p.id) ? '✓' : '✗'} {p.label}
              </span>
            ))}
          </div>
          <div className="mt-2 text-xs text-slate-500">
            {t(`${coveredPacks.size} / ${ALL_PACKS.length} selectable Rule Packs 有模板（operational 和 platform 自動啟用，無需 tenant 配置）`,
               `${coveredPacks.size} / ${ALL_PACKS.length} selectable Rule Packs have templates (operational and platform are auto-enabled, no tenant config needed)`)}
          </div>
        </div>
      </div>
    </div>
  );
}
