/**
 * Portal Shared Data & Utilities
 *
 * Extracted from self-service-portal.jsx in v2.3.0 for modularization.
 * Loaded as a dependency before tab components.
 *
 * Access via: window.__portalShared.{functionName}
 *
 * This file contains:
 * - Rule Pack catalog (RULE_PACK_DATA)
 * - Routing profiles and domain policies
 * - Reserved keys and receiver types
 * - Timing guardrails
 * - YAML parser (parseYaml)
 * - Config generation (generateSampleYaml)
 * - Validation engine (validateConfig)
 * - Alert simulation (simulateAlerts)
 * - Routing resolver (resolveRoutingLayers)
 */
(function() {
  'use strict';

  var t = window.__t || function(zh, en) { return en; };

  /* ════════════════════════════════════════════
     Data Definitions
     ════════════════════════════════════════════ */

  /**
   * Rule Pack catalog (from platform-data.json)
   * Fallback inline data if window.__platformData is not available.
   */
  var RULE_PACK_DATA = window.__platformData?.rulePacks || {
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

  /**
   * Category labels for Rule Packs
   */
  var CATEGORY_LABELS = {
    database: function() { return t('資料庫', 'Databases'); },
    messaging: function() { return t('訊息佇列', 'Messaging'); },
    runtime: function() { return t('運行環境', 'Runtime'); },
    webserver: function() { return t('網頁伺服器', 'Web Servers'); },
    infrastructure: function() { return t('基礎設施', 'Infrastructure'); },
  };

  /**
   * Reserved keys and validation constants
   */
  var RESERVED_KEYS = new Set([
    '_silent_mode', '_namespaces', '_metadata', '_profile',
    '_routing_defaults', '_routing_profile', '_domain_policy', '_instance_mapping'
  ]);

  var RESERVED_PREFIXES = ['_state_', '_routing'];

  var RESERVED_KEY_PATTERNS = {
    keys: Array.from(RESERVED_KEYS),
    prefixes: RESERVED_PREFIXES,
  };

  /**
   * Supported receiver types
   */
  var RECEIVER_TYPES = ['webhook', 'email', 'slack', 'teams', 'rocketchat', 'pagerduty'];

  /**
   * Routing Profiles (ADR-007)
   */
  var ROUTING_PROFILES = {
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

  /**
   * Domain Policies (ADR-007)
   */
  var DOMAIN_POLICIES = {
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

  var RECEIVER_REQUIRED = {
    webhook: ['url'], email: ['to', 'smarthost'], slack: ['api_url'],
    teams: ['webhook_url'], rocketchat: ['url'], pagerduty: ['service_key'],
  };

  /**
   * Timing parameter guardrails
   */
  var TIMING_GUARDRAILS = {
    group_wait: { min: 5, max: 300, unit: 's' },
    group_interval: { min: 5, max: 300, unit: 's' },
    repeat_interval: { min: 60, max: 259200, unit: 's' },
  };

  /**
   * Platform routing defaults (Layer 1 of four-layer merge)
   */
  var ROUTING_DEFAULTS = {
    receiver_type: 'webhook',
    group_by: ['alertname', 'tenant'],
    group_wait: '30s',
    group_interval: '5m',
    repeat_interval: '4h',
  };

  /* ════════════════════════════════════════════
     Utility Functions
     ════════════════════════════════════════════ */

  /**
   * Parse duration string to seconds
   * Supports: s, m, h, d suffixes
   */
  function parseDuration(str) {
    if (!str) return null;
    var m = String(str).match(/^(\d+\.?\d*)([smhd])$/);
    if (!m) return null;
    var multi = { s: 1, m: 60, h: 3600, d: 86400 };
    return parseFloat(m[1]) * (multi[m[2]] || 1);
  }

  /**
   * Lightweight regex-based YAML parser for browser-side validation.
   * Limitation: handles flat/single-nested YAML used in tenant config files.
   * Does NOT handle multi-line strings, anchors, or complex nesting.
   * URLs with colons (e.g. https://...) are handled by splitting on first ": ".
   */
  var UNSAFE_KEYS = new Set(['__proto__', 'constructor', 'prototype']);
  var MAX_YAML_SIZE = 100000;

  function parseYaml(text) {
    var errors = [];
    if (text.length > MAX_YAML_SIZE) {
      return { config: {}, errors: [t('YAML 超過大小限制（100KB）', 'YAML exceeds size limit (100KB)')] };
    }
    var config = {};
    var currentKey = null;
    var currentObj = null;

    var lines = text.split('\n');
    for (var i = 0; i < lines.length; i++) {
      var line = lines[i];
      // Strip comments but not inside quoted strings (simple heuristic)
      var trimmed = line.replace(/\s+#(?![^"']*["'][^"']*$).*$/, '').trimEnd();
      if (!trimmed || trimmed.trim() === '') continue;

      var lineIndent = line.search(/\S/);
      var content = trimmed.trim();

      // Split on first ": " (colon+space) to handle URLs with colons
      var kvMatch = content.match(/^([^:]+?):\s+(.+)$/);
      var objMatch = content.match(/^([^:]+?):\s*$/);

      if (lineIndent === 0 && kvMatch) {
        var key = kvMatch[1].trim();
        if (UNSAFE_KEYS.has(key)) continue;
        var val = kvMatch[2].trim();
        if (val.startsWith('"') && val.endsWith('"')) val = val.slice(1, -1);
        if (val.startsWith("'") && val.endsWith("'")) val = val.slice(1, -1);
        if (val.startsWith('[') && val.endsWith(']')) {
          val = val.slice(1, -1).split(',').map(function(s) { return s.trim().replace(/"/g, '').replace(/'/g, ''); });
        }
        config[key] = val;
        currentKey = null;
        currentObj = null;
      } else if (lineIndent === 0 && objMatch) {
        var key = objMatch[1].trim();
        if (UNSAFE_KEYS.has(key)) continue;
        config[key] = {};
        currentKey = key;
        currentObj = config[key];
      } else if (currentKey && lineIndent > 0 && kvMatch) {
        var key = kvMatch[1].trim();
        var val = kvMatch[2].trim();
        if (val.startsWith('"') && val.endsWith('"')) val = val.slice(1, -1);
        if (val.startsWith("'") && val.endsWith("'")) val = val.slice(1, -1);
        if (val.startsWith('[') && val.endsWith(']')) {
          val = val.slice(1, -1).split(',').map(function(s) { return s.trim().replace(/"/g, '').replace(/'/g, ''); });
        }

        if (currentKey === '_routing' || currentKey === '_metadata') {
          var depth = Math.floor(lineIndent / 2) - 1;
          if (depth === 0) {
            currentObj[key] = val;
          } else if (depth === 1 && typeof currentObj[Object.keys(currentObj).pop()] === 'object') {
            var parentKey = Object.keys(currentObj).pop();
            if (typeof currentObj[parentKey] === 'object') {
              currentObj[parentKey][key] = val;
            }
          }
        }
      } else if (currentKey && lineIndent > 0 && objMatch) {
        var key = objMatch[1].trim();
        if (currentObj) {
          currentObj[key] = {};
        }
      }
    }
    return { config: config, errors: errors };
  }

  /**
   * Collect all known metric keys from Rule Pack data
   */
  function getAllMetricKeys(selectedPacks) {
    var keys = [];
    var packs = selectedPacks && selectedPacks.length > 0
      ? selectedPacks
      : Object.keys(RULE_PACK_DATA);
    for (var i = 0; i < packs.length; i++) {
      var packId = packs[i];
      var pack = RULE_PACK_DATA[packId];
      if (!pack || !pack.defaults) continue;
      for (var key in pack.defaults) {
        if (pack.defaults.hasOwnProperty(key)) {
          var meta = pack.defaults[key];
          keys.push(Object.assign({ key: key, pack: packId, label: pack.label }, meta));
        }
      }
    }
    return keys;
  }

  /**
   * Generate sample YAML from selected Rule Packs
   */
  function generateSampleYaml(selectedPacks, withProfile) {
    var lines = ['# ' + t('從 Rule Pack 自動產生的 Tenant YAML', 'Auto-generated tenant YAML from Rule Packs')];
    for (var i = 0; i < selectedPacks.length; i++) {
      var packId = selectedPacks[i];
      var pack = RULE_PACK_DATA[packId];
      if (!pack || !pack.defaults || Object.keys(pack.defaults).length === 0) continue;
      lines.push('\n# --- ' + pack.label + ' ---');
      for (var key in pack.defaults) {
        if (pack.defaults.hasOwnProperty(key)) {
          var meta = pack.defaults[key];
          lines.push(key + ': "' + meta.value + '"  # ' + meta.desc);
        }
      }
    }
    lines.push('');
    if (withProfile) {
      lines.push('_routing_profile: team-sre-apac');
    } else {
      lines.push('_routing:');
      lines.push('  receiver_type: webhook');
      lines.push('  webhook_url: https://hooks.example.com/alerts');
      lines.push('  group_by: [alertname, severity]');
      lines.push('  group_wait: "30s"');
      lines.push('  repeat_interval: "4h"');
    }
    lines.push('');
    lines.push('_metadata:');
    lines.push('  runbook_url: https://runbooks.example.com/my-tenant');
    lines.push('  owner: platform-team');
    lines.push('  tier: production');
    return lines.join('\n');
  }

  /**
   * Validation engine for tenant config
   */
  function validateConfig(config, selectedPacks) {
    var issues = [];
    var info = [];
    var knownMetrics = new Set();
    var allMetrics = getAllMetricKeys(selectedPacks);
    for (var i = 0; i < allMetrics.length; i++) {
      knownMetrics.add(allMetrics[i].key);
    }

    for (var key in config) {
      if (!config.hasOwnProperty(key)) continue;
      if (key.startsWith('_')) continue;

      var val = config[key];
      if (val === 'disable') {
        info.push({ level: 'info', field: key,
          msg: t('已禁用此指標', 'Metric disabled') });
        continue;
      }

      var numVal = parseFloat(val);
      if (!isNaN(numVal)) {
        // Metrics with large natural values (counts, bytes, rates) should not warn on >100
        var LARGE_VALUE_PATTERNS = ['bytes', 'connections', 'lag', 'rate', 'messages',
          'threads', 'count', 'queue', 'consumers', 'controllers', 'broker', 'sessions',
          'partitions', 'queries', 'waiting'];
        var isLargeValueMetric = false;
        for (var j = 0; j < LARGE_VALUE_PATTERNS.length; j++) {
          if (key.includes(LARGE_VALUE_PATTERNS[j])) {
            isLargeValueMetric = true;
            break;
          }
        }
        if (numVal > 100 && !isLargeValueMetric) {
          issues.push({ level: 'warning', field: key,
            msg: t('閾值 ' + numVal + ' 超過 100，確認是否正確', 'Threshold ' + numVal + ' exceeds 100, verify if correct') });
        }
        if (numVal < 0) {
          issues.push({ level: 'error', field: key,
            msg: t('閾值 ' + numVal + ' < 0，無效', 'Threshold ' + numVal + ' < 0, invalid') });
        }
      }

      // Check if metric is recognized from selected packs
      if (selectedPacks && selectedPacks.length > 0 && knownMetrics.size > 0) {
        var isCriticalVariant = key.endsWith('_critical');
        var baseKey = isCriticalVariant ? key.replace(/_critical$/, '') : key;
        if (!knownMetrics.has(baseKey) && !knownMetrics.has(key)) {
          issues.push({ level: 'info', field: key,
            msg: t('此 metric key 不在已選 Rule Pack 的預設清單中', 'Metric key not found in selected Rule Pack defaults') });
        }
      }
    }

    // Check routing
    var routing = config._routing;
    if (routing && typeof routing === 'object') {
      var rtype = routing.receiver_type;
      if (rtype && RECEIVER_TYPES.indexOf(rtype) === -1) {
        issues.push({ level: 'error', field: '_routing.receiver_type',
          msg: t('不支援的 receiver 類型: ' + rtype, 'Unsupported receiver type: ' + rtype) });
      }
      var webhookUrl = routing.webhook_url;
      if (rtype === 'webhook' && webhookUrl) {
        try {
          var parsed = new URL(webhookUrl);
          if (['http:', 'https:'].indexOf(parsed.protocol) === -1) {
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

      for (var param in TIMING_GUARDRAILS) {
        if (!TIMING_GUARDRAILS.hasOwnProperty(param)) continue;
        var guard = TIMING_GUARDRAILS[param];
        var val = routing[param];
        if (val) {
          var secs = parseDuration(val);
          if (secs !== null) {
            if (secs < guard.min) {
              issues.push({ level: 'warning', field: '_routing.' + param,
                msg: t(val + ' 低於下限 ' + guard.min + 's', val + ' below minimum ' + guard.min + 's') });
            }
            if (secs > guard.max) {
              issues.push({ level: 'warning', field: '_routing.' + param,
                msg: t(val + ' 超過上限 ' + guard.max + 's', val + ' exceeds maximum ' + guard.max + 's') });
            }
          }
        }
      }
    }

    // Check routing profile reference (ADR-007)
    var profileRef = config._routing_profile;
    if (profileRef) {
      if (!ROUTING_PROFILES[profileRef]) {
        issues.push({ level: 'error', field: '_routing_profile',
          msg: t('路由 profile "' + profileRef + '" 不存在', 'Routing profile "' + profileRef + '" not found') });
      } else {
        info.push({ level: 'info', field: '_routing_profile',
          msg: t('使用路由 profile: ' + profileRef, 'Using routing profile: ' + profileRef) });
      }
    }

    // Check domain policy constraints (ADR-007)
    var resolvedReceiverType = routing
      ? (routing.receiver_type || (profileRef && ROUTING_PROFILES[profileRef]?.receiver_type))
      : (profileRef && ROUTING_PROFILES[profileRef]?.receiver_type);
    if (resolvedReceiverType) {
      for (var domain in DOMAIN_POLICIES) {
        if (!DOMAIN_POLICIES.hasOwnProperty(domain)) continue;
        var policy = DOMAIN_POLICIES[domain];
        var constraints = policy.constraints || {};
        if (constraints.forbidden_receiver_types && constraints.forbidden_receiver_types.indexOf(resolvedReceiverType) > -1) {
          issues.push({ level: 'warning', field: '_domain_policy',
            msg: t('domain "' + domain + '" 禁止使用 ' + resolvedReceiverType,
                   'Domain "' + domain + '" forbids receiver type: ' + resolvedReceiverType) });
        }
        if (constraints.allowed_receiver_types &&
            constraints.allowed_receiver_types.indexOf(resolvedReceiverType) === -1) {
          issues.push({ level: 'warning', field: '_domain_policy',
            msg: t('domain "' + domain + '" 不允許 ' + resolvedReceiverType,
                   'Domain "' + domain + '" does not allow receiver type: ' + resolvedReceiverType) });
        }
      }
    }

    if (config._metadata && typeof config._metadata === 'object') {
      if (!config._metadata.runbook_url) {
        info.push({ level: 'info', field: '_metadata.runbook_url',
          msg: t('建議配置 runbook URL', 'Consider adding runbook URL') });
      }
    }

    for (var key in config) {
      if (!config.hasOwnProperty(key)) continue;
      if (key.startsWith('_')) {
        var isReserved = RESERVED_KEYS.has(key);
        var hasReservedPrefix = false;
        for (var j = 0; j < RESERVED_PREFIXES.length; j++) {
          if (key.startsWith(RESERVED_PREFIXES[j])) {
            hasReservedPrefix = true;
            break;
          }
        }
        if (!isReserved && !hasReservedPrefix) {
          issues.push({ level: 'warning', field: key,
            msg: t('未知的保留字 key: ' + key, 'Unknown reserved key: ' + key) });
        }
      }
    }

    return { issues: issues.concat(info) };
  }

  /**
   * Alert simulation engine (multi-metric)
   */
  function simulateAlerts(config, metricValues) {
    var alerts = [];

    for (var metric in metricValues) {
      if (!metricValues.hasOwnProperty(metric)) continue;
      var val = metricValues[metric];
      var threshold = config[metric];

      if (!threshold || threshold === 'disable') {
        alerts.push({
          metric: metric, current: val.current, threshold: null, critical_threshold: null,
          firing: false, critical_firing: false, severity: threshold === 'disable' ? 'disabled' : 'no-threshold',
          unit: val.unit, packLabel: val.packLabel,
        });
        continue;
      }

      var thresholdNum = parseFloat(threshold);
      if (isNaN(thresholdNum)) continue;

      var currentVal = val.current;
      var firing = currentVal >= thresholdNum;

      var critKey = metric + '_critical';
      var critThreshold = config[critKey] ? parseFloat(config[critKey]) : null;
      var critFiring = critThreshold !== null && currentVal >= critThreshold;

      alerts.push({
        metric: metric, current: currentVal, threshold: thresholdNum,
        critical_threshold: critThreshold, firing: firing, critical_firing: critFiring,
        severity: critFiring ? 'critical' : (firing ? 'warning' : 'ok'),
        unit: val.unit, packLabel: val.packLabel,
      });
    }

    return alerts;
  }

  /**
   * Four-layer routing resolver (ADR-007)
   */
  function resolveRoutingLayers(config) {
    var layers = [];

    // Layer 1: _routing_defaults
    var L1 = Object.assign({}, ROUTING_DEFAULTS);
    layers.push({
      layer: 1, name: '_routing_defaults',
      label: t('平台預設', 'Platform Defaults'),
      values: Object.assign({}, L1), overrides: {}, source: 'platform',
    });

    // Layer 2: routing_profiles
    var profileRef = config._routing_profile;
    var profile = profileRef ? ROUTING_PROFILES[profileRef] : null;
    var L2 = Object.assign({}, L1);
    var L2overrides = {};
    if (profile) {
      for (var k in profile) {
        if (profile.hasOwnProperty(k)) {
          var v = profile[k];
          if (v !== undefined && v !== L2[k]) {
            L2overrides[k] = { from: L2[k], to: v };
            L2[k] = v;
          }
        }
      }
    }
    layers.push({
      layer: 2,
      name: profileRef ? ('routing_profiles[' + profileRef + ']') : t('（未指定 profile）', '(no profile)'),
      label: t('路由 Profile', 'Routing Profile'),
      values: Object.assign({}, L2), overrides: L2overrides,
      source: profileRef ? 'profile' : 'skip',
    });

    // Layer 3: tenant _routing
    var routing = config._routing;
    var L3 = Object.assign({}, L2);
    var L3overrides = {};
    if (routing && typeof routing === 'object') {
      for (var k in routing) {
        if (routing.hasOwnProperty(k)) {
          var v = routing[k];
          if (v !== undefined && k !== 'overrides' && v !== L3[k]) {
            L3overrides[k] = { from: L3[k], to: v };
            L3[k] = v;
          }
        }
      }
    }
    layers.push({
      layer: 3, name: '_routing',
      label: t('租戶覆蓋', 'Tenant Override'),
      values: Object.assign({}, L3), overrides: L3overrides,
      source: routing ? 'tenant' : 'skip',
    });

    // Layer 4: _routing_enforced (NOC)
    layers.push({
      layer: 4, name: '_routing_enforced',
      label: t('平台強制 (NOC)', 'Platform Enforced (NOC)'),
      values: Object.assign({}, L3), overrides: {}, source: 'enforced',
    });

    return { layers: layers, resolved: L3 };
  }

  /* ════════════════════════════════════════════
     Export
     ════════════════════════════════════════════ */

  window.__portalShared = {
    // Data
    RULE_PACK_DATA: RULE_PACK_DATA,
    CATEGORY_LABELS: CATEGORY_LABELS,
    ROUTING_PROFILES: ROUTING_PROFILES,
    DOMAIN_POLICIES: DOMAIN_POLICIES,
    RECEIVER_TYPES: RECEIVER_TYPES,
    RECEIVER_REQUIRED: RECEIVER_REQUIRED,
    TIMING_GUARDRAILS: TIMING_GUARDRAILS,
    ROUTING_DEFAULTS: ROUTING_DEFAULTS,
    RESERVED_KEYS: RESERVED_KEYS,
    RESERVED_PREFIXES: RESERVED_PREFIXES,
    RESERVED_KEY_PATTERNS: RESERVED_KEY_PATTERNS,

    // Utility functions
    parseDuration: parseDuration,
    parseYaml: parseYaml,
    getAllMetricKeys: getAllMetricKeys,
    generateSampleYaml: generateSampleYaml,
    validateConfig: validateConfig,
    simulateAlerts: simulateAlerts,
    resolveRoutingLayers: resolveRoutingLayers,
  };
})();
