---
title: "_common — Validation constants"
purpose: |
  Shared validation constants for tenant config + routing rules:
  reserved keys, key prefixes, supported receiver types, timing
  guardrails (group_wait / repeat_interval
  bounds), and the YAML parser's safety limits (UNSAFE_KEYS,
  MAX_YAML_SIZE).

  Pre-PR-portal-3 lived inline in portal-shared.jsx. Splitting lets
  the validator (alert-engine.js) and the YAML parser (yaml-parser.js)
  share the constants without circular-loading the rest of the
  validator surface.

  Public API:
    RESERVED_KEYS         Set of reserved tenant keys (_silent_mode, _metadata, ...)
    RESERVED_PREFIXES     Array of the open key prefixes (_state_)
    RECEIVER_TYPES        Array of supported notification receiver types
    TIMING_GUARDRAILS     map of timing param to bounds {min, max, unit}
    UNSAFE_KEYS           Set of prototype-pollution keys parser must reject
    MAX_YAML_SIZE         Hard cap (100KB) on parser input length

  RESERVED_KEYS / RESERVED_PREFIXES come from ../data/reserved-keys.json,
  which scripts/tools/dx/sync_schema.py generates from the reserved-key
  registry (components/threshold-exporter/app/pkg/config/reserved_keys.json;
  `--check` fails CI when it is stale, #3227). They are the `_` keys a
  tenant's config may carry — the set the tenant schema's propertyNames
  admits — so a platform-level key (_routing_defaults, _domain_policy, ...)
  under tenants.<id> reads as unknown.

  Closure deps: ../data/reserved-keys.json (generated data). Pure data.

  Consumers import these directly via ESM (dev-rules §S6).
---

import RESERVED_KEY_REGISTRY from '../data/reserved-keys.json';

const RESERVED_KEYS = new Set(RESERVED_KEY_REGISTRY.reserved_keys);

const RESERVED_PREFIXES = RESERVED_KEY_REGISTRY.open_prefixes;

const RECEIVER_TYPES = ['webhook', 'email', 'slack', 'teams', 'rocketchat', 'pagerduty'];

const TIMING_GUARDRAILS = {
  group_wait: { min: 5, max: 300, unit: 's' },
  group_interval: { min: 5, max: 300, unit: 's' },
  repeat_interval: { min: 60, max: 259200, unit: 's' },
};

const UNSAFE_KEYS = new Set(['__proto__', 'constructor', 'prototype']);
const MAX_YAML_SIZE = 100000;

export { RESERVED_KEYS, RESERVED_PREFIXES, RECEIVER_TYPES, TIMING_GUARDRAILS, UNSAFE_KEYS, MAX_YAML_SIZE };
