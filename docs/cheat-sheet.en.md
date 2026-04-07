---
title: "da-tools Quick Reference"
tags: [reference, cli, cheat-sheet]
audience: [all]
version: v2.6.0
lang: en
---

# da-tools Quick Reference

> **Language / 語言：** **English (Current)** | [中文](cheat-sheet.md)

da-tools command quick reference. Full docs at [cli-reference.en.md](cli-reference.en.md).

## Command Reference

| Command | Description | Key Flags | Example |
|---------|-------------|-----------|---------|
| `check-alert` | Query alert status for a specific tenant | - | `da-tools check-alert --help` |
| `diagnose` | Full health check for a single tenant | --config-dir <PATH>, --namespace <NS> | `da-tools diagnose --help` |
| `batch-diagnose` | Parallel health check for all tenants | --tenants <LIST>, --workers <N>, --timeout <SEC> | `da-tools batch-diagnose --help` |
| `baseline` | Observe metric time series, compute stats (p50/p90/p95/p99/max), suggest thresholds | --tenant <NAME>, --duration <SEC>, --interval <SEC> | `da-tools baseline --help` |
| `validate` | Shadow Monitoring validation: compare old/new Recording Rule values | --watch, --interval <SEC>, --rounds <N> | `da-tools validate --help` |
| `cutover` | Shadow Monitoring one-click switchover: stop old rules, enable new, verify health | --tenant <NAME>, --readiness-json <FILE>, --dry-run | `da-tools cutover --help` |
| `blind-spot` | Scan Prometheus active targets, cross-check with tenant config for blind spots | --config-dir <PATH>, --exclude-jobs <LIST>, --json-output | `da-tools blind-spot --help` |
| `maintenance-scheduler` | Evaluate scheduled maintenance windows (`_state_maintenance`) | --config-dir <PATH>, --output <FILE>, --timezone <TZ> | `da-tools maintenance-scheduler --help` |
| `backtest` | Historical backtest for PR threshold changes | --lookback <DAYS>, --output <FILE> | `da-tools backtest --help` |
| `shadow-verify` | Shadow Monitoring readiness & convergence 3-phase verification | --mapping <FILE>, --report-csv <FILE>, --readiness-json <FILE> | `da-tools shadow-verify --help` |
| `byo-check` | Automated BYO Prometheus & Alertmanager integration verification | --prometheus <URL>, --alertmanager <URL>, --json | `da-tools byo-check --help` |
| `federation-check` | Multi-cluster Federation integration verification | --prometheus <URL>, --edge-urls <URLS>, --json | `da-tools federation-check --help` |
| `grafana-import` | Grafana Dashboard import via ConfigMap sidecar auto-mount | --dashboard <FILE>, --dashboard-dir <DIR>, --name <NAME> | `da-tools grafana-import --help` |
| `alert-quality` | Alert quality scoring: 4 metrics, 3 grades, CI gate | --prometheus <URL>, --tenant <NAME>, --ci --min-score <N> | `da-tools alert-quality --help` |
| `alert-correlate` | Alert correlation analysis: time-window clustering + root cause inference | --prometheus <URL>, --input <FILE>, --window <MIN>, --min-score <N> | `da-tools alert-correlate --help` |
| `drift-detect` | Cross-cluster config drift detection: directory-level SHA-256 comparison | --dirs <LIST>, --labels <LIST>, --ci | `da-tools drift-detect --help` |
| `cardinality-forecast` | Per-tenant cardinality trend prediction with limit-breach warning | --prometheus <URL>, --limit <N>, --warn-days <N>, --ci | `da-tools cardinality-forecast --help` |
| `evaluate-policy` | Policy-as-Code evaluation: declarative DSL policy checks | --config-dir <PATH>, --policy <FILE>, --ci | `da-tools evaluate-policy --help` |
| `generate-routes` | Generate Alertmanager route + receiver + inhibit from tenant YAML | --config-dir <PATH>, --output <FILE>, --output-configmap | `da-tools generate-routes --help` |
| `patch-config` | ConfigMap partial update with preview (--diff) | --namespace <NS>, --configmap <CM>, --dry-run | `da-tools patch-config --help` |
| `scaffold` | Generate new tenant config (interactive or non-interactive) | --non-interactive, --tenant <NAME>, --db <LIST> | `da-tools scaffold --help` |
| `migrate` | Convert traditional Prometheus rules to dynamic format (AST engine) | --output <DIR>, --dry-run, --triage | `da-tools migrate --help` |
| `validate-config` | One-stop config validation: YAML, schema, routing, policy, version | --config-dir <PATH>, --policy <DOMAINS>, --ci | `da-tools validate-config --help` |
| `offboard` | Offboard tenant config and related resources | --config-dir <PATH>, --backup <DIR>, --cleanup-rules | `da-tools offboard --help` |
| `deprecate` | Mark metrics as disabled to prevent misuse | --config-dir <PATH>, --reason <TEXT>, --dry-run | `da-tools deprecate --help` |
| `lint` | Check Custom Rule governance compliance (`custom_` prefix rules) | --strict, --json-output | `da-tools lint --help` |
| `onboard` | Analyze existing Alertmanager/Prometheus config for migration hints | --alertmanager-config <FILE>, --output <FILE> | `da-tools onboard --help` |
| `analyze-gaps` | Compare custom rules vs Rule Packs for duplicates/gaps | --config <PATH>, --output <FILE>, --json-output | `da-tools analyze-gaps --help` |
| `config-diff` | Compare two config directories (GitOps PR review) | --old-dir <PATH>, --new-dir <PATH>, --json-output | `da-tools config-diff --help` |
| `test-notification` | Multi-channel notification connectivity testing | --config-dir <PATH>, --tenant <NAME>, --dry-run, --ci | `da-tools test-notification --help` |
| `threshold-recommend` | Threshold recommendation engine (historical P50/P95/P99) | --config-dir <PATH>, --prometheus <URL>, --lookback, --json | `da-tools threshold-recommend --help` |
| `explain-route` | Routing merge pipeline debugger: four-layer expansion + profile (ADR-007) | --config-dir <PATH>, --tenant <NAME>, --show-profile-expansion, --json | `da-tools explain-route --help` |
| `discover-mappings` | Auto-discover 1:N instance-tenant mappings (ADR-006) | --endpoint <URL> or --prometheus <URL> --instance <INST>, --job, -o, --json | `da-tools discover-mappings --help` |
| `init` | Project skeleton generation (CI/CD + conf.d + Kustomize overlays) | --ci <PLATFORM>, --tenants <LIST>, --non-interactive, --dry-run | `da-tools init --help` |
| `config-history` | Config snapshot & history tracking (snapshot / log / show / diff) | --config-dir <PATH>, -m <MSG>, --limit <N> | `da-tools config-history --help` |
| `gitops-check` | GitOps Native Mode readiness validation (repo / local / sidecar) | --url <URL>, --dir <PATH>, --namespace <NS> | `da-tools gitops-check --help` |
| `operator-generate` | Operator CRD generation (PrometheusRule / AlertmanagerConfig / ServiceMonitor) | --rule-packs-dir <DIR>, --config-dir <DIR>, --namespace, --split, --apply | `da-tools operator-generate --help` |
| `operator-check` | Operator CRD deployment status verification (5 checks + diagnostic report) | --namespace <NS>, --json | `da-tools operator-check --help` |
| `migrate-to-operator` | Read ConfigMap-based rules, produce equivalent CRD YAML + 6-stage migration plan | --source-dir <DIR>, --dry-run, --receiver-template | `da-tools migrate-to-operator --help` |
| `rule-pack-split` | Rule Pack hierarchical split (edge Part 1 + central Parts 2+3) | --rule-packs-dir <DIR>, --output-dir <DIR>, --scenario | `da-tools rule-pack-split --help` |
| `opa-evaluate` | OPA Rego policy evaluation bridge (OPA integration) | --config-dir <PATH>, --opa-url <URL>, --opa-binary, --policy-path, --dry-run | `da-tools opa-evaluate --help` |

## Quick Tips

- **Prometheus API Tools**: Require connectivity to Prometheus HTTP API
  - `check-alert` — Query alert status
  - `diagnose` / `batch-diagnose` — Tenant health check
  - `baseline` — Observe metrics, generate threshold suggestions
  - `validate` — Shadow Monitoring comparison
  - `cutover` — One-click switchover (final migration step)
  - Others: `blind-spot`, `maintenance-scheduler`, `backtest`
  - `alert-quality` — Alert quality scoring (noise, stale, latency, suppression)
  - `alert-correlate` — Alert correlation analysis (time-window clustering + root cause)
  - `cardinality-forecast` — Per-tenant cardinality trend prediction
  - `threshold-recommend` — Threshold recommendation (P50/P95/P99)

- **Config Generation Tools**
  - `generate-routes` — Tenant YAML → Alertmanager fragment
  - `patch-config` — ConfigMap partial update

- **Filesystem Tools** (offline capable)
  - `scaffold` — Generate tenant config
  - `migrate` — Rule format conversion
  - `validate-config` — Config validation
  - `offboard` / `deprecate` — Tenant offboarding / metric deprecation
  - `lint` / `onboard` / `analyze-gaps` / `config-diff` — Governance tools
  - `evaluate-policy` — Policy-as-Code evaluation (declarative DSL)
  - `test-notification` — Multi-channel notification connectivity testing
  - `explain-route` — Routing merge pipeline debugger (four-layer expansion)
  - `discover-mappings` — Auto-discover 1:N instance-tenant mappings

- **Adoption & Initialization**
  - `init` — Project skeleton generation (CI/CD + conf.d + Kustomize)
  - `config-history` — Config snapshot & history tracking
  - `gitops-check` — GitOps Native Mode readiness validation

- **Operator + Federation**
  - `operator-generate` — Operator CRD generation (PrometheusRule / AlertmanagerConfig / ServiceMonitor)
  - `operator-check` — Operator CRD deployment status verification
  - `rule-pack-split` — Rule Pack hierarchical split (Federation Scenario B)
  - `opa-evaluate` — OPA Rego policy evaluation bridge

## Network Configuration

```bash
# K8s internal
export PROMETHEUS_URL=http://prometheus.monitoring.svc.cluster.local:9090

# Docker Desktop
export PROMETHEUS_URL=http://host.docker.internal:9090

# Linux Docker (--network=host)
export PROMETHEUS_URL=http://localhost:9090
```

## Common Templates

```bash
# Basic command
docker run --rm --network=host \
  -e PROMETHEUS_URL=$PROMETHEUS_URL \
  ghcr.io/vencil/da-tools:v2.6.0 \
  <command> [arguments]

# With local files
docker run --rm --network=host \
  -v $(pwd)/conf.d:/etc/config:ro \
  -e PROMETHEUS_URL=$PROMETHEUS_URL \
  ghcr.io/vencil/da-tools:v2.6.0 \
  <command> --config-dir /etc/config
```

---

Full reference at [cli-reference.en.md](cli-reference.en.md).

## Related Resources

| Resource | Relevance |
|----------|-----------|
| [da-tools Quick Reference (中文)](./cheat-sheet.md) | ⭐⭐⭐ |
| [da-tools CLI Reference](./cli-reference.en.md) | ⭐⭐⭐ |
| [Glossary](./glossary.en.md) | ⭐⭐ |
| [Threshold Exporter API Reference](api/README.en.md) | ⭐⭐ |
