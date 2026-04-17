---
title: "Deployment Profile Wizard"
tags: [deployment, helm, values, guided, tier]
audience: [platform-engineer, sre, devops]
version: v2.6.0
lang: en
related: [architecture-quiz, cicd-setup-wizard, capacity-planner]
---

/* eslint-disable */
// Design token migration notes (v2.7.0 Phase .a0, DEC-F/G/I):
//   • DEC-F (C): removed all Tailwind `dark:` variants; rely on `[data-theme="dark"]`
//     attribute + design-tokens.css to swap `--da-color-*` automatically.
//   • DEC-G (B): Portal-wide slate→gray unification is implicit here — all slate/blue
//     shade classes were migrated to `var(--da-color-*)` tokens, so no literal slate/gray
//     remains except the fixed IDE-style dark code-preview panel (line ~820).
//   • REG-004: hardcoded portal paths (/template-gallery, /tenant-manager,
//     /docs/getting-started) replaced with proper `../../assets/jsx-loader.html?component=`
//     and relative docs paths so they resolve inside the jsx-loader portal.
//   • Residuals: `bg-slate-900 text-slate-100` in Review step code preview (IDE-intent),
//     `text-amber-800` for warning message copy (tokens don't include warning-fg variant).
//   • Follow-ups (v2.8.0): jsx-loader `navigate(key)` helper (DEC-I C) will replace the
//     raw href strings; add `--da-color-warning-fg` token to finish amber migration.
import React, { useState, useMemo } from 'react';

const t = window.__t || ((zh, en) => en);

/* ── Step definitions ── */
const STEPS = [
  { id: 'tier', label: () => t('部署層級', 'Deployment Tier') },
  { id: 'environment', label: () => t('運行環境', 'Environment') },
  { id: 'tenants', label: () => t('Tenant 數量', 'Tenant Count') },
  { id: 'auth', label: () => t('認證 (Tier 2)', 'Authentication (Tier 2)') },
  { id: 'packs', label: () => t('Rule Packs', 'Rule Packs') },
  { id: 'review', label: () => t('檢視與產出', 'Review & Generate') },
];

/* ── Deployment tiers ── */
const TIERS = [
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

/* ── Environments ── */
const ENVIRONMENTS = [
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

/* ── Tenant sizes ── */
const TENANT_SIZES = [
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

/* ── OAuth2 providers ── */
const OAUTH2_PROVIDERS = [
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

/* ── Rule packs ── */
const RULE_PACKS = [
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

/* ── Helm values generator ── */
function generateHelmValues(config) {
  const { tier, environment, tenantSize, auth, packs } = config;
  const size = TENANT_SIZES.find(s => s.id === tenantSize);
  const isTier2 = tier === 'tier2';

  let yaml = `# Generated Helm values for ${TIERS.find(t => t.id === tier)?.name}
# Environment: ${ENVIRONMENTS.find(e => e.id === environment)?.label}
# Tenant count: ${size?.label}
# Generated: ${new Date().toISOString().split('T')[0]}

# ────────────────────────────────────────────────────────────────────
# threshold-exporter Configuration
# ────────────────────────────────────────────────────────────────────

thresholdExporter:
  replicaCount: ${size?.replicas.exporter || 2}
  image:
    repository: ghcr.io/vencil/threshold-exporter
    tag: v2.5.0
    pullPolicy: IfNotPresent

  resources:
    requests:
      cpu: ${environment === 'local' ? '100m' : environment === 'staging' ? '250m' : '500m'}
      memory: ${environment === 'local' ? '128Mi' : environment === 'staging' ? '256Mi' : '512Mi'}
    limits:
      cpu: ${environment === 'local' ? '200m' : environment === 'staging' ? '500m' : '1000m'}
      memory: ${environment === 'local' ? '256Mi' : environment === 'staging' ? '512Mi' : '1Gi'}

  # Hot-reload SHA-256 validation
  configValidation:
    enabled: true
    sha256: "" # Set after generating config

  # Cardinality guard: per-tenant max metrics
  cardinalityGuard:
    enabled: true
    maxPerTenant: ${size?.cardinality || 2000}

  # Three-state operating modes: normal / silent / maintenance
  tripleState:
    enabled: true
    defaultMode: normal

prometheus:
  replicaCount: ${size?.replicas.prometheus || 2}
  image:
    repository: prom/prometheus
    tag: v2.52.0

  resources:
    requests:
      cpu: ${environment === 'local' ? '250m' : environment === 'staging' ? '500m' : '1000m'}
      memory: ${environment === 'local' ? '512Mi' : environment === 'staging' ? '1Gi' : '2Gi'}
    limits:
      cpu: ${environment === 'local' ? '500m' : environment === 'staging' ? '1000m' : '2000m'}
      memory: ${environment === 'local' ? '1Gi' : environment === 'staging' ? '2Gi' : '4Gi'}

  # Data retention based on tenant size
  retention: "${size?.retention || '14d'}"

  # Rule packs from ConfigMap + Projected Volume
  ruleConfigMaps:
    - name: platform-rules
      key: rules.yaml
    ${packs.length > 0 ? `# Auto-mounted rule packs via Projected Volume:\n    # ${packs.map(p => `- name: rules-${p}`).join('\n    # ')}` : ''}

  # ServiceMonitor for threshold-exporter
  serviceMonitor:
    enabled: true
    interval: 30s
    scrapeTimeout: 10s

alertmanager:
  replicaCount: ${size?.replicas.alertmanager || 3}
  image:
    repository: prom/alertmanager
    tag: v0.27.0

  resources:
    requests:
      cpu: ${environment === 'local' ? '100m' : environment === 'staging' ? '250m' : '500m'}
      memory: ${environment === 'local' ? '128Mi' : environment === 'staging' ? '256Mi' : '512Mi'}
    limits:
      cpu: ${environment === 'local' ? '200m' : environment === 'staging' ? '500m' : '1000m'}
      memory: ${environment === 'local' ? '256Mi' : environment === 'staging' ? '512Mi' : '1Gi'}

  # Dynamic route generation + configmap-reload
  configReload:
    enabled: true
    image: jimmidyson/configmap-reload:v0.5.0

  # Cluster mode for HA
  clustering:
    enabled: ${environment !== 'local' ? 'true' : 'false'}
    peers:
      enabled: ${environment !== 'local' ? 'true' : 'false'}

# ────────────────────────────────────────────────────────────────────
# Platform Common Settings
# ────────────────────────────────────────────────────────────────────

platform:
  # Environment label for metric routing
  environment: ${environment}

  # Namespace isolation
  namespaces:
    monitoring: monitoring
    # Add tenant namespaces as needed

  # Logging level
  logLevel: ${environment === 'production' ? 'warn' : 'info'}

  # Bilingual support (zh/en annotations)
  i18n:
    enabled: true
    defaultLanguage: en

# ────────────────────────────────────────────────────────────────────
# Tier 2: Portal + API Configuration
# ────────────────────────────────────────────────────────────────────
${isTier2 ? `
daPortal:
  enabled: true
  replicaCount: ${environment === 'local' ? 1 : size?.replicas.exporter || 2}
  image:
    repository: ghcr.io/vencil/da-portal
    tag: v2.5.0

  resources:
    requests:
      cpu: ${environment === 'local' ? '100m' : '250m'}
      memory: ${environment === 'local' ? '256Mi' : '512Mi'}
    limits:
      cpu: ${environment === 'local' ? '200m' : '500m'}
      memory: ${environment === 'local' ? '512Mi' : '1Gi'}

  # Portal ingress
  ingress:
    enabled: true
    className: nginx
    hosts:
      - host: da-portal.example.com
        paths:
          - path: /
            pathType: Prefix

tenantAPI:
  enabled: true
  replicaCount: ${environment === 'local' ? 1 : 2}
  image:
    repository: ghcr.io/vencil/tenant-api
    tag: v2.5.0

  resources:
    requests:
      cpu: ${environment === 'local' ? '100m' : '250m'}
      memory: ${environment === 'local' ? '128Mi' : '256Mi'}
    limits:
      cpu: ${environment === 'local' ? '200m' : '500m'}
      memory: ${environment === 'local' ? '256Mi' : '512Mi'}

  # RBAC hot-reload via atomic.Value
  rbac:
    enabled: true
    cacheRefreshInterval: 30s

  # Tenant API ingress
  ingress:
    enabled: true
    className: nginx
    hosts:
      - host: api.dynamic-alerting.example.com
        paths:
          - path: /v1
            pathType: Prefix

oauth2Proxy:
  enabled: true
  replicaCount: ${environment === 'local' ? 1 : 2}
  image:
    repository: oauth2-proxy/oauth2-proxy
    tag: v7.6.0

  resources:
    requests:
      cpu: 50m
      memory: 64Mi
    limits:
      cpu: 100m
      memory: 128Mi

  # OAuth2 provider configuration
  config:
    provider: "${auth || 'oidc'}"
    ${auth === 'github' ? `oauth_url: "https://github.com/login/oauth/authorize"
    token_url: "https://github.com/login/oauth/access_token"
    user_info_url: "https://api.github.com/user"
    scopes: ["user:email", "read:org"]` : auth === 'google' ? `oauth_url: "https://accounts.google.com/o/oauth2/v2/auth"
    token_url: "https://oauth2.googleapis.com/token"
    user_info_url: "https://www.googleapis.com/oauth2/v2/userinfo"
    scopes: ["openid", "email", "profile"]` : auth === 'gitlab' ? `oauth_url: "https://gitlab.com/oauth/authorize"
    token_url: "https://gitlab.com/oauth/token"
    user_info_url: "https://gitlab.com/api/v4/user"
    scopes: ["openid", "profile", "email"]` : `oauth_url: "https://your-keycloak.com/auth/realms/master/protocol/openid-connect/auth"
    token_url: "https://your-keycloak.com/auth/realms/master/protocol/openid-connect/token"
    user_info_url: "https://your-keycloak.com/auth/realms/master/protocol/openid-connect/userinfo"
    scopes: ["openid", "profile", "email"]`}
    client_id: "" # Set in secrets
    client_secret: "" # Set in secrets

  # Cookie configuration for session persistence
  cookie:
    domain: example.com
    secure: true
    httponly: true
    samesite: Lax
` : `
# Tier 1: Portal and API disabled
daPortal:
  enabled: false

tenantAPI:
  enabled: false

oauth2Proxy:
  enabled: false
`}

# ────────────────────────────────────────────────────────────────────
# Networking & Storage
# ────────────────────────────────────────────────────────────────────

persistence:
  # Prometheus TSDB storage
  prometheus:
    enabled: true
    storageClass: standard
    size: ${environment === 'local' ? '5Gi' : environment === 'staging' ? '20Gi' : '100Gi'}

  # Alertmanager state
  alertmanager:
    enabled: true
    storageClass: standard
    size: ${environment === 'local' ? '1Gi' : '5Gi'}

networkPolicy:
  enabled: ${environment === 'production' ? 'true' : 'false'}
  ingressNamespaces:
    - monitoring

# ────────────────────────────────────────────────────────────────────
# Observability & Debugging
# ────────────────────────────────────────────────────────────────────

monitoring:
  # Prometheus scrape config for self-monitoring
  prometheus:
    enabled: true
    interval: 60s

  # Log aggregation hints
  logging:
    level: ${environment === 'production' ? 'warn' : 'info'}
    format: json

# ────────────────────────────────────────────────────────────────────
# Security
# ────────────────────────────────────────────────────────────────────

rbac:
  create: true

serviceAccount:
  create: true
  name: threshold-exporter

podSecurityPolicy:
  enabled: ${environment === 'production' ? 'true' : 'false'}

# Secrets for OAuth2 (if Tier 2)
secrets:
  ${isTier2 ? `oauth2:
    clientId: "" # Fill from secrets manager
    clientSecret: "" # Fill from secrets manager
  ` : ''}# Add any additional secrets here
`;
  return yaml;
}

export default function DeploymentWizard() {
  const [step, setStep] = useState(0);
  const [config, setConfig] = useState({
    tier: 'tier1',
    environment: 'staging',
    tenantSize: 'medium',
    auth: 'github',
    packs: [],
  });
  const [showOutput, setShowOutput] = useState(false);
  const [copied, setCopied] = useState(false);
  const [packWarning, setPackWarning] = useState(false);

  const steps = STEPS.map((s, i) => {
    // Hide auth step if Tier 1
    if (s.id === 'auth' && config.tier === 'tier1') return null;
    return s;
  }).filter(Boolean);

  const stepIndex = step < steps.length ? step : 0;
  const currentStep = steps[stepIndex];
  const progress = stepIndex + 1;

  /* ── Handle step changes ── */
  const handleNext = () => {
    // Warn if advancing past packs step with zero packs selected
    if (currentStep.id === 'packs' && config.packs.length === 0) {
      setPackWarning(true);
      return;
    }
    setPackWarning(false);
    if (stepIndex < steps.length - 1) setStep(step + 1);
    else setShowOutput(true);
  };

  const handleBack = () => {
    if (stepIndex > 0) setStep(step - 1);
  };

  const handleReset = () => {
    setStep(0);
    setConfig({ tier: 'tier1', environment: 'staging', tenantSize: 'medium', auth: 'github', packs: [] });
    setShowOutput(false);
    setCopied(false);
  };

  /* ── Copy to clipboard ── */
  const helmValues = useMemo(() => generateHelmValues(config), [config]);
  const handleCopy = () => {
    navigator.clipboard.writeText(helmValues);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  /* ── Summary before output ── */
  const summary = useMemo(() => ({
    tier: TIERS.find(t => t.id === config.tier)?.name || '',
    environment: ENVIRONMENTS.find(e => e.id === config.environment)?.label || '',
    tenantSize: TENANT_SIZES.find(s => s.id === config.tenantSize)?.label || '',
    auth: config.tier === 'tier2' ? OAUTH2_PROVIDERS.find(o => o.id === config.auth)?.label : t('N/A', 'N/A'),
    packs: config.packs.length > 0 ? config.packs.join(', ') : t('無', 'None'),
  }), [config]);

  return (
    <div className="min-h-screen bg-gradient-to-br from-[color:var(--da-color-bg)] to-[color:var(--da-color-surface-hover)] p-8">
      <div className="max-w-4xl mx-auto">
        <h1 className="text-4xl font-bold text-[color:var(--da-color-fg)] mb-2">
          {t('部署設定精靈', 'Deployment Profile Wizard')}
        </h1>
        <p className="text-[color:var(--da-color-muted)] mb-8">
          {t('透過幾個簡單步驟，產生符合你需求的 Helm values 設定', 'Generate Helm values tailored to your deployment requirements in just a few steps')}
        </p>

        {!showOutput ? (
          <>
            {/* ── Progress indicator ── */}
            <div className="mb-8">
              <div className="flex items-center justify-between mb-3">
                <h3 className="text-sm font-semibold text-[color:var(--da-color-fg)]">{t('進度', 'Progress')}</h3>
                <span className="text-xs text-[color:var(--da-color-muted)]">{progress}/{steps.length}</span>
              </div>
              <div className="h-2 bg-[color:var(--da-color-tag-bg)] rounded-full overflow-hidden">
                <div
                  className="h-full bg-[color:var(--da-color-accent)] transition-all duration-300"
                  style={{ width: `${(progress / steps.length) * 100}%` }}
                />
              </div>
            </div>

            {/* ── Step indicators ── */}
            <div className="flex gap-2 mb-8 overflow-x-auto pb-2" role="list" aria-label={t('部署設定步驟', 'Deployment configuration steps')}>
              {steps.map((s, i) => (
                <button
                  key={s.id}
                  role="listitem"
                  aria-current={i === stepIndex ? 'step' : undefined}
                  onClick={() => setStep(i)}
                  className={`flex-shrink-0 px-3 py-1.5 rounded-lg text-xs font-medium transition-all ${
                    i === stepIndex
                      ? 'bg-[color:var(--da-color-accent)] text-white'
                      : i < stepIndex
                      ? 'bg-green-500 text-white'
                      : 'bg-[color:var(--da-color-tag-bg)] text-[color:var(--da-color-tag-fg)] '
                  }`}
                >
                  {i < stepIndex ? '✓' : i + 1} {s.label()}
                </button>
              ))}
            </div>

            {/* ── Step content ── */}
            <div className="bg-white rounded-xl shadow-sm border border-[color:var(--da-color-surface-border)] p-8 mb-8">
              {currentStep.id === 'tier' && (
                <div>
                  <h2 className="text-xl font-semibold text-[color:var(--da-color-fg)] mb-6">
                    {t('選擇部署層級', 'Choose Deployment Tier')}
                  </h2>
                  <div className="space-y-4">
                    {TIERS.map(tier => (
                      <button
                        key={tier.id}
                        onClick={() => setConfig({ ...config, tier: tier.id })}
                        className={`w-full p-5 rounded-xl border-2 text-left transition-all ${
                          config.tier === tier.id
                            ? 'border-[color:var(--da-color-accent)] bg-[color:var(--da-color-accent-soft)] '
                            : 'border-[color:var(--da-color-surface-border)] hover:border-[color:var(--da-color-card-hover-border)] '
                        }`}
                      >
                        <div className="flex items-start gap-4">
                          <span className="text-3xl">{tier.icon}</span>
                          <div className="flex-1">
                            <h3 className="font-semibold text-[color:var(--da-color-fg)]">{tier.name}</h3>
                            <p className="text-sm text-[color:var(--da-color-muted)] mt-1">{tier.desc}</p>
                            <ul className="text-xs text-[color:var(--da-color-muted)] mt-3 space-y-1">
                              {tier.features.map((f, i) => (
                                <li key={i} className="flex items-center gap-2">
                                  <span className="text-[color:var(--da-color-muted)]">•</span> {f}
                                </li>
                              ))}
                            </ul>
                            <p className="text-xs font-medium text-[color:var(--da-color-muted)] mt-3">
                              {t('成本', 'Cost')}: {tier.cost}
                            </p>
                          </div>
                          {config.tier === tier.id && <span className="text-[color:var(--da-color-accent)] font-bold">✓</span>}
                        </div>
                      </button>
                    ))}
                  </div>
                </div>
              )}

              {currentStep.id === 'environment' && (
                <div>
                  <h2 className="text-xl font-semibold text-[color:var(--da-color-fg)] mb-6">
                    {t('選擇運行環境', 'Choose Environment')}
                  </h2>
                  <div className="space-y-3">
                    {ENVIRONMENTS.map(env => (
                      <button
                        key={env.id}
                        onClick={() => setConfig({ ...config, environment: env.id })}
                        className={`w-full p-4 rounded-xl border-2 text-left transition-all ${
                          config.environment === env.id
                            ? 'border-[color:var(--da-color-accent)] bg-[color:var(--da-color-accent-soft)] '
                            : 'border-[color:var(--da-color-surface-border)] hover:border-[color:var(--da-color-card-hover-border)] '
                        }`}
                      >
                        <div className="flex items-center gap-3">
                          <span className="text-2xl">{env.icon}</span>
                          <div className="flex-1">
                            <div className="font-medium text-[color:var(--da-color-fg)]">{env.label}</div>
                            <div className="text-xs text-[color:var(--da-color-muted)] mt-0.5">{env.desc}</div>
                          </div>
                          {config.environment === env.id && <span className="text-[color:var(--da-color-accent)]">✓</span>}
                        </div>
                      </button>
                    ))}
                  </div>
                </div>
              )}

              {currentStep.id === 'tenants' && (
                <div>
                  <h2 className="text-xl font-semibold text-[color:var(--da-color-fg)] mb-6">
                    {t('選擇 Tenant 數量', 'Choose Tenant Count')}
                  </h2>
                  <div className="space-y-3">
                    {TENANT_SIZES.map(size => (
                      <button
                        key={size.id}
                        onClick={() => setConfig({ ...config, tenantSize: size.id })}
                        className={`w-full p-4 rounded-xl border-2 text-left transition-all ${
                          config.tenantSize === size.id
                            ? 'border-[color:var(--da-color-accent)] bg-[color:var(--da-color-accent-soft)] '
                            : 'border-[color:var(--da-color-surface-border)] hover:border-[color:var(--da-color-card-hover-border)] '
                        }`}
                      >
                        <div className="flex items-center gap-3">
                          <span className="text-2xl">{size.icon}</span>
                          <div className="flex-1">
                            <div className="font-medium text-[color:var(--da-color-fg)]">{size.label}</div>
                            <div className="text-xs text-[color:var(--da-color-muted)] mt-2 space-y-1">
                              <div>{t('複製數', 'Replicas')}: exporter={size.replicas.exporter}, prometheus={size.replicas.prometheus}, alertmanager={size.replicas.alertmanager}</div>
                              <div>{t('保留期', 'Retention')}: {size.retention} | {t('基數上限', 'Cardinality')}: {size.cardinality}</div>
                            </div>
                          </div>
                          {config.tenantSize === size.id && <span className="text-[color:var(--da-color-accent)]">✓</span>}
                        </div>
                      </button>
                    ))}
                  </div>
                </div>
              )}

              {currentStep.id === 'auth' && config.tier === 'tier2' && (
                <div>
                  <h2 className="text-xl font-semibold text-[color:var(--da-color-fg)] mb-6">
                    {t('選擇 OAuth2 供應商', 'Choose OAuth2 Provider')}
                  </h2>
                  <div className="space-y-3">
                    {OAUTH2_PROVIDERS.map(provider => (
                      <button
                        key={provider.id}
                        onClick={() => setConfig({ ...config, auth: provider.id })}
                        className={`w-full p-4 rounded-xl border-2 text-left transition-all ${
                          config.auth === provider.id
                            ? 'border-[color:var(--da-color-accent)] bg-[color:var(--da-color-accent-soft)] '
                            : 'border-[color:var(--da-color-surface-border)] hover:border-[color:var(--da-color-card-hover-border)] '
                        }`}
                      >
                        <div className="flex items-center gap-3">
                          <span className="text-2xl">{provider.icon}</span>
                          <div className="flex-1">
                            <div className="font-medium text-[color:var(--da-color-fg)]">{provider.label}</div>
                            <div className="text-xs text-[color:var(--da-color-muted)] mt-1">{provider.desc}</div>
                            <div className="text-xs text-[color:var(--da-color-muted)] mt-2">
                              {t('範圍', 'Scopes')}: {provider.scopes.join(', ')}
                            </div>
                          </div>
                          {config.auth === provider.id && <span className="text-[color:var(--da-color-accent)]">✓</span>}
                        </div>
                      </button>
                    ))}
                  </div>
                </div>
              )}

              {currentStep.id === 'packs' && (
                <div>
                  <h2 className="text-xl font-semibold text-[color:var(--da-color-fg)] mb-2">
                    {t('選擇 Rule Packs', 'Select Rule Packs')}
                  </h2>
                  <p className="text-sm text-[color:var(--da-color-muted)] mb-6">
                    {t('選擇你需要監控的技術棧（可選，留空則不含額外 Rule Pack）', 'Select the technology stacks you need to monitor (optional, leave empty for defaults only)')}
                  </p>
                  <div className="mb-4 flex gap-2">
                    <button
                      onClick={() => setConfig({ ...config, packs: RULE_PACKS.map(p => p.id) })}
                      className="px-3 py-1.5 text-xs font-medium bg-[color:var(--da-color-accent)] text-white rounded-lg hover:bg-[color:var(--da-color-accent-hover)]"
                    >
                      {t('全選', 'Select All')}
                    </button>
                    <button
                      onClick={() => setConfig({ ...config, packs: [] })}
                      className="px-3 py-1.5 text-xs font-medium bg-[color:var(--da-color-tag-bg)] text-[color:var(--da-color-fg)] rounded-lg hover:bg-[color:var(--da-color-surface-hover)]"
                    >
                      {t('清除', 'Clear')}
                    </button>
                  </div>
                  <div className="grid grid-cols-2 md:grid-cols-3 gap-3">
                    {RULE_PACKS.map(pack => (
                      <button
                        key={pack.id}
                        onClick={() => {
                          const newPacks = config.packs.includes(pack.id)
                            ? config.packs.filter(p => p !== pack.id)
                            : [...config.packs, pack.id];
                          setConfig({ ...config, packs: newPacks });
                        }}
                        className={`p-3 rounded-lg border-2 text-center transition-all ${
                          config.packs.includes(pack.id)
                            ? 'border-[color:var(--da-color-accent)] bg-[color:var(--da-color-accent-soft)] '
                            : 'border-[color:var(--da-color-surface-border)] hover:border-[color:var(--da-color-card-hover-border)] '
                        }`}
                      >
                        <div className="text-2xl mb-2">{pack.icon}</div>
                        <div className="text-xs font-medium text-[color:var(--da-color-fg)]">{pack.label}</div>
                        {config.packs.includes(pack.id) && (
                          <div className="text-[color:var(--da-color-accent)] text-sm mt-1">✓</div>
                        )}
                      </button>
                    ))}
                  </div>
                  {packWarning && (
                    <div role="alert" className="mt-4 p-3 bg-[color:var(--da-color-warning-soft)] border border-[color:var(--da-color-warning)] rounded-lg text-sm text-amber-800">
                      {t(
                        '請至少選擇一個 Rule Pack。沒有 Rule Pack 的部署將無法產生有意義的告警規則。',
                        'Please select at least one Rule Pack. A deployment without Rule Packs will not generate meaningful alerting rules.'
                      )}
                    </div>
                  )}
                </div>
              )}

              {currentStep.id === 'review' && (
                <div>
                  <h2 className="text-xl font-semibold text-[color:var(--da-color-fg)] mb-6">
                    {t('檢視摘要', 'Review Summary')}
                  </h2>
                  <div className="bg-[color:var(--da-color-surface-hover)] rounded-lg p-6 space-y-4">
                    <div className="flex justify-between items-center pb-4 border-b border-[color:var(--da-color-surface-border)]">
                      <span className="text-sm font-medium text-[color:var(--da-color-muted)]">{t('部署層級', 'Deployment Tier')}</span>
                      <span className="font-semibold text-[color:var(--da-color-fg)]">{summary.tier}</span>
                    </div>
                    <div className="flex justify-between items-center pb-4 border-b border-[color:var(--da-color-surface-border)]">
                      <span className="text-sm font-medium text-[color:var(--da-color-muted)]">{t('運行環境', 'Environment')}</span>
                      <span className="font-semibold text-[color:var(--da-color-fg)]">{summary.environment}</span>
                    </div>
                    <div className="flex justify-between items-center pb-4 border-b border-[color:var(--da-color-surface-border)]">
                      <span className="text-sm font-medium text-[color:var(--da-color-muted)]">{t('Tenant 數量', 'Tenant Count')}</span>
                      <span className="font-semibold text-[color:var(--da-color-fg)]">{summary.tenantSize}</span>
                    </div>
                    {config.tier === 'tier2' && (
                      <div className="flex justify-between items-center pb-4 border-b border-[color:var(--da-color-surface-border)]">
                        <span className="text-sm font-medium text-[color:var(--da-color-muted)]">{t('認證', 'Authentication')}</span>
                        <span className="font-semibold text-[color:var(--da-color-fg)]">{summary.auth}</span>
                      </div>
                    )}
                    <div className="flex justify-between items-center">
                      <span className="text-sm font-medium text-[color:var(--da-color-muted)]">{t('Rule Packs', 'Rule Packs')}</span>
                      <span className="font-semibold text-[color:var(--da-color-fg)]">{summary.packs}</span>
                    </div>
                  </div>
                  <p className="text-sm text-[color:var(--da-color-muted)] mt-6">
                    {t('點擊「產生輸出」以查看完整的 Helm values。你可以複製內容到你的 values.yaml 檔案。', 'Click "Generate Output" below to see your complete Helm values. You can then copy it to your values.yaml file.')}
                  </p>
                </div>
              )}
            </div>

            {/* ── Navigation ── */}
            <div className="flex items-center justify-between">
              <button
                onClick={handleBack}
                disabled={stepIndex === 0}
                className="px-4 py-2.5 text-sm font-medium text-[color:var(--da-color-muted)] hover:text-[color:var(--da-color-fg)] disabled:opacity-30"
              >
                ← {t('上一步', 'Back')}
              </button>
              <button
                onClick={handleNext}
                className="px-6 py-2.5 text-sm font-medium bg-[color:var(--da-color-accent)] text-white rounded-lg hover:bg-[color:var(--da-color-accent-hover)] transition-colors"
              >
                {stepIndex === steps.length - 1 ? t('產生輸出', 'Generate Output') : t('下一步', 'Next')} →
              </button>
            </div>
          </>
        ) : (
          /* ── Output view ── */
          <>
            <div className="flex items-center justify-between mb-6">
              <h2 className="text-2xl font-bold text-[color:var(--da-color-fg)]">
                {t('Helm Values 設定', 'Generated Helm Values')}
              </h2>
              <button
                onClick={handleReset}
                className="text-sm text-[color:var(--da-color-accent)] hover:underline"
              >
                {t('重新設定', 'Start Over')}
              </button>
            </div>

            <div className="bg-white rounded-xl shadow-sm border border-[color:var(--da-color-surface-border)] overflow-hidden mb-6">
              {/* ── Code display ── */}
              <div className="bg-slate-900 text-slate-100 p-6 font-mono text-sm overflow-x-auto max-h-96">
                <pre className="whitespace-pre-wrap break-words">{helmValues}</pre>
              </div>

              {/* ── Copy button ── */}
              <div className="bg-[color:var(--da-color-surface-hover)] border-t border-[color:var(--da-color-surface-border)] p-4 flex items-center justify-between">
                <p className="text-xs text-[color:var(--da-color-muted)]">
                  {t('複製下方內容到你的 values.yaml 檔案', 'Copy the above content to your values.yaml file')}
                </p>
                <button
                  onClick={handleCopy}
                  className={`px-4 py-2 text-sm font-medium rounded-lg transition-all ${
                    copied
                      ? 'bg-green-500 text-white'
                      : 'bg-[color:var(--da-color-accent)] text-white hover:bg-[color:var(--da-color-accent-hover)]'
                  }`}
                >
                  {copied ? '✓ ' + t('已複製', 'Copied') : t('複製', 'Copy')}
                </button>
              </div>
            </div>

            {/* ── Next steps ── */}
            <div className="bg-[color:var(--da-color-accent-soft)] rounded-xl border border-[color:var(--da-color-accent-soft)] p-6 space-y-6">
              <div>
                <h3 className="font-semibold text-[color:var(--da-color-fg)] mb-4">{t('後續步驟', 'Next Steps')}</h3>
                <ol className="text-sm text-[color:var(--da-color-fg)] space-y-2">
                  <li className="flex gap-3">
                    <span className="font-bold text-[color:var(--da-color-accent)]">1.</span>
                    <span>{t('複製上方 values 到你的 values.yaml 或 Helm chart 目錄', 'Copy the values above to your values.yaml or Helm chart directory')}</span>
                  </li>
                  <li className="flex gap-3">
                    <span className="font-bold text-[color:var(--da-color-accent)]">2.</span>
                    <span>{t('根據你的環境填入 example.com、OAuth2 認證資訊等', 'Fill in example.com, OAuth2 credentials, and other environment-specific values')}</span>
                  </li>
                  <li className="flex gap-3">
                    <span className="font-bold text-[color:var(--da-color-accent)]">3.</span>
                    <span>{t('執行驗證：da-tools validate-config --config-dir ./conf.d', 'Run validation: da-tools validate-config --config-dir ./conf.d')}</span>
                  </li>
                  <li className="flex gap-3">
                    <span className="font-bold text-[color:var(--da-color-accent)]">4.</span>
                    <span>{t('使用 Helm 部署：helm install threshold-exporter oci://ghcr.io/vencil/charts/threshold-exporter -f values.yaml -n monitoring', 'Deploy with Helm: helm install threshold-exporter oci://ghcr.io/vencil/charts/threshold-exporter -f values.yaml -n monitoring')}</span>
                  </li>
                </ol>
              </div>

              {config.tier === 'tier2' && (
                <div className="border-t border-[color:var(--da-color-accent-soft)] pt-4">
                  <h4 className="font-semibold text-[color:var(--da-color-fg)] mb-3">{t('Tier 2：建立首個租戶', 'Tier 2: Create Your First Tenant')}</h4>
                  <ol className="text-sm text-[color:var(--da-color-fg)] space-y-2">
                    <li className="flex gap-3">
                      <span className="font-bold text-[color:var(--da-color-accent)]">5.</span>
                      <span>
                        {t(
                          '使用 template-gallery 或 playground 建立首個租戶配置。查看',
                          'Create your first tenant config using template-gallery or playground. See'
                        )}{' '}
                        <a href="../../assets/jsx-loader.html?component=template-gallery" className="text-[color:var(--da-color-accent)] underline hover:text-[color:var(--da-color-accent-hover)]">
                          {t('範本庫', 'template-gallery')}
                        </a>
                      </span>
                    </li>
                    <li className="flex gap-3">
                      <span className="font-bold text-[color:var(--da-color-accent)]">6.</span>
                      <span>
                        {t('透過 Portal 開啟 ', 'Open ')}
                        <a href="../../assets/jsx-loader.html?component=tenant-manager" className="text-[color:var(--da-color-accent)] underline hover:text-[color:var(--da-color-accent-hover)]">
                          {t('租戶管理工具', 'tenant-manager')}
                        </a>
                        {t(' 來管理租戶與群組成員資格', ' to manage tenant and group membership')}
                      </span>
                    </li>
                    <li className="flex gap-3">
                      <span className="font-bold text-[color:var(--da-color-accent)]">7.</span>
                      <span>
                        {t('查看 ', 'Review ')}
                        <a href="../../getting-started/" className="text-[color:var(--da-color-accent)] underline hover:text-[color:var(--da-color-accent-hover)]">
                          {t('完整入門指南', 'complete getting-started guide')}
                        </a>
                        {t(' 深入了解多租戶告警配置', ' to learn more about multi-tenant alerting')}
                      </span>
                    </li>
                  </ol>
                </div>
              )}
            </div>
          </>
        )}
      </div>
    </div>
  );
}
