import React, { useState, useEffect, useMemo } from 'react';

const YAML_TEMPLATES = {
  mariadb: `tenants:
  db-a:
    mysql_connections: "70"
    mysql_connections_critical: "95"
    mysql_cpu: "80"
    _silent_mode: "disable"
    _severity_dedup:
      enabled: true
    _routing:
      receiver:
        type: "webhook"
        url: "https://webhook.example.com/alerts"
      group_by: ["alertname", "severity"]
      group_wait: "30s"
      repeat_interval: "4h"`,
  postgresql: `tenants:
  db-b:
    pg_connections: "150"
    pg_connections_critical: "200"
    pg_cache_hit_ratio: "85"
    pg_query_time: "5000"
    _state_maintenance:
      expires: "2026-03-20T06:00:00Z"
    _routing:
      receiver:
        type: "slack"
        channel: "#alerts"
      group_wait: "1m"
      group_interval: "5m"
      repeat_interval: "12h"`,
  redis: `tenants:
  cache:
    redis_memory: "80"
    redis_memory_critical: "95"
    redis_evictions: "1000"
    redis_connected_clients: "5000"
    _silent_mode:
      expires: "2026-03-13T00:00:00Z"
    _severity_dedup:
      enabled: false
    _routing:
      receiver:
        type: "email"
        to: "ops@example.com"
      group_wait: "45s"
      repeat_interval: "6h"`,
  kafka: `tenants:
  streaming:
    kafka_lag: "100000"
    kafka_lag_critical: "500000"
    kafka_broker_active: "3"
    kafka_controller_active: "1"
    kafka_isr_shrank: "0"
    _routing:
      receiver:
        type: "teams"
        webhook_url: "https://teams.example.com/webhook"
      group_by: ["topic", "partition"]
      group_wait: "2m"
      group_interval: "3m"
      repeat_interval: "24h"`
};

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

// Simple YAML parser (handles basic structure)
function parseYAML(text) {
  try {
    const lines = text.split('\n');
    const result = {};
    let currentTenant = null;
    let currentKey = null;
    let indentLevel = 0;

    for (let i = 0; i < lines.length; i++) {
      const line = lines[i];
      if (!line.trim() || line.trim().startsWith('#')) continue;

      const indent = line.search(/\S/);
      const trimmed = line.trim();

      if (trimmed.endsWith(':')) {
        const key = trimmed.slice(0, -1);
        if (indent === 0) {
          if (key === 'tenants') {
            result.tenants = {};
          }
        } else if (indent === 2 && result.tenants) {
          currentTenant = key;
          result.tenants[currentTenant] = {};
        } else if (indent === 4 && currentTenant) {
          currentKey = key;
          if (key === '_routing' || key === '_silent_mode' || key === '_state_maintenance' || key === '_severity_dedup') {
            result.tenants[currentTenant][key] = {};
          }
        }
      } else if (trimmed.includes(':') && !trimmed.endsWith(':')) {
        const [key, value] = trimmed.split(':').map(s => s.trim());
        if (currentTenant && indent === 4) {
          result.tenants[currentTenant][key] = value;
        } else if (currentTenant && currentKey && indent === 6) {
          if (!result.tenants[currentTenant][currentKey]) {
            result.tenants[currentTenant][currentKey] = {};
          }
          result.tenants[currentTenant][currentKey][key] = value;
        }
      } else if (trimmed.startsWith('- ') && currentTenant && currentKey && indent === 6) {
        if (!Array.isArray(result.tenants[currentTenant][currentKey])) {
          result.tenants[currentTenant][currentKey] = [];
        }
        result.tenants[currentTenant][currentKey].push(trimmed.slice(2).trim());
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
  if (!dur) return { valid: false, error: 'Invalid format' };
  if (dur.ms < minMs || dur.ms > maxMs) {
    return { valid: false, error: `Out of range (${minMs / 1000}s - ${maxMs / 1000}s)` };
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
  let routingStatus = 'not configured';

  // Step 1: Parse YAML
  const parsed = parseYAML(yamlText);
  if (!parsed.success) {
    return {
      valid: false,
      errors: [{ rule: 'YAML Syntax', message: `Parse error: ${parsed.error}` }],
      warnings: [],
      metrics: [],
      summary: { thresholds: 0, specialKeys: 0, routing: 'error' }
    };
  }

  const config = parsed.data;
  if (!config.tenants) {
    return {
      valid: false,
      errors: [{ rule: 'Structure', message: 'No "tenants" root key found' }],
      warnings: [],
      metrics: [],
      summary: { thresholds: 0, specialKeys: 0, routing: 'error' }
    };
  }

  // Step 2: Validate each tenant
  Object.entries(config.tenants).forEach(([tenantId, tenantConfig]) => {
    if (typeof tenantConfig !== 'object' || tenantConfig === null) {
      errors.push({ rule: 'Tenant Structure', message: `Tenant "${tenantId}" is not an object` });
      return;
    }

    Object.entries(tenantConfig).forEach(([key, value]) => {
      // Threshold validation
      if (KNOWN_METRIC_KEYS.has(key)) {
        thresholdCount++;
        if (typeof value !== 'string' || !/^\d+$/.test(value)) {
          errors.push({
            rule: 'Threshold Format',
            message: `${tenantId}.${key}: must be quoted number string, got "${value}"`
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
                message: `${tenantId}._silent_mode.expires: invalid ISO 8601 timestamp`
              });
            }
          } else {
            errors.push({
              rule: '_silent_mode',
              message: `${tenantId}._silent_mode: must be "disable" or object with expires`
            });
          }
        } else if (key === '_state_maintenance') {
          if (typeof value === 'object' && value !== null) {
            if (value.expires && !isValidISO8601(value.expires)) {
              errors.push({
                rule: '_state_maintenance',
                message: `${tenantId}._state_maintenance.expires: invalid ISO 8601 timestamp`
              });
            }
          } else {
            errors.push({
              rule: '_state_maintenance',
              message: `${tenantId}._state_maintenance: must be object`
            });
          }
        } else if (key === '_severity_dedup') {
          if (typeof value === 'object' && value !== null && 'enabled' in value) {
            if (typeof value.enabled !== 'boolean' && value.enabled !== 'true' && value.enabled !== 'false') {
              errors.push({
                rule: '_severity_dedup',
                message: `${tenantId}._severity_dedup.enabled: must be boolean`
              });
            }
          }
        } else if (key === '_routing') {
          routingStatus = 'configured';
          if (typeof value === 'object' && value !== null) {
            const routing = value;
            if (!routing.receiver) {
              errors.push({
                rule: '_routing',
                message: `${tenantId}._routing: must have "receiver" field`
              });
            } else {
              if (!routing.receiver.type) {
                errors.push({
                  rule: '_routing.receiver',
                  message: `${tenantId}._routing.receiver: must have "type" field`
                });
              } else if (!RECEIVER_TYPES.has(routing.receiver.type)) {
                errors.push({
                  rule: '_routing.receiver.type',
                  message: `${tenantId}._routing.receiver.type: unknown type "${routing.receiver.type}"`
                });
              }
            }

            // Timing guardrails
            if (routing.group_wait) {
              const gwCheck = validateDurationRange(routing.group_wait, 5000, 5 * 60 * 1000);
              if (!gwCheck.valid) {
                errors.push({
                  rule: '_routing.group_wait',
                  message: `${tenantId}._routing.group_wait: ${gwCheck.error}`
                });
              }
            }
            if (routing.group_interval) {
              const giCheck = validateDurationRange(routing.group_interval, 5000, 5 * 60 * 1000);
              if (!giCheck.valid) {
                errors.push({
                  rule: '_routing.group_interval',
                  message: `${tenantId}._routing.group_interval: ${giCheck.error}`
                });
              }
            }
            if (routing.repeat_interval) {
              const riCheck = validateDurationRange(routing.repeat_interval, 60000, 72 * 60 * 60 * 1000);
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
            rule: 'Unknown Special Key',
            message: `${tenantId}.${key}: unknown special key (starts with _)`
          });
        }
      } else {
        // Unknown regular key
        warnings.push({
          rule: 'Unknown Key',
          message: `${tenantId}.${key}: not in known metrics list (possible typo?)`
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

export default function TenantYAMLPlayground() {
  const [yaml, setYaml] = useState(YAML_TEMPLATES.mariadb);
  const [selectedTemplate, setSelectedTemplate] = useState('mariadb');

  const validation = useMemo(() => validateTenantConfig(yaml), [yaml]);

  const handleResetExample = () => {
    setYaml(YAML_TEMPLATES[selectedTemplate]);
  };

  const handleTemplateChange = (e) => {
    const template = e.target.value;
    setSelectedTemplate(template);
    setYaml(YAML_TEMPLATES[template]);
  };

  return (
    <div className="flex h-screen bg-gray-50">
      {/* Header */}
      <div className="fixed top-0 left-0 right-0 bg-white border-b border-gray-200 p-4 shadow-sm z-10">
        <div className="max-w-7xl mx-auto flex items-center justify-between">
          <div>
            <h1 className="text-2xl font-bold text-gray-900">Tenant YAML Validator</h1>
            <p className="text-sm text-gray-500 mt-1">Interactive playground for tenant configuration validation</p>
          </div>
          <div className="flex gap-3">
            <select
              value={selectedTemplate}
              onChange={handleTemplateChange}
              className="px-3 py-2 bg-white border border-gray-300 rounded-md text-sm font-medium text-gray-700 hover:bg-gray-50 focus:outline-none focus:ring-2 focus:ring-blue-500"
            >
              <option value="mariadb">MariaDB Example</option>
              <option value="postgresql">PostgreSQL Example</option>
              <option value="redis">Redis Example</option>
              <option value="kafka">Kafka Example</option>
            </select>
            <button
              onClick={handleResetExample}
              className="px-4 py-2 bg-gray-600 text-white rounded-md text-sm font-medium hover:bg-gray-700 focus:outline-none focus:ring-2 focus:ring-gray-500"
            >
              Reset Example
            </button>
          </div>
        </div>
      </div>

      {/* Main Content */}
      <div className="flex w-full pt-24">
        {/* Left Pane: YAML Editor */}
        <div className="w-1/2 border-r border-gray-200 flex flex-col bg-white">
          <div className="px-6 py-4 border-b border-gray-200">
            <h2 className="text-lg font-semibold text-gray-900">Tenant YAML</h2>
            <p className="text-xs text-gray-500 mt-1">Edit YAML below. Validation updates in real-time.</p>
          </div>
          <div className="flex-1 overflow-hidden flex">
            <div className="w-12 bg-gray-100 border-r border-gray-200 flex flex-col items-center py-4 text-xs text-gray-500 font-mono">
              {yaml.split('\n').map((_, i) => (
                <div key={i} className="h-6 flex items-center justify-center">
                  {i + 1}
                </div>
              ))}
            </div>
            <textarea
              value={yaml}
              onChange={(e) => setYaml(e.target.value)}
              className="flex-1 p-4 font-mono text-sm text-gray-900 bg-white focus:outline-none focus:ring-2 focus:ring-inset focus:ring-blue-500 resize-none"
              spellCheck="false"
              style={{ fontFamily: 'Courier New, monospace' }}
            />
          </div>
        </div>

        {/* Right Pane: Validation Results */}
        <div className="w-1/2 flex flex-col bg-gray-50 overflow-hidden">
          {/* Validation Summary */}
          <div className="px-6 py-4 border-b border-gray-200 bg-white">
            <div className="flex items-center justify-between">
              <div>
                <h2 className="text-lg font-semibold text-gray-900">Validation Results</h2>
                <p className="text-xs text-gray-500 mt-1">
                  {validation.errors.length === 0
                    ? 'All checks passed!'
                    : `${validation.errors.length} error(s) found`}
                </p>
              </div>
              <div className="text-right">
                <div className="text-3xl font-bold">
                  {validation.valid ? (
                    <span className="text-green-600">✓</span>
                  ) : (
                    <span className="text-red-600">✗</span>
                  )}
                </div>
              </div>
            </div>
          </div>

          {/* Results Scroll Area */}
          <div className="flex-1 overflow-y-auto p-6 space-y-6">
            {/* Summary Stats */}
            <div className="grid grid-cols-3 gap-4">
              <div className="bg-white rounded-lg p-4 border border-gray-200">
                <div className="text-2xl font-bold text-blue-600">{validation.summary.thresholds}</div>
                <div className="text-xs text-gray-600 mt-1">Thresholds Configured</div>
              </div>
              <div className="bg-white rounded-lg p-4 border border-gray-200">
                <div className="text-2xl font-bold text-purple-600">{validation.summary.specialKeys}</div>
                <div className="text-xs text-gray-600 mt-1">Special Keys</div>
              </div>
              <div className="bg-white rounded-lg p-4 border border-gray-200">
                <div
                  className={`text-2xl font-bold ${
                    validation.summary.routing === 'configured'
                      ? 'text-green-600'
                      : validation.summary.routing === 'error'
                      ? 'text-red-600'
                      : 'text-gray-400'
                  }`}
                >
                  {validation.summary.routing === 'configured' ? '✓' : '○'}
                </div>
                <div className="text-xs text-gray-600 mt-1">Routing Status</div>
              </div>
            </div>

            {/* Errors */}
            {validation.errors.length > 0 && (
              <div>
                <h3 className="font-semibold text-red-700 mb-3 flex items-center gap-2">
                  <span className="text-lg">✗</span> Errors ({validation.errors.length})
                </h3>
                <div className="space-y-2">
                  {validation.errors.map((err, i) => (
                    <div
                      key={i}
                      className="bg-red-50 border border-red-200 rounded-md p-3 text-sm text-red-800"
                    >
                      <div className="font-mono text-xs text-red-700 mb-1">{err.rule}</div>
                      <div>{err.message}</div>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {/* Warnings */}
            {validation.warnings.length > 0 && (
              <div>
                <h3 className="font-semibold text-amber-700 mb-3 flex items-center gap-2">
                  <span className="text-lg">⚠</span> Warnings ({validation.warnings.length})
                </h3>
                <div className="space-y-2">
                  {validation.warnings.map((warn, i) => (
                    <div
                      key={i}
                      className="bg-amber-50 border border-amber-200 rounded-md p-3 text-sm text-amber-800"
                    >
                      <div className="font-mono text-xs text-amber-700 mb-1">{warn.rule}</div>
                      <div>{warn.message}</div>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {/* Metrics Preview */}
            {validation.metrics.length > 0 && (
              <div>
                <h3 className="font-semibold text-gray-900 mb-3 flex items-center gap-2">
                  <span className="text-lg">📊</span> Exported Metrics ({validation.metrics.length})
                </h3>
                <div className="space-y-2">
                  {validation.metrics.map((metric, i) => (
                    <div
                      key={i}
                      className="bg-blue-50 border border-blue-200 rounded-md p-3 text-sm font-mono text-gray-800"
                    >
                      <div className="text-blue-700 font-semibold">{metric.name}</div>
                      <div className="text-xs text-gray-600 mt-1">
                        tenant="{metric.tenant}" severity="warning" value={metric.value}
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {/* All Valid */}
            {validation.valid && validation.errors.length === 0 && (
              <div className="bg-green-50 border border-green-200 rounded-md p-4 text-center">
                <div className="text-2xl mb-2">✓</div>
                <div className="text-green-800 font-semibold">Configuration is valid!</div>
                <div className="text-xs text-green-700 mt-2">
                  {validation.summary.thresholds} thresholds • {validation.summary.specialKeys} special keys •
                  {validation.summary.routing === 'configured' ? ' routing configured' : ' no routing'}
                </div>
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
