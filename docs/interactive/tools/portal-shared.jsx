---
title: "Portal Shared Module"
tags: [self-service, shared, internal]
audience: ["platform-engineer"]
version: v2.5.0
lang: en
---

import React, { useState, useMemo, useCallback, useEffect, useRef } from 'react';

const t = window.__t || ((zh, en) => en);

/* ── Rule Pack catalog (from platform-data.json) ── */
const RULE_PACK_DATA = window.__platformData?.rulePacks || {
  mariadb: { label: 'MariaDB/MySQL', category: 'database', defaults: { mysql_connections: { value: 80, unit: 'count', desc: 'Max connections warning' }, mysql_cpu: { value: 80, unit: '%', desc: 'CPU threads rate warning' } }, metrics: ['connections', 'cpu', 'memory', 'slow_queries', 'replication_lag', 'query_errors'] },
  postgresql: { label: 'PostgreSQL', category: 'database', defaults: { pg_connections: { value: 80, unit: '%', desc: 'Connection usage warning' }, pg_replication_lag: { value: 30, unit: 'seconds', desc: 'Replication lag warning' } }, metrics: ['connections', 'cache_hit', 'query_time', 'disk_usage', 'replication_lag'] },
  redis: { label: 'Redis', category: 'database', defaults: { redis_memory_used_bytes: { value: 4294967296, unit: 'bytes', desc: 'Memory usage warning' }, redis_connected_clients: { value: 200, unit: 'count', desc: 'Connected clients warning' } }, metrics: ['memory', 'evictions', 'connected_clients', 'keyspace_hits'] },
  mongodb: { label: 'MongoDB', category: 'database', defaults: { mongodb_connections_current: { value: 300, unit: 'count', desc: 'Current connections warning' }, mongodb_repl_lag_seconds: { value: 10, unit: 'seconds', desc: 'Replication lag warning' } }, metrics: ['connections', 'memory', 'page_faults', 'replication'] },
  elasticsearch: { label: 'Elasticsearch', category: 'database', defaults: { es_jvm_memory_used_percent: { value: 85, unit: '%', desc: 'JVM heap usage warning' }, es_filesystem_free_percent: { value: 15, unit: '%', desc: 'Disk free space warning' } }, metrics: ['heap', 'unassigned_shards', 'cluster_health', 'indexing_rate'] },
  oracle: { label: 'Oracle', category: 'database', defaults: { oracle_sessions_active: { value: 200, unit: 'count', desc: 'Active sessions warning' }, oracle_tablespace_used_percent: { value: 85, unit: '%', desc: 'Tablespace usage warning' } }, metrics: ['sessions', 'tablespace', 'wait_events', 'redo_log'] },
  db2: { label: 'DB2', category: 'database', defaults: { db2_connections_active: { value: 200, unit: 'count', desc: 'Active connections warning' }, db2_bufferpool_hit_ratio: { value: 0.95, unit: 'ratio', desc: 'Bufferpool hit ratio warning' } }, metrics: ['connections', 'bufferpool', 'tablespace', 'lock_waits'] },
  clickhouse: { label: 'ClickHouse', category: 'database', defaults: { clickhouse_queries_rate: { value: 500, unit: 'qps', desc: 'Query rate warning' }, clickhouse_active_connections: { value: 200, unit: 'count', desc: 'Active connections warning' } }, metrics: ['queries', 'merges', 'replicated_lag', 'memory'] },
  kafka: { label: 'Kafka', category: 'messaging', defaults: { kafka_consumer_lag: { value: 1000, unit: 'messages', desc: 'Consumer lag warning' }, kafka_under_replicated_partitions: { value: 0, unit: 'count', desc: 'Under-replicated partitions' }, kafka_broker_count: { value: 3, unit: 'count', desc: 'Min broker count' }, kafka_active_controllers: { value: 1, unit: 'count', desc: 'Min active controllers' }, kafka_request_rate: { value: 10000, unit: 'msg/s', desc: 'Message rate warning' } }, metrics: ['consumer_lag', 'broker_active', 'controller', 'isr_shrink', 'under_replicated'] },
  rabbitmq: { label: 'RabbitMQ', category: 'messaging', defaults: { rabbitmq_queue_messages: { value: 100000, unit: 'messages', desc: 'Queue depth warning' }, rabbitmq_node_mem_percent: { value: 80, unit: '%', desc: 'Node memory usage warning' }, rabbitmq_connections: { value: 1000, unit: 'count', desc: 'Connection count warning' }, rabbitmq_consumers: { value: 5, unit: 'count', desc: 'Min consumer count' }, rabbitmq_unacked_messages: { value: 10000, unit: 'messages', desc: 'Unacked messages warning' } }, metrics: ['queue_depth', 'consumers', 'memory', 'disk_free', 'connections'] },
  jvm: { label: 'JVM', category: 'runtime', defaults: { jvm_gc_pause: { value: 0.5, unit: 'seconds/5m', desc: 'GC pause duration warning' }, jvm_memory: { value: 80, unit: '%', desc: 'Heap usage warning' }, jvm_threads: { value: 500, unit: 'count', desc: 'Active thread count warning' } }, metrics: ['gc_pause', 'heap_usage', 'thread_pool', 'class_loading'] },
  nginx: { label: 'Nginx', category: 'webserver', defaults: { nginx_connections: { value: 1000, unit: 'count', desc: 'Active connections warning' }, nginx_request_rate: { value: 5000, unit: 'req/s', desc: 'Request rate warning' }, nginx_waiting: { value: 200, unit: 'count', desc: 'Waiting connections warning' } }, metrics: ['active_connections', 'request_rate', 'connection_backlog'] },
  kubernetes: { label: 'Kubernetes', category: 'infrastructure', defaults: { container_cpu: { value: 80, unit: '%', desc: 'Container CPU % of limit' }, container_memory: { value: 85, unit: '%', desc: 'Container memory % of limit' } }, metrics: ['pod_restart', 'cpu_limit', 'memory_limit', 'pvc_usage'] },
  operational: { label: 'Operational', category: 'infrastructure', required: true, defaults: {}, metrics: ['exporter_health', 'config_reload'] },
  platform: { label: 'Platform', category: 'infrastructure', required: true, defaults: {}, metrics: ['threshold_metric_count', 'recording_rule_health', 'scrape_success'] },
};

const CATEGORY_LABELS = {
  database: () => t('資料庫', 'Databases'),
  messaging: () => t('訊息佇列', 'Messaging'),
  runtime: () => t('運行環境', 'Runtime'),
  webserver: () => t('網頁伺服器', 'Web Servers'),
  infrastructure: () => t('基礎設施', 'Infrastructure'),
};

/* ── Reserved keys and validation constants ── */
const RESERVED_KEYS = new Set([
  '_silent_mode', '_namespaces', '_metadata', '_profile',
  '_routing_defaults', '_routing_profile', '_domain_policy', '_instance_mapping'
]);
const RESERVED_PREFIXES = ['_state_', '_routing'];
const RECEIVER_TYPES = ['webhook', 'email', 'slack', 'teams', 'rocketchat', 'pagerduty'];

/* ── Routing Profiles (ADR-007) ── */
const ROUTING_PROFILES = {
  'team-sre-apac': {
    receiver_type: 'slack', group_wait: '30s', group_interval: '5m', repeat_interval: '4h',
  },
  'team-dba-global': {
    receiver_type: 'webhook', group_wait: '1m', group_interval: '10m', repeat_interval: '8h',
  },
  'domain-finance-tier1': {
    receiver_type: 'pagerduty', group_wait: '30s', group_interval: '5m', repeat_interval: '1h',
  },
};

/* ── Domain Policies (ADR-007) ── */
const DOMAIN_POLICIES = {
  finance: {
    description: 'Finance domain compliance',
    tenants: ['db-a', 'db-b'],
    constraints: {
      allowed_receiver_types: ['pagerduty', 'email', 'opsgenie'],
      forbidden_receiver_types: ['slack', 'webhook'],
      max_repeat_interval: '1h',
    },
  },
  ecommerce: {
    description: 'E-commerce domain standards',
    tenants: ['db-c', 'db-d'],
    constraints: {
      allowed_receiver_types: ['slack', 'pagerduty', 'email', 'webhook'],
      max_repeat_interval: '12h',
    },
  },
};
const RECEIVER_REQUIRED = {
  webhook: ['url'], email: ['to', 'smarthost'], slack: ['api_url'],
  teams: ['webhook_url'], rocketchat: ['url'], pagerduty: ['service_key'],
};
const TIMING_GUARDRAILS = {
  group_wait: { min: 5, max: 300, unit: 's' },
  group_interval: { min: 5, max: 300, unit: 's' },
  repeat_interval: { min: 60, max: 259200, unit: 's' },
};

/* ── Duration parser ── */
function parseDuration(str) {
  if (!str) return null;
  const m = String(str).match(/^(\d+\.?\d*)([smhd])$/);
  if (!m) return null;
  const multi = { s: 1, m: 60, h: 3600, d: 86400 };
  return parseFloat(m[1]) * (multi[m[2]] || 1);
}

/* ── Simple YAML parser (for tenant config subset) ── */
const UNSAFE_KEYS = new Set(['__proto__', 'constructor', 'prototype']);
const MAX_YAML_SIZE = 100000;

function parseYaml(text) {
  const errors = [];
  if (text.length > MAX_YAML_SIZE) {
    return { config: {}, errors: [t('YAML 超過大小限制（100KB）', 'YAML exceeds size limit (100KB)')] };
  }
  const config = {};
  let currentKey = null;
  let currentObj = null;

  const lines = text.split('\n');
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const trimmed = line.replace(/\s+#(?![^"']*["'][^"']*$).*$/, '').trimEnd();
    if (!trimmed || trimmed.trim() === '') continue;

    const lineIndent = line.search(/\S/);
    const content = trimmed.trim();

    const kvMatch = content.match(/^([^:]+?):\s+(.+)$/);
    const objMatch = content.match(/^([^:]+?):\s*$/);

    if (lineIndent === 0 && kvMatch) {
      const key = kvMatch[1].trim();
      if (UNSAFE_KEYS.has(key)) continue;
      let val = kvMatch[2].trim();
      if (val.startsWith('"') && val.endsWith('"')) val = val.slice(1, -1);
      if (val.startsWith("'") && val.endsWith("'")) val = val.slice(1, -1);
      if (val.startsWith('[') && val.endsWith(']')) {
        val = val.slice(1, -1).split(',').map(s => s.trim().replace(/"/g, '').replace(/'/g, ''));
      }
      config[key] = val;
      currentKey = null;
      currentObj = null;
    } else if (lineIndent === 0 && objMatch) {
      const key = objMatch[1].trim();
      if (UNSAFE_KEYS.has(key)) continue;
      config[key] = {};
      currentKey = key;
      currentObj = config[key];
    } else if (currentKey && lineIndent > 0 && kvMatch) {
      const key = kvMatch[1].trim();
      let val = kvMatch[2].trim();
      if (val.startsWith('"') && val.endsWith('"')) val = val.slice(1, -1);
      if (val.startsWith("'") && val.endsWith("'")) val = val.slice(1, -1);
      if (val.startsWith('[') && val.endsWith(']')) {
        val = val.slice(1, -1).split(',').map(s => s.trim().replace(/"/g, '').replace(/'/g, ''));
      }

      if (currentKey === '_routing' || currentKey === '_metadata') {
        const depth = Math.floor(lineIndent / 2) - 1;
        if (depth === 0) {
          currentObj[key] = val;
        } else if (depth === 1 && typeof currentObj[Object.keys(currentObj).pop()] === 'object') {
          const parentKey = Object.keys(currentObj).pop();
          if (typeof currentObj[parentKey] === 'object') {
            currentObj[parentKey][key] = val;
          }
        }
      }
    } else if (currentKey && lineIndent > 0 && objMatch) {
      const key = objMatch[1].trim();
      if (currentObj) {
        currentObj[key] = {};
      }
    }
  }
  return { config, errors };
}

/* ── Collect all known metric keys from Rule Pack data ── */
function getAllMetricKeys(selectedPacks) {
  const keys = [];
  const packs = selectedPacks && selectedPacks.length > 0
    ? selectedPacks
    : Object.keys(RULE_PACK_DATA);
  for (const packId of packs) {
    const pack = RULE_PACK_DATA[packId];
    if (!pack || !pack.defaults) continue;
    for (const [key, meta] of Object.entries(pack.defaults)) {
      keys.push({ key, pack: packId, label: pack.label, ...meta });
    }
  }
  return keys;
}

/* ── Generate sample YAML from selected Rule Packs ── */
function generateSampleYaml(selectedPacks, withProfile) {
  const lines = [`# ${t('從 Rule Pack 自動產生的 Tenant YAML', 'Auto-generated tenant YAML from Rule Packs')}`];
  for (const packId of selectedPacks) {
    const pack = RULE_PACK_DATA[packId];
    if (!pack || !pack.defaults || Object.keys(pack.defaults).length === 0) continue;
    lines.push(`\n# --- ${pack.label} ---`);
    for (const [key, meta] of Object.entries(pack.defaults)) {
      lines.push(`${key}: "${meta.value}"  # ${meta.desc}`);
    }
  }
  lines.push('');
  if (withProfile) {
    lines.push('_routing_profile: team-sre-apac');
  } else {
    lines.push(`_routing:`);
    lines.push(`  receiver_type: webhook`);
    lines.push(`  webhook_url: https://hooks.example.com/alerts`);
    lines.push(`  group_by: [alertname, severity]`);
    lines.push(`  group_wait: "30s"`);
    lines.push(`  repeat_interval: "4h"`);
  }
  lines.push('');
  lines.push(`_metadata:`);
  lines.push(`  runbook_url: https://runbooks.example.com/my-tenant`);
  lines.push(`  owner: platform-team`);
  lines.push(`  tier: production`);
  return lines.join('\n');
}

/* ── Validation engine ── */
function validateConfig(config, selectedPacks) {
  const issues = [];
  const info = [];
  const knownMetrics = new Set(getAllMetricKeys(selectedPacks).map(m => m.key));

  for (const [key, val] of Object.entries(config)) {
    if (key.startsWith('_')) continue;
    if (val === 'disable') {
      info.push({ level: 'info', field: key,
        msg: t('已禁用此指標', 'Metric disabled') });
      continue;
    }
    const numVal = parseFloat(val);
    if (!isNaN(numVal)) {
      const LARGE_VALUE_PATTERNS = ['bytes', 'connections', 'lag', 'rate', 'messages',
        'threads', 'count', 'queue', 'consumers', 'controllers', 'broker', 'sessions',
        'partitions', 'queries', 'waiting'];
      const isLargeValueMetric = LARGE_VALUE_PATTERNS.some(p => key.includes(p));
      if (numVal > 100 && !isLargeValueMetric) {
        issues.push({ level: 'warning', field: key,
          msg: t(`閾值 ${numVal} 超過 100，確認是否正確`, `Threshold ${numVal} exceeds 100, verify if correct`) });
      }
      if (numVal < 0) {
        issues.push({ level: 'error', field: key,
          msg: t(`閾值 ${numVal} < 0，無效`, `Threshold ${numVal} < 0, invalid`) });
      }
    }
    if (selectedPacks && selectedPacks.length > 0 && knownMetrics.size > 0) {
      const isCriticalVariant = key.endsWith('_critical');
      const baseKey = isCriticalVariant ? key.replace(/_critical$/, '') : key;
      if (!knownMetrics.has(baseKey) && !knownMetrics.has(key)) {
        issues.push({ level: 'info', field: key,
          msg: t(`此 metric key 不在已選 Rule Pack 的預設清單中`, `Metric key not found in selected Rule Pack defaults`) });
      }
    }
  }

  const routing = config._routing;
  if (routing && typeof routing === 'object') {
    const rtype = routing.receiver_type;
    if (rtype && !RECEIVER_TYPES.includes(rtype)) {
      issues.push({ level: 'error', field: '_routing.receiver_type',
        msg: t(`不支援的 receiver 類型: ${rtype}`, `Unsupported receiver type: ${rtype}`) });
    }
    const webhookUrl = routing.webhook_url;
    if (rtype === 'webhook' && webhookUrl) {
      try {
        const parsed = new URL(webhookUrl);
        if (!['http:', 'https:'].includes(parsed.protocol)) {
          issues.push({ level: 'error', field: '_routing.webhook_url',
            msg: t('僅允許 http/https URL', 'Only http/https URLs allowed') });
        } else if (parsed.protocol !== 'https:') {
          issues.push({ level: 'error', field: '_routing.webhook_url',
            msg: t('生產環境必須使用 HTTPS — HTTP 會導致告警通知明文傳輸', 'Production requires HTTPS — HTTP transmits alert notifications in plaintext') });
        }
      } catch (e) {
        issues.push({ level: 'error', field: '_routing.webhook_url',
          msg: t('無效的 URL 格式', 'Invalid URL format') });
      }
    }
    for (const [param, guard] of Object.entries(TIMING_GUARDRAILS)) {
      const val = routing[param];
      if (val) {
        const secs = parseDuration(val);
        if (secs !== null) {
          if (secs < guard.min) {
            issues.push({ level: 'warning', field: `_routing.${param}`,
              msg: t(`${val} 低於下限 ${guard.min}s`, `${val} below minimum ${guard.min}s`) });
          }
          if (secs > guard.max) {
            issues.push({ level: 'warning', field: `_routing.${param}`,
              msg: t(`${val} 超過上限 ${guard.max}s`, `${val} exceeds maximum ${guard.max}s`) });
          }
        }
      }
    }
  }

  const profileRef = config._routing_profile;
  if (profileRef) {
    if (!ROUTING_PROFILES[profileRef]) {
      issues.push({ level: 'error', field: '_routing_profile',
        msg: t(`路由 profile "${profileRef}" 不存在`, `Routing profile "${profileRef}" not found`) });
    } else {
      info.push({ level: 'info', field: '_routing_profile',
        msg: t(`使用路由 profile: ${profileRef}`, `Using routing profile: ${profileRef}`) });
    }
  }

  const resolvedReceiverType = routing
    ? (routing.receiver_type || (profileRef && ROUTING_PROFILES[profileRef]?.receiver_type))
    : (profileRef && ROUTING_PROFILES[profileRef]?.receiver_type);
  if (resolvedReceiverType) {
    for (const [domain, policy] of Object.entries(DOMAIN_POLICIES)) {
      const constraints = policy.constraints || {};
      if (constraints.forbidden_receiver_types?.includes(resolvedReceiverType)) {
        issues.push({ level: 'warning', field: '_domain_policy',
          msg: t(`domain "${domain}" 禁止使用 ${resolvedReceiverType}`,
                 `Domain "${domain}" forbids receiver type: ${resolvedReceiverType}`) });
      }
      if (constraints.allowed_receiver_types &&
          !constraints.allowed_receiver_types.includes(resolvedReceiverType)) {
        issues.push({ level: 'warning', field: '_domain_policy',
          msg: t(`domain "${domain}" 不允許 ${resolvedReceiverType}`,
                 `Domain "${domain}" does not allow receiver type: ${resolvedReceiverType}`) });
      }
    }
  }

  if (config._metadata && typeof config._metadata === 'object') {
    if (!config._metadata.runbook_url) {
      issues.push({ level: 'info', field: '_metadata.runbook_url',
        msg: t('建議配置 runbook URL', 'Consider adding runbook URL') });
    }
  }

  for (const key of Object.keys(config)) {
    if (key.startsWith('_')) {
      if (!RESERVED_KEYS.has(key) && !RESERVED_PREFIXES.some(p => key.startsWith(p))) {
        issues.push({ level: 'warning', field: key,
          msg: t(`未知的保留字 key: ${key}`, `Unknown reserved key: ${key}`) });
      }
    }
  }

  return { issues: [...issues, ...info] };
}

/* ── Alert simulation engine (multi-metric) ── */
function simulateAlerts(config, metricValues) {
  const alerts = [];

  for (const [metric, val] of Object.entries(metricValues)) {
    const threshold = config[metric];
    if (!threshold || threshold === 'disable') {
      alerts.push({
        metric, current: val.current, threshold: null, critical_threshold: null,
        firing: false, critical_firing: false, severity: threshold === 'disable' ? 'disabled' : 'no-threshold',
        unit: val.unit, packLabel: val.packLabel,
      });
      continue;
    }

    const thresholdNum = parseFloat(threshold);
    if (isNaN(thresholdNum)) continue;

    const currentVal = val.current;
    const firing = currentVal >= thresholdNum;

    const critKey = `${metric}_critical`;
    const critThreshold = config[critKey] ? parseFloat(config[critKey]) : null;
    const critFiring = critThreshold !== null && currentVal >= critThreshold;

    alerts.push({
      metric, current: currentVal, threshold: thresholdNum,
      critical_threshold: critThreshold, firing, critical_firing: critFiring,
      severity: critFiring ? 'critical' : (firing ? 'warning' : 'ok'),
      unit: val.unit, packLabel: val.packLabel,
    });
  }

  return alerts;
}

/* ── Four-layer routing resolver (ADR-007) ── */
const ROUTING_DEFAULTS = {
  receiver_type: 'webhook',
  group_by: ['alertname', 'tenant'],
  group_wait: '30s',
  group_interval: '5m',
  repeat_interval: '4h',
};

function resolveRoutingLayers(config) {
  const layers = [];

  const L1 = { ...ROUTING_DEFAULTS };
  layers.push({
    layer: 1, name: '_routing_defaults',
    label: t('平台預設', 'Platform Defaults'),
    values: { ...L1 }, overrides: {}, source: 'platform',
  });

  const profileRef = config._routing_profile;
  const profile = profileRef ? ROUTING_PROFILES[profileRef] : null;
  const L2 = { ...L1 };
  const L2overrides = {};
  if (profile) {
    for (const [k, v] of Object.entries(profile)) {
      if (v !== undefined && v !== L2[k]) {
        L2overrides[k] = { from: L2[k], to: v };
        L2[k] = v;
      }
    }
  }
  layers.push({
    layer: 2,
    name: profileRef ? `routing_profiles[${profileRef}]` : t('（未指定 profile）', '(no profile)'),
    label: t('路由 Profile', 'Routing Profile'),
    values: { ...L2 }, overrides: L2overrides,
    source: profileRef ? 'profile' : 'skip',
  });

  const routing = config._routing;
  const L3 = { ...L2 };
  const L3overrides = {};
  if (routing && typeof routing === 'object') {
    for (const [k, v] of Object.entries(routing)) {
      if (v !== undefined && k !== 'overrides' && v !== L3[k]) {
        L3overrides[k] = { from: L3[k], to: v };
        L3[k] = v;
      }
    }
  }
  layers.push({
    layer: 3, name: '_routing',
    label: t('租戶覆蓋', 'Tenant Override'),
    values: { ...L3 }, overrides: L3overrides,
    source: routing ? 'tenant' : 'skip',
  });

  layers.push({
    layer: 4, name: '_routing_enforced',
    label: t('平台強制 (NOC)', 'Platform Enforced (NOC)'),
    values: { ...L3 }, overrides: {}, source: 'enforced',
  });

  return { layers, resolved: L3 };
}

/* ── Metric key autocomplete dropdown ── */
function MetricAutocomplete({ allMetrics, onInsert }) {
  const [query, setQuery] = useState('');
  const [open, setOpen] = useState(false);
  const ref = useRef(null);

  const filtered = useMemo(() => {
    if (!query) return allMetrics.slice(0, 15);
    const q = query.toLowerCase();
    return allMetrics.filter(m =>
      m.key.toLowerCase().includes(q) || m.label.toLowerCase().includes(q)
    ).slice(0, 15);
  }, [query, allMetrics]);

  useEffect(() => {
    const handler = (e) => {
      if (ref.current && !ref.current.contains(e.target)) setOpen(false);
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, []);

  return (
    <div ref={ref} className="relative">
      <div className="flex gap-2 items-center">
        <input
          type="text"
          value={query}
          onChange={(e) => { setQuery(e.target.value); setOpen(true); }}
          onFocus={() => setOpen(true)}
          placeholder={t('搜尋 metric key...', 'Search metric key...')}
          className="flex-1 text-sm px-3 py-1.5 border rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
        />
      </div>
      {open && filtered.length > 0 && (
        <div className="absolute z-10 w-full mt-1 bg-white border rounded-lg shadow-lg max-h-48 overflow-y-auto">
          {filtered.map((m, i) => (
            <button
              key={i}
              onClick={() => { onInsert(m); setQuery(''); setOpen(false); }}
              className="w-full text-left px-3 py-2 hover:bg-blue-50 text-sm border-b border-gray-50 last:border-0"
            >
              <code className="font-mono text-blue-700">{m.key}</code>
              <span className="ml-2 text-gray-400 text-xs">{m.label}</span>
              {m.desc && <span className="ml-1 text-gray-400 text-xs">— {m.desc}</span>}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

/* ── Rule Pack multi-select ── */
function RulePackSelector({ selected, onChange }) {
  const grouped = useMemo(() => {
    const groups = {};
    for (const [id, pack] of Object.entries(RULE_PACK_DATA)) {
      const cat = pack.category || 'other';
      if (!groups[cat]) groups[cat] = [];
      groups[cat].push({ id, ...pack });
    }
    return groups;
  }, []);

  const toggle = (id) => {
    if (RULE_PACK_DATA[id]?.required) return;
    const next = selected.includes(id)
      ? selected.filter(x => x !== id)
      : [...selected, id];
    onChange(next);
  };

  return (
    <div className="space-y-2">
      {Object.entries(grouped).map(([cat, packs]) => (
        <div key={cat}>
          <div className="text-xs font-medium text-gray-500 uppercase tracking-wide mb-1">
            {CATEGORY_LABELS[cat] ? CATEGORY_LABELS[cat]() : cat}
          </div>
          <div className="flex flex-wrap gap-1.5">
            {packs.map(p => {
              const isSelected = selected.includes(p.id);
              const isRequired = p.required;
              return (
                <button
                  key={p.id}
                  onClick={() => toggle(p.id)}
                  disabled={isRequired}
                  className={`text-xs px-2.5 py-1 rounded-full border transition-colors ${
                    isRequired
                      ? 'bg-gray-100 text-gray-400 border-gray-200 cursor-not-allowed'
                      : isSelected
                        ? 'bg-blue-100 text-blue-800 border-blue-300 hover:bg-blue-200'
                        : 'bg-white text-gray-600 border-gray-200 hover:bg-gray-50'
                  }`}
                  title={isRequired ? t('必選（自動啟用）', 'Required (auto-enabled)') : ''}
                >
                  {isSelected && !isRequired && <span className="mr-1">&#10003;</span>}
                  {isRequired && <span className="mr-1">&#128274;</span>}
                  {p.label}
                  {p.defaults && Object.keys(p.defaults).length > 0 && (
                    <span className="ml-1 text-gray-400">({Object.keys(p.defaults).length})</span>
                  )}
                </button>
              );
            })}
          </div>
        </div>
      ))}
    </div>
  );
}

/* ── Register all exports on window for dependency loading ── */
window.__portalShared = {
  // Data constants
  RULE_PACK_DATA,
  CATEGORY_LABELS,
  RESERVED_KEYS,
  RESERVED_PREFIXES,
  RECEIVER_TYPES,
  ROUTING_PROFILES,
  DOMAIN_POLICIES,
  RECEIVER_REQUIRED,
  TIMING_GUARDRAILS,
  ROUTING_DEFAULTS,
  UNSAFE_KEYS,
  MAX_YAML_SIZE,
  // Utility functions
  parseDuration,
  parseYaml,
  getAllMetricKeys,
  generateSampleYaml,
  validateConfig,
  simulateAlerts,
  resolveRoutingLayers,
  // React components
  MetricAutocomplete,
  RulePackSelector,
};
