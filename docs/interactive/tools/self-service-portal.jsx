---
title: "Tenant Self-Service Portal"
tags: [self-service, validation, routing, alerts, tenant]
audience: ["platform-engineer", "domain-expert", "tenant"]
version: v2.2.0
lang: en
related: [playground, config-lint, alert-simulator, schema-explorer]
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

/* parseYaml — lightweight regex-based YAML parser for browser-side validation.
 * Limitation: handles flat/single-nested YAML used in tenant config files.
 * Does NOT handle multi-line strings, anchors, or complex nesting.
 * URLs with colons (e.g. https://...) are handled by splitting on first ": ". */
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
    // Strip comments but not inside quoted strings (simple heuristic)
    const trimmed = line.replace(/\s+#(?![^"']*["'][^"']*$).*$/, '').trimEnd();
    if (!trimmed || trimmed.trim() === '') continue;

    const lineIndent = line.search(/\S/);
    const content = trimmed.trim();

    // Split on first ": " (colon+space) to handle URLs with colons
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
      // Metrics with large natural values (counts, bytes, rates) should not warn on >100
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
    // Check if metric is recognized from selected packs
    if (selectedPacks && selectedPacks.length > 0 && knownMetrics.size > 0) {
      const isCriticalVariant = key.endsWith('_critical');
      const baseKey = isCriticalVariant ? key.replace(/_critical$/, '') : key;
      if (!knownMetrics.has(baseKey) && !knownMetrics.has(key)) {
        issues.push({ level: 'info', field: key,
          msg: t(`此 metric key 不在已選 Rule Pack 的預設清單中`, `Metric key not found in selected Rule Pack defaults`) });
      }
    }
  }

  // Check routing
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

  // Check routing profile reference (ADR-007)
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

  // Check domain policy constraints (ADR-007)
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

  // Layer 1: _routing_defaults
  const L1 = { ...ROUTING_DEFAULTS };
  layers.push({
    layer: 1, name: '_routing_defaults',
    label: t('平台預設', 'Platform Defaults'),
    values: { ...L1 }, overrides: {}, source: 'platform',
  });

  // Layer 2: routing_profiles
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

  // Layer 3: tenant _routing
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

  // Layer 4: _routing_enforced (NOC)
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

/* ══════════════════════════════════════════════
   Tab 1: YAML Validator (with Rule Pack selector + autocomplete)
   ══════════════════════════════════════════════ */
function YamlValidatorTab() {
  const [selectedPacks, setSelectedPacks] = useState(['mariadb', 'kubernetes']);
  const [yaml, setYaml] = useState('');
  const [result, setResult] = useState(null);

  // Generate initial YAML from selected packs
  useEffect(() => {
    if (!yaml) {
      setYaml(generateSampleYaml(selectedPacks, false));
    }
  }, []);

  const allMetrics = useMemo(() => getAllMetricKeys(selectedPacks), [selectedPacks]);

  const validate = useCallback(() => {
    const { config, errors } = parseYaml(yaml);
    const validation = validateConfig(config, selectedPacks);
    setResult({ config, parseErrors: errors, ...validation });
  }, [yaml, selectedPacks]);

  const handleInsertMetric = useCallback((m) => {
    const line = `${m.key}: "${m.value}"  # ${m.desc || ''}`;
    setYaml(prev => {
      const lines = prev.split('\n');
      // Insert before first _ key or at end of metric section
      let insertIdx = lines.length;
      for (let i = 0; i < lines.length; i++) {
        const trimmed = lines[i].trim();
        if (trimmed.startsWith('_') && !trimmed.startsWith('#')) {
          insertIdx = i;
          break;
        }
      }
      lines.splice(insertIdx, 0, line);
      return lines.join('\n');
    });
  }, []);

  const issueIcon = (level) => {
    if (level === 'error') return '🔴';
    if (level === 'warning') return '🟡';
    return '🔵';
  };

  return (
    <div>
      <h3 className="text-lg font-semibold mb-3">
        {t('YAML 驗證', 'YAML Validation')}
      </h3>
      <p className="text-sm text-gray-600 mb-4">
        {t('選擇 Rule Pack，自動帶入 metric key。貼入或編輯 tenant YAML，即時檢查 schema、routing、policy 問題。',
           'Select Rule Packs for metric key suggestions. Paste or edit tenant YAML to check schema, routing, and policy issues.')}
      </p>

      {/* Rule Pack Selector */}
      <div className="mb-4 p-3 bg-gray-50 rounded-lg border">
        <div className="text-sm font-medium text-gray-700 mb-2">
          {t('選擇 Rule Pack', 'Select Rule Packs')}
          <span className="ml-2 text-gray-400 text-xs font-normal">
            {t(`已選 ${selectedPacks.length} 個`, `${selectedPacks.length} selected`)}
          </span>
        </div>
        <RulePackSelector selected={selectedPacks} onChange={setSelectedPacks} />
      </div>

      {/* Metric autocomplete */}
      <div className="mb-3">
        <div className="text-sm font-medium text-gray-700 mb-1">
          {t('插入 Metric Key', 'Insert Metric Key')}
        </div>
        <MetricAutocomplete allMetrics={allMetrics} onInsert={handleInsertMetric} />
      </div>

      {/* Quick actions */}
      <div className="flex gap-2 mb-2">
        <button
          onClick={() => setYaml(generateSampleYaml(selectedPacks, false))}
          className="text-xs px-2 py-1 bg-gray-200 hover:bg-gray-300 rounded"
        >{t('產生範例：直接 routing', 'Generate: Direct routing')}</button>
        <button
          onClick={() => setYaml(generateSampleYaml(selectedPacks, true))}
          className="text-xs px-2 py-1 bg-gray-200 hover:bg-gray-300 rounded"
        >{t('產生範例：Routing Profile', 'Generate: Routing Profile')}</button>
      </div>

      <textarea
        value={yaml}
        onChange={(e) => setYaml(e.target.value)}
        className="w-full h-64 font-mono text-sm p-3 border rounded-lg bg-gray-50 focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
        placeholder={t('貼入 tenant YAML...', 'Paste tenant YAML...')}
      />

      <button
        onClick={validate}
        className="mt-3 px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 transition-colors"
      >
        {t('驗證', 'Validate')}
      </button>

      {result && (
        <div className="mt-4 space-y-2">
          {result.issues.length === 0 ? (
            <div className="p-3 bg-green-50 border border-green-200 rounded-lg text-green-800">
              {t('✓ 所有檢查通過', '✓ All checks passed')}
            </div>
          ) : (
            result.issues.map((issue, i) => (
              <div key={i} className={`p-3 rounded-lg border ${
                issue.level === 'error' ? 'bg-red-50 border-red-200 text-red-800' :
                issue.level === 'warning' ? 'bg-yellow-50 border-yellow-200 text-yellow-800' :
                'bg-blue-50 border-blue-200 text-blue-800'
              }`}>
                <span className="mr-2">{issueIcon(issue.level)}</span>
                <code className="text-xs font-mono bg-white bg-opacity-50 px-1 rounded">{issue.field}</code>
                <span className="ml-2">{issue.msg}</span>
              </div>
            ))
          )}

          <div className="mt-3 p-3 bg-gray-50 rounded-lg">
            <span className="text-sm font-medium text-gray-700">
              {t(`共 ${result.issues.filter(i => i.level === 'error').length} 錯誤、` +
                 `${result.issues.filter(i => i.level === 'warning').length} 警告、` +
                 `${result.issues.filter(i => i.level === 'info').length} 建議`,
                 `${result.issues.filter(i => i.level === 'error').length} errors, ` +
                 `${result.issues.filter(i => i.level === 'warning').length} warnings, ` +
                 `${result.issues.filter(i => i.level === 'info').length} info`)}
            </span>
          </div>
        </div>
      )}
    </div>
  );
}

/* ══════════════════════════════════════════════
   Tab 2: Alert Preview (multi-metric simultaneous, Rule-Pack-aware)
   ══════════════════════════════════════════════ */
function AlertPreviewTab() {
  const [selectedPacks, setSelectedPacks] = useState(['mariadb', 'kubernetes']);
  const [yaml, setYaml] = useState('');
  const [metrics, setMetrics] = useState({});
  const [alerts, setAlerts] = useState(null);

  // Build metric sliders from selected packs
  const packMetrics = useMemo(() => {
    const result = {};
    for (const packId of selectedPacks) {
      const pack = RULE_PACK_DATA[packId];
      if (!pack || !pack.defaults) continue;
      for (const [key, meta] of Object.entries(pack.defaults)) {
        result[key] = {
          current: meta.value * 0.9,
          threshold: meta.value,
          unit: meta.unit,
          packLabel: pack.label,
          packId,
        };
      }
    }
    return result;
  }, [selectedPacks]);

  // Initialize YAML and metrics when packs change
  useEffect(() => {
    setYaml(generateSampleYaml(selectedPacks, false));
    const initMetrics = {};
    for (const [key, meta] of Object.entries(packMetrics)) {
      initMetrics[key] = {
        current: Math.round(meta.threshold * 0.9),
        unit: meta.unit,
        packLabel: meta.packLabel,
      };
    }
    setMetrics(initMetrics);
    setAlerts(null);
  }, [selectedPacks]);

  const simulate = useCallback(() => {
    const { config } = parseYaml(yaml);
    const result = simulateAlerts(config, metrics);
    setAlerts(result);
  }, [yaml, metrics]);

  const updateMetric = (key, value) => {
    setMetrics(prev => ({
      ...prev,
      [key]: { ...prev[key], current: parseFloat(value) || 0 },
    }));
  };

  // Group alerts by pack
  const groupedAlerts = useMemo(() => {
    if (!alerts) return {};
    const groups = {};
    for (const a of alerts) {
      const label = a.packLabel || t('其他', 'Other');
      if (!groups[label]) groups[label] = [];
      groups[label].push(a);
    }
    return groups;
  }, [alerts]);

  // Determine slider max based on unit
  const getSliderMax = (key, meta) => {
    if (meta.unit === '%') return 100;
    const threshold = packMetrics[key]?.threshold || 100;
    return Math.max(threshold * 2, 200);
  };

  return (
    <div>
      <h3 className="text-lg font-semibold mb-3">
        {t('多指標告警預覽', 'Multi-Metric Alert Preview')}
      </h3>
      <p className="text-sm text-gray-600 mb-4">
        {t('選擇 Rule Pack，同時預覽所有相關 metric 的告警觸發狀態。調整 slider 觀察聯動效果。',
           'Select Rule Packs and preview all related metric alert states simultaneously. Adjust sliders to observe interactions.')}
      </p>

      {/* Rule Pack Selector */}
      <div className="mb-4 p-3 bg-gray-50 rounded-lg border">
        <div className="text-sm font-medium text-gray-700 mb-2">
          {t('選擇 Rule Pack', 'Select Rule Packs')}
        </div>
        <RulePackSelector selected={selectedPacks} onChange={setSelectedPacks} />
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {/* YAML config */}
        <div>
          <h4 className="text-sm font-medium text-gray-700 mb-2">
            {t('Tenant 配置', 'Tenant Config')}
          </h4>
          <textarea
            value={yaml}
            onChange={(e) => setYaml(e.target.value)}
            className="w-full h-48 font-mono text-xs p-2 border rounded-lg bg-gray-50"
          />
        </div>

        {/* Metric sliders grouped by pack */}
        <div>
          <h4 className="text-sm font-medium text-gray-700 mb-2">
            {t('模擬指標值', 'Simulated Metrics')}
            <span className="ml-2 text-gray-400 text-xs font-normal">
              {t(`共 ${Object.keys(metrics).length} 個指標`, `${Object.keys(metrics).length} metrics`)}
            </span>
          </h4>
          <div className="space-y-3 max-h-64 overflow-y-auto pr-1">
            {selectedPacks.map(packId => {
              const pack = RULE_PACK_DATA[packId];
              if (!pack || !pack.defaults) return null;
              const packKeys = Object.keys(pack.defaults).filter(k => metrics[k]);
              if (packKeys.length === 0) return null;
              return (
                <div key={packId}>
                  <div className="text-xs font-medium text-gray-500 mb-1">{pack.label}</div>
                  {packKeys.map(key => {
                    const val = metrics[key];
                    if (!val) return null;
                    const sliderMax = getSliderMax(key, val);
                    return (
                      <div key={key} className="flex items-center gap-2 mb-1">
                        <label className="text-xs font-mono w-44 text-gray-600 truncate" title={key}>{key}</label>
                        <input
                          type="range"
                          min="0" max={sliderMax}
                          value={val.current}
                          onChange={(e) => updateMetric(key, e.target.value)}
                          className="flex-1"
                        />
                        <span className="text-xs font-mono w-20 text-right">
                          {val.current}{val.unit === '%' ? '%' : ` ${val.unit}`}
                        </span>
                      </div>
                    );
                  })}
                </div>
              );
            })}
          </div>
        </div>
      </div>

      <button
        onClick={simulate}
        className="mt-3 px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 transition-colors"
      >
        {t('模擬所有告警', 'Simulate All Alerts')}
      </button>

      {alerts && (
        <div className="mt-4">
          {/* Summary bar */}
          <div className="flex gap-3 mb-3 text-sm">
            <span className="px-2 py-1 bg-red-100 text-red-800 rounded">
              {t('觸發中', 'Firing')}: {alerts.filter(a => a.firing).length}
            </span>
            <span className="px-2 py-1 bg-green-100 text-green-800 rounded">
              OK: {alerts.filter(a => a.severity === 'ok').length}
            </span>
            <span className="px-2 py-1 bg-gray-100 text-gray-600 rounded">
              {t('無閾值/已禁用', 'No threshold/Disabled')}: {alerts.filter(a => !a.threshold).length}
            </span>
          </div>

          {/* Grouped alert results */}
          {Object.entries(groupedAlerts).map(([group, groupAlerts]) => (
            <div key={group} className="mb-4">
              <div className="text-sm font-medium text-gray-700 mb-1 flex items-center gap-2">
                <span>{group}</span>
                <span className="text-xs text-gray-400">({groupAlerts.length} {t('指標', 'metrics')})</span>
              </div>
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="bg-gray-100">
                      <th className="px-3 py-1.5 text-left text-xs">{t('指標', 'Metric')}</th>
                      <th className="px-3 py-1.5 text-right text-xs">{t('目前值', 'Current')}</th>
                      <th className="px-3 py-1.5 text-center text-xs">{t('閾值', 'Threshold')}</th>
                      <th className="px-3 py-1.5 text-left text-xs">{t('狀態列', 'Bar')}</th>
                      <th className="px-3 py-1.5 text-center text-xs">{t('狀態', 'Status')}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {groupAlerts.map((a, i) => {
                      const barWidthStyle = { width: `${Math.min(100, (a.current / (a.threshold * 1.5)) * 100)}%` };
                      const thresholdMarkerStyle = { left: `${Math.min(100, (a.threshold / (a.threshold * 1.5)) * 100)}%` };
                      const criticalMarkerStyle = { left: `${Math.min(100, (a.critical_threshold / (a.threshold * 1.5)) * 100)}%` };
                      return (
                      <tr key={i} className={
                        a.critical_firing ? 'bg-red-50' :
                        a.firing ? 'bg-yellow-50' : 'bg-white'
                      }>
                        <td className="px-3 py-1.5 font-mono text-xs">{a.metric}</td>
                        <td className="px-3 py-1.5 text-right text-xs">
                          {a.current != null ? a.current : '-'}
                        </td>
                        <td className="px-3 py-1.5 text-center text-xs">
                          {a.threshold != null ? (
                            <span>
                              {a.threshold}
                              {a.critical_threshold ? ` / ${a.critical_threshold}` : ''}
                            </span>
                          ) : '-'}
                        </td>
                        <td className="px-3 py-1.5">
                          {a.threshold != null && (
                            <div className="relative h-3 bg-gray-200 rounded-full overflow-hidden w-32">
                              <div
                                className={`absolute top-0 left-0 h-full rounded-full transition-all ${
                                  a.critical_firing ? 'bg-red-500' :
                                  a.firing ? 'bg-yellow-500' : 'bg-green-500'
                                }`}
                                style={barWidthStyle}
                              />
                              {/* Threshold marker */}
                              <div
                                className="absolute top-0 h-full w-0.5 bg-gray-600"
                                style={thresholdMarkerStyle}
                                title={`threshold: ${a.threshold}`}
                              />
                              {a.critical_threshold && (
                                <div
                                  className="absolute top-0 h-full w-0.5 bg-red-700"
                                  style={criticalMarkerStyle}
                                  title={`critical: ${a.critical_threshold}`}
                                />
                              )}
                            </div>
                          )}
                        </td>
                        <td className="px-3 py-1.5 text-center">
                          {a.severity === 'critical' ? (
                            <span className="px-2 py-0.5 bg-red-100 text-red-800 rounded-full text-xs font-medium">CRITICAL</span>
                          ) : a.severity === 'warning' ? (
                            <span className="px-2 py-0.5 bg-yellow-100 text-yellow-800 rounded-full text-xs font-medium">FIRING</span>
                          ) : a.severity === 'disabled' ? (
                            <span className="px-2 py-0.5 bg-gray-100 text-gray-500 rounded-full text-xs font-medium">DISABLED</span>
                          ) : a.severity === 'no-threshold' ? (
                            <span className="px-2 py-0.5 bg-gray-100 text-gray-400 rounded-full text-xs font-medium">—</span>
                          ) : (
                            <span className="px-2 py-0.5 bg-green-100 text-green-800 rounded-full text-xs font-medium">OK</span>
                          )}
                        </td>
                      </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </div>
          ))}

          {/* Inhibit rule explanation */}
          {alerts.some(a => a.critical_firing) && (
            <div className="mt-3 p-3 rounded-lg bg-purple-50 border border-purple-200 text-xs text-purple-800">
              <span className="font-medium">{t('Severity Dedup 生效', 'Severity Dedup Active')}:</span>{' '}
              {t('有 CRITICAL 等級觸發 — Alertmanager inhibit rule 將自動抑制對應的 WARNING 告警，通知管道只收到一次最高嚴重度。',
                 'CRITICAL severity firing — Alertmanager inhibit rules will suppress corresponding WARNING alerts. Notification channel receives only the highest severity.')}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/* ══════════════════════════════════════════════
   Tab 3: Routing Trace (full path with enforced + inhibit)
   ══════════════════════════════════════════════ */
function RoutingTraceTab() {
  const [yaml, setYaml] = useState('');
  const [traceMetric, setTraceMetric] = useState('mysql_connections');
  const [traceSeverity, setTraceSeverity] = useState('warning');
  const [result, setResult] = useState(null);

  useEffect(() => {
    if (!yaml) {
      setYaml(generateSampleYaml(['mariadb', 'kubernetes'], true));
    }
  }, []);

  const visualize = useCallback(() => {
    const { config } = parseYaml(yaml);
    const routingResult = resolveRoutingLayers(config);

    // Build alert trace
    const threshold = config[traceMetric];
    const critKey = `${traceMetric}_critical`;
    const critThreshold = config[critKey] ? parseFloat(config[critKey]) : null;
    const hasCritical = critThreshold !== null;

    // Determine inhibit effect
    const isInhibited = traceSeverity === 'warning' && hasCritical;

    // Check domain policy violations
    const resolvedReceiver = routingResult.resolved.receiver_type;
    const policyViolations = [];
    for (const [domain, policy] of Object.entries(DOMAIN_POLICIES)) {
      const c = policy.constraints || {};
      if (c.forbidden_receiver_types?.includes(resolvedReceiver)) {
        policyViolations.push({ domain, reason: t(`禁止使用 ${resolvedReceiver}`, `${resolvedReceiver} forbidden`) });
      }
    }

    setResult({
      ...routingResult,
      trace: {
        metric: traceMetric,
        severity: traceSeverity,
        threshold: threshold ? parseFloat(threshold) : null,
        criticalThreshold: critThreshold,
        hasCritical,
        isInhibited,
        policyViolations,
      },
    });
  }, [yaml, traceMetric, traceSeverity]);

  const layerColors = {
    platform: 'bg-slate-100 text-slate-800 border-slate-300',
    profile: 'bg-blue-50 text-blue-800 border-blue-300',
    tenant: 'bg-amber-50 text-amber-800 border-amber-300',
    enforced: 'bg-red-50 text-red-800 border-red-300',
    skip: 'bg-gray-50 text-gray-400 border-gray-200',
  };

  const ROUTING_KEYS = ['receiver_type', 'group_by', 'group_wait', 'group_interval', 'repeat_interval', 'webhook_url', 'email_to'];

  return (
    <div>
      <h3 className="text-lg font-semibold mb-3">
        {t('告警路由追蹤', 'Alert Routing Trace')}
      </h3>
      <p className="text-sm text-gray-600 mb-4">
        {t('輸入 tenant YAML 與要追蹤的 metric，顯示告警從觸發到通知的完整路徑，包括四層路由合併、platform enforced route、inhibit rule 抑制效果。',
           'Enter tenant YAML and select a metric to trace. Shows the complete alert path from firing to notification, including four-layer routing merge, platform enforced routes, and inhibit rule suppression.')}
      </p>

      {/* Trace controls */}
      <div className="flex flex-wrap gap-3 mb-4 p-3 bg-indigo-50 rounded-lg border border-indigo-200">
        <div className="flex-1 min-w-48">
          <label className="text-xs font-medium text-indigo-700 block mb-1">
            {t('追蹤 Metric', 'Trace Metric')}
          </label>
          <input
            type="text"
            value={traceMetric}
            onChange={(e) => setTraceMetric(e.target.value)}
            className="w-full text-sm px-2 py-1.5 border rounded focus:ring-2 focus:ring-indigo-500"
            placeholder="mysql_connections"
          />
        </div>
        <div>
          <label className="text-xs font-medium text-indigo-700 block mb-1">
            {t('嚴重度', 'Severity')}
          </label>
          <select
            value={traceSeverity}
            onChange={(e) => setTraceSeverity(e.target.value)}
            className="text-sm px-2 py-1.5 border rounded focus:ring-2 focus:ring-indigo-500"
          >
            <option value="warning">warning</option>
            <option value="critical">critical</option>
          </select>
        </div>
      </div>

      <div className="flex gap-2 mb-2">
        <button
          onClick={() => setYaml(generateSampleYaml(['mariadb', 'kubernetes'], false))}
          className="text-xs px-2 py-1 bg-gray-200 hover:bg-gray-300 rounded"
        >{t('範例：直接 routing', 'Example: Direct routing')}</button>
        <button
          onClick={() => setYaml(generateSampleYaml(['mariadb', 'kubernetes'], true))}
          className="text-xs px-2 py-1 bg-gray-200 hover:bg-gray-300 rounded"
        >{t('範例：Routing Profile', 'Example: Routing Profile')}</button>
      </div>

      <textarea
        value={yaml}
        onChange={(e) => setYaml(e.target.value)}
        className="w-full h-40 font-mono text-xs p-2 border rounded-lg bg-gray-50"
      />

      <button
        onClick={visualize}
        className="mt-3 px-4 py-2 bg-indigo-600 text-white rounded-lg hover:bg-indigo-700 transition-colors"
      >
        {t('追蹤告警路徑', 'Trace Alert Path')}
      </button>

      {result && (
        <div className="mt-4">
          {/* Alert origin */}
          <div className="p-4 rounded-lg border-2 border-indigo-300 bg-indigo-50 mb-0">
            <div className="flex items-center gap-2 mb-2">
              <span className="w-6 h-6 rounded-full bg-indigo-600 text-white flex items-center justify-center text-xs font-bold">!</span>
              <span className="font-semibold text-sm text-indigo-800">
                {t('告警觸發', 'Alert Fires')}
              </span>
            </div>
            <div className="text-xs font-mono bg-white bg-opacity-60 rounded p-2 space-y-0.5">
              <div><span className="text-gray-500">metric:</span> <span className="font-bold">{result.trace.metric}</span></div>
              <div><span className="text-gray-500">severity:</span> <span className={`font-bold ${result.trace.severity === 'critical' ? 'text-red-700' : 'text-yellow-700'}`}>{result.trace.severity}</span></div>
              {result.trace.threshold !== null && (
                <div>
                  <span className="text-gray-500">threshold:</span> {result.trace.threshold}
                  {result.trace.hasCritical && <span className="ml-2 text-gray-500">critical: {result.trace.criticalThreshold}</span>}
                </div>
              )}
            </div>
          </div>

          {/* Inhibit check */}
          <div className="flex justify-center py-1">
            <div className="w-px h-4 bg-gray-300"></div>
          </div>
          <div className={`p-3 rounded-lg border-2 ${
            result.trace.isInhibited
              ? 'border-purple-400 bg-purple-50'
              : 'border-gray-200 bg-gray-50'
          }`}>
            <div className="flex items-center gap-2 text-sm">
              <span className={`w-5 h-5 rounded-full flex items-center justify-center text-xs ${
                result.trace.isInhibited ? 'bg-purple-600 text-white' : 'bg-gray-300 text-gray-600'
              }`}>
                {result.trace.isInhibited ? '!' : '✓'}
              </span>
              <span className="font-medium">
                {t('Inhibit Rule 檢查', 'Inhibit Rule Check')}
              </span>
            </div>
            <div className="mt-1 text-xs">
              {result.trace.isInhibited ? (
                <span className="text-purple-800">
                  {t(`⚠ WARNING 被抑制 — 同時存在 ${result.trace.metric}_critical 閾值。Alertmanager inhibit rule 將抑制此 WARNING，只發送 CRITICAL。此告警不會到達通知管道。`,
                     `⚠ WARNING suppressed — ${result.trace.metric}_critical threshold exists. Alertmanager inhibit rule will suppress this WARNING, sending only CRITICAL. This alert will NOT reach the notification channel.`)}
                </span>
              ) : result.trace.hasCritical && result.trace.severity === 'critical' ? (
                <span className="text-green-700">
                  {t('✓ CRITICAL 直通 — 同時會抑制對應的 WARNING 告警', '✓ CRITICAL passes through — also suppresses corresponding WARNING alert')}
                </span>
              ) : (
                <span className="text-gray-600">
                  {t('✓ 通過 — 無 inhibit 規則影響此告警', '✓ Pass — no inhibit rules affect this alert')}
                </span>
              )}
            </div>
          </div>

          {/* If inhibited, show stop */}
          {result.trace.isInhibited ? (
            <div className="mt-2 p-3 rounded-lg border-2 border-dashed border-purple-300 bg-purple-50 text-center">
              <span className="text-purple-600 text-sm font-medium">
                {t('🛑 告警在此被抑制，不繼續路由', '🛑 Alert suppressed here, routing stops')}
              </span>
            </div>
          ) : (
            <>
              {/* Four-layer routing cascade */}
              <div className="flex justify-center py-1">
                <div className="w-px h-4 bg-gray-300"></div>
              </div>

              <div className="p-3 rounded-lg border bg-gray-50 mb-0">
                <div className="text-sm font-medium text-gray-700 mb-2">
                  {t('四層路由合併 (ADR-007)', 'Four-Layer Routing Merge (ADR-007)')}
                </div>

                <div className="space-y-0">
                  {result.layers.map((layer, i) => {
                    const isSkip = layer.source === 'skip';
                    const hasOverrides = Object.keys(layer.overrides).length > 0;
                    return (
                      <div key={i}>
                        {i > 0 && (
                          <div className="flex justify-center py-0.5">
                            <div className="w-px h-3 bg-gray-300"></div>
                          </div>
                        )}
                        <div className={`p-3 rounded-lg border ${layerColors[layer.source]} ${isSkip ? 'opacity-50' : ''}`}>
                          <div className="flex items-center justify-between mb-1">
                            <div className="flex items-center gap-2">
                              <span className={`w-5 h-5 rounded-full flex items-center justify-center text-xs font-bold ${
                                isSkip ? 'bg-gray-200 text-gray-500' : 'bg-white text-gray-700'
                              }`}>{layer.layer}</span>
                              <span className="font-semibold text-xs">{layer.label}</span>
                            </div>
                            <code className="text-xs font-mono px-1.5 py-0.5 bg-white bg-opacity-60 rounded">
                              {layer.name}
                            </code>
                          </div>
                          {hasOverrides && (
                            <div className="mb-1 space-y-0.5">
                              {Object.entries(layer.overrides).map(([key, change]) => (
                                <div key={key} className="text-xs flex items-center gap-1">
                                  <span className="font-mono font-medium">{key}:</span>
                                  <span className="line-through opacity-50">{String(change.from || t('（未設）', '(unset)'))}</span>
                                  <span className="mx-1">&rarr;</span>
                                  <span className="font-bold">{String(change.to)}</span>
                                </div>
                              ))}
                            </div>
                          )}
                          {isSkip && (
                            <div className="text-xs italic">
                              {t('本層未配置，繼承上層值', 'Not configured, inherits from above')}
                            </div>
                          )}
                        </div>
                      </div>
                    );
                  })}
                </div>
              </div>

              {/* Policy check */}
              {result.trace.policyViolations.length > 0 && (
                <>
                  <div className="flex justify-center py-1">
                    <div className="w-px h-4 bg-gray-300"></div>
                  </div>
                  <div className="p-3 rounded-lg border-2 border-orange-300 bg-orange-50">
                    <div className="text-sm font-medium text-orange-800 mb-1">
                      {t('⚠ Domain Policy 警告', '⚠ Domain Policy Warnings')}
                    </div>
                    {result.trace.policyViolations.map((v, i) => (
                      <div key={i} className="text-xs text-orange-700">
                        <span className="font-mono">{v.domain}</span>: {v.reason}
                      </div>
                    ))}
                  </div>
                </>
              )}

              {/* Final resolved route */}
              <div className="flex justify-center py-1">
                <div className="w-px h-4 bg-gray-300"></div>
              </div>
              <div className="p-4 rounded-lg border-2 border-green-400 bg-green-50">
                <div className="flex items-center gap-2 mb-2">
                  <span className="w-6 h-6 rounded-full bg-green-600 text-white flex items-center justify-center text-xs font-bold">✓</span>
                  <span className="text-green-800 font-bold text-sm">
                    {t('通知送達', 'Notification Delivered')}
                  </span>
                </div>
                <div className="text-xs font-mono bg-white bg-opacity-60 rounded p-2 space-y-0.5">
                  {ROUTING_KEYS.map(k => {
                    const v = result.resolved[k];
                    if (v === undefined) return null;
                    return (
                      <div key={k}>
                        {k}: {Array.isArray(v) ? `[${v.join(', ')}]` : String(v)}
                      </div>
                    );
                  })}
                </div>
                <div className="mt-2 text-xs text-green-700">
                  {t(
                    `告警 ${result.trace.metric} (severity=${result.trace.severity}) 透過 ${result.resolved.receiver_type} 送達通知管道。`,
                    `Alert ${result.trace.metric} (severity=${result.trace.severity}) delivered via ${result.resolved.receiver_type} channel.`
                  )}
                </div>
              </div>

              {/* Platform enforced (NOC) duplicate notification note */}
              <div className="flex justify-center py-1">
                <div className="w-px h-4 bg-gray-300"></div>
              </div>
              <div className="p-3 rounded-lg border-2 border-red-200 bg-red-50">
                <div className="flex items-center gap-2 mb-1">
                  <span className="w-5 h-5 rounded-full bg-red-600 text-white flex items-center justify-center text-xs font-bold">4</span>
                  <span className="text-red-800 font-semibold text-xs">
                    {t('平台強制路由 (NOC) — 同步副本', 'Platform Enforced Route (NOC) — Parallel Copy')}
                  </span>
                </div>
                <div className="text-xs text-red-700">
                  {t(
                    '_routing_enforced 路由獨立於 tenant 路由，平台 NOC 團隊永遠會收到所有告警的副本（使用 platform_summary 雙視角 annotation）。此機制確保平台團隊具有全局告警可見性。',
                    '_routing_enforced operates independently from tenant routing. Platform NOC team always receives a copy of all alerts (using platform_summary dual-perspective annotations). This ensures platform-wide alert visibility.'
                  )}
                </div>
              </div>
            </>
          )}

          {/* Export trace result as JSON */}
          <div className="mt-3 flex justify-end">
            <button
              className="text-xs px-3 py-1 rounded border border-gray-300 text-gray-600 hover:bg-gray-50"
              onClick={() => {
                const json = JSON.stringify(result, null, 2);
                navigator.clipboard.writeText(json).then(() => {
                  const el = document.getElementById('copy-trace-feedback');
                  if (el) { el.textContent = t('已複製', 'Copied!'); setTimeout(() => { el.textContent = ''; }, 2000); }
                });
              }}
            >
              {t('複製 JSON', 'Copy JSON')} <span id="copy-trace-feedback" className="ml-1 text-green-600"></span>
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

/* ── Main Portal Component ── */
const TABS = [
  { id: 'validate', label: () => t('YAML 驗證', 'YAML Validation'), icon: '🔍' },
  { id: 'alerts', label: () => t('告警預覽', 'Alert Preview'), icon: '🔔' },
  { id: 'routing', label: () => t('路由追蹤', 'Routing Trace'), icon: '🌐' },
];

export default function SelfServicePortal() {
  const [activeTab, setActiveTab] = useState('validate');

  return (
    <div className="max-w-4xl mx-auto">
      <div className="mb-6">
        <h1 className="text-2xl font-bold text-gray-900">
          {t('租戶自助入口', 'Tenant Self-Service Portal')}
        </h1>
        <p className="text-gray-600 mt-1">
          {t('驗證配置、預覽告警、追蹤路由 — 無需 CLI 或部署。支援 15 個 Rule Pack、四層路由合併 (ADR-007)、Severity Dedup。',
             'Validate configs, preview alerts, trace routing — no CLI or deployment needed. Supports 15 Rule Packs, four-layer routing merge (ADR-007), severity dedup.')}
        </p>
      </div>

      {/* Tabs */}
      <div className="flex gap-1 bg-gray-100 p-1 rounded-lg mb-6">
        {TABS.map(tab => (
          <button
            key={tab.id}
            onClick={() => setActiveTab(tab.id)}
            className={`flex-1 px-3 py-2 rounded-md text-sm font-medium transition-colors ${
              activeTab === tab.id
                ? 'bg-white text-blue-600 shadow-sm'
                : 'text-gray-600 hover:text-gray-800'
            }`}
          >
            <span className="mr-1">{tab.icon}</span>
            {tab.label()}
          </button>
        ))}
      </div>

      {/* Tab content */}
      <div className="bg-white rounded-lg border p-6">
        {activeTab === 'validate' && <YamlValidatorTab />}
        {activeTab === 'alerts' && <AlertPreviewTab />}
        {activeTab === 'routing' && <RoutingTraceTab />}
      </div>

      {/* Footer info */}
      <div className="mt-6 p-4 bg-blue-50 rounded-lg border border-blue-100">
        <h4 className="text-sm font-medium text-blue-800 mb-2">
          {t('提示', 'Tips')}
        </h4>
        <ul className="text-sm text-blue-700 space-y-1">
          <li>{t('• 此工具在瀏覽器端執行，YAML 不會送往任何伺服器。',
                 '• This tool runs entirely in your browser — YAML is never sent to any server.')}</li>
          <li>{t('• 工具自動載入 platform-data.json 中的 15 個 Rule Pack metric 定義。',
                 '• Tool auto-loads 15 Rule Pack metric definitions from platform-data.json.')}</li>
          <li>{t('• 完整驗證請使用 CLI: da-tools validate-config --config-dir conf.d/',
                 '• For full validation use CLI: da-tools validate-config --config-dir conf.d/')}</li>
          <li>{t('• Policy-as-Code 策略需透過 CLI 評估: da-tools evaluate-policy --config-dir conf.d/',
                 '• Policy-as-Code evaluation via CLI: da-tools evaluate-policy --config-dir conf.d/')}</li>
        </ul>
      </div>
    </div>
  );
}
