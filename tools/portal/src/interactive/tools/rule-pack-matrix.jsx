---
title: "Rule Pack Comparison Matrix"
tags: [matrix, comparison, rule packs]
audience: ["platform-engineer", "domain-expert"]
version: v2.9.0
lang: en
related: [rule-pack-detail, dependency-graph, rule-pack-selector]
---

import React, { useState, useMemo } from 'react';
import { RULE_PACK_DATA, PACK_ORDER } from './_common/data/rule-packs.js';

const t = window.__t || ((zh, en) => en);

// Category display name mapping
const CAT_DISPLAY = { database: 'Database', messaging: 'Messaging', runtime: 'Runtime', webserver: 'Web Server', infrastructure: 'Infra' };

// Rule Pack rows from the canonical accessor (canonicalize PR-3). Online this
// resolves to window.__PLATFORM_DATA.rulePacks; offline to the baked-in mirror
// in _common/data/rule-packs.js. Either way the counts come from ONE catalog
// that rule-packs-fallback-drift.test.ts pins to platform-data.json — this file
// used to bake its own table, which had drifted to a v2.7.0 snapshot.
const PACKS = PACK_ORDER
  .filter(key => RULE_PACK_DATA[key])
  .map(key => {
    const p = RULE_PACK_DATA[key];
    return {
      key,
      label: p.label,
      cat: CAT_DISPLAY[p.category] || p.category,
      rec: p.recordingRules,
      alert: p.alertRules,
      exporter: p.exporter,
      metrics: p.metrics || [],
      ...(p.required && { required: true }),
    };
  });

const CATEGORIES = [...new Set(PACKS.map(p => p.cat))];
const ALL_METRIC_TYPES = [...new Set(PACKS.flatMap(p => p.metrics))].sort();

export default function RulePackMatrix() {
  const [filterCat, setFilterCat] = useState('all');
  const [sortBy, setSortBy] = useState('alert');
  const [search, setSearch] = useState('');

  const filtered = useMemo(() => {
    let result = PACKS;
    if (filterCat !== 'all') result = result.filter(p => p.cat === filterCat);
    if (search) {
      const q = search.toLowerCase();
      result = result.filter(p => p.label.toLowerCase().includes(q) || p.exporter.toLowerCase().includes(q) || p.metrics.some(m => m.includes(q)));
    }
    if (sortBy === 'alert') result = [...result].sort((a, b) => b.alert - a.alert);
    else if (sortBy === 'rec') result = [...result].sort((a, b) => b.rec - a.rec);
    else if (sortBy === 'name') result = [...result].sort((a, b) => a.label.localeCompare(b.label));
    return result;
  }, [filterCat, sortBy, search]);

  const totalRec = filtered.reduce((s, p) => s + p.rec, 0);
  const totalAlert = filtered.reduce((s, p) => s + p.alert, 0);

  return (
    <div className="min-h-screen bg-gradient-to-br from-slate-50 to-slate-100 p-8">
      <div className="max-w-6xl mx-auto">
        <div className="mb-6">
          <h1 className="text-3xl font-bold text-slate-900 mb-2">{t('Rule Pack 比較矩陣', 'Rule Pack Comparison Matrix')}</h1>
          <p className="text-slate-600">{t(`所有 ${PACKS.length} 個 Rule Pack 並排比較`, `All ${PACKS.length} Rule Packs compared side by side`)}</p>
        </div>

        <div className="flex flex-wrap gap-3 mb-6">
          <input
            type="text" value={search} onChange={(e) => setSearch(e.target.value)}
            placeholder={t('搜尋...', 'Search...')}
            className="px-3 py-2 rounded-lg border border-slate-200 text-sm bg-white focus:outline-none focus:ring-2 focus:ring-blue-400"
          />
          <select value={filterCat} onChange={(e) => setFilterCat(e.target.value)}
            aria-label={t('依分類篩選', 'Filter by category')}
            className="px-3 py-2 rounded-lg border border-slate-200 text-sm bg-white">
            <option value="all">{t('所有分類', 'All')}</option>
            {CATEGORIES.map(c => <option key={c} value={c}>{c}</option>)}
          </select>
          <select value={sortBy} onChange={(e) => setSortBy(e.target.value)}
            aria-label={t('排序方式', 'Sort by')}
            className="px-3 py-2 rounded-lg border border-slate-200 text-sm bg-white">
            <option value="alert">{t('按告警數排序', 'Sort by Alerts')}</option>
            <option value="rec">{t('按記錄數排序', 'Sort by Recording')}</option>
            <option value="name">{t('按名稱排序', 'Sort by Name')}</option>
          </select>
        </div>

        {/* Summary */}
        <div className="flex gap-4 mb-6">
          <div className="bg-white rounded-lg border border-slate-200 px-4 py-2">
            <span className="text-xs text-slate-500">{t('顯示', 'Showing')}: </span>
            <span className="font-bold text-blue-600">{filtered.length}</span>
            <span className="text-xs text-slate-500"> packs</span>
          </div>
          <div className="bg-white rounded-lg border border-slate-200 px-4 py-2">
            <span className="text-xs text-slate-500">{t('記錄規則', 'Recording')}: </span>
            <span className="font-bold text-green-600">{totalRec}</span>
          </div>
          <div className="bg-white rounded-lg border border-slate-200 px-4 py-2">
            <span className="text-xs text-slate-500">{t('告警規則', 'Alerts')}: </span>
            <span className="font-bold text-red-600">{totalAlert}</span>
          </div>
        </div>

        {/* Matrix Table */}
        <div className="bg-white rounded-xl shadow-sm border border-slate-200 overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="bg-slate-50 border-b border-slate-200">
                <th className="text-left px-4 py-3 font-semibold text-slate-700">{t('名稱', 'Name')}</th>
                <th className="text-left px-4 py-3 font-semibold text-slate-700">{t('分類', 'Category')}</th>
                <th className="text-center px-4 py-3 font-semibold text-slate-700">{t('記錄', 'Rec.')}</th>
                <th className="text-center px-4 py-3 font-semibold text-slate-700">{t('告警', 'Alert')}</th>
                <th className="text-left px-4 py-3 font-semibold text-slate-700">Exporter</th>
                <th className="text-left px-4 py-3 font-semibold text-slate-700">{t('涵蓋指標', 'Metrics Covered')}</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map(p => (
                <tr key={p.key} className={`border-b border-slate-100 hover:bg-slate-50 ${p.required ? 'bg-blue-50' : ''}`}>
                  <td className="px-4 py-3">
                    <div className="flex items-center gap-2">
                      <span className="font-semibold text-slate-900">{p.label}</span>
                      {p.required && <span className="text-xs bg-blue-200 text-blue-800 px-1.5 py-0.5 rounded font-medium">required</span>}
                    </div>
                  </td>
                  <td className="px-4 py-3 text-xs text-slate-500">{p.cat}</td>
                  <td className="px-4 py-3 text-center font-mono font-bold text-green-600">{p.rec}</td>
                  <td className="px-4 py-3 text-center font-mono font-bold text-red-600">{p.alert}</td>
                  <td className="px-4 py-3"><code className="text-xs bg-slate-100 px-2 py-0.5 rounded">{p.exporter}</code></td>
                  <td className="px-4 py-3">
                    <div className="flex flex-wrap gap-1">
                      {p.metrics.map(m => (
                        <span key={m} className="text-xs bg-slate-100 text-slate-600 px-1.5 py-0.5 rounded">{m}</span>
                      ))}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
