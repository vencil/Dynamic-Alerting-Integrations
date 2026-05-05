---
title: "_common — Validation constants"
purpose: |
  Shared validation constants for tenant config + routing rules:
  reserved keys, key prefixes, supported receiver types + per-receiver
  required fields, timing guardrails (group_wait / repeat_interval
  bounds), and the YAML parser's safety limits (UNSAFE_KEYS,
  MAX_YAML_SIZE).

  Pre-PR-portal-3 lived inline in portal-shared.jsx. Splitting lets
  the validator (alert-engine.js) and the YAML parser (yaml-parser.js)
  share the constants without circular-loading the rest of the
  validator surface.

  Public API:
    window.__RESERVED_KEYS         Set of reserved tenant keys (_silent_mode, _metadata, ...)
    window.__RESERVED_PREFIXES     Array of reserved key prefixes (_state_, _routing)
    window.__RECEIVER_TYPES        Array of supported notification receiver types
    window.__RECEIVER_REQUIRED     map of receiver_type to required field names
    window.__TIMING_GUARDRAILS     map of timing param to bounds {min, max, unit}
    window.__UNSAFE_KEYS           Set of prototype-pollution keys parser must reject
    window.__MAX_YAML_SIZE         Hard cap (100KB) on parser input length

  Closure deps: none. Pure data.

  Backward compatibility: portal-shared.jsx re-exports all of these on
  window.__portalShared unchanged.
---

const RESERVED_KEYS = new Set([
  '_silent_mode', '_namespaces', '_metadata', '_profile',
  '_routing_defaults', '_routing_profile', '_domain_policy', '_instance_mapping'
]);

const RESERVED_PREFIXES = ['_state_', '_routing'];

const RECEIVER_TYPES = ['webhook', 'email', 'slack', 'teams', 'rocketchat', 'pagerduty'];

const RECEIVER_REQUIRED = {
  webhook: ['url'], email: ['to', 'smarthost'], slack: ['api_url'],
  teams: ['webhook_url'], rocketchat: ['url'], pagerduty: ['service_key'],
};

const TIMING_GUARDRAILS = {
  group_wait: { min: 5, max: 300, unit: 's' },
  group_interval: { min: 5, max: 300, unit: 's' },
  repeat_interval: { min: 60, max: 259200, unit: 's' },
};

const UNSAFE_KEYS = new Set(['__proto__', 'constructor', 'prototype']);
const MAX_YAML_SIZE = 100000;

window.__RESERVED_KEYS = RESERVED_KEYS;
window.__RESERVED_PREFIXES = RESERVED_PREFIXES;
window.__RECEIVER_TYPES = RECEIVER_TYPES;
window.__RECEIVER_REQUIRED = RECEIVER_REQUIRED;
window.__TIMING_GUARDRAILS = TIMING_GUARDRAILS;
window.__UNSAFE_KEYS = UNSAFE_KEYS;
window.__MAX_YAML_SIZE = MAX_YAML_SIZE;

// TD-030c: ESM exports for esbuild bundle + Vitest. Removed in TD-030z.
// <!-- jsx-loader-compat: ignore -->
export { RESERVED_KEYS, RESERVED_PREFIXES, RECEIVER_TYPES, RECEIVER_REQUIRED, TIMING_GUARDRAILS, UNSAFE_KEYS, MAX_YAML_SIZE };
