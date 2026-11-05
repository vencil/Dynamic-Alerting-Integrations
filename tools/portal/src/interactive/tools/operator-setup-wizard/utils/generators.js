---
title: "Operator Setup Wizard — command + config generators"
purpose: |
  Pure functions that build the artifacts the wizard ships back to
  the user: `da-tools operator-generate` command line, the optional
  `migrate-to-operator --dry-run` migration command, and the Alertmanager
  routing commands (generate-routes → Secret → configSecret; #3572 — routing
  is no AlertmanagerConfig CRD).

  Pre-PR-portal-4 these were inline at the top of
  operator-setup-wizard.jsx. Splitting them out drops 75 LOC from the orchestrator
  and lets the StepReview component (also extracted in PR-portal-4)
  pull from the same canonical implementation.

  Public API:
    validateTenantName(name)               tenant-id rule (ADR-035, tenant-id.json)
    generateOperatorCommand(config)        build da-tools CLI
    generateMigrationCommand(config)       null unless dual-stack
    generateRoutingCommands(config)        generate-routes + Secret + configSecret

  Closure deps: none. Pure functions; receive config as arg.
---

// ADR-035: the tenant-id rule's one source is the tenant-config schema;
// tenant-id.json is the copy generated from it (gen_tenant_id_json.py).
import TENANT_ID_RULE from '../../_common/data/tenant-id.json';

const TENANT_ID_RE = new RegExp(TENANT_ID_RULE.pattern);

function validateTenantName(name) {
  // A DNS-1123 label; the pattern is anchored with ^…$ (ECMAScript's `$`
  // does not match before a trailing newline).
  return TENANT_ID_RE.test(name);
}

// #3572: operator-generate emits PrometheusRule + ServiceMonitor only. Its
// AlertmanagerConfig output and the flags that fed it (--config-dir,
// --api-version, --receiver-template, --secret-name, --secret-key,
// --components alertmanager) are refused (rc 2), so none of them is emitted
// here. Every flag below is one the tool's parser has.
function generateOperatorCommand(config) {
  return [
    'da-tools operator-generate',
    '--rule-packs-dir rule-packs',
    `--namespace=${config.namespace || 'monitoring'}`,
    '--output-dir operator-crds/',
  ].join(' \\\n  ');
}

function generateMigrationCommand(config) {
  if (config.ruleMode !== 'dual-stack') return null;

  return [
    'da-tools migrate-to-operator',
    '--source-dir configmaps/',
    `--namespace=${config.namespace || 'monitoring'}`,
    '--dry-run',
  ].join(' \\\n  ');
}

// The Secret name the routing commands use for the rendered alertmanager.yaml.
const ROUTING_SECRET = 'alertmanager-dynamic-alerting';

// #3572: Alertmanager routing is not AlertmanagerConfig CRDs — the Prometheus
// Operator puts their routes first with `continue: true` and a namespace
// matcher, so the platform routes (Watchdog, custom isolation, synthetic
// probe, sentinel) could never come first. generate-routes renders
// alertmanager.yaml (base config + conf.d) and the Alertmanager reads it as
// its configSecret.
function generateRoutingCommands(config) {
  const ns = config.namespace || 'monitoring';
  return `da-tools generate-routes --config-dir conf.d --output-alertmanager \\
  --base-config <your alertmanager.yaml> -o alertmanager.yaml

kubectl create secret generic ${ROUTING_SECRET} -n ${ns} \\
  --from-file=alertmanager.yaml --dry-run=client -o yaml | kubectl apply -f -

# then set the Alertmanager's configSecret to ${ROUTING_SECRET}
# (kube-prometheus-stack: alertmanager.alertmanagerSpec.configSecret)
`;
}

export { validateTenantName, generateOperatorCommand, generateMigrationCommand, generateRoutingCommands, ROUTING_SECRET };
