---
title: "Deployment Profile Wizard — per-chart Helm values generator"
purpose: |
  Emits one Helm values file PER CHART THIS REPO SHIPS (threshold-exporter;
  plus da-portal + tenant-api for Tier 2), each with its own install command,
  and a list of notes for the choices that are NOT Helm values.

  ⛔ Every key emitted here must be one the target chart's templates read.
  deployment-wizard-generators.test.ts resolves each emitted leaf against the
  chart's `.Values.*` references and fails on any key the chart ignores. The
  previous generator emitted an umbrella-chart shape (`thresholdExporter:`,
  `prometheus:`, `alertmanager:`, `platform:` …) that no chart in helm/ has:
  under its own `helm install threshold-exporter -f values.yaml` instruction
  not one of its leaves was read, including three features that are not
  Helm values at all (`configValidation`, `cardinalityGuard`, `tripleState`).
  Those are explained as notes now — see deployGenerateHelmReleases().notes.

  Public API:
    deployGenerateHelmReleases(config)
      → { releases: [{ chart, namespace, file, install, yaml }], notes: [{ id, title, body }] }

  Data deps (DEPLOY_TIERS / DEPLOY_ENVIRONMENTS / DEPLOY_TENANT_SIZES) are
  ESM-imported from ../fixtures/wizard-defaults.js — TRK-230z Wave 2 retired
  the window.__X call-time reads.
---

import { DEPLOY_TIERS as TIERS, DEPLOY_ENVIRONMENTS as ENVIRONMENTS, DEPLOY_TENANT_SIZES as TENANT_SIZES } from '../fixtures/wizard-defaults.js';
// #1245: image refs come from the generated platform data (tag only, never a
// digest — see _common/data/images.js). They used to be literals here and
// rotted to a v2.7.0-era snapshot, one of them to a ref that does not exist.
import { PLATFORM_IMAGES, imageRepository, imageTag } from '../../_common/data/images.js';

const t = (zh, en) => (window.__t || ((_zh, e) => e))(zh, en);

const CHART_REPO = 'oci://ghcr.io/vencil/charts';

// Per-environment container resources, keyed local / staging / production.
const pick = (environment, local, staging, production) =>
  environment === 'local' ? local : environment === 'staging' ? staging : production;

function header(chart, config) {
  const { tier, environment, tenantSize } = config;
  return `# Helm values for chart: ${chart}
# Profile: ${TIERS.find(x => x.id === tier)?.name} / ${ENVIRONMENTS.find(e => e.id === environment)?.label} / ${TENANT_SIZES.find(s => s.id === tenantSize)?.label}
# Generated: ${new Date().toISOString().split('T')[0]}
# Every key below is read by this chart's templates; anything not listed keeps the chart default.
`;
}

function imageBlock(key) {
  return `image:
  repository: ${imageRepository(key)}
  tag: ${imageTag(key)}
  pullPolicy: IfNotPresent`;
}

function thresholdExporterValues(config, size) {
  const { environment } = config;
  return `${header('threshold-exporter', config)}
replicaCount: ${size?.replicas.exporter || 2}

${imageBlock('thresholdExporter')}

resources:
  requests:
    cpu: ${pick(environment, '50m', '100m', '250m')}
    memory: ${pick(environment, '64Mi', '128Mi', '256Mi')}
  limits:
    cpu: ${pick(environment, '200m', '500m', '1000m')}
    memory: ${pick(environment, '128Mi', '256Mi', '512Mi')}
`;
}

function oauthProviderLines(auth) {
  // oidcIssuerUrl is only read (via `with`) when non-empty; only OIDC needs it.
  return auth === 'oidc'
    ? `  provider: oidc
  oidcIssuerUrl: "https://your-idp.example.com/realms/your-realm"  # replace`
    : `  provider: ${auth || 'github'}`;
}

function daPortalValues(config) {
  const { environment, auth } = config;
  return `${header('da-portal', config)}
replicaCount: ${pick(environment, 1, 2, 3)}

${imageBlock('daPortal')}

oauth2Proxy:
  enabled: true
${oauthProviderLines(auth)}
  redirectUrl: "https://da-portal.example.com/oauth2/callback"  # replace with your host
  cookieSecure: ${environment === 'local' ? 'false' : 'true'}
  # Production: create the Secret yourself (client-id / client-secret / cookie-secret)
  createSecret: ${environment === 'production' ? 'false' : 'true'}

resources:
  nginx:
    requests:
      cpu: ${pick(environment, '30m', '50m', '100m')}
      memory: ${pick(environment, '64Mi', '64Mi', '128Mi')}
    limits:
      cpu: ${pick(environment, '100m', '200m', '500m')}
      memory: ${pick(environment, '128Mi', '128Mi', '256Mi')}
`;
}

function tenantApiValues(config) {
  const { environment, auth } = config;
  return `${header('tenant-api', config)}
replicaCount: ${pick(environment, 1, 2, 2)}

${imageBlock('tenantApi')}

oauth2Proxy:
  enabled: true
${oauthProviderLines(auth)}
  redirectUrl: "https://tenant-api.example.com/oauth2/callback"  # replace with your host
  cookieSecure: ${environment === 'local' ? 'false' : 'true'}
  createSecret: ${environment === 'production' ? 'false' : 'true'}

resources:
  tenantApi:
    requests:
      cpu: ${pick(environment, '50m', '100m', '250m')}
      memory: ${pick(environment, '64Mi', '128Mi', '256Mi')}
    limits:
      cpu: ${pick(environment, '200m', '500m', '1000m')}
      memory: ${pick(environment, '128Mi', '256Mi', '512Mi')}
`;
}

function release(chart, namespace, yaml) {
  const file = `values-${chart}.yaml`;
  return {
    chart,
    namespace,
    file,
    install: `helm upgrade --install ${chart} ${CHART_REPO}/${chart} -n ${namespace} --create-namespace -f ${file}`,
    yaml,
  };
}

function deployNotes(config, size) {
  const { tier, packs } = config;
  const notes = [
    {
      id: 'prometheus-alertmanager',
      title: t('Prometheus / Alertmanager 不是這些 chart 部署的', 'Prometheus / Alertmanager are not deployed by these charts'),
      body: t(
        `請用你既有的 Prometheus 堆疊（例如 kube-prometheus-stack）。此規模建議：Prometheus ×${size?.replicas.prometheus}、保留 ${size?.retention}；Alertmanager ×${size?.replicas.alertmanager}。平台驗證過的版本：${PLATFORM_IMAGES.prometheus}、${PLATFORM_IMAGES.alertmanager}、${PLATFORM_IMAGES.configReloader}。`,
        `Use your existing Prometheus stack (e.g. kube-prometheus-stack). Suggested for this size: Prometheus ×${size?.replicas.prometheus}, retention ${size?.retention}; Alertmanager ×${size?.replicas.alertmanager}. Versions the platform is validated against: ${PLATFORM_IMAGES.prometheus}, ${PLATFORM_IMAGES.alertmanager}, ${PLATFORM_IMAGES.configReloader}.`,
      ),
    },
    {
      id: 'rule-packs',
      title: t('Rule Pack 不是 Helm value', 'Rule Packs are not a Helm value'),
      body: packs.length > 0
        ? t(
          `你選的 Rule Pack 以 ConfigMap 形式出貨，需掛進你的 Prometheus：${packs.map(p => `configmap-rules-${p}.yaml`).join('、')}（位於 k8s/03-monitoring/）。`,
          `The Rule Packs you picked ship as ConfigMaps that your Prometheus must mount: ${packs.map(p => `configmap-rules-${p}.yaml`).join(', ')} (in k8s/03-monitoring/).`,
        )
        : t('未選 Rule Pack。Rule Pack 以 ConfigMap 形式出貨（k8s/03-monitoring/configmap-rules-*.yaml），需掛進你的 Prometheus。',
          'No Rule Pack selected. Rule Packs ship as ConfigMaps (k8s/03-monitoring/configmap-rules-*.yaml) that your Prometheus must mount.'),
    },
    {
      id: 'silent-mode',
      title: t('靜默 / 維護模式是租戶設定，不是 Helm value', 'Silent / maintenance mode is tenant config, not a Helm value'),
      body: t(
        '三態模式逐租戶設定：在 conf.d 的租戶檔寫 `_silent_mode`（接受的寫法見 Schema Explorer）或 `_state_maintenance: enable`。chart 沒有平台層級的「預設模式」開關。',
        'The three-state mode is per tenant: set `_silent_mode` (accepted forms: see Schema Explorer) or `_state_maintenance: enable` in the tenant\'s conf.d file. The chart has no platform-wide "default mode" switch.',
      ),
    },
    {
      id: 'cardinality',
      title: t('每租戶指標上限不可經 Helm 設定', 'The per-tenant metric cap is not configurable through Helm'),
      body: t(
        'chart 以 -config-dir 模式執行，此模式下 `max_metrics_per_tenant` 不生效，上限固定為內建的 500（見 ADR-017）。超限時 `da_tenant_metrics_over_limit` 會 > 0。',
        'The chart runs the exporter in -config-dir mode, where `max_metrics_per_tenant` does not take effect; the cap is the built-in 500 (see ADR-017). When a tenant exceeds it, `da_tenant_metrics_over_limit` goes above 0.',
      ),
    },
    {
      id: 'hot-reload',
      title: t('SHA-256 熱重載不需設定', 'SHA-256 hot-reload needs no configuration'),
      body: t(
        'exporter 內建以 SHA-256 偵測設定變更；檢查間隔是 `exporter.reloadInterval`（預設 30s）。沒有要填的 sha256 值。',
        'The exporter detects config changes by SHA-256 on its own; the check interval is `exporter.reloadInterval` (default 30s). There is no sha256 value to fill in.',
      ),
    },
  ];
  if (tier === 'tier2') {
    notes.push({
      id: 'tier2-extra',
      title: t('Tier 2：這些仍需你自己補', 'Tier 2: what you still need to add yourself'),
      body: t(
        `對外入口：在該 chart 的 values 設 \`ingress.enabled: true\`、\`ingress.className\` 與 \`ingress.hosts\`（兩個 chart 皆有 Ingress template，後端固定走 oauth2-proxy）。tenant-api 需另設 conf.d 來源（\`confDir\` / \`gitRepoUrl\`），見 chart 的 values.yaml。oauth2-proxy image 由 chart 以 tag + digest 釘住，請勿覆寫（#1302）。`,
        `External access: set \`ingress.enabled: true\`, \`ingress.className\` and \`ingress.hosts\` in that chart's values (both charts ship an Ingress template; its backend is always oauth2-proxy). tenant-api also needs its conf.d source (\`confDir\` / \`gitRepoUrl\`); see the chart's values.yaml. The oauth2-proxy image is pinned by the chart by tag + digest — do not override it (#1302).`,
      ),
    });
  }
  return notes;
}

function deployGenerateHelmReleases(config) {
  const size = TENANT_SIZES.find(s => s.id === config.tenantSize);
  const releases = [release('threshold-exporter', 'monitoring', thresholdExporterValues(config, size))];
  if (config.tier === 'tier2') {
    releases.push(release('da-portal', 'monitoring', daPortalValues(config)));
    // tenant-api lives in its own namespace (#1004); da-portal's default
    // portal.tenantApiUrl and NetworkPolicy egress already point there.
    releases.push(release('tenant-api', 'tenant-api', tenantApiValues(config)));
  }
  return { releases, notes: deployNotes(config, size) };
}

export { deployGenerateHelmReleases };
