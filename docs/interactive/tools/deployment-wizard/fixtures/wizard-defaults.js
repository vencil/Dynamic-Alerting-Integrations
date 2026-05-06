---
title: "Deployment Profile Wizard — Default catalogs"
purpose: |
  Static data tables for the 6-step Deployment Profile Wizard:
  step metadata, deployment tiers (Tier 1 Git-Native vs Tier 2
  Portal+API), environment sizes, tenant size presets with replica
  counts, OAuth2 providers, Rule Pack catalog.

  Pre-PR-portal-10 these were inline at the top of deployment-
  wizard.jsx (~135 LOC). Splitting matches the operator-setup-wizard
  pattern from PR-portal-4.

  Public API:
    window.__DEPLOY_STEPS              ordered step metadata
    window.__DEPLOY_TIERS              Tier 1 / Tier 2 catalog
    window.__DEPLOY_ENVIRONMENTS       local / staging / production
    window.__DEPLOY_TENANT_SIZES       small / medium / large + replica recipes
    window.__DEPLOY_OAUTH2_PROVIDERS   github / google / oidc / gitlab
    window.__DEPLOY_RULE_PACKS         13 Rule Pack catalog

  Closure deps: reads window.__t at consumer call time.
---

const t = window.__t || ((zh, en) => en);

const DEPLOY_STEPS = [
  { id: 'tier', label: () => t('部署層級', 'Deployment Tier') },
  { id: 'environment', label: () => t('運行環境', 'Environment') },
  { id: 'tenants', label: () => t('Tenant 數量', 'Tenant Count') },
  { id: 'auth', label: () => t('認證 (Tier 2)', 'Authentication (Tier 2)') },
  { id: 'packs', label: () => t('Rule Packs', 'Rule Packs') },
  { id: 'review', label: () => t('檢視與產出', 'Review & Generate') },
];

const DEPLOY_TIERS = [
  {
    id: 'tier1',
    name: t('Tier 1：Git-Native', 'Tier 1: Git-Native'),
    desc: t('純 GitOps：YAML + da-tools CLI + Helm values', 'Pure GitOps: YAML + da-tools CLI + Helm values'),
    features: [
      t('threshold-exporter × 2 (HA)', 'threshold-exporter × 2 (HA)'),
      t('Prometheus + Alertmanager (Helm)', 'Prometheus + Alertmanager (Helm)'),
      t('ConfigMap 管理告警規則', 'ConfigMap for alert rules'),
      t('無 Portal / API', 'No Portal / API'),
    ],
    icon: '📦',
    cost: t('低', 'Low'),
  },
  {
    id: 'tier2',
    name: t('Tier 2：Portal + API', 'Tier 2: Portal + API'),
    desc: t('完整功能：Tier 1 + da-portal + tenant-api + OAuth2', 'Full-featured: Tier 1 + da-portal + tenant-api + OAuth2'),
    features: [
      t('所有 Tier 1 功能', 'All Tier 1 features'),
      t('da-portal UI (自託管或 SaaS)', 'da-portal UI (self-hosted or SaaS)'),
      t('tenant-api（RBAC + 熱更新）', 'tenant-api (RBAC + hot-reload)'),
      t('oauth2-proxy（GitHub / Google / OIDC）', 'oauth2-proxy (GitHub / Google / OIDC)'),
    ],
    icon: '🌐',
    cost: t('中', 'Medium'),
  },
];

const DEPLOY_ENVIRONMENTS = [
  {
    id: 'local',
    label: t('本地開發 (Kind/Minikube)', 'Local Dev (Kind/Minikube)'),
    icon: '💻',
    desc: t('2–4 CPU, 4–8 GB RAM, 簡化部署', '2–4 CPU, 4–8 GB RAM, simplified'),
  },
  {
    id: 'staging',
    label: t('測試環境 (Staging)', 'Staging Environment'),
    icon: '🧪',
    desc: t('4–8 CPU, 16 GB RAM, HA 就緒', '4–8 CPU, 16 GB RAM, HA-ready'),
  },
  {
    id: 'production',
    label: t('生產環境 (Production)', 'Production Environment'),
    icon: '🚀',
    desc: t('8+ CPU, 32+ GB RAM, 多區域', '8+ CPU, 32+ GB RAM, multi-region'),
  },
];

const DEPLOY_TENANT_SIZES = [
  {
    id: 'small',
    label: t('小型 (1–10)', 'Small (1–10)'),
    icon: '1️⃣',
    replicas: { exporter: 1, prometheus: 1, alertmanager: 1 },
    retention: '7d',
    cardinality: 500,
  },
  {
    id: 'medium',
    label: t('中型 (10–50)', 'Medium (10–50)'),
    icon: '📊',
    replicas: { exporter: 2, prometheus: 2, alertmanager: 3 },
    retention: '14d',
    cardinality: 2000,
  },
  {
    id: 'large',
    label: t('大型 (50+)', 'Large (50+)'),
    icon: '📈',
    replicas: { exporter: 3, prometheus: 3, alertmanager: 3 },
    retention: '30d',
    cardinality: 5000,
  },
];

const DEPLOY_OAUTH2_PROVIDERS = [
  {
    id: 'github',
    label: 'GitHub',
    icon: '🐙',
    desc: t('使用 GitHub 帳戶登入', 'Sign in with GitHub account'),
    scopes: ['user:email', 'read:org'],
  },
  {
    id: 'google',
    label: 'Google',
    icon: '🔵',
    desc: t('使用 Google 帳戶登入', 'Sign in with Google account'),
    scopes: ['openid', 'email', 'profile'],
  },
  {
    id: 'oidc',
    label: 'OIDC / Keycloak',
    icon: '🔐',
    desc: t('自託管 OIDC（Keycloak、Okta 等）', 'Self-hosted OIDC (Keycloak, Okta, etc.)'),
    scopes: ['openid', 'profile', 'email'],
  },
  {
    id: 'gitlab',
    label: 'GitLab',
    icon: '🦊',
    desc: t('使用 GitLab 帳戶登入', 'Sign in with GitLab account'),
    scopes: ['openid', 'profile', 'email'],
  },
];

const DEPLOY_RULE_PACKS = [
  { id: 'mariadb', label: 'MariaDB/MySQL', category: 'database', icon: '🐬' },
  { id: 'postgresql', label: 'PostgreSQL', category: 'database', icon: '🐘' },
  { id: 'redis', label: 'Redis', category: 'database', icon: '🔴' },
  { id: 'mongodb', label: 'MongoDB', category: 'database', icon: '🍃' },
  { id: 'elasticsearch', label: 'Elasticsearch', category: 'database', icon: '🔎' },
  { id: 'oracle', label: 'Oracle', category: 'database', icon: '🏛️' },
  { id: 'db2', label: 'DB2', category: 'database', icon: '🔷' },
  { id: 'clickhouse', label: 'ClickHouse', category: 'database', icon: '🖱️' },
  { id: 'kafka', label: 'Kafka', category: 'messaging', icon: '📨' },
  { id: 'rabbitmq', label: 'RabbitMQ', category: 'messaging', icon: '🐰' },
  { id: 'jvm', label: 'JVM', category: 'runtime', icon: '☕' },
  { id: 'nginx', label: 'Nginx', category: 'webserver', icon: '🌐' },
  { id: 'kubernetes', label: 'Kubernetes', category: 'infrastructure', icon: '⎈' },
];

window.__DEPLOY_STEPS = DEPLOY_STEPS;
window.__DEPLOY_TIERS = DEPLOY_TIERS;
window.__DEPLOY_ENVIRONMENTS = DEPLOY_ENVIRONMENTS;
window.__DEPLOY_TENANT_SIZES = DEPLOY_TENANT_SIZES;
window.__DEPLOY_OAUTH2_PROVIDERS = DEPLOY_OAUTH2_PROVIDERS;
window.__DEPLOY_RULE_PACKS = DEPLOY_RULE_PACKS;

// TD-030e: ESM exports. Removed in TD-030z.
// <!-- jsx-loader-compat: ignore -->
export { DEPLOY_STEPS, DEPLOY_TIERS, DEPLOY_ENVIRONMENTS, DEPLOY_TENANT_SIZES, DEPLOY_OAUTH2_PROVIDERS, DEPLOY_RULE_PACKS };
