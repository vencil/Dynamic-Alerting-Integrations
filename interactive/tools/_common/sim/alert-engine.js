---
title: "_common — Alert engine: validation + simulation + routing resolver"
purpose: |
  Pure-function alerting engine pulled out of portal-shared.jsx so
  multi-tool flows (alert simulator UI / self-service portal /
  notification previewer) can share one canonical implementation.

  Three exported functions, all stateless:

  generateSampleYaml(packs, withProfile)
    Emit a starter tenant.yaml string from selected Rule Pack
    defaults. Used to seed the YAML editor on first load.

  validateConfig(config, packs)
    Lint a parsed tenant config against the validation constants
    (RECEIVER_TYPES / TIMING_GUARDRAILS / RESERVED_KEYS) + cross-
    references to Rule Pack metric keys + DOMAIN_POLICIES. Returns
    {issues: [{level, field, msg}]}.

  simulateAlerts(config, metricValues)
    Given current metric readings, compute which alerts fire and at
    what severity. Returns array of {metric, current, threshold,
    critical_threshold, firing, critical_firing, severity, ...}.

  resolveRoutingLayers(config)
    Apply the four-layer routing model (ADR-007): L1 platform
    defaults → L2 routing profile → L3 tenant override → L4 platform-
    enforced. Returns {layers: [...], resolved: <final>}.

  Closure deps: reads window.__t, window.__RULE_PACK_DATA,
  window.__getAllMetricKeys, window.__RECEIVER_TYPES,
  window.__RESERVED_KEYS, window.__RESERVED_PREFIXES,
  window.__TIMING_GUARDRAILS, window.__ROUTING_DEFAULTS,
  window.__ROUTING_PROFILES, window.__DOMAIN_POLICIES. Pulled at
  call time so consumers loaded after the data files see them.

  Backward compatibility: portal-shared.jsx re-exports all 4
  functions on window.__portalShared unchanged.
---

function generateSampleYaml(selectedPacks, withProfile) {
  const t = window.__t || ((zh, en) => en);
  const RULE_PACK_DATA = window.__RULE_PACK_DATA || {};

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

function validateConfig(config, selectedPacks) {
  const t = window.__t || ((zh, en) => en);
  const getAllMetricKeys = window.__getAllMetricKeys;
  const RECEIVER_TYPES = window.__RECEIVER_TYPES || [];
  const RESERVED_KEYS = window.__RESERVED_KEYS || new Set();
  const RESERVED_PREFIXES = window.__RESERVED_PREFIXES || [];
  const TIMING_GUARDRAILS = window.__TIMING_GUARDRAILS || {};
  const ROUTING_PROFILES = window.__ROUTING_PROFILES || {};
  const DOMAIN_POLICIES = window.__DOMAIN_POLICIES || {};
  const parseDuration = window.__parseDuration;

  const issues = [];
  const info = [];
  const knownMetrics = new Set((getAllMetricKeys ? getAllMetricKeys(selectedPacks) : []).map(m => m.key));

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
        const secs = parseDuration ? parseDuration(val) : null;
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

function resolveRoutingLayers(config) {
  const t = window.__t || ((zh, en) => en);
  const ROUTING_DEFAULTS = window.__ROUTING_DEFAULTS || {};
  const ROUTING_PROFILES = window.__ROUTING_PROFILES || {};

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

window.__generateSampleYaml = generateSampleYaml;
window.__validateConfig = validateConfig;
window.__simulateAlerts = simulateAlerts;
window.__resolveRoutingLayers = resolveRoutingLayers;

// TD-030c: ESM exports for esbuild bundle + Vitest. Removed in TD-030z.
// <!-- jsx-loader-compat: ignore -->
export { generateSampleYaml, validateConfig, simulateAlerts, resolveRoutingLayers };
