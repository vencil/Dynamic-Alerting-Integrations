---
title: "_common — Routing profiles, domain policies, defaults"
purpose: |
  In-browser mirror of the four-layer routing model defined in ADR-007:
  L1 platform defaults → L2 routing profile → L3 tenant override → L4
  platform-enforced. The actual resolution lives in
  `_common/sim/alert-engine.js` (`resolveRoutingLayers`); this file
  owns just the data tables.

  Three exported tables:
    - ROUTING_DEFAULTS  — L1 baseline that every tenant inherits
    - ROUTING_PROFILES  — L2 named bundles (team-sre-apac, etc.)
    - DOMAIN_POLICIES   — domain-level constraints (allow/forbid lists,
                          max repeat_interval) used by the validator

  Pre-PR-portal-3 these were inline in portal-shared.jsx. Splitting
  them out lets future tools (notification-previewer, alert-noise-
  analyzer, etc.) read the catalog without dragging in the validator
  + simulator surface.

  Public API:
    window.__ROUTING_DEFAULTS
    window.__ROUTING_PROFILES
    window.__DOMAIN_POLICIES

  Closure deps: none. Pure data.

  Backward compatibility: portal-shared.jsx re-exports these on
  window.__portalShared unchanged.
---

const ROUTING_DEFAULTS = {
  receiver_type: 'webhook',
  group_by: ['alertname', 'tenant'],
  group_wait: '30s',
  group_interval: '5m',
  repeat_interval: '4h',
};

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

window.__ROUTING_DEFAULTS = ROUTING_DEFAULTS;
window.__ROUTING_PROFILES = ROUTING_PROFILES;
window.__DOMAIN_POLICIES = DOMAIN_POLICIES;
