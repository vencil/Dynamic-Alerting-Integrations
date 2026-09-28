---
title: "YAML Schema Explorer"
version: v2.9.0
lang: en
related: [playground, glossary, config-lint]
---

import React, { useState, useMemo } from 'react';
import SILENT_SCHEMA from './_common/data/silent-mode-schema.json';
import { silentModeExpires } from './tenant-manager/utils/yaml-generators.js';

const t = window.__t || ((zh, en) => en);

// _silent_mode is rendered from a verbatim copy of the schema definition, not
// restated here (#1988): docs/schemas/tenant-config.schema.json
// #/definitions/silentMode (deep-equal drift guard in
// tests/silent-mode-schema.drift.test.ts). Types are read from the
// definition, and each sub-key shows the definition's own description; only
// the short labels are written here.
const SILENT = SILENT_SCHEMA.definition;
const SILENT_LABELS = {
  target: t('要靜默的 severity', 'Severity to silence'),
  expires: t('自動失效時間', 'Auto-expiry time'),
  reason: t('原因', 'Reason'),
};
const typeOf = (d) => (d.enum
  ? d.enum.map(v => JSON.stringify(v)).join(' | ')
  : (d.format ? `${d.type} (${d.format})` : d.type));
// Insertable example taken from the schema itself (examples[0], else
// default). Timestamps are skipped: a fixed date is either already past or
// silences for decades (#1988), so a date-time field offers no insert.
const schemaExample = (p) => {
  if (p.format === 'date-time') return undefined;
  const v = p.examples ? p.examples[0] : p.default;
  return v === undefined ? undefined : JSON.stringify(v);
};
function silentModeNode() {
  const object = SILENT.oneOf.find(b => b.type === 'object') || {};
  const required = object.required || [];
  return {
    key: '_silent_mode',
    type: SILENT.oneOf.map(typeOf).join(' | '),
    desc: t('靜默模式：告警照常產生，通知被抑制', 'Silent mode: alerts still fire, notifications are suppressed'),
    rulePack: 'all',
    children: Object.entries(object.properties || {}).map(([key, p]) => ({
      key,
      type: typeOf(p),
      desc: (SILENT_LABELS[key] || key) + (required.includes(key) ? t('（必填）', ' (required)') : '')
        + (p.description ? `: ${p.description}` : ''),
      rulePack: 'all',
      example: schemaExample(p),
    })),
  };
}

// Not enumerated here: the per-type contract lives in
// tenant-config.schema.json#/definitions/receiver, and a receiver breaking it
// has its whole route dropped (#2033). The examples inserted from here are
// pinned by tests/receiver-required.drift.test.ts.
const RECEIVER_RANGE = 'type decides the required fields (see tenant-config.schema.json)';

/* ── Tenant-file schema tree ──
 * Every key below is written under tenants.<id>: in a tenant file
 * (docs/schemas/tenant-config.schema.json#/definitions/tenantConfig).
 * Platform files (_defaults.yaml, _routing_profiles.yaml, _domain_policy.yaml,
 * _instance_mapping.yaml) have their own schemas and are not listed here
 * (#2033). tests/schema-explorer-schema.drift.test.tsx checks every path below
 * against the live tenant schema and validates every insert.
 * `<tenant_name>` stands for the tenant itself: its children sit directly
 * under tenants.<id>:. A `[].` prefix marks a key inside an array item. */
const TENANT_PLACEHOLDER = '<tenant_name>';
const SCHEMA = [
  {
    key: TENANT_PLACEHOLDER,
    type: 'map',
    desc: t('Tenant 配置區塊（如 db-a, db-b）', 'Tenant config block (e.g., db-a, db-b)'),
    rulePack: 'all',
    children: [
      { key: 'mariadb_connections_warning', type: 'number | "disable"', desc: t('MariaDB 連線數 warning 閾值', 'MariaDB connections warning threshold'), rulePack: 'mariadb', example: '"150"', range: '> 0' },
      { key: 'mariadb_connections_warning_critical', type: 'number | "disable"', desc: t('MariaDB 連線數 critical 閾值', 'MariaDB connections critical threshold'), rulePack: 'mariadb', example: '"200"', range: '> warning' },
      { key: 'mariadb_replication_lag_warning', type: 'number', desc: t('複製延遲 warning（秒）', 'Replication lag warning (seconds)'), rulePack: 'mariadb', example: '"5"', range: '> 0' },
      { key: 'mariadb_replication_lag_warning_critical', type: 'number', desc: t('複製延遲 critical（秒）', 'Replication lag critical (seconds)'), rulePack: 'mariadb', example: '"10"', range: '> warning' },
      { key: 'mariadb_threads_running_warning', type: 'number', desc: t('執行緒飽和 warning（threads_running，非 host CPU%）', 'Running-threads saturation warning (threads_running, NOT host CPU%)'), rulePack: 'mariadb', example: '"30"', range: '> 0' },
      { key: 'mariadb_threads_running_warning_critical', type: 'number', desc: t('執行緒飽和 critical', 'Running-threads saturation critical'), rulePack: 'mariadb', example: '"50"', range: '> warning' },
      { key: 'redis_memory_usage_warning', type: 'number', desc: t('Redis 記憶體使用率 warning（%）', 'Redis memory usage warning (%)'), rulePack: 'redis', example: '"75"', range: '0-100' },
      { key: 'redis_memory_usage_warning_critical', type: 'number', desc: t('Redis 記憶體使用率 critical', 'Redis memory usage critical'), rulePack: 'redis', example: '"90"', range: '0-100' },
      { key: 'redis_cache_hit_ratio_warning', type: 'number', desc: t('快取命中率 warning（低於觸發）', 'Cache hit ratio warning (fires when below)'), rulePack: 'redis', example: '"85"', range: '0-100' },
      { key: 'postgresql_connections_warning', type: 'number', desc: t('PostgreSQL 連線數 warning', 'PostgreSQL connections warning'), rulePack: 'postgresql', example: '"100"', range: '> 0' },
      { key: 'postgresql_deadlocks_warning', type: 'number', desc: t('Deadlock 數量 warning', 'Deadlock count warning'), rulePack: 'postgresql', example: '"5"', range: '>= 0' },
      { key: 'kafka_consumer_lag_warning', type: 'number', desc: t('Consumer lag warning', 'Consumer lag warning'), rulePack: 'kafka', example: '"1000"', range: '> 0' },
      { key: 'kafka_consumer_lag_warning_critical', type: 'number', desc: t('Consumer lag critical', 'Consumer lag critical'), rulePack: 'kafka', example: '"5000"', range: '> warning' },
      { key: 'elasticsearch_heap_usage_warning', type: 'number', desc: t('ES heap 使用率 warning（%）', 'ES heap usage warning (%)'), rulePack: 'elasticsearch', example: '"75"', range: '0-100' },
      { key: 'kubernetes_pod_restart_warning', type: 'number', desc: t('Pod 重啟次數 warning', 'Pod restart count warning'), rulePack: 'kubernetes', example: '"5"', range: '>= 0' },
      { key: 'kubernetes_cpu_throttle_warning', type: 'number', desc: t('CPU throttle 比例 warning（%）', 'CPU throttle ratio warning (%)'), rulePack: 'kubernetes', example: '"25"', range: '0-100' },
      { key: 'jvm_gc_pause_warning', type: 'number', desc: t('GC 暫停時間 warning（秒）', 'GC pause time warning (seconds)'), rulePack: 'jvm', example: '"0.5"', range: '> 0' },
      { key: 'node_disk_usage_warning', type: 'number', desc: t('磁碟使用率 warning（%）', 'Disk usage warning (%)'), rulePack: 'node', example: '"80"', range: '0-100' },
      { key: 'node_disk_usage_warning_critical', type: 'number', desc: t('磁碟使用率 critical（%）', 'Disk usage critical (%)'), rulePack: 'node', example: '"90"', range: '0-100' },
    ],
  },
  silentModeNode(),
  {
    key: '_state_maintenance',
    type: '"enable" | "disable" | object',
    desc: t('維護模式：完全抑制告警', 'Maintenance mode: full alert suppression'),
    rulePack: 'all',
    children: [
      { key: 'enabled', type: 'boolean', desc: t('啟用維護模式', 'Enable maintenance mode'), rulePack: 'all', example: 'true' },
      { key: 'expires', type: 'string (date-time, RFC3339)', desc: t('自動失效時間（UTC，例如 now + 7 天）；無法解析時整個設定被忽略', 'Auto-expiry timestamp (UTC, e.g. now + 7 days); an unparseable value makes the whole setting ignored'), rulePack: 'all' },
      { key: 'reason', type: 'string', desc: t('原因', 'Reason'), rulePack: 'all', example: '"Planned DB migration"' },
      { key: 'recurring', type: 'array', desc: t('週期性維護排程', 'Recurring maintenance schedules'), rulePack: 'all',
        children: [
          { key: '[].cron', type: 'string', desc: t('Cron 表達式（必填）', 'Cron expression (required)'), rulePack: 'all', example: '"0 2 * * 0"' },
          { key: '[].duration', type: 'string', desc: t('持續時間（必填）', 'Duration (required)'), rulePack: 'all', example: '"2h"' },
        ],
      },
    ],
  },
  {
    key: '_routing',
    type: 'object',
    desc: t('告警路由配置', 'Alert routing configuration'),
    rulePack: 'all',
    children: [
      { key: 'receiver', type: 'object', desc: t('接收器（exporter 必讀；缺少時整個 _routing 被略過）。以 type 決定必填欄位', 'Receiver (required by the exporter; without it the whole _routing is skipped). `type` decides the required fields'), rulePack: 'all', range: RECEIVER_RANGE },
      { key: 'group_by', type: 'array', desc: t('分組標籤', 'Grouping labels'), rulePack: 'all', example: '["alertname", "tenant"]' },
      { key: 'group_wait', type: 'string', desc: t('告警分組等待時間', 'Alert group wait time'), rulePack: 'all', example: '"30s"', range: '5s–5m' },
      { key: 'group_interval', type: 'string', desc: t('告警分組間隔', 'Alert group interval'), rulePack: 'all', example: '"5m"', range: '5s–5m' },
      { key: 'repeat_interval', type: 'string', desc: t('重複通知間隔', 'Repeat notification interval'), rulePack: 'all', example: '"4h"', range: '1m–72h' },
      { key: 'overrides', type: 'array', desc: t('Per-rule 路由覆寫', 'Per-rule routing overrides'), rulePack: 'all' },
      { key: 'routes', type: 'array', desc: t('依 label 等值分流的子路由（排在 overrides 之後；ADR-007）', 'Label-equality sub-routes, matched after overrides (ADR-007)'), rulePack: 'all' },
    ],
  },
  {
    key: '_routing_profile',
    type: 'string',
    desc: t('引用 _routing_profiles.yaml 中的具名路由設定檔（ADR-007）', 'Name of a routing profile defined in _routing_profiles.yaml (ADR-007)'),
    rulePack: 'all',
    example: '"team-sre-apac"',
  },
  {
    key: '_routing_enforced',
    type: 'object',
    desc: t('平台強制雙軌路由（NOC + tenant）', 'Platform enforced dual routing (NOC + tenant)'),
    rulePack: 'all',
    children: [
      { key: 'enabled', type: 'boolean', desc: t('是否啟用', 'Whether it is enabled'), rulePack: 'all', example: 'true' },
      { key: 'receiver', type: 'object', desc: t('NOC 接收器，形狀同 _routing.receiver', 'NOC receiver, same shape as _routing.receiver'), rulePack: 'all', range: RECEIVER_RANGE },
      { key: 'match', type: 'array', desc: t('Alertmanager label matcher 清單，符合任一即送 NOC', 'Alertmanager label matchers; alerts matching any go to the NOC'), rulePack: 'all' },
    ],
  },
  {
    key: '_metadata',
    type: 'object',
    desc: t('Tenant metadata，透過 info metric 注入 Runbook 等', 'Tenant metadata injected via info metric for Runbook etc.'),
    rulePack: 'all',
    children: [
      { key: 'runbook_url', type: 'string', desc: t('Runbook 基底 URL', 'Runbook base URL'), rulePack: 'all' },
      { key: 'owner', type: 'string', desc: t('負責團隊或人', 'Owning team or person'), rulePack: 'all', example: '"dba-team"' },
      { key: 'tier', type: 'string', desc: t('服務等級', 'Service tier'), rulePack: 'all', example: '"tier-1"' },
    ],
  },
  {
    key: '_routing_defaults',
    type: 'object',
    desc: t('路由預設值（四層合併第一層，ADR-007；通常寫在 _defaults.yaml）', 'Routing defaults (four-layer merge layer 1, ADR-007; usually set in _defaults.yaml)'),
    rulePack: 'all',
    children: [
      { key: 'receiver', type: 'object', desc: t('預設接收器，形狀同 _routing.receiver', 'Default receiver, same shape as _routing.receiver'), rulePack: 'all', range: RECEIVER_RANGE },
      { key: 'group_wait', type: 'string', desc: t('預設分組等待', 'Default group wait'), rulePack: 'all', example: '"30s"' },
      { key: 'group_interval', type: 'string', desc: t('預設分組間隔', 'Default group interval'), rulePack: 'all', example: '"5m"' },
      { key: 'repeat_interval', type: 'string', desc: t('預設重複間隔', 'Default repeat interval'), rulePack: 'all', example: '"4h"' },
    ],
  },
];

const RULE_PACKS = ['all', 'mariadb', 'postgresql', 'redis', 'mongodb', 'elasticsearch', 'oracle', 'db2', 'clickhouse', 'kafka', 'rabbitmq', 'jvm', 'nginx', 'kubernetes', 'operational', 'platform'];

// Insert payload for a leaf at `path` (the keys from the tree root to the
// leaf): a whole tenant document, `tenants:` at the root and the leaf nested
// under its parents, so the playground receives something it can check.
// `<tenant_name>` is the tenant itself; `[].key` opens an array item.
function insertSegments(path) {
  return path
    .filter(k => k !== TENANT_PLACEHOLDER)
    .flatMap(k => (k.startsWith('[].') ? ['[]', k.slice(3)] : [k]));
}
// A leaf under these roots is not safe on its own, so its insert also carries
// a sibling (#2033): a `_routing` without `receiver` is skipped by the
// exporter (WARN) and rejected by da-guard; a `_state_maintenance` without
// `expires` keeps the tenant in maintenance indefinitely, so it gets a
// relative expiry computed from `now` (never a fixed date).
const INSERT_SIBLINGS = {
  _routing: () => [
    'receiver:',
    '  type: "webhook"',
    '  url: "https://webhook.example.com/alerts"',
  ],
  _state_maintenance: (now) => [`expires: "${silentModeExpires(now)}"`],
};

function buildInsertYaml(path, example, now = new Date(), tenantId = 'db-a') {
  const lines = ['# Inserted from Schema Explorer', 'tenants:', `  ${tenantId}:`];
  const segs = insertSegments(path);
  let indent = 4;
  let item = false;
  segs.forEach((seg, i) => {
    if (seg === '[]') { item = true; return; }
    const last = i === segs.length - 1;
    lines.push(`${' '.repeat(indent)}${item ? '- ' : ''}${seg}:${last ? ` ${example}` : ''}`);
    if (i === 0 && !last && INSERT_SIBLINGS[seg]) {
      for (const l of INSERT_SIBLINGS[seg](now)) lines.push(`${' '.repeat(indent + 2)}${l}`);
    }
    indent += item ? 4 : 2;
    item = false;
  });
  return lines.join('\n');
}

function SchemaNode({ node, depth, path = [node.key], search, expandedKeys, toggleExpand, onInsert }) {
  const matchesSearch = search && (
    node.key.toLowerCase().includes(search.toLowerCase()) ||
    node.desc.toLowerCase().includes(search.toLowerCase())
  );
  const hasChildren = node.children && node.children.length > 0;
  const isExpanded = expandedKeys.has(node.key);
  const indent = depth * 20;

  // If searching and this node + children don't match, hide
  const childrenMatch = hasChildren && node.children.some(c =>
    c.key.toLowerCase().includes((search || '').toLowerCase()) ||
    c.desc.toLowerCase().includes((search || '').toLowerCase())
  );
  if (search && !matchesSearch && !childrenMatch && depth > 0) return null;

  const nodePaddingStyle = { paddingLeft: indent + 12 };
  return (
    <>
      <div
        className={`flex items-start gap-2 py-2 px-3 rounded-lg transition-colors ${matchesSearch ? 'bg-yellow-50' : 'hover:bg-slate-50'}`}
        style={nodePaddingStyle}
      >
        {hasChildren ? (
          <button onClick={() => toggleExpand(node.key)} className="mt-0.5 text-slate-400 hover:text-slate-700 flex-shrink-0 w-5 text-center">
            {isExpanded ? '▾' : '▸'}
          </button>
        ) : (
          <span className="w-5 flex-shrink-0" />
        )}
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <code className="text-sm font-bold text-blue-700 bg-blue-50 px-1.5 py-0.5 rounded">{node.key}</code>
            <span className="text-xs text-slate-400 font-mono">{node.type}</span>
            {node.rulePack && node.rulePack !== 'all' && (
              <span className="text-xs px-1.5 py-0.5 rounded bg-purple-100 text-purple-700">{node.rulePack}</span>
            )}
            {node.range && (
              <span className="text-xs text-slate-400">[{node.range}]</span>
            )}
          </div>
          <p className="text-xs text-slate-600 mt-0.5">{node.desc}</p>
          {node.example && (
            <div className="flex items-center gap-2 mt-1">
              <span className="text-xs text-slate-400">{t('範例', 'e.g.')}:</span>
              <code className="text-xs bg-slate-100 px-1.5 py-0.5 rounded text-slate-700">{node.example}</code>
              {onInsert && !hasChildren && (
                <button onClick={() => onInsert(node, path)} className="text-xs text-blue-600 hover:underline">
                  {t('插入 Playground', 'Insert to Playground')} →
                </button>
              )}
            </div>
          )}
        </div>
      </div>
      {hasChildren && isExpanded && node.children.map((child, i) => (
        <SchemaNode key={`${node.key}-${i}`} node={child} depth={depth + 1} path={[...path, child.key]}
          search={search} expandedKeys={expandedKeys} toggleExpand={toggleExpand} onInsert={onInsert} />
      ))}
    </>
  );
}

export { SCHEMA, TENANT_PLACEHOLDER, buildInsertYaml };

export default function SchemaExplorer() {
  const [search, setSearch] = useState('');
  const [filterPack, setFilterPack] = useState('all');
  const [expandedKeys, setExpandedKeys] = useState(new Set(['<tenant_name>', '_routing']));

  const toggleExpand = (key) => {
    setExpandedKeys(prev => {
      const next = new Set(prev);
      next.has(key) ? next.delete(key) : next.add(key);
      return next;
    });
  };

  const expandAll = () => {
    const all = new Set();
    const collect = (nodes) => nodes.forEach(n => { all.add(n.key); if (n.children) collect(n.children); });
    collect(SCHEMA);
    setExpandedKeys(all);
  };
  const collapseAll = () => setExpandedKeys(new Set());

  const filtered = useMemo(() => {
    if (filterPack === 'all') return SCHEMA;
    return SCHEMA.map(node => {
      if (!node.children) return node;
      const filteredChildren = node.children.filter(c => c.rulePack === 'all' || c.rulePack === filterPack);
      return { ...node, children: filteredChildren };
    }).filter(n => !n.children || n.children.length > 0);
  }, [filterPack]);

  // Count all leaf keys
  const totalKeys = useMemo(() => {
    let count = 0;
    const walk = (nodes) => nodes.forEach(n => { count++; if (n.children) walk(n.children); });
    walk(filtered);
    return count;
  }, [filtered]);

  const handleInsert = (node, path) => {
    const encoded = btoa(unescape(encodeURIComponent(buildInsertYaml(path, node.example))));
    window.open(`../assets/jsx-loader.html?component=../playground.jsx#yaml=${encoded}`, '_blank');
  };

  return (
    <div className="min-h-screen bg-gradient-to-br from-slate-50 to-slate-100 p-8">
      <div className="max-w-5xl mx-auto">
        <h1 className="text-3xl font-bold text-slate-900 mb-2">{t('YAML Schema 瀏覽器', 'YAML Schema Explorer')}</h1>
        <p className="text-[color:var(--da-color-muted)] mb-6">{t('互動式瀏覽租戶檔 tenants.<id>: 之下的合法 YAML key，了解型別、範圍和所屬 Rule Pack。平台檔（_defaults.yaml、_routing_profiles.yaml、_domain_policy.yaml 等）有各自的 schema，不在此列。',
          'Browse the valid YAML keys under tenants.<id>: in a tenant file — types, ranges, and Rule Pack ownership. Platform files (_defaults.yaml, _routing_profiles.yaml, _domain_policy.yaml, …) have their own schemas and are not listed here.')}</p>

        {/* Toolbar */}
        <div className="flex flex-wrap items-center gap-3 mb-6">
          <input
            type="text"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder={t('搜尋 key 或描述...', 'Search key or description...')}
            className="flex-1 min-w-48 px-4 py-2 border border-slate-200 rounded-lg text-sm focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
          />
          <select value={filterPack} onChange={(e) => setFilterPack(e.target.value)}
            aria-label={t('依 Rule Pack 篩選', 'Filter by Rule Pack')}
            className="px-3 py-2 border border-slate-200 rounded-lg text-sm bg-white">
            {RULE_PACKS.map(rp => (
              <option key={rp} value={rp}>{rp === 'all' ? t('所有 Rule Pack', 'All Rule Packs') : rp}</option>
            ))}
          </select>
          <button onClick={expandAll} className="px-3 py-2 text-xs text-blue-600 hover:text-blue-800 border border-blue-200 rounded-lg hover:bg-blue-50">
            {t('全部展開', 'Expand All')}
          </button>
          <button onClick={collapseAll} className="px-3 py-2 text-xs text-slate-600 hover:text-slate-800 border border-slate-200 rounded-lg hover:bg-slate-50">
            {t('全部收合', 'Collapse All')}
          </button>
        </div>

        {/* Stats */}
        <div className="text-xs text-slate-500 mb-3">
          {t(`顯示 ${totalKeys} 個 key`, `Showing ${totalKeys} keys`)}
          {filterPack !== 'all' && <span className="ml-2 px-2 py-0.5 bg-purple-100 text-purple-700 rounded">{filterPack}</span>}
        </div>

        {/* Tree */}
        <div className="bg-white rounded-xl shadow-sm border border-slate-200 p-4 space-y-0.5">
          {filtered.map((node, i) => (
            <SchemaNode key={i} node={node} depth={0} search={search}
              expandedKeys={expandedKeys} toggleExpand={toggleExpand} onInsert={handleInsert} />
          ))}
        </div>

        {/* Legend */}
        <div className="mt-6 flex flex-wrap gap-4 text-xs text-slate-500">
          <span><code className="bg-blue-50 text-blue-700 px-1 rounded">key</code> {t('鍵名', 'Key name')}</span>
          <span><span className="font-mono text-slate-400">type</span> {t('資料類型', 'Data type')}</span>
          <span><span className="px-1.5 py-0.5 rounded bg-purple-100 text-purple-700">pack</span> {t('所屬 Rule Pack', 'Rule Pack')}</span>
          <span>[range] {t('合法範圍', 'Valid range')}</span>
        </div>
      </div>
    </div>
  );
}
