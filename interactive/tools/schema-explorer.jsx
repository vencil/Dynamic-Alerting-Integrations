---
title: "YAML Schema Explorer"
tags: [schema, reference, yaml]
audience: ["platform-engineer", "domain-expert"]
version: v2.0.0
lang: en
related: [playground, glossary, config-lint]
---

import React, { useState, useMemo } from 'react';

const t = window.__t || ((zh, en) => en);

/* ── Full schema tree ── */
const SCHEMA = [
  {
    key: '_defaults',
    type: 'map',
    desc: t('全域預設閾值，所有 tenant 繼承', 'Global default thresholds inherited by all tenants'),
    rulePack: 'all',
    children: [
      { key: '<metric_key>', type: 'number | "disable"', desc: t('閾值數值或 "disable" 關閉', 'Threshold value or "disable" to turn off'), rulePack: 'all', example: '80' },
      { key: '<metric_key>_critical', type: 'number | "disable"', desc: t('Critical 嚴重度閾值', 'Critical severity threshold'), rulePack: 'all', example: '90' },
    ],
  },
  {
    key: '<tenant_name>',
    type: 'map',
    desc: t('Tenant 配置區塊（如 db-a, db-b）', 'Tenant config block (e.g., db-a, db-b)'),
    rulePack: 'all',
    children: [
      { key: 'mariadb_connections_warning', type: 'number | "disable"', desc: t('MariaDB 連線數 warning 閾值', 'MariaDB connections warning threshold'), rulePack: 'mariadb', example: '150', range: '> 0' },
      { key: 'mariadb_connections_warning_critical', type: 'number | "disable"', desc: t('MariaDB 連線數 critical 閾值', 'MariaDB connections critical threshold'), rulePack: 'mariadb', example: '200', range: '> warning' },
      { key: 'mariadb_replication_lag_warning', type: 'number', desc: t('複製延遲 warning（秒）', 'Replication lag warning (seconds)'), rulePack: 'mariadb', example: '5', range: '> 0' },
      { key: 'mariadb_replication_lag_warning_critical', type: 'number', desc: t('複製延遲 critical（秒）', 'Replication lag critical (seconds)'), rulePack: 'mariadb', example: '10', range: '> warning' },
      { key: 'mariadb_cpu_usage_warning', type: 'number', desc: t('CPU 使用率 warning（%）', 'CPU usage warning (%)'), rulePack: 'mariadb', example: '80', range: '0-100' },
      { key: 'mariadb_cpu_usage_warning_critical', type: 'number', desc: t('CPU 使用率 critical（%）', 'CPU usage critical (%)'), rulePack: 'mariadb', example: '90', range: '0-100' },
      { key: 'redis_memory_usage_warning', type: 'number', desc: t('Redis 記憶體使用率 warning（%）', 'Redis memory usage warning (%)'), rulePack: 'redis', example: '75', range: '0-100' },
      { key: 'redis_memory_usage_warning_critical', type: 'number', desc: t('Redis 記憶體使用率 critical', 'Redis memory usage critical'), rulePack: 'redis', example: '90', range: '0-100' },
      { key: 'redis_cache_hit_ratio_warning', type: 'number', desc: t('快取命中率 warning（低於觸發）', 'Cache hit ratio warning (fires when below)'), rulePack: 'redis', example: '85', range: '0-100' },
      { key: 'postgresql_connections_warning', type: 'number', desc: t('PostgreSQL 連線數 warning', 'PostgreSQL connections warning'), rulePack: 'postgresql', example: '100', range: '> 0' },
      { key: 'postgresql_deadlocks_warning', type: 'number', desc: t('Deadlock 數量 warning', 'Deadlock count warning'), rulePack: 'postgresql', example: '5', range: '>= 0' },
      { key: 'kafka_consumer_lag_warning', type: 'number', desc: t('Consumer lag warning', 'Consumer lag warning'), rulePack: 'kafka', example: '1000', range: '> 0' },
      { key: 'kafka_consumer_lag_warning_critical', type: 'number', desc: t('Consumer lag critical', 'Consumer lag critical'), rulePack: 'kafka', example: '5000', range: '> warning' },
      { key: 'elasticsearch_heap_usage_warning', type: 'number', desc: t('ES heap 使用率 warning（%）', 'ES heap usage warning (%)'), rulePack: 'elasticsearch', example: '75', range: '0-100' },
      { key: 'kubernetes_pod_restart_warning', type: 'number', desc: t('Pod 重啟次數 warning', 'Pod restart count warning'), rulePack: 'kubernetes', example: '5', range: '>= 0' },
      { key: 'kubernetes_cpu_throttle_warning', type: 'number', desc: t('CPU throttle 比例 warning（%）', 'CPU throttle ratio warning (%)'), rulePack: 'kubernetes', example: '25', range: '0-100' },
      { key: 'jvm_gc_pause_warning', type: 'number', desc: t('GC 暫停時間 warning（秒）', 'GC pause time warning (seconds)'), rulePack: 'jvm', example: '0.5', range: '> 0' },
      { key: 'node_disk_usage_warning', type: 'number', desc: t('磁碟使用率 warning（%）', 'Disk usage warning (%)'), rulePack: 'node', example: '80', range: '0-100' },
      { key: 'node_disk_usage_warning_critical', type: 'number', desc: t('磁碟使用率 critical（%）', 'Disk usage critical (%)'), rulePack: 'node', example: '90', range: '0-100' },
    ],
  },
  {
    key: '_silent_mode',
    type: 'boolean | object',
    desc: t('靜默模式：告警產生但不發送通知', 'Silent mode: alerts fire but notifications suppressed'),
    rulePack: 'all',
    children: [
      { key: 'enabled', type: 'boolean', desc: t('啟用靜默模式', 'Enable silent mode'), rulePack: 'all', example: 'true' },
      { key: 'expires', type: 'string (ISO8601)', desc: t('自動失效時間', 'Auto-expiry timestamp'), rulePack: 'all', example: '"2026-03-15T00:00:00Z"' },
    ],
  },
  {
    key: '_state_maintenance',
    type: 'object',
    desc: t('維護模式：完全抑制告警', 'Maintenance mode: full alert suppression'),
    rulePack: 'all',
    children: [
      { key: 'enabled', type: 'boolean', desc: t('啟用維護模式', 'Enable maintenance mode'), rulePack: 'all', example: 'true' },
      { key: 'expires', type: 'string (ISO8601)', desc: t('自動失效時間', 'Auto-expiry timestamp'), rulePack: 'all', example: '"2026-03-15T06:00:00Z"' },
      { key: 'recurring', type: 'array', desc: t('週期性維護排程', 'Recurring maintenance schedules'), rulePack: 'all',
        children: [
          { key: '[].cron', type: 'string', desc: t('Cron 表達式', 'Cron expression'), rulePack: 'all', example: '"0 2 * * 0"' },
          { key: '[].duration', type: 'string', desc: t('持續時間', 'Duration'), rulePack: 'all', example: '"2h"' },
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
      { key: 'receiver_type', type: 'string', desc: t('接收器類型', 'Receiver type'), rulePack: 'all', example: '"webhook"', range: 'webhook|email|slack|teams|rocketchat|pagerduty' },
      { key: 'webhook_url', type: 'string', desc: t('Webhook URL', 'Webhook URL'), rulePack: 'all', example: '"https://hooks.slack.com/..."' },
      { key: 'group_wait', type: 'string', desc: t('告警分組等待時間', 'Alert group wait time'), rulePack: 'all', example: '"30s"', range: '5s–5m' },
      { key: 'group_interval', type: 'string', desc: t('告警分組間隔', 'Alert group interval'), rulePack: 'all', example: '"5m"', range: '5s–5m' },
      { key: 'repeat_interval', type: 'string', desc: t('重複通知間隔', 'Repeat notification interval'), rulePack: 'all', example: '"4h"', range: '1m–72h' },
      { key: 'overrides', type: 'array', desc: t('Per-rule 路由覆寫', 'Per-rule routing overrides'), rulePack: 'all' },
    ],
  },
  {
    key: '_routing_enforced',
    type: 'object',
    desc: t('平台強制雙軌路由（NOC + tenant）', 'Platform enforced dual routing (NOC + tenant)'),
    rulePack: 'all',
    children: [
      { key: 'noc_webhook_url', type: 'string', desc: t('NOC 接收 webhook', 'NOC receiver webhook'), rulePack: 'all' },
      { key: 'tenant_channel_template', type: 'string', desc: t('Tenant 頻道模板，用 {{tenant}} 替換', 'Tenant channel template with {{tenant}} placeholder'), rulePack: 'all', example: '"#alerts-{{tenant}}"' },
    ],
  },
  {
    key: '_metadata',
    type: 'object',
    desc: t('Tenant metadata，透過 info metric 注入 Runbook 等', 'Tenant metadata injected via info metric for Runbook etc.'),
    rulePack: 'all',
    children: [
      { key: 'runbook_url', type: 'string', desc: t('Runbook 基底 URL', 'Runbook base URL'), rulePack: 'all' },
      { key: 'team', type: 'string', desc: t('團隊名稱', 'Team name'), rulePack: 'all' },
      { key: 'tier', type: 'string', desc: t('服務等級', 'Service tier'), rulePack: 'all', example: '"gold"' },
    ],
  },
];

const RULE_PACKS = ['all', 'mariadb', 'postgresql', 'redis', 'kafka', 'elasticsearch', 'kubernetes', 'jvm', 'node'];

function SchemaNode({ node, depth, search, expandedKeys, toggleExpand, onInsert }) {
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
                <button onClick={() => onInsert(node)} className="text-xs text-blue-600 hover:underline">
                  {t('插入 Playground', 'Insert to Playground')} →
                </button>
              )}
            </div>
          )}
        </div>
      </div>
      {hasChildren && isExpanded && node.children.map((child, i) => (
        <SchemaNode key={`${node.key}-${i}`} node={child} depth={depth + 1}
          search={search} expandedKeys={expandedKeys} toggleExpand={toggleExpand} onInsert={onInsert} />
      ))}
    </>
  );
}

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

  const handleInsert = (node) => {
    const yaml = `${node.key}: ${node.example || ''}`;
    const encoded = btoa(unescape(encodeURIComponent(`# Inserted from Schema Explorer\ndb-a:\n  ${yaml}`)));
    window.open(`../assets/jsx-loader.html?component=../playground.jsx#yaml=${encoded}`, '_blank');
  };

  return (
    <div className="min-h-screen bg-gradient-to-br from-slate-50 to-slate-100 p-8">
      <div className="max-w-5xl mx-auto">
        <h1 className="text-3xl font-bold text-slate-900 mb-2">{t('YAML Schema 瀏覽器', 'YAML Schema Explorer')}</h1>
        <p className="text-slate-600 mb-6">{t('互動式瀏覽所有合法 YAML key，了解型別、範圍和所屬 Rule Pack', 'Browse all valid YAML keys — types, ranges, and Rule Pack ownership')}</p>

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
