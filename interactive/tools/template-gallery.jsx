---
title: "Config Template Gallery"
tags: [templates, examples, stacks]
audience: [tenant, "platform-engineer"]
version: v2.2.0
lang: en
related: [playground, rule-pack-selector, threshold-calculator]
---

import React, { useState, useMemo } from 'react';

const t = window.__t || ((zh, en) => en);

/* ── All 15 Rule Pack labels for filtering ── */
const ALL_PACKS = [
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

/* TODO(v2.3): Extract TEMPLATES to a JSON/YAML data file (e.g. template-data.json)
 * for easier maintenance and tooling integration. Keep inline for now to avoid
 * an extra fetch() call in browser-side Babel mode. */
const TEMPLATES = [
  /* ── Scenario templates (multi-pack) ── */
  {
    id: 'ecommerce',
    name: () => t('電商平台', 'E-Commerce Platform'),
    icon: '🛒',
    packs: ['mariadb', 'redis', 'nginx', 'kubernetes'],
    desc: () => t(
      '經典網店：MariaDB 用於訂單/庫存，Redis 用於會話/快取，Nginx 作反向代理，Kubernetes 容器監控',
      'Classic web store: MariaDB for orders/inventory, Redis for sessions/cache, Nginx as reverse proxy, Kubernetes container monitoring.'
    ),
    yaml: `# E-Commerce Platform — MariaDB + Redis + Nginx + Kubernetes
mysql_connections: "150"
mysql_connections_critical: "200"
mysql_cpu: "75"

redis_memory_used_bytes: "3221225472"
redis_memory_used_bytes_critical: "4294967296"
redis_connected_clients: "3000"

nginx_connections: "2000"
nginx_request_rate: "8000"
nginx_waiting: "300"

container_cpu: "75"
container_memory: "80"

_routing:
  receiver_type: slack
  webhook_url: https://hooks.slack.com/services/xxx/yyy/zzz
  group_by: [alertname, severity]
  group_wait: "30s"
  repeat_interval: "4h"

_metadata:
  owner: ecommerce-team
  tier: production`,
  },
  {
    id: 'iot-pipeline',
    name: () => t('IoT 數據管道', 'IoT Data Pipeline'),
    icon: '📡',
    packs: ['kafka', 'mongodb', 'jvm', 'kubernetes'],
    desc: () => t(
      '高吞吐 IoT：Kafka 資料引入、MongoDB 時序儲存、JVM 處理服務',
      'High-throughput IoT: Kafka ingestion, MongoDB time-series storage, JVM-based processing services.'
    ),
    yaml: `# IoT Data Pipeline — Kafka + MongoDB + JVM + Kubernetes
kafka_consumer_lag: "50000"
kafka_consumer_lag_critical: "200000"
kafka_broker_count: "3"
kafka_active_controllers: "1"
kafka_under_replicated_partitions: "0"
kafka_request_rate: "15000"

mongodb_connections_current: "200"
mongodb_connections_current_critical: "300"
mongodb_repl_lag_seconds: "5"

jvm_gc_pause: "0.8"
jvm_memory: "85"
jvm_threads: "400"

container_cpu: "80"
container_memory: "85"

_routing:
  receiver_type: pagerduty
  group_by: [alertname, topic]
  group_wait: "1m"
  group_interval: "5m"
  repeat_interval: "12h"

_metadata:
  owner: iot-platform-team
  tier: production`,
  },
  {
    id: 'saas-backend',
    name: () => t('SaaS 多服務後端', 'SaaS Multi-Service Backend'),
    icon: '☁️',
    packs: ['postgresql', 'redis', 'elasticsearch', 'jvm', 'kubernetes'],
    desc: () => t(
      'SaaS 後端：PostgreSQL 核心資料、Redis 快取、Elasticsearch 全文搜尋、JVM 服務',
      'SaaS backend: PostgreSQL for core data, Redis for caching, Elasticsearch for full-text search, JVM services.'
    ),
    yaml: `# SaaS Multi-Service — PostgreSQL + Redis + ES + JVM + K8s
pg_connections: "100"
pg_connections_critical: "150"
pg_replication_lag: "10"

redis_memory_used_bytes: "2147483648"
redis_connected_clients: "2000"

es_jvm_memory_used_percent: "80"
es_jvm_memory_used_percent_critical: "92"
es_filesystem_free_percent: "20"

jvm_memory: "80"
jvm_gc_pause: "0.5"
jvm_threads: "500"

container_cpu: "70"
container_memory: "80"

_routing:
  receiver_type: webhook
  webhook_url: https://hooks.example.com/alerts
  group_wait: "30s"
  repeat_interval: "6h"

_metadata:
  owner: backend-team
  tier: production`,
  },
  {
    id: 'analytics',
    name: () => t('即時分析堆棧', 'Real-time Analytics Stack'),
    icon: '📊',
    packs: ['clickhouse', 'kafka', 'jvm', 'kubernetes'],
    desc: () => t(
      '實時分析：Kafka 事件串流、ClickHouse OLAP 儲存、JVM 分析工作程式',
      'Real-time analytics: Kafka event streaming, ClickHouse OLAP storage, JVM analytics workers.'
    ),
    yaml: `# Real-time Analytics — ClickHouse + Kafka + JVM + K8s
clickhouse_queries_rate: "800"
clickhouse_queries_rate_critical: "1200"
clickhouse_active_connections: "150"

kafka_consumer_lag: "100000"
kafka_consumer_lag_critical: "500000"
kafka_broker_count: "5"
kafka_request_rate: "20000"

jvm_memory: "85"
jvm_gc_pause: "1.0"

container_cpu: "85"
container_memory: "90"

_routing:
  receiver_type: email
  group_wait: "2m"
  group_interval: "5m"
  repeat_interval: "24h"

_metadata:
  owner: analytics-team
  tier: production`,
  },
  {
    id: 'enterprise-db',
    name: () => t('企業資料庫叢集', 'Enterprise Database Cluster'),
    icon: '🏢',
    packs: ['oracle', 'db2', 'kubernetes'],
    desc: () => t(
      '企業級 DB：Oracle 用於核心交易、DB2 用於資料倉儲，搭配 K8s 容器化部署監控',
      'Enterprise DBs: Oracle for core transactions, DB2 for data warehouse, with K8s containerized deployment monitoring.'
    ),
    yaml: `# Enterprise Database — Oracle + DB2 + Kubernetes
oracle_sessions_active: "150"
oracle_sessions_active_critical: "250"
oracle_tablespace_used_percent: "80"
oracle_tablespace_used_percent_critical: "90"

db2_connections_active: "180"
db2_connections_active_critical: "250"
db2_bufferpool_hit_ratio: "0.95"

container_cpu: "80"
container_memory: "85"

_routing_profile: team-dba-global

_metadata:
  owner: dba-team
  tier: production
  domain: finance`,
  },
  {
    id: 'event-driven',
    name: () => t('事件驅動微服務', 'Event-Driven Microservices'),
    icon: '⚡',
    packs: ['rabbitmq', 'postgresql', 'jvm', 'kubernetes'],
    desc: () => t(
      '事件驅動架構：RabbitMQ 訊息佇列、PostgreSQL 事件溯源、JVM 微服務',
      'Event-driven architecture: RabbitMQ message queue, PostgreSQL event sourcing, JVM microservices.'
    ),
    yaml: `# Event-Driven Microservices — RabbitMQ + PostgreSQL + JVM + K8s
rabbitmq_queue_messages: "50000"
rabbitmq_queue_messages_critical: "100000"
rabbitmq_node_mem_percent: "75"
rabbitmq_connections: "800"
rabbitmq_consumers: "10"
rabbitmq_unacked_messages: "5000"

pg_connections: "120"
pg_replication_lag: "5"

jvm_memory: "75"
jvm_gc_pause: "0.3"
jvm_threads: "300"

container_cpu: "70"
container_memory: "80"

_routing:
  receiver_type: slack
  webhook_url: https://hooks.slack.com/services/xxx/yyy/zzz
  group_wait: "30s"
  repeat_interval: "4h"

_metadata:
  owner: microservices-team
  tier: production`,
  },
  {
    id: 'search-platform',
    name: () => t('搜尋平台', 'Search Platform'),
    icon: '🔍',
    packs: ['elasticsearch', 'nginx', 'jvm', 'kubernetes'],
    desc: () => t(
      '搜尋平台：Elasticsearch 叢集搜尋引擎、Nginx 負載均衡、JVM 索引服務',
      'Search platform: Elasticsearch cluster search engine, Nginx load balancer, JVM indexing services.'
    ),
    yaml: `# Search Platform — Elasticsearch + Nginx + JVM + K8s
es_jvm_memory_used_percent: "75"
es_jvm_memory_used_percent_critical: "88"
es_filesystem_free_percent: "20"
es_filesystem_free_percent_critical: "10"

nginx_connections: "5000"
nginx_request_rate: "10000"
nginx_waiting: "500"

jvm_memory: "80"
jvm_gc_pause: "0.5"

container_cpu: "80"
container_memory: "85"

_routing_profile: team-sre-apac

_metadata:
  owner: search-team
  tier: production`,
  },

  /* ── Single-pack quickstart templates ── */
  {
    id: 'quick-mariadb',
    name: () => t('快速入門：MariaDB', 'Quick Start: MariaDB'),
    icon: '🐬',
    packs: ['mariadb'],
    desc: () => t('最小 MariaDB 監控配置', 'Minimal MariaDB monitoring config.'),
    yaml: `# MariaDB Quick Start
mysql_connections: "80"
mysql_cpu: "80"

_routing:
  receiver_type: webhook
  webhook_url: https://hooks.example.com/alerts
  group_wait: "30s"
  repeat_interval: "4h"`,
  },
  {
    id: 'quick-postgresql',
    name: () => t('快速入門：PostgreSQL', 'Quick Start: PostgreSQL'),
    icon: '🐘',
    packs: ['postgresql'],
    desc: () => t('最小 PostgreSQL 監控配置', 'Minimal PostgreSQL monitoring config.'),
    yaml: `# PostgreSQL Quick Start
pg_connections: "80"
pg_replication_lag: "30"

_routing:
  receiver_type: webhook
  webhook_url: https://hooks.example.com/alerts
  group_wait: "30s"
  repeat_interval: "4h"`,
  },
  {
    id: 'quick-redis',
    name: () => t('快速入門：Redis', 'Quick Start: Redis'),
    icon: '🔴',
    packs: ['redis'],
    desc: () => t('最小 Redis 監控配置', 'Minimal Redis monitoring config.'),
    yaml: `# Redis Quick Start
redis_memory_used_bytes: "4294967296"
redis_connected_clients: "200"

_routing:
  receiver_type: webhook
  webhook_url: https://hooks.example.com/alerts
  group_wait: "30s"
  repeat_interval: "4h"`,
  },
  {
    id: 'quick-mongodb',
    name: () => t('快速入門：MongoDB', 'Quick Start: MongoDB'),
    icon: '🍃',
    packs: ['mongodb'],
    desc: () => t('最小 MongoDB 監控配置', 'Minimal MongoDB monitoring config.'),
    yaml: `# MongoDB Quick Start
mongodb_connections_current: "300"
mongodb_repl_lag_seconds: "10"

_routing:
  receiver_type: webhook
  webhook_url: https://hooks.example.com/alerts
  group_wait: "30s"
  repeat_interval: "4h"`,
  },
  {
    id: 'quick-oracle',
    name: () => t('快速入門：Oracle', 'Quick Start: Oracle'),
    icon: '🏛️',
    packs: ['oracle'],
    desc: () => t('最小 Oracle 監控配置', 'Minimal Oracle monitoring config.'),
    yaml: `# Oracle Quick Start
oracle_sessions_active: "200"
oracle_tablespace_used_percent: "85"

_routing_profile: team-dba-global

_metadata:
  owner: dba-team
  tier: production`,
  },
  {
    id: 'quick-db2',
    name: () => t('快速入門：DB2', 'Quick Start: DB2'),
    icon: '🔷',
    packs: ['db2'],
    desc: () => t('最小 DB2 監控配置', 'Minimal DB2 monitoring config.'),
    yaml: `# DB2 Quick Start
db2_connections_active: "200"
db2_bufferpool_hit_ratio: "0.95"

_routing_profile: team-dba-global

_metadata:
  owner: dba-team
  tier: production`,
  },
  {
    id: 'quick-kafka',
    name: () => t('快速入門：Kafka', 'Quick Start: Kafka'),
    icon: '📨',
    packs: ['kafka'],
    desc: () => t('最小 Kafka 監控配置', 'Minimal Kafka monitoring config.'),
    yaml: `# Kafka Quick Start
kafka_consumer_lag: "1000"
kafka_under_replicated_partitions: "0"
kafka_broker_count: "3"
kafka_active_controllers: "1"
kafka_request_rate: "10000"

_routing:
  receiver_type: webhook
  webhook_url: https://hooks.example.com/alerts
  group_wait: "1m"
  repeat_interval: "8h"`,
  },
  {
    id: 'quick-rabbitmq',
    name: () => t('快速入門：RabbitMQ', 'Quick Start: RabbitMQ'),
    icon: '🐰',
    packs: ['rabbitmq'],
    desc: () => t('最小 RabbitMQ 監控配置', 'Minimal RabbitMQ monitoring config.'),
    yaml: `# RabbitMQ Quick Start
rabbitmq_queue_messages: "100000"
rabbitmq_node_mem_percent: "80"
rabbitmq_connections: "1000"
rabbitmq_consumers: "5"
rabbitmq_unacked_messages: "10000"

_routing:
  receiver_type: webhook
  webhook_url: https://hooks.example.com/alerts
  group_wait: "30s"
  repeat_interval: "4h"`,
  },
  {
    id: 'quick-clickhouse',
    name: () => t('快速入門：ClickHouse', 'Quick Start: ClickHouse'),
    icon: '🖱️',
    packs: ['clickhouse'],
    desc: () => t('最小 ClickHouse 監控配置', 'Minimal ClickHouse monitoring config.'),
    yaml: `# ClickHouse Quick Start
clickhouse_queries_rate: "500"
clickhouse_active_connections: "200"

_routing:
  receiver_type: webhook
  webhook_url: https://hooks.example.com/alerts
  group_wait: "30s"
  repeat_interval: "4h"`,
  },
  {
    id: 'quick-nginx',
    name: () => t('快速入門：Nginx', 'Quick Start: Nginx'),
    icon: '🌐',
    packs: ['nginx'],
    desc: () => t('最小 Nginx 監控配置', 'Minimal Nginx monitoring config.'),
    yaml: `# Nginx Quick Start
nginx_connections: "1000"
nginx_request_rate: "5000"
nginx_waiting: "200"

_routing:
  receiver_type: webhook
  webhook_url: https://hooks.example.com/alerts
  group_wait: "30s"
  repeat_interval: "4h"`,
  },
  {
    id: 'quick-elasticsearch',
    name: () => t('快速入門：Elasticsearch', 'Quick Start: Elasticsearch'),
    icon: '🔎',
    packs: ['elasticsearch'],
    desc: () => t('最小 Elasticsearch 監控配置', 'Minimal Elasticsearch monitoring config.'),
    yaml: `# Elasticsearch Quick Start
es_jvm_memory_used_percent: "85"
es_filesystem_free_percent: "15"

_routing:
  receiver_type: webhook
  webhook_url: https://hooks.example.com/alerts
  group_wait: "30s"
  repeat_interval: "4h"`,
  },
  {
    id: 'quick-jvm',
    name: () => t('快速入門：JVM', 'Quick Start: JVM'),
    icon: '☕',
    packs: ['jvm'],
    desc: () => t('最小 JVM 監控配置', 'Minimal JVM monitoring config.'),
    yaml: `# JVM Quick Start
jvm_gc_pause: "0.5"
jvm_memory: "80"
jvm_threads: "500"

_routing:
  receiver_type: webhook
  webhook_url: https://hooks.example.com/alerts
  group_wait: "30s"
  repeat_interval: "4h"`,
  },
  {
    id: 'quick-kubernetes',
    name: () => t('快速入門：Kubernetes', 'Quick Start: Kubernetes'),
    icon: '⎈',
    packs: ['kubernetes'],
    desc: () => t('最小 Kubernetes 容器監控配置', 'Minimal Kubernetes container monitoring config.'),
    yaml: `# Kubernetes Quick Start
container_cpu: "80"
container_memory: "85"

_routing:
  receiver_type: webhook
  webhook_url: https://hooks.example.com/alerts
  group_wait: "30s"
  repeat_interval: "4h"`,
  },

  /* ── Special / advanced templates ── */
  {
    id: 'maintenance',
    name: () => t('維護窗口示例', 'Maintenance Window Demo'),
    icon: '🔧',
    packs: ['mariadb'],
    desc: () => t(
      '展示如何設定臨時維護窗口、自動過期和靜默模式',
      'Shows how to set up a temporary maintenance window with auto-expiry and silent mode.'
    ),
    yaml: `# Maintenance Window Demo
mysql_connections: "200"
mysql_cpu: "90"

_state_maintenance:
  expires: "2026-03-20T06:00:00Z"

_silent_mode:
  expires: "2026-03-15T12:00:00Z"

_routing:
  receiver_type: teams
  webhook_url: https://teams.example.com/webhook
  group_wait: "1m"
  repeat_interval: "12h"`,
  },
  {
    id: 'routing-profile',
    name: () => t('路由 Profile 示例 (ADR-007)', 'Routing Profile Demo (ADR-007)'),
    icon: '🗂️',
    packs: ['mariadb', 'kubernetes'],
    desc: () => t(
      '使用 _routing_profile 取代 inline _routing — 團隊共用路由設定、減少配置重複',
      'Use _routing_profile instead of inline _routing — share routing settings across tenants, reduce config duplication.'
    ),
    yaml: `# Routing Profile Demo (ADR-007)
mysql_connections: "80"
mysql_cpu: "70"
container_cpu: "80"
container_memory: "85"

_routing_profile: team-sre-apac

_metadata:
  runbook_url: https://runbooks.example.com/db-b
  owner: sre-apac
  tier: production
  domain: finance`,
  },
  {
    id: 'finance-compliance',
    name: () => t('金融合規模板', 'Finance Compliance Template'),
    icon: '🏦',
    packs: ['oracle', 'postgresql', 'kubernetes'],
    desc: () => t(
      '金融 domain：使用 PagerDuty（禁止 Slack/webhook）、嚴格閾值、合規 metadata',
      'Finance domain: PagerDuty required (Slack/webhook forbidden), strict thresholds, compliance metadata.'
    ),
    yaml: `# Finance Compliance — Oracle + PostgreSQL + K8s
oracle_sessions_active: "100"
oracle_sessions_active_critical: "150"
oracle_tablespace_used_percent: "75"
oracle_tablespace_used_percent_critical: "85"

pg_connections: "80"
pg_connections_critical: "120"
pg_replication_lag: "5"

container_cpu: "70"
container_memory: "80"

_routing_profile: domain-finance-tier1

_domain_policy: finance

_metadata:
  owner: finance-dba-team
  tier: production
  domain: finance
  compliance: SOX`,
  },
  {
    id: 'minimal',
    name: () => t('最小配置（2 行）', 'Minimal Config (2 Lines)'),
    icon: '✨',
    packs: ['mariadb'],
    desc: () => t('絕對最小配置 — 完美入門選擇。省略的 key 套用平台預設值。', 'The absolute minimum config — perfect for getting started. Omitted keys use platform defaults.'),
    yaml: `# Minimal — just thresholds, platform defaults handle the rest
mysql_connections: "100"
mysql_cpu: "80"`,
  },
];

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

const PackBadge = ({ id }) => {
  const pack = ALL_PACKS.find(p => p.id === id);
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

  const filtered = useMemo(() => {
    return TEMPLATES.filter(tpl => {
      // View mode filter
      if (viewMode === 'scenarios' && tpl.packs.length <= 1 && !['maintenance', 'routing-profile', 'finance-compliance', 'minimal'].includes(tpl.id)) return false;
      if (viewMode === 'quickstart' && (tpl.packs.length > 1 || ['maintenance', 'routing-profile', 'finance-compliance', 'minimal'].includes(tpl.id))) return false;

      // Pack filter
      if (packFilter && !tpl.packs.includes(packFilter)) return false;

      // Text search
      if (!filter) return true;
      const q = filter.toLowerCase();
      return tpl.name().toLowerCase().includes(q) ||
             tpl.desc().toLowerCase().includes(q) ||
             tpl.packs.some(p => p.toLowerCase().includes(q));
    });
  }, [filter, packFilter, viewMode]);

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
  }, []);

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
                  {tpl.packs.map(p => <PackBadge key={p} id={p} />)}
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
