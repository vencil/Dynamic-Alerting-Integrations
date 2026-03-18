---
title: "Config Lint Report"
tags: [lint, validation, best practices]
audience: ["platform-engineer", tenant]
version: v2.2.0
lang: en
related: [config-diff, playground, schema-explorer]
---

import React, { useState, useMemo } from 'react';

const t = window.__t || ((zh, en) => en);

const SAMPLE_YAML = `_defaults:
  mariadb_connections_warning: 150
  mariadb_connections_warning_critical: 200
  redis_memory_usage_warning: 75

db-a:
  mariadb_connections_warning: 300
  mariadb_cpu_usage_warning: 95
  mariadb_cpu_usage_warning_critical: 98
  redis_memory_usage_warning: disable
  _routing:
    receiver_type: webhook
    webhook_url: https://hooks.example.com/alerts
    group_wait: 1s
    repeat_interval: 100h

db-b:
  mariadb_connections_warning: 50
  mariadb_replication_lag_warning: 30
  kafka_consumer_lag_warning: 10000
`;

/* ── Lint rules ── */
const LINT_RULES = [
  {
    id: 'threshold-too-high',
    severity: 'warning',
    category: t('閾值', 'Threshold'),
    check: (tenants) => {
      const findings = [];
      for (const [tenant, keys] of Object.entries(tenants)) {
        for (const [key, val] of Object.entries(keys)) {
          if (typeof val === 'number' && key.includes('_percent') || key.includes('_usage') || key.includes('cpu')) {
            if (typeof val === 'number' && val > 95) {
              findings.push({ tenant, key, message: t(`${key} = ${val} 非常高（>95），可能來不及反應`, `${key} = ${val} is very high (>95) — may not leave reaction time`) });
            }
          }
        }
      }
      return findings;
    },
  },
  {
    id: 'threshold-too-low',
    severity: 'info',
    category: t('閾值', 'Threshold'),
    check: (tenants) => {
      const findings = [];
      for (const [tenant, keys] of Object.entries(tenants)) {
        for (const [key, val] of Object.entries(keys)) {
          if (typeof val === 'number' && key.includes('_warning') && !key.includes('_critical') && val < 10) {
            findings.push({ tenant, key, message: t(`${key} = ${val} 非常低（<10），可能產生過多噪音`, `${key} = ${val} is very low (<10) — may generate excessive noise`) });
          }
        }
      }
      return findings;
    },
  },
  {
    id: 'missing-critical-pair',
    severity: 'warning',
    category: t('嚴重度', 'Severity'),
    check: (tenants) => {
      const findings = [];
      for (const [tenant, keys] of Object.entries(tenants)) {
        for (const key of Object.keys(keys)) {
          if (key.endsWith('_warning') && !key.endsWith('_warning_critical')) {
            const critKey = key + '_critical';
            if (!(critKey in keys)) {
              findings.push({ tenant, key, message: t(`${key} 有設定但缺少對應的 ${critKey}`, `${key} is set but missing paired ${critKey}`) });
            }
          }
        }
      }
      return findings;
    },
  },
  {
    id: 'critical-lower-than-warning',
    severity: 'error',
    category: t('嚴重度', 'Severity'),
    check: (tenants) => {
      const findings = [];
      for (const [tenant, keys] of Object.entries(tenants)) {
        for (const [key, val] of Object.entries(keys)) {
          if (key.endsWith('_warning_critical') && typeof val === 'number') {
            const warnKey = key.replace('_critical', '');
            const warnVal = keys[warnKey];
            if (typeof warnVal === 'number' && val <= warnVal) {
              findings.push({ tenant, key, message: t(`Critical (${val}) ≤ Warning (${warnVal}) — critical 應高於 warning`, `Critical (${val}) ≤ Warning (${warnVal}) — critical should be higher than warning`) });
            }
          }
        }
      }
      return findings;
    },
  },
  {
    id: 'no-routing',
    severity: 'warning',
    category: t('路由', 'Routing'),
    check: (tenants) => {
      const findings = [];
      for (const [tenant, keys] of Object.entries(tenants)) {
        if (tenant.startsWith('_')) continue;
        if (!keys['_routing'] && !keys['_routing.receiver_type']) {
          // Check if any non-_ key exists (actual threshold)
          const hasThresholds = Object.keys(keys).some(k => !k.startsWith('_'));
          if (hasThresholds) {
            findings.push({ tenant, key: '_routing', message: t(`Tenant ${tenant} 有設定閾值但沒有配置路由`, `Tenant ${tenant} has thresholds but no routing configured`) });
          }
        }
      }
      return findings;
    },
  },
  {
    id: 'group-wait-too-low',
    severity: 'warning',
    category: t('路由', 'Routing'),
    check: (tenants) => {
      const findings = [];
      for (const [tenant, keys] of Object.entries(tenants)) {
        const routing = keys['_routing'];
        if (routing && typeof routing === 'object') {
          const gw = routing.group_wait;
          if (gw && (gw === '1s' || gw === '2s' || gw === '3s' || gw === '4s')) {
            findings.push({ tenant, key: '_routing.group_wait', message: t(`group_wait = ${gw} 太短，建議 ≥ 5s 避免告警碎片化`, `group_wait = ${gw} is too short — recommend ≥ 5s to avoid alert fragmentation`) });
          }
        }
      }
      return findings;
    },
  },
  {
    id: 'repeat-interval-too-long',
    severity: 'info',
    category: t('路由', 'Routing'),
    check: (tenants) => {
      const findings = [];
      for (const [tenant, keys] of Object.entries(tenants)) {
        const routing = keys['_routing'];
        if (routing && typeof routing === 'object') {
          const ri = routing.repeat_interval;
          if (ri && ri.endsWith('h')) {
            const hours = parseInt(ri);
            if (hours > 72) {
              findings.push({ tenant, key: '_routing.repeat_interval', message: t(`repeat_interval = ${ri} 超過上限 72h`, `repeat_interval = ${ri} exceeds maximum of 72h`) });
            } else if (hours > 24) {
              findings.push({ tenant, key: '_routing.repeat_interval', message: t(`repeat_interval = ${ri} 很長，可能錯過持續性問題`, `repeat_interval = ${ri} is quite long — may miss persistent issues`) });
            }
          }
        }
      }
      return findings;
    },
  },
  {
    id: 'disabled-all-in-pack',
    severity: 'info',
    category: t('配置', 'Config'),
    check: (tenants) => {
      const findings = [];
      for (const [tenant, keys] of Object.entries(tenants)) {
        if (tenant.startsWith('_')) continue;
        const disabled = Object.entries(keys).filter(([k, v]) => v === 'disable');
        if (disabled.length > 3) {
          findings.push({ tenant, key: '-', message: t(`${disabled.length} 個 key 被 disable，考慮是否需要該 Rule Pack`, `${disabled.length} keys disabled — consider whether this Rule Pack is needed`) });
        }
      }
      return findings;
    },
  },
  {
    id: 'routing-profile-undefined',
    severity: 'error',
    category: t('路由設定檔', 'Routing Profiles'),
    check: (tenants) => {
      const findings = [];
      const profiles = tenants['routing_profiles'] || {};
      for (const [tenant, keys] of Object.entries(tenants)) {
        if (tenant.startsWith('_') || tenant === 'routing_profiles') continue;
        const routing = keys['_routing'];
        if (routing && typeof routing === 'object' && routing.profile) {
          if (!profiles[routing.profile]) {
            findings.push({ tenant, key: '_routing.profile', message: t(`引用的 profile "${routing.profile}" 未在 routing_profiles 中定義`, `Referenced profile "${routing.profile}" is not defined in routing_profiles`) });
          }
        }
      }
      return findings;
    },
  },
  {
    id: 'domain-policy-violation',
    severity: 'error',
    category: t('域名策略', 'Domain Policy'),
    check: (tenants) => {
      const findings = [];
      const policy = tenants['_domain_policy'];
      if (!policy) return findings;
      const denied = policy.denied_domains || [];
      for (const [tenant, keys] of Object.entries(tenants)) {
        if (tenant.startsWith('_') || tenant === 'routing_profiles') continue;
        const routing = keys['_routing'];
        if (routing && typeof routing === 'object' && routing.webhook_url) {
          for (const d of denied) {
            const pattern = d.replace('*.', '');
            if (routing.webhook_url.includes(pattern)) {
              findings.push({ tenant, key: '_routing.webhook_url', message: t(`Webhook URL 匹配禁止域名 "${d}"`, `Webhook URL matches denied domain "${d}"`) });
            }
          }
        }
      }
      return findings;
    },
  },
  {
    id: 'instance-mapping-missing-partition',
    severity: 'warning',
    category: t('實例映射', 'Instance Mapping'),
    check: (tenants) => {
      const findings = [];
      const mappings = tenants['_instance_mapping'];
      if (!Array.isArray(mappings)) return findings;
      for (const m of mappings) {
        if (!m.partition_label) {
          findings.push({ tenant: '_instance_mapping', key: m.instance || '?', message: t(`映射缺少 partition_label`, `Mapping missing partition_label`) });
        }
        if (!m.partitions || m.partitions.length === 0) {
          findings.push({ tenant: '_instance_mapping', key: m.instance || '?', message: t(`映射缺少 partitions 列表`, `Mapping missing partitions list`) });
        }
      }
      return findings;
    },
  },
];

const SEVERITY_COLORS = {
  error: { bg: 'bg-red-50', border: 'border-red-200', text: 'text-red-700', badge: 'bg-red-100 text-red-800' },
  warning: { bg: 'bg-amber-50', border: 'border-amber-200', text: 'text-amber-700', badge: 'bg-amber-100 text-amber-800' },
  info: { bg: 'bg-blue-50', border: 'border-blue-200', text: 'text-blue-700', badge: 'bg-blue-100 text-blue-800' },
};

/* ── Simple YAML parser ── */
function parseYaml(text) {
  const tenants = {};
  let currentTenant = null;
  let currentSubKey = null;
  let subObj = null;

  for (const line of text.split('\n')) {
    if (!line.trim() || line.trim().startsWith('#')) continue;
    const indent = line.search(/\S/);

    if (indent === 0 && line.includes(':')) {
      const key = line.split(':')[0].trim();
      currentTenant = key;
      tenants[currentTenant] = {};
      currentSubKey = null;
      subObj = null;
      continue;
    }

    if (currentTenant && indent >= 2) {
      const trimmed = line.trim();
      if (trimmed.includes(':')) {
        const colonIdx = trimmed.indexOf(':');
        const key = trimmed.substring(0, colonIdx).trim();
        const rawVal = trimmed.substring(colonIdx + 1).trim();

        if (indent === 2 && !rawVal) {
          // Nested object like _routing:
          currentSubKey = key;
          subObj = {};
          tenants[currentTenant][key] = subObj;
        } else if (indent > 2 && subObj && currentSubKey) {
          // Sub-key of nested object
          subObj[key] = parseValue(rawVal);
        } else {
          currentSubKey = null;
          subObj = null;
          tenants[currentTenant][key] = parseValue(rawVal);
        }
      }
    }
  }
  return tenants;
}

function parseValue(raw) {
  if (raw === 'true') return true;
  if (raw === 'false') return false;
  if (raw === 'disable') return 'disable';
  if (raw.startsWith('"') && raw.endsWith('"')) return raw.slice(1, -1);
  const num = Number(raw);
  if (!isNaN(num) && raw !== '') return num;
  return raw;
}

export default function ConfigLint() {
  const [yaml, setYaml] = useState(SAMPLE_YAML);

  const results = useMemo(() => {
    try {
      const tenants = parseYaml(yaml);
      const allFindings = [];
      for (const rule of LINT_RULES) {
        const findings = rule.check(tenants);
        for (const f of findings) {
          allFindings.push({ ...f, ruleId: rule.id, severity: rule.severity, category: rule.category });
        }
      }
      // Sort: error → warning → info
      const order = { error: 0, warning: 1, info: 2 };
      allFindings.sort((a, b) => order[a.severity] - order[b.severity]);
      return { ok: true, findings: allFindings, tenantCount: Object.keys(tenants).filter(k => !k.startsWith('_')).length };
    } catch (e) {
      return { ok: false, findings: [], error: e.message, tenantCount: 0 };
    }
  }, [yaml]);

  const counts = useMemo(() => {
    const c = { error: 0, warning: 0, info: 0 };
    results.findings.forEach(f => c[f.severity]++);
    return c;
  }, [results]);

  return (
    <div className="min-h-screen bg-gradient-to-br from-slate-50 to-slate-100 p-8">
      <div className="max-w-5xl mx-auto">
        <h1 className="text-3xl font-bold text-slate-900 mb-2">{t('配置 Lint 報告', 'Config Lint Report')}</h1>
        <p className="text-slate-600 mb-6">{t('貼入 YAML 取得最佳實踐建議、缺少配對、路由問題等', 'Paste YAML to get best-practice suggestions, missing pairs, routing issues, and more')}</p>

        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
          {/* Editor */}
          <div className="bg-white rounded-xl shadow-sm border border-slate-200 p-4">
            <h2 className="text-sm font-semibold text-slate-800 mb-3">{t('Tenant YAML', 'Tenant YAML')}</h2>
            <textarea
              value={yaml}
              onChange={(e) => setYaml(e.target.value)}
              rows={24}
              spellCheck={false}
              className="w-full font-mono text-xs border border-slate-200 rounded-lg p-3 focus:ring-2 focus:ring-blue-500 focus:border-blue-500 bg-slate-50 resize-none"
            />
          </div>

          {/* Report */}
          <div className="space-y-4">
            {/* Summary */}
            <div className="bg-white rounded-xl shadow-sm border border-slate-200 p-4">
              <h2 className="text-sm font-semibold text-slate-800 mb-3">{t('摘要', 'Summary')}</h2>
              <div className="flex gap-4">
                {[
                  { label: t('錯誤', 'Errors'), count: counts.error, color: 'text-red-600 bg-red-50' },
                  { label: t('警告', 'Warnings'), count: counts.warning, color: 'text-amber-600 bg-amber-50' },
                  { label: t('建議', 'Info'), count: counts.info, color: 'text-blue-600 bg-blue-50' },
                ].map((s, i) => (
                  <div key={i} className={`flex-1 p-3 rounded-lg ${s.color} text-center`}>
                    <div className="text-2xl font-bold">{s.count}</div>
                    <div className="text-xs mt-1">{s.label}</div>
                  </div>
                ))}
              </div>
              <div className="mt-3 text-xs text-slate-500">
                {t(`分析了 ${results.tenantCount} 個 tenant`, `Analyzed ${results.tenantCount} tenants`)}
                {results.findings.length === 0 && results.ok && (
                  <span className="ml-2 text-green-600 font-medium">✅ {t('一切看起來很好！', 'Everything looks good!')}</span>
                )}
              </div>
            </div>

            {/* Findings */}
            <div className="space-y-3 max-h-96 overflow-y-auto">
              {!results.ok && (
                <div className="p-4 bg-red-50 border border-red-200 rounded-xl text-sm text-red-700">
                  {t('YAML 解析錯誤', 'YAML parse error')}: {results.error}
                </div>
              )}
              {results.findings.map((f, i) => {
                const colors = SEVERITY_COLORS[f.severity];
                return (
                  <div key={i} className={`${colors.bg} ${colors.border} border rounded-xl p-4`}>
                    <div className="flex items-start gap-2">
                      <span className={`${colors.badge} text-xs font-bold px-2 py-0.5 rounded flex-shrink-0`}>
                        {f.severity.toUpperCase()}
                      </span>
                      <div className="flex-1">
                        <div className="flex items-center gap-2 mb-1">
                          <span className="text-xs text-slate-400">{f.category}</span>
                          <span className="text-xs text-slate-400">•</span>
                          <span className="text-xs font-mono text-slate-500">{f.tenant}</span>
                        </div>
                        <p className={`text-sm ${colors.text}`}>{f.message}</p>
                        {f.key !== '-' && (
                          <code className="text-xs text-slate-500 mt-1 block">{f.key}</code>
                        )}
                      </div>
                    </div>
                  </div>
                );
              })}
            </div>

            {/* Rules reference */}
            <div className="bg-white rounded-xl shadow-sm border border-slate-200 p-4">
              <h3 className="text-xs font-semibold text-slate-500 uppercase mb-2">{t('檢查項目', 'Lint Rules')}</h3>
              <div className="space-y-1 text-xs">
                {LINT_RULES.map(r => (
                  <div key={r.id} className="flex items-center gap-2">
                    <span className={`${SEVERITY_COLORS[r.severity].badge} px-1.5 py-0.5 rounded text-xs`}>{r.severity}</span>
                    <span className="text-slate-600">{r.id.replace(/-/g, ' ')}</span>
                  </div>
                ))}
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
