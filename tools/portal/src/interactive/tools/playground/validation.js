---
title: "YAML Playground — tenant config parser & validator"
purpose: |
  Playground-local YAML parsing + tenant-config validation. A hand-rolled
  line parser (parseYAML) feeds validateTenantConfig, which checks identity,
  thresholds against known metric keys, receiver shape, and routing duration
  guardrails (group_wait / group_interval / repeat_interval).

  Pre-PR-portal-16 this lived inline in playground.jsx (777 LOC) with 0%
  coverage. NOTE: parseDuration / parseYAML here are PLAYGROUND-LOCAL and
  intentionally NOT the _common/validation/yaml-parser.js versions — the
  contracts differ (this parseDuration returns {ms,value,unit} and supports
  only s/m/h; _common returns seconds and supports s/m/h/d). Left separate to
  preserve behavior; do not merge without reconciling contracts.

  Public API:
    validateTenantConfig(yamlText)  -> { valid, errors, warnings, ... }
    parseYAML(text)                 -> parsed object (lenient)
    parseDuration(str)              -> {ms,value,unit} | null (playground-local)

  Closure deps: window.__t for bilingual error/warning messages (falls back to
  English). Otherwise pure; receives text as args.
---

// i18n fallback — error/warning messages are bilingual (same pattern as the
// orchestrator). Moved with the cluster from playground.jsx (PR-portal-16).
const t = window.__t || ((zh, en) => en);

const KNOWN_METRIC_KEYS = new Set([
  'mysql_connections',
  'mysql_connections_critical',
  'mysql_cpu',
  'mysql_memory',
  'mysql_slow_queries',
  'mysql_query_errors',
  'mysql_replication_lag',
  'pg_connections',
  'pg_connections_critical',
  'pg_cache_hit_ratio',
  'pg_query_time',
  'pg_disk_usage',
  'pg_replication_lag',
  'redis_memory',
  'redis_memory_critical',
  'redis_evictions',
  'redis_connected_clients',
  'redis_keyspace_hits',
  'kafka_lag',
  'kafka_lag_critical',
  'kafka_broker_active',
  'kafka_controller_active',
  'kafka_isr_shrank',
  'kafka_under_replicated',
  'mongo_connections',
  'mongo_connections_critical',
  'mongo_memory',
  'elasticsearch_heap',
  'elasticsearch_heap_critical',
  'elasticsearch_unassigned_shards'
]);

const RECEIVER_TYPES = new Set(['webhook', 'email', 'slack', 'teams', 'rocketchat', 'pagerduty']);

// Strip surrounding quotes from a YAML value
function stripQuotes(s) {
  if (!s) return s;
  if ((s.startsWith('"') && s.endsWith('"')) || (s.startsWith("'") && s.endsWith("'"))) {
    return s.slice(1, -1);
  }
  return s;
}

// Coerce YAML scalars: booleans and numbers stay as-is (string), but quotes are stripped
function coerceValue(raw) {
  const s = stripQuotes(raw);
  if (s === 'true') return true;
  if (s === 'false') return false;
  return s;
}

// Parse inline YAML array: ["a", "b"] → ['a', 'b']
function parseInlineArray(s) {
  if (!s.startsWith('[') || !s.endsWith(']')) return null;
  return s.slice(1, -1).split(',').map(item => stripQuotes(item.trim())).filter(Boolean);
}

// Split "key: value" on the FIRST colon only (preserves URLs like https://...)
function splitKeyValue(line) {
  const idx = line.indexOf(':');
  if (idx === -1) return null;
  const key = line.slice(0, idx).trim();
  const value = line.slice(idx + 1).trim();
  return { key, value };
}

// Simple YAML parser (handles tenant config structure up to 4 indent levels)
function parseYAML(text) {
  try {
    const lines = text.split('\n');
    const result = {};
    let currentTenant = null;
    let level4Key = null;     // key at indent 4 (e.g., _routing)
    let level6Key = null;     // key at indent 6 (e.g., receiver)

    for (let i = 0; i < lines.length; i++) {
      const line = lines[i];
      if (!line.trim() || line.trim().startsWith('#')) continue;

      const indent = line.search(/\S/);
      const trimmed = line.trim();

      // Reset deeper context when indent decreases
      if (indent <= 4) level6Key = null;
      if (indent <= 2) level4Key = null;

      // Pure key line (ends with ":" and nothing after)
      if (trimmed.endsWith(':') && !trimmed.includes(': ')) {
        const key = trimmed.slice(0, -1);
        if (indent === 0) {
          if (key === 'tenants') result.tenants = {};
        } else if (indent === 2 && result.tenants) {
          currentTenant = key;
          result.tenants[currentTenant] = {};
          level4Key = null;
          level6Key = null;
        } else if (indent === 4 && currentTenant) {
          level4Key = key;
          level6Key = null;
          result.tenants[currentTenant][key] = {};
        } else if (indent === 6 && currentTenant && level4Key) {
          level6Key = key;
          if (!result.tenants[currentTenant][level4Key]) {
            result.tenants[currentTenant][level4Key] = {};
          }
          result.tenants[currentTenant][level4Key][key] = {};
        }
        continue;
      }

      // Key-value line
      const kv = splitKeyValue(trimmed);
      if (kv && kv.value !== '') {
        const key = kv.key;
        // Check for inline array
        const arr = parseInlineArray(kv.value);
        const value = arr !== null ? arr : coerceValue(kv.value);

        if (indent === 4 && currentTenant) {
          result.tenants[currentTenant][key] = value;
          // If this is a special key with a simple value (e.g., _silent_mode: "disable")
          if (key.startsWith('_') && typeof value === 'string') {
            level4Key = null; // not a nested object
          }
        } else if (indent === 6 && currentTenant && level4Key) {
          if (!result.tenants[currentTenant][level4Key]) {
            result.tenants[currentTenant][level4Key] = {};
          }
          result.tenants[currentTenant][level4Key][key] = value;
        } else if (indent === 8 && currentTenant && level4Key && level6Key) {
          if (!result.tenants[currentTenant][level4Key][level6Key]) {
            result.tenants[currentTenant][level4Key][level6Key] = {};
          }
          result.tenants[currentTenant][level4Key][level6Key][key] = value;
        }
        continue;
      }

      // List item (- value)
      if (trimmed.startsWith('- ')) {
        const itemValue = stripQuotes(trimmed.slice(2).trim());
        if (indent === 6 && currentTenant && level4Key) {
          const target = result.tenants[currentTenant][level4Key];
          // Convert to array if needed (for group_by etc.)
          // This handles rare cases of block-style arrays at indent 6
        }
      }
    }

    return { success: true, data: result };
  } catch (e) {
    return { success: false, error: e.message };
  }
}

// Duration parser (e.g., "30s", "5m", "1h")
function parseDuration(str) {
  if (!str) return null;
  const match = str.match(/^(\d+)([smh])$/);
  if (!match) return null;
  const [_, value, unit] = match;
  const v = parseInt(value);
  if (unit === 's') return { ms: v * 1000, value: v, unit: 's' };
  if (unit === 'm') return { ms: v * 60 * 1000, value: v, unit: 'm' };
  if (unit === 'h') return { ms: v * 60 * 60 * 1000, value: v, unit: 'h' };
  return null;
}

// Validate duration is within guardrails
function validateDurationRange(str, minMs, maxMs) {

  const dur = parseDuration(str);
  if (!dur) return { valid: false, error: t('無效格式', 'Invalid format') };
  if (dur.ms < minMs || dur.ms > maxMs) {
    return { valid: false, error: t(`超出範圍 (${minMs / 1000}s - ${maxMs / 1000}s)`, `Out of range (${minMs / 1000}s - ${maxMs / 1000}s)`) };
  }
  return { valid: true };
}

// Validate ISO 8601 timestamp
function isValidISO8601(str) {
  if (!str) return false;
  try {
    const date = new Date(str);
    return !isNaN(date.getTime()) && str.includes('T') && str.includes('Z');
  } catch {
    return false;
  }
}

// Main validation logic
function validateTenantConfig(yamlText) {

  const errors = [];
  const warnings = [];
  const metrics = [];
  let thresholdCount = 0;
  let specialKeysCount = 0;
  let routingStatus = 'not_configured';

  // Step 1: Parse YAML
  const parsed = parseYAML(yamlText);
  if (!parsed.success) {
    return {
      valid: false,
      errors: [{ rule: t('YAML 語法', 'YAML Syntax'), message: t(`解析錯誤: ${parsed.error}`, `Parse error: ${parsed.error}`) }],
      warnings: [],
      metrics: [],
      summary: { thresholds: 0, specialKeys: 0, routing: 'error' }
    };
  }

  const config = parsed.data;
  if (!config.tenants) {
    return {
      valid: false,
      errors: [{ rule: t('結構', 'Structure'), message: t('未找到 "tenants" 根鍵', 'No "tenants" root key found') }],
      warnings: [],
      metrics: [],
      summary: { thresholds: 0, specialKeys: 0, routing: 'error' }
    };
  }

  // Step 2: Validate each tenant
  Object.entries(config.tenants).forEach(([tenantId, tenantConfig]) => {
    if (typeof tenantConfig !== 'object' || tenantConfig === null) {
      errors.push({ rule: t('租戶結構', 'Tenant Structure'), message: t(`租戶 "${tenantId}" 不是一個對象`, `Tenant "${tenantId}" is not an object`) });
      return;
    }

    Object.entries(tenantConfig).forEach(([key, value]) => {
      // Threshold validation
      if (KNOWN_METRIC_KEYS.has(key)) {
        thresholdCount++;
        const strVal = String(value);
        if (!/^\d+$/.test(strVal)) {
          errors.push({
            rule: t('閾值格式', 'Threshold Format'),
            message: t(`${tenantId}.${key}: 必須是數字字串, 得到 "${strVal}"`, `${tenantId}.${key}: must be a number string, got "${strVal}"`)
          });
        } else {
          metrics.push({
            name: `${key}`,
            tenant: tenantId,
            value: value,
            labels: { tenant: tenantId, severity: 'warning' }
          });
        }
      } else if (key.startsWith('_')) {
        // Special keys validation
        specialKeysCount++;

        if (key === '_silent_mode') {
          if (value === 'disable') {
            // Valid
          } else if (typeof value === 'object' && value !== null) {
            if (value.expires && !isValidISO8601(value.expires)) {
              errors.push({
                rule: '_silent_mode',
                message: t(`${tenantId}._silent_mode.expires: 無效的 ISO 8601 時間戳`, `${tenantId}._silent_mode.expires: invalid ISO 8601 timestamp`)
              });
            }
          } else {
            errors.push({
              rule: '_silent_mode',
              message: t(`${tenantId}._silent_mode: 必須是 "disable" 或具有 expires 的對象`, `${tenantId}._silent_mode: must be "disable" or object with expires`)
            });
          }
        } else if (key === '_state_maintenance') {
          if (typeof value === 'object' && value !== null) {
            if (value.expires && !isValidISO8601(value.expires)) {
              errors.push({
                rule: '_state_maintenance',
                message: t(`${tenantId}._state_maintenance.expires: 無效的 ISO 8601 時間戳`, `${tenantId}._state_maintenance.expires: invalid ISO 8601 timestamp`)
              });
            }
          } else {
            errors.push({
              rule: '_state_maintenance',
              message: t(`${tenantId}._state_maintenance: 必須是對象`, `${tenantId}._state_maintenance: must be object`)
            });
          }
        } else if (key === '_routing') {
          routingStatus = 'configured';
          if (typeof value === 'object' && value !== null) {
            const routing = value;
            // v2.1.0: receiver_type (flat) or legacy receiver.type (nested)
            const recType = routing.receiver_type || (routing.receiver && routing.receiver.type);
            if (!recType && !routing.profile) {
              errors.push({
                rule: '_routing',
                message: t(`${tenantId}._routing: 需要 "receiver_type"、"profile" 或 "receiver" 之一`, `${tenantId}._routing: needs "receiver_type", "profile", or "receiver"`)
              });
            } else if (recType && !RECEIVER_TYPES.has(recType)) {
              errors.push({
                rule: '_routing.receiver_type',
                message: t(`${tenantId}._routing.receiver_type: 未知的類型 "${recType}"`, `${tenantId}._routing.receiver_type: unknown type "${recType}"`)
              });
            }

            // Timing guardrails (values are already unquoted strings)
            if (routing.group_wait) {
              const gwCheck = validateDurationRange(String(routing.group_wait), 5000, 5 * 60 * 1000);
              if (!gwCheck.valid) {
                errors.push({
                  rule: '_routing.group_wait',
                  message: `${tenantId}._routing.group_wait: ${gwCheck.error}`
                });
              }
            }
            if (routing.group_interval) {
              const giCheck = validateDurationRange(String(routing.group_interval), 5000, 5 * 60 * 1000);
              if (!giCheck.valid) {
                errors.push({
                  rule: '_routing.group_interval',
                  message: `${tenantId}._routing.group_interval: ${giCheck.error}`
                });
              }
            }
            if (routing.repeat_interval) {
              const riCheck = validateDurationRange(String(routing.repeat_interval), 60000, 72 * 60 * 60 * 1000);
              if (!riCheck.valid) {
                errors.push({
                  rule: '_routing.repeat_interval',
                  message: `${tenantId}._routing.repeat_interval: ${riCheck.error}`
                });
              }
            }
          }
        } else {
          warnings.push({
            rule: t('未知特殊鍵', 'Unknown Special Key'),
            message: t(`${tenantId}.${key}: 未知的特殊鍵 (以 _ 開頭)`, `${tenantId}.${key}: unknown special key (starts with _)`)
          });
        }
      } else {
        // Unknown regular key
        warnings.push({
          rule: t('未知鍵', 'Unknown Key'),
          message: t(`${tenantId}.${key}: 不在已知指標列表中 (可能是拼寫錯誤?)`, `${tenantId}.${key}: not in known metrics list (possible typo?)`)
        });
      }
    });
  });

  return {
    valid: errors.length === 0,
    errors,
    warnings,
    metrics,
    summary: {
      thresholds: thresholdCount,
      specialKeys: specialKeysCount,
      routing: routingStatus
    }
  };
}

export { validateTenantConfig, parseYAML, parseDuration };
