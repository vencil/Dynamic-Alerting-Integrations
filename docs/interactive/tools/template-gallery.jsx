---
title: "Config Template Gallery"
tags: [templates, examples, stacks]
audience: [tenant, "platform-engineer"]
version: v2.1.0
lang: en
related: [playground, rule-pack-selector, threshold-calculator]
---

import React, { useState } from 'react';

const t = window.__t || ((zh, en) => en);

const TEMPLATES = [
  {
    id: 'ecommerce',
    name: t('電商平台', 'E-Commerce Platform'),
    icon: '🛒',
    stack: ['MariaDB', 'Redis', 'Nginx'],
    desc: t('經典網店：MariaDB 用於訂單/庫存，Redis 用於會話/快取，Nginx 作反向代理', 'Classic web store: MariaDB for orders/inventory, Redis for sessions/cache, Nginx as reverse proxy.'),
    yaml: `tenants:
  ecommerce-prod:
    # MariaDB — order processing database
    mysql_connections: "150"
    mysql_connections_critical: "200"
    mysql_cpu: "75"
    mysql_slow_queries: "10"
    mysql_replication_lag: "5"
    # Redis — session store & product cache
    redis_memory: "80"
    redis_memory_critical: "95"
    redis_evictions: "500"
    redis_connected_clients: "3000"
    # Nginx — frontend reverse proxy
    # (uses Nginx Rule Pack recording rules)
    _routing:
      receiver_type: "slack"
      webhook_url: "https://hooks.slack.com/services/xxx/yyy/zzz"
      group_by: ["alertname", "severity"]
      group_wait: "30s"
      repeat_interval: "4h"`,
  },
  {
    id: 'iot-pipeline',
    name: t('IoT 數據管道', 'IoT Data Pipeline'),
    icon: '📡',
    stack: ['Kafka', 'MongoDB', 'JVM'],
    desc: t('高吞吐 IoT：Kafka 資料引入、MongoDB 時序儲存、JVM 處理服務', 'High-throughput IoT: Kafka ingestion, MongoDB time-series storage, JVM-based processing services.'),
    yaml: `tenants:
  iot-pipeline:
    # Kafka — device telemetry ingestion
    kafka_lag: "50000"
    kafka_lag_critical: "200000"
    kafka_broker_active: "3"
    kafka_controller_active: "1"
    kafka_isr_shrank: "0"
    # MongoDB — time-series device data
    mongo_connections: "200"
    mongo_connections_critical: "300"
    mongo_memory: "85"
    _routing:
      receiver_type: "pagerduty"
      group_by: ["alertname", "topic"]
      group_wait: "1m"
      group_interval: "5m"
      repeat_interval: "12h"`,
  },
  {
    id: 'saas-multi',
    name: t('SaaS 多服務', 'SaaS Multi-Service'),
    icon: '☁️',
    stack: ['PostgreSQL', 'Redis', 'Elasticsearch'],
    desc: t('SaaS 後端：PostgreSQL 用於核心資料、Redis 用於快取、Elasticsearch 用於全文搜尋', 'SaaS backend: PostgreSQL for core data, Redis for caching, Elasticsearch for full-text search.'),
    yaml: `tenants:
  saas-backend:
    # PostgreSQL — core application database
    pg_connections: "100"
    pg_connections_critical: "150"
    pg_cache_hit_ratio: "90"
    pg_query_time: "3000"
    pg_replication_lag: "10"
    # Redis — API response cache
    redis_memory: "70"
    redis_memory_critical: "90"
    redis_connected_clients: "2000"
    # Elasticsearch — search index
    elasticsearch_heap: "80"
    elasticsearch_heap_critical: "92"
    elasticsearch_unassigned_shards: "0"
    _routing:
      receiver_type: "webhook"
      webhook_url: "https://hooks.slack.com/services/xxx/yyy/zzz"
      group_wait: "30s"
      repeat_interval: "6h"`,
  },
  {
    id: 'analytics',
    name: t('分析堆棧', 'Analytics Stack'),
    icon: '📊',
    stack: ['ClickHouse', 'Kafka', 'JVM'],
    desc: t('實時分析：Kafka 事件串流、ClickHouse OLAP 儲存、JVM 分析工作程式', 'Real-time analytics: Kafka event streaming, ClickHouse OLAP storage, JVM analytics workers.'),
    yaml: `tenants:
  analytics:
    # Kafka — event stream
    kafka_lag: "100000"
    kafka_lag_critical: "500000"
    kafka_broker_active: "5"
    # ClickHouse thresholds use custom keys
    _routing:
      receiver_type: "email"
      email_to: "analytics-team@company.com"
      group_wait: "2m"
      group_interval: "5m"
      repeat_interval: "24h"`,
  },
  {
    id: 'minimal',
    name: t('最小配置（3 行）', 'Minimal (3 Lines)'),
    icon: '✨',
    stack: ['MariaDB'],
    desc: t('絕對最小配置 — 只需 3 行！完美入門選擇', 'The absolute minimum config — just 3 lines! Perfect for getting started.'),
    yaml: `tenants:
  my-app:
    mysql_connections: "100"`,
  },
  {
    id: 'maintenance',
    name: t('維護窗口示例', 'Maintenance Window Demo'),
    icon: '🔧',
    stack: ['MariaDB'],
    desc: t('展示如何設定臨時維護窗口、自動過期和靜默模式', 'Shows how to set up a temporary maintenance window with auto-expiry and silent mode.'),
    yaml: `tenants:
  db-maintenance:
    mysql_connections: "200"
    mysql_cpu: "90"
    _state_maintenance:
      expires: "2026-03-20T06:00:00Z"
    _silent_mode:
      expires: "2026-03-15T12:00:00Z"
    _routing:
      receiver_type: "teams"
      webhook_url: "https://teams.example.com/webhook"
      group_wait: "1m"
      repeat_interval: "12h"`,
  },
];

const StackBadge = ({ name }) => {
  const colors = {
    MariaDB: 'bg-blue-100 text-blue-700',
    PostgreSQL: 'bg-indigo-100 text-indigo-700',
    Redis: 'bg-red-100 text-red-700',
    MongoDB: 'bg-green-100 text-green-700',
    Kafka: 'bg-amber-100 text-amber-700',
    Elasticsearch: 'bg-purple-100 text-purple-700',
    ClickHouse: 'bg-orange-100 text-orange-700',
    JVM: 'bg-teal-100 text-teal-700',
    Nginx: 'bg-emerald-100 text-emerald-700',
  };
  return (
    <span className={`inline-block px-2 py-0.5 text-xs font-medium rounded ${colors[name] || 'bg-gray-100 text-gray-700'}`}>
      {name}
    </span>
  );
};

export default function TemplateGallery() {
  const [selected, setSelected] = useState(null);
  const [copiedId, setCopiedId] = useState(null);
  const [filter, setFilter] = useState('');

  const filtered = TEMPLATES.filter(tpl => {
    if (!filter) return true;
    const q = filter.toLowerCase();
    return tpl.name.toLowerCase().includes(q) ||
           tpl.desc.toLowerCase().includes(q) ||
           tpl.stack.some(s => s.toLowerCase().includes(q));
  });

  const copyYaml = (tpl) => {
    navigator.clipboard.writeText(tpl.yaml);
    setCopiedId(tpl.id);
    setTimeout(() => setCopiedId(null), 2000);
  };

  const playgroundUrl = (tpl) => {
    const encoded = btoa(unescape(encodeURIComponent(tpl.yaml)));
    return `../assets/jsx-loader.html?component=../playground.jsx#yaml=${encoded}`;
  };

  return (
    <div className="min-h-screen bg-gradient-to-br from-slate-50 to-slate-100 p-8">
      <div className="max-w-5xl mx-auto">
        <div className="mb-8">
          <h1 className="text-3xl font-bold text-slate-900 mb-2">{t('配置模板庫', 'Config Template Gallery')}</h1>
          <p className="text-slate-600">{t('瀏覽真實場景配置模板，一鍵載入到 Playground 驗證', 'Browse real-world config templates. One click to validate in the Playground.')}</p>
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
                    <h3 className="font-semibold text-slate-900">{tpl.name}</h3>
                    <p className="text-xs text-slate-500 mt-1">{tpl.desc}</p>
                  </div>
                </div>
                <div className="flex flex-wrap gap-1.5">
                  {tpl.stack.map(s => <StackBadge key={s} name={s} />)}
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
                      {copiedId === tpl.id ? '✓ Copied' : t('複製 YAML', 'Copy YAML')}
                    </button>
                    <a
                      href={playgroundUrl(tpl)}
                      onClick={(e) => e.stopPropagation()}
                      className="flex-1 px-3 py-2 bg-blue-600 text-white rounded-lg text-xs font-medium hover:bg-blue-700 text-center"
                    >
                      {t('在 Playground 驗證', 'Open in Playground')}
                    </a>
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
      </div>
    </div>
  );
}
