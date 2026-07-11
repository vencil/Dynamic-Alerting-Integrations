---
title: "Config Lint — engine (parser + rules)"
purpose: |
  Pure config-lint engine: a lenient YAML-ish parser (parseYaml/parseValue) and
  LINT_RULES, a set of best-practice checks (threshold sanity, missing
  warning/critical pairs, critical<warning ordering, routing issues, ...).
  lintConfig(yamlText) parses, runs every rule, tags + severity-sorts findings.

  Pre-PR-portal-20 this was inline in config-lint.jsx (458 LOC) with 0%
  coverage — including the rule check() functions where the real lint logic
  lives. Extracted here so the rules can be exercised directly. LINT_RULES is
  re-exported because the component also renders the rule list.

  Public API:
    lintConfig(yamlText) -> { ok, findings[], tenantCount, error? }
    LINT_RULES           (also rendered as the rule list)
    parseYaml(text)      (exposed for testing)

  Closure deps: window.__t for bilingual rule categories/messages (falls back
  to English).
---

// i18n fallback — rule categories + finding messages are bilingual.
// Evaluated at module load (LINT_RULES category fields call t()); the host page
// sets window.__t before the bundle loads. Moved with the cluster (PR-portal-20).
const t = window.__t || ((zh, en) => en);

/* ── Simple YAML parser ── */
function parseYaml(text) {
  const tenants = {};
  let currentTenant = null;
  let currentSubKey = null;
  let subObj = null;

  for (const line of text.split('\n')) {
    if (!line.trim() || line.trim().startsWith('#')) continue;
    const indent = line.search(/\S/);

    if (indent === 0 && line.includes(':')) {
      const key = line.split(':')[0].trim();
      currentTenant = key;
      tenants[currentTenant] = {};
      currentSubKey = null;
      subObj = null;
      continue;
    }

    if (currentTenant && indent >= 2) {
      const trimmed = line.trim();
      if (trimmed.includes(':')) {
        const colonIdx = trimmed.indexOf(':');
        const key = trimmed.substring(0, colonIdx).trim();
        const rawVal = trimmed.substring(colonIdx + 1).trim();

        if (indent === 2 && !rawVal) {
          // Nested object like _routing:
          currentSubKey = key;
          subObj = {};
          tenants[currentTenant][key] = subObj;
        } else if (indent > 2 && subObj && currentSubKey) {
          // Sub-key of nested object
          subObj[key] = parseValue(rawVal);
        } else {
          currentSubKey = null;
          subObj = null;
          tenants[currentTenant][key] = parseValue(rawVal);
        }
      }
    }
  }
  return tenants;
}

function parseValue(raw) {
  if (raw === 'true') return true;
  if (raw === 'false') return false;
  if (raw === 'disable') return 'disable';
  if (raw.startsWith('"') && raw.endsWith('"')) return raw.slice(1, -1);
  const num = Number(raw);
  if (!isNaN(num) && raw !== '') return num;
  return raw;
}
const LINT_RULES = [
  {
    id: 'threshold-too-high',
    severity: 'warning',
    category: t('閾值', 'Threshold'),
    check: (tenants) => {
      const findings = [];
      for (const [tenant, keys] of Object.entries(tenants)) {
        for (const [key, val] of Object.entries(keys)) {
          if (typeof val === 'number' && key.includes('_percent') || key.includes('_usage') || key.includes('cpu')) {
            if (typeof val === 'number' && val > 95) {
              findings.push({ tenant, key, message: t(`${key} = ${val} 非常高（>95），可能來不及反應`, `${key} = ${val} is very high (>95) — may not leave reaction time`) });
            }
          }
        }
      }
      return findings;
    },
  },
  {
    id: 'threshold-too-low',
    severity: 'info',
    category: t('閾值', 'Threshold'),
    check: (tenants) => {
      const findings = [];
      for (const [tenant, keys] of Object.entries(tenants)) {
        for (const [key, val] of Object.entries(keys)) {
          if (typeof val === 'number' && key.includes('_warning') && !key.includes('_critical') && val < 10) {
            findings.push({ tenant, key, message: t(`${key} = ${val} 非常低（<10），可能產生過多噪音`, `${key} = ${val} is very low (<10) — may generate excessive noise`) });
          }
        }
      }
      return findings;
    },
  },
  {
    id: 'missing-critical-pair',
    severity: 'warning',
    category: t('嚴重度', 'Severity'),
    check: (tenants) => {
      const findings = [];
      for (const [tenant, keys] of Object.entries(tenants)) {
        for (const key of Object.keys(keys)) {
          if (key.endsWith('_warning') && !key.endsWith('_warning_critical')) {
            const critKey = key + '_critical';
            if (!(critKey in keys)) {
              findings.push({ tenant, key, message: t(`${key} 有設定但缺少對應的 ${critKey}`, `${key} is set but missing paired ${critKey}`) });
            }
          }
        }
      }
      return findings;
    },
  },
  {
    id: 'critical-lower-than-warning',
    severity: 'error',
    category: t('嚴重度', 'Severity'),
    check: (tenants) => {
      const findings = [];
      for (const [tenant, keys] of Object.entries(tenants)) {
        for (const [key, val] of Object.entries(keys)) {
          if (key.endsWith('_warning_critical') && typeof val === 'number') {
            const warnKey = key.replace('_critical', '');
            const warnVal = keys[warnKey];
            if (typeof warnVal === 'number' && val <= warnVal) {
              findings.push({ tenant, key, message: t(`Critical (${val}) ≤ Warning (${warnVal}) — critical 應高於 warning`, `Critical (${val}) ≤ Warning (${warnVal}) — critical should be higher than warning`) });
            }
          }
        }
      }
      return findings;
    },
  },
  {
    id: 'no-routing',
    severity: 'warning',
    category: t('路由', 'Routing'),
    check: (tenants) => {
      const findings = [];
      for (const [tenant, keys] of Object.entries(tenants)) {
        if (tenant.startsWith('_')) continue;
        if (!keys['_routing'] && !keys['_routing.receiver_type']) {
          // Check if any non-_ key exists (actual threshold)
          const hasThresholds = Object.keys(keys).some(k => !k.startsWith('_'));
          if (hasThresholds) {
            findings.push({ tenant, key: '_routing', message: t(`Tenant ${tenant} 有設定閾值但沒有配置路由`, `Tenant ${tenant} has thresholds but no routing configured`) });
          }
        }
      }
      return findings;
    },
  },
  {
    id: 'group-wait-too-low',
    severity: 'warning',
    category: t('路由', 'Routing'),
    check: (tenants) => {
      const findings = [];
      for (const [tenant, keys] of Object.entries(tenants)) {
        const routing = keys['_routing'];
        if (routing && typeof routing === 'object') {
          const gw = routing.group_wait;
          if (gw && (gw === '1s' || gw === '2s' || gw === '3s' || gw === '4s')) {
            findings.push({ tenant, key: '_routing.group_wait', message: t(`group_wait = ${gw} 太短，建議 ≥ 5s 避免告警碎片化`, `group_wait = ${gw} is too short — recommend ≥ 5s to avoid alert fragmentation`) });
          }
        }
      }
      return findings;
    },
  },
  {
    id: 'repeat-interval-too-long',
    severity: 'info',
    category: t('路由', 'Routing'),
    check: (tenants) => {
      const findings = [];
      for (const [tenant, keys] of Object.entries(tenants)) {
        const routing = keys['_routing'];
        if (routing && typeof routing === 'object') {
          const ri = routing.repeat_interval;
          if (ri && ri.endsWith('h')) {
            const hours = parseInt(ri);
            if (hours > 72) {
              findings.push({ tenant, key: '_routing.repeat_interval', message: t(`repeat_interval = ${ri} 超過上限 72h`, `repeat_interval = ${ri} exceeds maximum of 72h`) });
            } else if (hours > 24) {
              findings.push({ tenant, key: '_routing.repeat_interval', message: t(`repeat_interval = ${ri} 很長，可能錯過持續性問題`, `repeat_interval = ${ri} is quite long — may miss persistent issues`) });
            }
          }
        }
      }
      return findings;
    },
  },
  {
    id: 'disabled-all-in-pack',
    severity: 'info',
    category: t('配置', 'Config'),
    check: (tenants) => {
      const findings = [];
      for (const [tenant, keys] of Object.entries(tenants)) {
        if (tenant.startsWith('_')) continue;
        const disabled = Object.entries(keys).filter(([k, v]) => v === 'disable');
        if (disabled.length > 3) {
          findings.push({ tenant, key: '-', message: t(`${disabled.length} 個 key 被 disable，考慮是否需要該 Rule Pack`, `${disabled.length} keys disabled — consider whether this Rule Pack is needed`) });
        }
      }
      return findings;
    },
  },
  {
    id: 'routing-profile-undefined',
    severity: 'error',
    category: t('路由設定檔', 'Routing Profiles'),
    check: (tenants) => {
      const findings = [];
      const profiles = tenants['routing_profiles'] || {};
      for (const [tenant, keys] of Object.entries(tenants)) {
        if (tenant.startsWith('_') || tenant === 'routing_profiles') continue;
        const routing = keys['_routing'];
        if (routing && typeof routing === 'object' && routing.profile) {
          if (!profiles[routing.profile]) {
            findings.push({ tenant, key: '_routing.profile', message: t(`引用的 profile "${routing.profile}" 未在 routing_profiles 中定義`, `Referenced profile "${routing.profile}" is not defined in routing_profiles`) });
          }
        }
      }
      return findings;
    },
  },
  {
    id: 'domain-policy-violation',
    severity: 'error',
    category: t('域名策略', 'Domain Policy'),
    check: (tenants) => {
      const findings = [];
      const policy = tenants['_domain_policy'];
      if (!policy) return findings;
      const denied = policy.denied_domains || [];
      for (const [tenant, keys] of Object.entries(tenants)) {
        if (tenant.startsWith('_') || tenant === 'routing_profiles') continue;
        const routing = keys['_routing'];
        if (routing && typeof routing === 'object' && routing.webhook_url) {
          for (const d of denied) {
            const pattern = d.replace('*.', '');
            if (routing.webhook_url.includes(pattern)) {
              findings.push({ tenant, key: '_routing.webhook_url', message: t(`Webhook URL 匹配禁止域名 "${d}"`, `Webhook URL matches denied domain "${d}"`) });
            }
          }
        }
      }
      return findings;
    },
  },
  {
    id: 'instance-mapping-missing-partition',
    severity: 'warning',
    category: t('實例映射', 'Instance Mapping'),
    check: (tenants) => {
      const findings = [];
      const mappings = tenants['_instance_mapping'];
      if (!Array.isArray(mappings)) return findings;
      for (const m of mappings) {
        if (!m.partition_label) {
          findings.push({ tenant: '_instance_mapping', key: m.instance || '?', message: t(`映射缺少 partition_label`, `Mapping missing partition_label`) });
        }
        if (!m.partitions || m.partitions.length === 0) {
          findings.push({ tenant: '_instance_mapping', key: m.instance || '?', message: t(`映射缺少 partitions 列表`, `Mapping missing partitions list`) });
        }
      }
      return findings;
    },
  },
];

/* ── Lint engine: parse, run every rule, tag + severity-sort findings ── */
function lintConfig(yamlText) {
  try {
    const tenants = parseYaml(yamlText);
    const allFindings = [];
    for (const rule of LINT_RULES) {
      const findings = rule.check(tenants);
      for (const f of findings) {
        allFindings.push({ ...f, ruleId: rule.id, severity: rule.severity, category: rule.category });
      }
    }
    // Sort: error -> warning -> info
    const order = { error: 0, warning: 1, info: 2 };
    allFindings.sort((a, b) => order[a.severity] - order[b.severity]);
    return { ok: true, findings: allFindings, tenantCount: Object.keys(tenants).filter(k => !k.startsWith('_')).length };
  } catch (e) {
    return { ok: false, findings: [], error: e.message, tenantCount: 0 };
  }
}

// Legacy jsx-loader path: expose as window globals (see PR-portal-12 / TD-030z).

export { parseYaml, LINT_RULES, lintConfig };
