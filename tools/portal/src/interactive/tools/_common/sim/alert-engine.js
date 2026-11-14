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
    (RECEIVER_TYPES / TIMING_GUARDRAILS) + DOMAIN_POLICIES; no key
    name is judged here (ADR-038 §11). Returns
    {issues: [{level, field, msg}]}.

  isFiring(current, threshold, inverted)
    Canonical threshold-crossing primitive. Strict comparison to mirror
    the real Prometheus alert rules in rule-packs/*.yaml (e.g.
    `... > 0.05`, or `< threshold` for inverted/lower-bound metrics): a
    reading exactly equal to the threshold does NOT fire. Shared by
    simulateAlerts + simulateWithDedup so the two simulator surfaces can
    never diverge on the comparison operator.

  simulateAlerts(config, metricValues)
    Given current metric readings, compute which alerts fire and at what
    severity (strict `>`, via isFiring). The `_critical` companion key
    escalates the same reading, and so does a `:critical` suffix on the
    base key. Returns array of {metric, current, threshold,
    critical_threshold, firing, critical_firing, severity, ...}.
    Thresholds are read by ../validation/threshold-value.js, as the
    exporter reads them (#3865): a value it serves no number for is a row
    with its text in `raw` and threshold null (the platform default the
    exporter falls back to is not simulated), severity 'unreadable' unless
    its critical tier fires; the `_critical` companion is read whole (no
    `:severity`), and an unreadable one puts its text in
    `critical_unreadable`. A map or a list (a scheduled value) has no row.

  simulateWithDedup(config, metrics, dedupEnabled, alertDefs)
    Alert-simulator model: each config key is an independent alert whose
    definition comes from alertDefs (the ALERT_DEFS map, injected by the
    caller — shape adapter). Buckets into {firing, suppressed, ok,
    unreadable}; `unreadable` lists {key, def, raw} for each value the
    exporter does not read (#3865), which is in no other bucket. Only the
    NUMBER is read the exporter's way: the severity is the def's (a
    `:critical` suffix is not applied) and a disabled base does not switch
    its `_critical` key off — its one page has number fields only;
    honours inverted defs (via isFiring) and severity-dedup (a firing
    critical suppresses its matching warning). Behaviour relocated verbatim
    from alert-simulator.jsx so it is shared + unit-tested.

  unreadableThresholdMessage(raw, tier = 'base')
    The sentence (window.__t) the pages show for such a value. It says what
    the exporter does instead, which differs for a `_critical` key.

  resolveRoutingLayers(config)
    Apply the four-layer routing model (ADR-007): L1 platform
    defaults → L2 routing profile → L3 tenant override → L4 platform-
    enforced. Returns {layers: [...], resolved: <final>}.

  Data deps are ESM-imported from ../data/* and ../validation/*
  (esbuild orders them ahead of this module — TRK-230z Wave 2 retired the
  jsx-loader-era `window.__X || fallback` call-time reads). The only
  runtime global left is window.__t (host-page i18n), read per-call.

  Consumers import these functions directly via ESM (dev-rules §S6).
---

// `counterexampleComment` lives next to the data it renders (`rule-packs.js`),
// not here. It was local until a THIRD caller appeared outside this module and,
// unable to import it, hand-copied the sentence — which had already drifted
// before anyone reviewed it. The fix is where it is defined, not another
// factoring inside one caller (#1344).
import {
  RULE_PACK_DATA, getDeclaredKeys, counterexampleComment,
} from '../data/rule-packs.js';
import { ROUTING_DEFAULTS, ROUTING_PROFILES, DOMAIN_POLICIES } from '../data/routing-profiles.js';
import { RECEIVER_TYPES, TIMING_GUARDRAILS } from '../validation/constants.js';
import { parseDuration } from '../validation/yaml-parser.js';
import { readThresholdRow, isCriticalKey } from '../validation/threshold-value.js';

function generateSampleYaml(selectedPacks, withProfile) {
  const t = window.__t || ((zh, en) => en);

  const lines = [`# ${t('從 Rule Pack 自動產生的 Tenant YAML', 'Auto-generated tenant YAML from Rule Packs')}`];
  for (const packId of selectedPacks) {
    const pack = RULE_PACK_DATA[packId];
    if (!pack) continue;
    const packDefaults = pack.defaults || {};
    // Declared keys are a SECOND reason to emit a pack section. The old guard
    // skipped on empty `defaults` alone, which predates this tier — a pack with
    // a declared tier and no platform defaults would have been dropped whole,
    // silently losing exactly the thing this change exists to surface. No
    // shipped pack is in that state today; the guard was stale, not broken.
    const declared = getDeclaredKeys([packId]);
    if (Object.keys(packDefaults).length === 0 && declared.length === 0) continue;
    lines.push(`\n# --- ${pack.label} ---`);
    for (const [key, meta] of Object.entries(packDefaults)) {
      // ⛔ A LIVE, copy-paste value — one of THREE such faces, not "the one"
      // as this comment used to claim (the Config Template Gallery and
      // `YamlValidatorTab`'s insert-metric button are the others; a blind
      // review found both, #1344). The declared block below is emitted
      // commented precisely because the platform asserts no number for those —
      // but these defaults-tier keys ARE asserted, and #1176 measured
      // counter-examples for two of them. A
      // template that stays silent about that teaches the reader the number is
      // endorsed. Rendered from the registry field, so a pack with nothing
      // measured emits nothing (absence ≠ "validated").
      // Guard on the RENDERED sentence, not on the field: an unrecognised
      // `direction` yields null, and emitting `# undefined` above a live
      // threshold is worse than emitting nothing.
      const comment = counterexampleComment(meta.valueCounterexample);
      if (comment) lines.push(`# ${comment}`);
      lines.push(`${key}: "${meta.value}"  # ${meta.desc}`);
    }
    // Declared keys belong in the starter template too — #1321 is about
    // discoverability, and a template that silently omits them teaches the
    // reader they do not exist. ⛔ Emitted COMMENTED, never with a live value:
    // the platform asserts no number for these, and ADR-030's blind-write
    // library measured several of the reference figures false-alarming on
    // benign load (#1176). Same rule the `<tenant>.yaml` generators follow.
    if (declared.length > 0) {
      lines.push(`#   ${t('以下為平台宣告、但不主張值的 key：取消註解並填入依自身 baseline 校準的數值；不填＝靜默',
                          'Declared by the platform, no platform value: uncomment and fill in a number calibrated from your own baseline. Left out = silent')}`);
      for (const m of declared) {
        // ⛔ Same caveat as the live-value loop above. These rows carry a
        // reference number too, and #1320 D1 says this is the tier a tenant can
        // fill in TODAY — so of the two loops, this is the one whose numbers are
        // most likely to be copied. Covering only `defaults` left them bare.
        const comment = counterexampleComment(m.valueCounterexample);
        if (comment) lines.push(`#   ${comment}`);
        lines.push(`#   ${m.key}: "<${t('你的值', 'your value')}>"  # ${m.desc}${t('（參考起點', ' (reference start')} ${m.value} ${m.unit}${t('，非背書）', ', not an endorsement)')}`);
      }
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

// The sentence for a threshold value the exporter does not read (#3865),
// shared by validateConfig and the pages that render a simulation. What the
// exporter does next depends on the key, and the sentence says which:
//   - tier 'base': resolveBaseRows falls back to the platform default; a key
//     with none (a declared key) is not served.
//   - tier 'critical' (`<base>_critical`): the whole value must be the
//     number, and resolveCriticalRows drops that key's row otherwise.
function unreadableThresholdMessage(raw, tier = 'base') {
  const t = window.__t || ((zh, en) => en);
  const shown = JSON.stringify(String(raw));
  if (tier === 'critical') {
    return t(`exporter 不會讀這個值：${shown} 不是 _critical key 接受的寫法（十進位數字，不帶 :severity），這個 _critical key 不會生效`,
      `The exporter will not read this value: ${shown} is not what a _critical key accepts (a decimal number, with no :severity), so this _critical key is not served`);
  }
  return t(`exporter 不會讀這個值：${shown} 不是它接受的閾值寫法（十進位數字，可加 :warning／:critical）。exporter 會改用平台預設值，沒有預設值的 key 則沒有閾值；這裡不模擬`,
    `The exporter will not read this value: ${shown} is not a threshold it accepts (a decimal number, optionally with :warning / :critical). The exporter falls back to the platform default, and a key with no default has no threshold; not simulated here`);
}

function validateConfig(config, selectedPacks) {
  const t = window.__t || ((zh, en) => en);

  const issues = [];
  const info = [];
  // Keys the platform recognises but gives no value to.
  const declaredMetrics = new Set(getDeclaredKeys(selectedPacks).map(m => m.key));

  for (const [key, val] of Object.entries(config)) {
    if (key.startsWith('_')) continue;
    // The value is read as the exporter reads it (#3865), never with
    // parseFloat: that reads `1_000` as 1 and `0x1F` as 0, and the page then
    // judges a number the exporter never serves.
    // A `<base>_critical` key is read by its own rule (the whole value is
    // the number).
    const tier = isCriticalKey(key) ? 'critical' : 'base';
    const row = readThresholdRow(val, tier);
    if (row.kind === 'disabled') {
      info.push({ level: 'info', field: key,
        msg: t('已禁用此指標', 'Metric disabled') });
      continue;
    }
    if (row.kind === 'unreadable') {
      issues.push({ level: 'error', field: key, msg: unreadableThresholdMessage(row.raw, tier) });
    }
    if (row.kind === 'number') {
      const numVal = row.number;
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
    // ADR-038 §11: whether a key is read is the shared key verdict's (CI's
    // validate-config, `da-tools explain-keys`, tenant-api's write gate), not
    // this page's — it judges no key name. A declared key still gets its
    // calibration note: that is about the value, not about the name.
    if (declaredMetrics.has(key)) {
      // Recognised, settable, and deliberately value-less. Say that the
      // number has to come from the tenant's own baseline, because the
      // reference figures shipped alongside these keys have measured
      // false-alarm cases on benign load (#1176).
      info.push({ level: 'info', field: key,
        msg: t(`平台宣告的 key：平台不主張預設值，填了才生效（不填＝靜默）；數值請依自身 baseline 校準`,
               `Platform-declared key: no platform default, it takes effect only once you set it (omit = silent). Calibrate the number from your own baseline`) });
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
        const ms = parseDuration(val);
        if (ms === null) {
          // #2711: a value Alertmanager cannot read is not skipped. The route
          // generator renders the platform default in its place and
          // `generate-routes --validate` / validate-config refuse the config
          // (_grar_merge._apply_timing_params), so say so here.
          issues.push({ level: 'error', field: `_routing.${param}`,
            msg: t(`${val} 不是 Alertmanager 能讀的時長：整數加單位、大單位在前、每個單位最多一次（y w d h m s ms，或單獨的 0），例如 30s、5m、1h30m、1d；不接受小數（1.5h）。產生器會改用平台預設值，--validate 會拒收`,
                   `${val} is not an Alertmanager duration: whole numbers with units, largest first, each unit at most once (y w d h m s ms, or a bare 0), e.g. 30s, 5m, 1h30m, 1d; no fractions (1.5h). The generator renders the platform default instead and --validate refuses the config`) });
          continue;
        }
        const secs = ms / 1000;
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

  return { issues: [...issues, ...info] };
}

// ─────────────────────────────────────────────────────────────────────
// Threshold-crossing primitive — single source of truth for the operator.
// Strict `>` (or `<` for inverted/lower-bound) to mirror the real
// Prometheus rules in rule-packs/*.yaml (e.g. `... > 0.05`): a
// reading exactly at the threshold does NOT fire.
// ─────────────────────────────────────────────────────────────────────
function isFiring(current, threshold, inverted) {
  return inverted ? current < threshold : current > threshold;
}

function simulateAlerts(config, metricValues) {
  const alerts = [];

  for (const [metric, val] of Object.entries(metricValues)) {
    const threshold = config[metric];
    // parseYaml keeps YAML types (#2033): an unquoted `0` is the number 0,
    // which is a real threshold — test for absence, not falsiness.
    const row = readThresholdRow(threshold);
    if (row.kind === 'absent' || row.kind === 'disabled') {
      alerts.push({
        metric, current: val.current, threshold: null, critical_threshold: null,
        firing: false, critical_firing: false, severity: row.kind === 'disabled' ? 'disabled' : 'no-threshold',
        unit: val.unit, packLabel: val.packLabel,
      });
      continue;
    }
    // A scheduled value (a map): no schedule is modelled here.
    if (row.kind === 'structured') continue;

    const currentVal = val.current;
    // The `_critical` companion key: the whole value is the number. One the
    // exporter cannot read gives no critical tier; `critical_unreadable`
    // carries its text so the page can say so. An unreadable base does not
    // switch the tier off; a disabled one does (handled above).
    // ⚠️ Not modelled: a base key the tenant did not write at all. The
    // exporter serves its platform default and the `_critical` tier; this
    // function has no defaults and reports 'no-threshold' (as it always did).
    const crit = readThresholdRow(config[`${metric}_critical`], 'critical');
    const critThreshold = crit.kind === 'number' ? crit.number : null;
    const critTierFiring = critThreshold !== null && isFiring(currentVal, critThreshold);

    let alert;
    if (row.kind === 'unreadable') {
      // A base value the exporter does not read (#3865) is a row that says
      // so — never a number cut out of the text, and never a dropped row.
      // The exporter serves the platform default in its place; that is not
      // simulated (threshold null), the critical tier is.
      alert = {
        metric, current: currentVal, threshold: null, critical_threshold: critThreshold,
        firing: false, critical_firing: critTierFiring,
        severity: critTierFiring ? 'critical' : 'unreadable', raw: row.raw,
        unit: val.unit, packLabel: val.packLabel,
      };
    } else {
      const thresholdNum = row.number;
      const firing = isFiring(currentVal, thresholdNum);
      // `80:critical` on the base key is a critical-severity row at 80.
      const critFiring = critTierFiring || (firing && row.severity === 'critical');
      alert = {
        metric, current: currentVal, threshold: thresholdNum,
        critical_threshold: critThreshold, firing, critical_firing: critFiring,
        severity: critFiring ? 'critical' : (firing ? 'warning' : 'ok'),
        unit: val.unit, packLabel: val.packLabel,
      };
    }
    if (crit.kind === 'unreadable') alert.critical_unreadable = crit.raw;
    alerts.push(alert);
  }

  return alerts;
}

// simulateWithDedup — alert-simulator's per-alert model + severity dedup.
// Relocated verbatim from alert-simulator.jsx (Wave-3 convergence); the
// only change is routing the crossing decision through isFiring so it
// shares one operator with simulateAlerts. alertDefs is injected by the
// caller (ALERT_DEFS) — keeps this module data-driven / tenant-agnostic.
function simulateWithDedup(config, metrics, dedupEnabled, alertDefs) {
  const defs = alertDefs || {};
  const firing = [];
  const suppressed = [];
  const ok = [];
  const unreadable = [];

  Object.entries(config).forEach(([key, thresholdStr]) => {
    const def = defs[key];
    if (!def) return;
    // Read as the exporter reads it (#3865). A value it serves no number for
    // is listed as such; absent, disabled and scheduled values have no row.
    const row = readThresholdRow(thresholdStr, isCriticalKey(key) ? 'critical' : 'base');
    if (row.kind === 'unreadable') { unreadable.push({ key, def, raw: row.raw }); return; }
    if (row.kind !== 'number') return;
    const threshold = row.number;
    const current = metrics[key];
    if (current === undefined || current === '') return;
    const val = parseFloat(current);

    const wouldFire = isFiring(val, threshold, def.inverted);
    if (wouldFire) {
      firing.push({ key, def, threshold, current: val });
    } else {
      ok.push({ key, def, threshold, current: val });
    }
  });

  // Severity dedup: if critical fires, suppress matching warning
  if (dedupEnabled) {
    const criticalFiring = new Set(firing.filter(f => f.def.severity === 'critical').map(f => f.key.replace('_critical', '')));
    firing.forEach(f => {
      if (f.def.severity === 'warning' && criticalFiring.has(f.key)) {
        suppressed.push({ ...f, reason: 'Suppressed by severity dedup (critical alert active)' });
      }
    });
    const suppressedKeys = new Set(suppressed.map(s => s.key));
    return {
      firing: firing.filter(f => !suppressedKeys.has(f.key)),
      suppressed,
      ok,
      unreadable,
    };
  }

  return { firing, suppressed: [], ok, unreadable };
}

function resolveRoutingLayers(config) {
  const t = window.__t || ((zh, en) => en);

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

export {
  generateSampleYaml, validateConfig, isFiring, simulateAlerts, simulateWithDedup, resolveRoutingLayers,
  unreadableThresholdMessage,
};
