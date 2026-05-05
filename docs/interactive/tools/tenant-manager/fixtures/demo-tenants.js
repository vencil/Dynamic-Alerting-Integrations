---
title: "Tenant Manager — Demo Fixtures"
purpose: |
  Static demo data for the docs-site fallback path. Used when both the
  live `/api/v1/tenants/search` API and `platform-data.json` are
  unavailable (e.g. local docs preview, GitHub Pages without backend).

  After PR-2d (#153) the orchestrator imports these via jsx-loader's
  front-matter `dependencies: [...]` so they register as window globals
  for the main `tenant-manager.jsx` to reference.

  Behavior contract: identical to the inline definitions that lived in
  tenant-manager.jsx prior to decomposition. Pure data — no React,
  no side effects.
---

const DEMO_TENANTS = {
  "prod-mariadb-01": {
    environment: "production", region: "ap-northeast-1", tier: "tier-1",
    domain: "finance", db_type: "mariadb",
    rule_packs: ["mariadb", "kubernetes", "operational"],
    owner: "team-dba-global", routing_channel: "slack:#dba-alerts",
    operational_mode: "normal", metric_count: 8, last_config_commit: "abc1234",
    tags: ["critical-path", "pci"], groups: ["production-dba"]
  },
  "prod-redis-01": {
    environment: "production", region: "ap-northeast-1", tier: "tier-1",
    domain: "cache", db_type: "redis",
    rule_packs: ["redis", "kubernetes"],
    owner: "team-platform", routing_channel: "pagerduty:dba-oncall",
    operational_mode: "silent", metric_count: 5, last_config_commit: "abc1234",
    tags: ["session-store"], groups: ["production-dba"]
  },
  "staging-pg-01": {
    environment: "staging", region: "us-west-2", tier: "tier-2",
    domain: "analytics", db_type: "postgresql",
    rule_packs: ["postgresql", "jvm", "kubernetes"],
    owner: "team-analytics", routing_channel: "slack:#staging-alerts",
    operational_mode: "normal", metric_count: 6, last_config_commit: "def5678",
    tags: [], groups: ["staging-all"]
  },
  "dev-mongodb-01": {
    environment: "development", region: "us-west-2", tier: "tier-3",
    domain: "mobile", db_type: "mongodb",
    rule_packs: ["mongodb", "kubernetes"],
    owner: "team-mobile", routing_channel: "email:mobile-dev@example.com",
    operational_mode: "maintenance", metric_count: 3, last_config_commit: "ghi9012",
    tags: ["experimental"], groups: []
  },
  "prod-kafka-01": {
    environment: "production", region: "eu-west-1", tier: "tier-1",
    domain: "streaming", db_type: "kafka",
    rule_packs: ["kafka", "jvm", "kubernetes"],
    owner: "team-streaming", routing_channel: "slack:#kafka-alerts",
    operational_mode: "normal", metric_count: 7, last_config_commit: "abc1234",
    tags: ["event-bus"], groups: ["production-dba"]
  }
};

const DEMO_GROUPS = {
  "production-dba": {
    label: "Production DBA",
    description: "All production database tenants managed by the DBA team",
    members: ["prod-mariadb-01", "prod-redis-01", "prod-kafka-01"]
  },
  "staging-all": {
    label: "All Staging",
    description: "All staging environment tenants",
    members: ["staging-pg-01"]
  }
};

// Register on window so the orchestrator (loaded after this dep) can
// pull these via `const X = window.__X;` — same pattern self-service-portal
// uses for AlertPreviewTab / YamlValidatorTab / RoutingTraceTab.
window.__DEMO_TENANTS = DEMO_TENANTS;
window.__DEMO_GROUPS = DEMO_GROUPS;

// TD-030b: ESM exports. Removed in TD-030z.
// <!-- jsx-loader-compat: ignore -->
export { DEMO_TENANTS, DEMO_GROUPS };
