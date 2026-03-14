---
title: "Config Version Diff"
tags: [diff, interactive, tools]
audience: [platform-engineer, tenant]
version: v2.0.0-preview.2
lang: en
related: [config-lint, playground, migration-simulator]
---

import React, { useState, useMemo } from 'react';

const t = window.__t || ((zh, en) => en);

const EXAMPLE_OLD = `tenants:
  db-a:
    mysql_connections: "80"
    mysql_cpu: "75"
    _routing:
      receiver:
        type: "slack"
        channel: "#alerts"
      group_wait: "30s"
  db-b:
    pg_connections: "100"
    pg_cache_hit_ratio: "90"`;

const EXAMPLE_NEW = `tenants:
  db-a:
    mysql_connections: "120"
    mysql_connections_critical: "200"
    mysql_cpu: "75"
    _routing:
      receiver:
        type: "webhook"
        url: "https://hooks.example.com/alerts"
      group_wait: "30s"
  db-b:
    pg_connections: "150"
    pg_cache_hit_ratio: "85"
  cache:
    redis_memory: "80"
    redis_memory_critical: "95"`;

// Simple key-value extractor from YAML (tenant-level only)
function extractTenants(yaml) {
  const tenants = {};
  let currentTenant = null;
  let inSpecial = false;
  const lines = yaml.split('\n');
  for (const line of lines) {
    const indent = line.search(/\S/);
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith('#')) continue;
    if (indent === 0 && trimmed === 'tenants:') continue;
    if (indent === 2 && trimmed.endsWith(':') && !trimmed.includes(': ')) {
      currentTenant = trimmed.slice(0, -1);
      tenants[currentTenant] = {};
      inSpecial = false;
      continue;
    }
    if (indent === 4 && currentTenant) {
      if (trimmed.startsWith('_')) {
        inSpecial = true;
        const kv = trimmed.split(':');
        tenants[currentTenant][kv[0]] = trimmed;
      } else if (trimmed.includes(':')) {
        inSpecial = false;
        const idx = trimmed.indexOf(':');
        const key = trimmed.slice(0, idx).trim();
        const val = trimmed.slice(idx + 1).trim();
        tenants[currentTenant][key] = val;
      }
    } else if (indent >= 6 && currentTenant && inSpecial) {
      // Collect sub-keys for special keys display
      const lastSpecial = Object.keys(tenants[currentTenant]).filter(k => k.startsWith('_')).pop();
      if (lastSpecial) {
        tenants[currentTenant][lastSpecial] += '\n' + line;
      }
    }
  }
  return tenants;
}

function computeDiff(oldYaml, newYaml) {
  const oldTenants = extractTenants(oldYaml);
  const newTenants = extractTenants(newYaml);
  const allTenants = new Set([...Object.keys(oldTenants), ...Object.keys(newTenants)]);
  const changes = [];

  allTenants.forEach(tenant => {
    const oldT = oldTenants[tenant];
    const newT = newTenants[tenant];
    if (!oldT && newT) {
      changes.push({ type: 'tenant-added', tenant, keys: Object.keys(newT) });
      return;
    }
    if (oldT && !newT) {
      changes.push({ type: 'tenant-removed', tenant, keys: Object.keys(oldT) });
      return;
    }
    const allKeys = new Set([...Object.keys(oldT), ...Object.keys(newT)]);
    allKeys.forEach(key => {
      const oldVal = oldT[key];
      const newVal = newT[key];
      if (oldVal === undefined && newVal !== undefined) {
        changes.push({ type: 'key-added', tenant, key, newVal });
      } else if (oldVal !== undefined && newVal === undefined) {
        changes.push({ type: 'key-removed', tenant, key, oldVal });
      } else if (oldVal !== newVal) {
        changes.push({ type: 'key-changed', tenant, key, oldVal, newVal });
      }
    });
  });

  return changes;
}

const DiffBadge = ({ type }) => {
  const styles = {
    'tenant-added': 'bg-green-100 text-green-800',
    'tenant-removed': 'bg-red-100 text-red-800',
    'key-added': 'bg-green-100 text-green-700',
    'key-removed': 'bg-red-100 text-red-700',
    'key-changed': 'bg-amber-100 text-amber-700',
  };
  const labels = {
    'tenant-added': 'NEW TENANT',
    'tenant-removed': 'REMOVED TENANT',
    'key-added': 'ADDED',
    'key-removed': 'REMOVED',
    'key-changed': 'CHANGED',
  };
  return <span className={`text-xs font-semibold px-2 py-0.5 rounded ${styles[type]}`}>{labels[type]}</span>;
};

export default function ConfigDiff() {
  const [oldYaml, setOldYaml] = useState(EXAMPLE_OLD);
  const [newYaml, setNewYaml] = useState(EXAMPLE_NEW);

  const changes = useMemo(() => computeDiff(oldYaml, newYaml), [oldYaml, newYaml]);

  const tenantAdded = changes.filter(c => c.type === 'tenant-added').length;
  const tenantRemoved = changes.filter(c => c.type === 'tenant-removed').length;
  const keysChanged = changes.filter(c => c.type === 'key-changed').length;
  const keysAdded = changes.filter(c => c.type === 'key-added').length;
  const keysRemoved = changes.filter(c => c.type === 'key-removed').length;

  return (
    <div className="min-h-screen bg-gradient-to-br from-slate-50 to-slate-100 p-8">
      <div className="max-w-6xl mx-auto">
        <div className="mb-8">
          <h1 className="text-3xl font-bold text-slate-900 mb-2">{t('配置版本比較', 'Config Version Diff')}</h1>
          <p className="text-slate-600">{t('比較兩個版本的 tenant 配置，查看變更摘要和影響範圍', 'Compare two config versions to see what changed and its blast radius')}</p>
        </div>

        {/* Side by side editors */}
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 mb-6">
          <div className="bg-white rounded-xl shadow-sm border border-slate-200 p-4">
            <h3 className="text-sm font-semibold text-slate-700 mb-2">{t('舊版本', 'Old Version')}</h3>
            <textarea
              value={oldYaml}
              onChange={(e) => setOldYaml(e.target.value)}
              rows={14}
              className="w-full font-mono text-xs bg-slate-900 text-slate-100 p-3 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500 resize-none"
              spellCheck="false"
            />
          </div>
          <div className="bg-white rounded-xl shadow-sm border border-slate-200 p-4">
            <h3 className="text-sm font-semibold text-slate-700 mb-2">{t('新版本', 'New Version')}</h3>
            <textarea
              value={newYaml}
              onChange={(e) => setNewYaml(e.target.value)}
              rows={14}
              className="w-full font-mono text-xs bg-slate-900 text-slate-100 p-3 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500 resize-none"
              spellCheck="false"
            />
          </div>
        </div>

        {/* Summary */}
        <div className="bg-white rounded-xl shadow-sm border border-slate-200 p-6 mb-6">
          <h3 className="text-sm font-semibold text-slate-900 mb-3">{t('變更摘要', 'Change Summary')}</h3>
          <div className="flex flex-wrap gap-4 text-center">
            <div className="bg-green-50 border border-green-200 rounded-lg px-4 py-2">
              <div className="text-lg font-bold text-green-600">{tenantAdded}</div>
              <div className="text-xs text-green-700">{t('新增租戶', 'Tenants Added')}</div>
            </div>
            <div className="bg-red-50 border border-red-200 rounded-lg px-4 py-2">
              <div className="text-lg font-bold text-red-600">{tenantRemoved}</div>
              <div className="text-xs text-red-700">{t('移除租戶', 'Tenants Removed')}</div>
            </div>
            <div className="bg-amber-50 border border-amber-200 rounded-lg px-4 py-2">
              <div className="text-lg font-bold text-amber-600">{keysChanged}</div>
              <div className="text-xs text-amber-700">{t('閾值變更', 'Values Changed')}</div>
            </div>
            <div className="bg-blue-50 border border-blue-200 rounded-lg px-4 py-2">
              <div className="text-lg font-bold text-blue-600">{keysAdded}</div>
              <div className="text-xs text-blue-700">{t('新增 Key', 'Keys Added')}</div>
            </div>
            <div className="bg-slate-50 border border-slate-200 rounded-lg px-4 py-2">
              <div className="text-lg font-bold text-slate-600">{keysRemoved}</div>
              <div className="text-xs text-slate-700">{t('移除 Key', 'Keys Removed')}</div>
            </div>
          </div>
        </div>

        {/* Detailed Changes */}
        {changes.length > 0 ? (
          <div className="bg-white rounded-xl shadow-sm border border-slate-200 p-6">
            <h3 className="text-sm font-semibold text-slate-900 mb-3">{t('詳細變更', 'Detailed Changes')}</h3>
            <div className="space-y-2">
              {changes.map((c, i) => (
                <div key={i} className="flex items-start gap-3 p-3 rounded-lg bg-slate-50 border border-slate-100">
                  <DiffBadge type={c.type} />
                  <div className="flex-1 text-sm">
                    <span className="font-mono font-semibold text-slate-900">{c.tenant}</span>
                    {c.key && <span className="font-mono text-slate-500">.{c.key}</span>}
                    {c.type === 'key-changed' && (
                      <div className="mt-1 text-xs">
                        <span className="text-red-600 line-through">{c.oldVal}</span>
                        <span className="mx-2 text-slate-400">→</span>
                        <span className="text-green-600 font-medium">{c.newVal}</span>
                      </div>
                    )}
                    {c.type === 'key-added' && <span className="ml-2 text-xs text-green-600">= {c.newVal}</span>}
                    {c.type === 'key-removed' && <span className="ml-2 text-xs text-red-600 line-through">= {c.oldVal}</span>}
                    {c.type === 'tenant-added' && <span className="ml-2 text-xs text-green-600">({c.keys.length} keys)</span>}
                    {c.type === 'tenant-removed' && <span className="ml-2 text-xs text-red-600">({c.keys.length} keys)</span>}
                  </div>
                </div>
              ))}
            </div>
          </div>
        ) : (
          <div className="bg-white rounded-xl shadow-sm border border-slate-200 p-6 text-center">
            <div className="text-3xl mb-3">✓</div>
            <p className="text-sm text-slate-500">{t('兩個版本完全相同', 'Both versions are identical')}</p>
          </div>
        )}
      </div>
    </div>
  );
}
