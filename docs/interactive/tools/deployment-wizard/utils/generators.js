---
title: "Deployment Profile Wizard — Helm values generator"
purpose: |
  Single-function generator that emits a fully-rendered Helm values
  YAML for the chosen tier + environment + tenant size + auth + Rule
  Pack selection. Output is ready to copy-paste into the user's
  values.yaml.

  Pre-PR-portal-10 lived inline in deployment-wizard.jsx as the
  single largest helper (~290 LOC of YAML template). Splitting drops
  it into the sibling subdirectory matching operator-setup-wizard
  PR-portal-4 pattern.

  Public API:
    window.__deployGenerateHelmValues(config)   build full Helm values YAML

  Closure deps: reads window.__DEPLOY_TIERS, __DEPLOY_ENVIRONMENTS,
  __DEPLOY_TENANT_SIZES at call time so the generator picks up the
  catalog from the fixtures dep loaded earlier.
---

function deployGenerateHelmValues(config) {
  const TIERS = window.__DEPLOY_TIERS || [];
  const ENVIRONMENTS = window.__DEPLOY_ENVIRONMENTS || [];
  const TENANT_SIZES = window.__DEPLOY_TENANT_SIZES || [];

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
    tag: v2.7.0
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
    tag: v2.7.0

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
    tag: v2.7.0

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

window.__deployGenerateHelmValues = deployGenerateHelmValues;
