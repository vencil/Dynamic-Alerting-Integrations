---
title: "da-tools CLI Reference"
tags: [cli, reference, da-tools, tools]
audience: [platform-engineer, sre, devops, tenant]
version: v2.3.0
lang: en
---

# da-tools CLI Reference

> **Audience**: Platform Engineers, SREs, DevOps, Tenants
> **Container Image**: `ghcr.io/vencil/da-tools:v2.3.0`
> **Version**: (synced with platform version)

da-tools is a portable CLI container that bundles validation, migration, configuration, and operational tools for the Dynamic Alerting platform. This document is a complete reference for all subcommands.

---

## Table of Contents

1. [Quick Start](#quick-start)
2. [Global Options](#global-options)
3. [Command Categories](#command-categories)
4. [Command Reference](#command-reference)
   - [Prometheus API Tools](#prometheus-api-tools)
   - [Configuration Generation Tools](#configuration-generation-tools)
   - [Filesystem Tools](#filesystem-tools)
5. [Environment Variables](#environment-variables)
6. [Docker Quick Reference](#docker-quick-reference)

---

## Quick Start

### Pull the Image

```bash
# Pull from OCI registry (requires CI/CD push)
docker pull ghcr.io/vencil/da-tools:v2.3.0

# Local build (development)
cd components/da-tools/app && ./build.sh v1.11.0
```

### View Help

```bash
docker run --rm ghcr.io/vencil/da-tools:v2.3.0 --help
docker run --rm ghcr.io/vencil/da-tools:v2.3.0 --version
da-tools <command> --help
```

---

## Docker Usage Pattern

--8<-- "docs/includes/docker-usage-pattern.en.md"

> Subsequent examples omit this prefix and show only `da-tools <command>` form.

---

## Global Options

All commands support the following global options:

| Option | Description |
|--------|-------------|
| `--help` | Show help message |
| `--version` | Show version information |
| `--config-dir <PATH>` | Path to tenant configuration directory (default: `./conf.d`; required by some commands) |

---

## Command Categories

### Prometheus API Tools (Network Access Required)

These tools only need HTTP access to Prometheus API and can run from anywhere.

| Command | Purpose | Minimum Parameters |
|---------|---------|-------------------|
| `check-alert` | Query alert state for a specific tenant | `<alert_name> <tenant>` |
| `diagnose` | Tenant health check (config + metrics + alert state) | `<tenant>` |
| `batch-diagnose` | Multi-tenant health check (auto-discover + parallel) | (auto-discover) |
| `baseline` | Observe metrics + recommend thresholds | `--tenant <name>` |
| `validate` | Shadow Monitoring dual-track comparison (with auto-convergence) | `--mapping <file>` or `--old <query> --new <query>` |
| `cutover` | Shadow Monitoring one-click cutover | `--tenant <name>` |
| `blind-spot` | Scan cluster targets vs tenant config blind spots | `--config-dir <dir>` |
| `maintenance-scheduler` | Evaluate scheduled maintenance windows, auto-create Alertmanager silences | `--config-dir <dir>` |
| `backtest` | Backtest PR threshold changes against historical data | `--git-diff` or `--config-dir` + `--baseline` |
| `shadow-verify` | Shadow Monitoring readiness & convergence verification (preflight / runtime / convergence) | `<phase>` |
| `byo-check` | BYO Prometheus & Alertmanager integration verification | `<target>` |
| `federation-check` | Multi-cluster federation integration verification (edge / central / e2e) | `<target>` |
| `grafana-import` | Grafana dashboard ConfigMap import (sidecar auto-mount) | `--dashboard <file>` or `--verify` |
| `alert-quality` | Alert quality scoring (4 metrics, 3 grades, CI gate) | `--prometheus <url>` |
| `alert-correlate` | Alert correlation analysis (time-window clustering + root cause inference) | `--prometheus <url>` or `--input <file>` |
| `drift-detect` | Cross-cluster config drift detection (directory-level SHA-256) | `--dirs <list>` |
| `cardinality-forecast` | Per-tenant cardinality trend prediction with limit-breach warning | `--prometheus <url>` |
| `config-history` | Config snapshot & history tracking (snapshot / log / show / diff) | `--config-dir <dir> <action>` |

### Adoption & Initialization

| Command | Purpose | Minimum Parameters |
|---------|---------|-------------------|
| `init` | Project skeleton generation (CI/CD + conf.d + Kustomize overlays) | `--ci <platform>` or interactive mode |
| `gitops-check` | GitOps Native Mode readiness validation (repo / local / sidecar) | `<subcommand>` |

### Operator + Federation Tools

| Command | Purpose | Minimum Parameters |
|---------|---------|-------------------|
| `operator-generate` | Rule Packs + Tenant config → PrometheusRule / AlertmanagerConfig / ServiceMonitor CRD YAML | `--rule-packs-dir <dir>` |
| `operator-check` | Operator CRD deployment status verification (5 checks + diagnostic report) | (auto-discover or `--namespace <ns>`) |
| `rule-pack-split` | Rule Pack hierarchical split (edge Part 1 + central Parts 2+3), Federation Scenario B | `--rule-packs-dir <dir>` |

### Configuration Generation Tools

| Command | Purpose | Minimum Parameters |
|---------|---------|-------------------|
| `generate-routes` | Tenant YAML → Alertmanager route + receiver + inhibit fragment | `--config-dir <dir>` |
| `patch-config` | ConfigMap partial update with `--diff` preview | `<tenant> <metric> <value>` or `--diff` |

### Filesystem Tools (Offline Available)

These tools operate on local YAML files and don't require network.

| Command | Purpose | Minimum Parameters |
|---------|---------|-------------------|
| `scaffold` | Generate tenant configuration | `--tenant <name> --db <types>` |
| `migrate` | Legacy rules → dynamic format conversion (AST engine) | `<input_file>` |
| `validate-config` | One-stop configuration validation (YAML + schema + routes + policy) | `--config-dir <dir>` |
| `offboard` | Offboard tenant configuration | `<tenant>` |
| `deprecate` | Mark metrics as disabled | `<metric_keys...>` |
| `lint` | Check Custom Rule governance compliance | `<path...>` |
| `onboard` | Analyze existing Alertmanager/Prometheus config for migration | `<config_file>` or `--alertmanager-config <file>` |
| `analyze-gaps` | Compare custom rules with Rule Pack coverage | `--config <path>` |
| `config-diff` | Directory-level config diff (GitOps PR review) | `--old-dir <dir> --new-dir <dir>` |
| `evaluate-policy` | Policy-as-Code DSL evaluation engine | `--config-dir <dir>` |
| `opa-evaluate` | OPA Rego policy evaluation bridge (OPA integration) | `--config-dir <dir>` |
| `test-notification` | Multi-channel notification connectivity testing | `--config-dir <dir>` |
| `threshold-recommend` | Threshold recommendation engine (historical P50/P95/P99) | `--config-dir <dir>` + `--prometheus <url>` |
| `explain-route` | Routing merge pipeline debugger (four-layer expansion + profile, ADR-007) | `--config-dir <dir>` |
| `discover-mappings` | Auto-discover 1:N instance-tenant mappings (scrape exporter /metrics, ADR-006) | `--endpoint <url>` or `--prometheus <url>` |

---

## Command Reference

### Prometheus API Tools

#### check-alert

Query the state of a specific alert for a tenant.

**Purpose**: BYOP integration validation, debugging alert state.

**Syntax**

```bash
da-tools check-alert <alert_name> <tenant> [options]
```

**Required Parameters**

| Parameter | Description | Example |
|-----------|-------------|---------|
| `<alert_name>` | Alert name | `MariaDBHighConnections` |
| `<tenant>` | Tenant ID | `db-a` |

**Output**

JSON format containing alert state (firing / pending / inactive).

```json
{
  "alert": "MariaDBHighConnections",
  "tenant": "db-a",
  "state": "firing",
  "details": [
    {
      "state": "firing",
      "activeAt": "2026-03-12T10:30:00Z"
    }
  ]
}
```

**Examples**

```bash
da-tools check-alert MariaDBHighConnections db-a
```

**Exit Codes**

| Code | Description |
|------|-------------|
| `0` | Success (any state) |
| `1` | Prometheus connection failed |

---

#### diagnose

Perform comprehensive health check for a single tenant.

**Purpose**: Verify tenant configuration, metric collection, alert rule completeness.

**Syntax**

```bash
da-tools diagnose <tenant> [options]
```

**Required Parameters**

| Parameter | Description | Example |
|-----------|-------------|---------|
| `<tenant>` | Tenant ID | `db-a` |

**Options**

| Option | Description | Default |
|--------|-------------|---------|
| `--config-dir <PATH>` | Tenant config directory (to query profile info) | `./conf.d` |
| `--namespace <NS>` | K8s namespace (to query ConfigMap) | `monitoring` |

**Output**

JSON format health check report.

```json
{
  "status": "healthy",
  "tenant": "db-a",
  "profile": "standard-mariadb",
  "checks": {
    "config": "ok",
    "metrics": "ok",
    "alerts": "ok"
  },
  "details": {
    "config_source": "threshold-config ConfigMap",
    "metric_count": 42,
    "alert_count": 18
  }
}
```

**Examples**

```bash
# Basic check
docker run --rm --network=host \
  -e PROMETHEUS_URL=http://prometheus.monitoring.svc.cluster.local:9090 \
  ghcr.io/vencil/da-tools:v2.3.0 \
  diagnose db-a

# With local config directory
docker run --rm --network=host \
  -v $(pwd)/conf.d:/etc/config:ro \
  -e PROMETHEUS_URL=http://prometheus.monitoring.svc.cluster.local:9090 \
  ghcr.io/vencil/da-tools:v2.3.0 \
  diagnose db-a --config-dir /etc/config
```

**Exit Codes**

| Code | Description |
|------|-------------|
| `0` | Healthy (all checks pass) |
| `1` | One or more checks failed |
| `2` | Parameter error or connection failed |

---

#### batch-diagnose

Run parallel health checks on all tenants.

**Purpose**: Post-migration regular health checks; quick platform-wide status scan.

**Syntax**

```bash
da-tools batch-diagnose [options]
```

**Required Parameters**

None (auto-discover tenants).

**Options**

| Option | Description | Default |
|--------|-------------|---------|
| `--tenants <LIST>` | Comma-separated tenant list (if omitted, auto-discover) | (auto) |
| `--workers <N>` | Number of parallel diagnosis threads | `5` |
| `--timeout <SEC>` | Single diagnose timeout in seconds | `30` |
| `--output <FILE>` | Output to file (JSON format) | stdout |
| `--dry-run` | Only list tenants, don't run checks | false |
| `--namespace <NS>` | K8s namespace (for auto-discover) | `monitoring` |

**Output**

Unified JSON report with summary of all tenant checks.

**Examples**

```bash
da-tools batch-diagnose --workers 10
da-tools batch-diagnose --tenants db-a,db-b,db-c --output /tmp/report.json
```

**Exit Codes**

| Code | Description |
|------|-------------|
| `0` | All tenants healthy |
| `1` | One or more tenant checks failed |

---

#### baseline

Observe metric time series, calculate statistics (p50/p90/p95/p99/max), produce threshold recommendations.

**Purpose**: Get reasonable initial thresholds when adding DB instances; decide threshold adjustments after load testing.

**Syntax**

```bash
da-tools baseline --tenant <name> [options]
```

**Required Parameters**

| Parameter | Description |
|-----------|-------------|
| `--tenant <NAME>` | Tenant ID |

**Options**

| Option | Description | Default |
|--------|-------------|---------|
| `--duration <SEC>` | Observation duration in seconds | `300` |
| `--interval <SEC>` | Sampling interval in seconds | `30` |
| `--metrics <LIST>` | Comma-separated metric list (empty=all) | (all) |
| `--output <FILE>` | Output to CSV file | stdout |
| `--dry-run` | Only show metrics to observe, don't sample | false |

**Output**

CSV format with one line per metric containing statistical summary (p50, p90, p95, p99, max, recommended threshold).

**Examples**

```bash
# 30-minute observation with 30-second sampling
docker run --rm --network=host \
  -e PROMETHEUS_URL=http://prometheus.monitoring.svc.cluster.local:9090 \
  ghcr.io/vencil/da-tools:v2.3.0 \
  baseline --tenant db-a --duration 1800 --interval 30 --output /tmp/baseline.csv
```

**Exit Codes**

| Code | Description |
|------|-------------|
| `0` | Success |
| `1` | Prometheus connection or query failed |

---

#### validate

Shadow Monitoring validation tool: compare new vs old Recording Rule values, detect auto-convergence.

**Purpose**: Monitor rule equivalence during migration; determine when it's safe to cutover.

**Syntax**

```bash
da-tools validate [--mapping <file> | --old <query> --new <query>] [options]
```

**Required Parameters**

Choose one mode:

1. **Mapping Mode**: `--mapping <file>`
   mapping.csv format:
   ```
   old_rule,new_rule
   mysql_connections,tenant:custom_mysql_connections:max
   mysql_replication_lag,tenant:custom_mysql_replication_lag:max
   ```

2. **Query Mode**: `--old <query> --new <query>`
   Directly specify two PromQL expressions.

**Options**

| Option | Description | Default |
|--------|-------------|---------|
| `--watch` | Continuous monitoring mode (compare every N seconds) | false |
| `--interval <SEC>` | Monitoring interval in seconds | `60` |
| `--rounds <N>` | Number of monitoring rounds (0 = infinite) | `0` |
| `--tolerance <PCT>` | Allowed deviation percentage | `5` |
| `--auto-detect-convergence` | Auto-detect convergence and output readiness JSON | false |
| `--output <FILE>` | Output to CSV or JSON file | stdout |

**Output**

CSV format with one line per rule showing comparison results (old value, new value, difference %, convergence status).

If `--auto-detect-convergence` is used, additionally outputs `cutover-readiness.json` for use with `cutover` command.

**Examples**

```bash
# One-time comparison
docker run --rm --network=host \
  -v $(pwd)/mapping.csv:/data/mapping.csv:ro \
  -e PROMETHEUS_URL=http://prometheus.monitoring.svc.cluster.local:9090 \
  ghcr.io/vencil/da-tools:v2.3.0 \
  validate --mapping /data/mapping.csv

# Continuous monitoring (every 60 seconds for 24 hours)
docker run --rm --network=host \
  -v $(pwd)/mapping.csv:/data/mapping.csv:ro \
  -e PROMETHEUS_URL=http://prometheus.monitoring.svc.cluster.local:9090 \
  ghcr.io/vencil/da-tools:v2.3.0 \
  validate --mapping /data/mapping.csv --watch --interval 60 --rounds 1440

# Auto-detect convergence
docker run --rm --network=host \
  -v $(pwd)/mapping.csv:/data/mapping.csv:ro \
  -v $(pwd)/output:/data/output \
  -e PROMETHEUS_URL=http://prometheus.monitoring.svc.cluster.local:9090 \
  ghcr.io/vencil/da-tools:v2.3.0 \
  validate --mapping /data/mapping.csv \
    --auto-detect-convergence \
    --output /data/output/validation-report.csv
```

**Exit Codes**

| Code | Description |
|------|-------------|
| `0` | Success (any convergence state) |
| `1` | Prometheus connection or query failed |

---

#### cutover

Shadow Monitoring one-click cutover: stop old rules, enable new rules, verify health.

**Purpose**: Final migration step, automates complete cutover workflow.

**Syntax**

```bash
da-tools cutover --tenant <name> [options]
```

**Required Parameters**

| Parameter | Description |
|-----------|-------------|
| `--tenant <NAME>` | Tenant ID |

**Options**

| Option | Description | Default |
|--------|-------------|---------|
| `--readiness-json <FILE>` | JSON output from validate --auto-detect-convergence | (optional) |
| `--dry-run` | Preview cutover steps without making changes | false |
| `--force` | Skip readiness check and proceed directly | false |
| `--namespace <NS>` | K8s namespace | `monitoring` |

**Automated Steps**

1. Verify readiness (if provided)
2. Stop Shadow Monitor Job
3. Remove old Recording Rules
4. Remove `migration_status: shadow` label
5. Remove Alertmanager shadow route
6. Run `check-alert` + `diagnose` verification

**Examples**

```bash
# Dry run — preview cutover
docker run --rm --network=host \
  -v $(pwd)/output:/data \
  -e PROMETHEUS_URL=http://prometheus.monitoring.svc.cluster.local:9090 \
  ghcr.io/vencil/da-tools:v2.3.0 \
  cutover --readiness-json /data/cutover-readiness.json \
    --tenant db-a --dry-run

# Execute cutover
docker run --rm --network=host \
  -v $(pwd)/output:/data \
  -e PROMETHEUS_URL=http://prometheus.monitoring.svc.cluster.local:9090 \
  ghcr.io/vencil/da-tools:v2.3.0 \
  cutover --readiness-json /data/cutover-readiness.json \
    --tenant db-a

# Force cutover (after confirming safety)
docker run --rm --network=host \
  -e PROMETHEUS_URL=http://prometheus.monitoring.svc.cluster.local:9090 \
  ghcr.io/vencil/da-tools:v2.3.0 \
  cutover --tenant db-a --force
```

**Exit Codes**

| Code | Description |
|------|-------------|
| `0` | Cutover successful |
| `1` | Readiness check failed |
| `2` | Error during cutover process |

---

#### blind-spot

Scan Prometheus cluster's active targets, cross-reference with tenant config to find blind spots (exporter present but no tenant config).

**Purpose**: Regular post-migration health check; ensure new exporters are managed.

**Syntax**

```bash
da-tools blind-spot --config-dir <path> [options]
```

**Required Parameters**

| Parameter | Description |
|-----------|-------------|
| `--config-dir <PATH>` | Tenant configuration directory |

**Options**

| Option | Description | Default |
|--------|-------------|---------|
| `--exclude-jobs <LIST>` | Exclude job list (comma-separated) | (none) |
| `--json-output` | Structured JSON output | false |

**Output**

Presented in three sections:
- **Covered**: Exporters with corresponding tenant config
- **Blind Spots**: Exporters without tenant config
- **Unrecognized**: Jobs with unidentifiable DB type

**Examples**

```bash
# Basic scan
docker run --rm --network=host \
  -v $(pwd)/conf.d:/etc/config:ro \
  -e PROMETHEUS_URL=http://prometheus.monitoring.svc.cluster.local:9090 \
  ghcr.io/vencil/da-tools:v2.3.0 \
  blind-spot --config-dir /etc/config

# Exclude infrastructure jobs
docker run --rm --network=host \
  -v $(pwd)/conf.d:/etc/config:ro \
  -e PROMETHEUS_URL=http://prometheus.monitoring.svc.cluster.local:9090 \
  ghcr.io/vencil/da-tools:v2.3.0 \
  blind-spot --config-dir /etc/config \
    --exclude-jobs node-exporter,kube-state-metrics

# JSON output (for CI consumption)
docker run --rm --network=host \
  -v $(pwd)/conf.d:/etc/config:ro \
  -e PROMETHEUS_URL=http://prometheus.monitoring.svc.cluster.local:9090 \
  ghcr.io/vencil/da-tools:v2.3.0 \
  blind-spot --config-dir /etc/config --json-output > /tmp/blind-spots.json
```

**Exit Codes**

| Code | Description |
|------|-------------|
| `0` | Success (regardless of blind spots) |
| `1` | Prometheus connection failed |

---

#### maintenance-scheduler

Evaluate scheduled maintenance windows (cron expressions in `_state_maintenance.recurring[]`), auto-generate Alertmanager silence YAML.

**Purpose**: Automate scheduled maintenance window silences; pair with CronJob.

**Syntax**

```bash
da-tools maintenance-scheduler --config-dir <path> [options]
```

**Required Parameters**

| Parameter | Description |
|-----------|-------------|
| `--config-dir <PATH>` | Tenant configuration directory |

**Options**

| Option | Description | Default |
|--------|-------------|---------|
| `--output <FILE>` | Output to YAML file | stdout |
| `--timezone <TZ>` | Timezone (IANA format) | `UTC` |
| `--dry-run` | Only show silences to generate, don't write | false |

**Output**

Alertmanager silence YAML (can be piped directly to Alertmanager API or kubectl apply).

**Examples**

```bash
# Preview silences to generate
docker run --rm \
  -v $(pwd)/conf.d:/etc/config:ro \
  ghcr.io/vencil/da-tools:v2.3.0 \
  maintenance-scheduler --config-dir /etc/config --dry-run

# Generate YAML for CronJob use
docker run --rm \
  -v $(pwd)/conf.d:/etc/config:ro \
  -v $(pwd)/output:/data/output \
  ghcr.io/vencil/da-tools:v2.3.0 \
  maintenance-scheduler --config-dir /etc/config \
    --timezone Asia/Taipei \
    -o /data/output/alertmanager-silences.yaml
```

**Exit Codes**

| Code | Description |
|------|-------------|
| `0` | Success |
| `1` | Invalid config directory |

---

#### backtest

Execute historical backtest of PR threshold changes.

**Purpose**: Validate threshold adjustment impacts; estimate expected alert changes from PR.

**Syntax**

```bash
docker run --rm --network=host \
  [-v <config_dir>:/etc/config:ro] \
  [-v <baseline_dir>:/data/baseline:ro] \
  -e PROMETHEUS_URL=<url> \
  ghcr.io/vencil/da-tools:v2.3.0 \
  backtest [--git-diff | --config-dir <dir> --baseline <dir>] [options]
```

**Required Parameters**

Choose one mode:

1. **Git Diff Mode**: `--git-diff`
   (Run inside Git repo, auto-detect changes)

2. **Directory Comparison Mode**: `--config-dir <dir> --baseline <dir>`
   (Compare two config versions)

**Options**

| Option | Description | Default |
|--------|-------------|---------|
| `--lookback <DAYS>` | Historical lookback in days | `7` |
| `--output <FILE>` | Output to JSON or CSV | stdout |

**Output**

Comparison report showing impact of threshold changes on historical data (potential alerts increased/decreased).

**Examples**

```bash
# Git Diff mode
cd <repo> && docker run --rm --network=host \
  -v $(pwd):/workspace:ro \
  -e PROMETHEUS_URL=http://prometheus.monitoring.svc.cluster.local:9090 \
  ghcr.io/vencil/da-tools:v2.3.0 \
  backtest --git-diff --lookback 7

# Directory comparison mode
docker run --rm --network=host \
  -v $(pwd)/conf.d-old:/data/old:ro \
  -v $(pwd)/conf.d-new:/data/new:ro \
  -e PROMETHEUS_URL=http://prometheus.monitoring.svc.cluster.local:9090 \
  ghcr.io/vencil/da-tools:v2.3.0 \
  backtest --config-dir /data/new --baseline /data/old --lookback 7
```

**Exit Codes**

| Code | Description |
|------|-------------|
| `0` | Success |
| `1` | Prometheus connection or Git operation failed |

---

#### shadow-verify

Shadow Monitoring readiness and convergence three-phase verification.

**Purpose**: Pre-flight checks before starting shadow monitoring, runtime health checks during shadow monitoring, convergence assessment before cutover decision.

**Syntax**

```bash
da-tools shadow-verify <phase> [options]
```

**Required Parameters**

| Parameter | Description | Values |
|-----------|-------------|--------|
| `<phase>` | Verification phase | `preflight` / `runtime` / `convergence` / `all` |

**Options**

| Option | Description | Default |
|--------|-------------|---------|
| `--mapping <FILE>` | Path to prefix-mapping.yaml (for preflight) | (none) |
| `--report-csv <FILE>` | Path to validation-report.csv (for runtime/convergence) | (none) |
| `--readiness-json <FILE>` | Path to cutover-readiness.json (for convergence) | (none) |
| `--prometheus <URL>` | Prometheus Query API URL | `http://localhost:9090` |
| `--alertmanager <URL>` | Alertmanager API URL | `http://localhost:9093` |
| `--json` | JSON structured output (for CI) | false |

**Three-Phase Checks**

| Phase | Checks |
|-------|--------|
| `preflight` | Mapping file exists, recording rules loaded, AM interception route |
| `runtime` | Mismatch count, tenant coverage, three-state mode consistency |
| `convergence` | cutover-readiness assessment, 7-day zero-mismatch check |

**Examples**

```bash
da-tools shadow-verify preflight --mapping migration_output/prefix-mapping.yaml
da-tools shadow-verify runtime --report-csv validation_output/validation-report.csv
da-tools shadow-verify convergence --report-csv validation_output/validation-report.csv --readiness-json validation_output/cutover-readiness.json
da-tools shadow-verify all --mapping mapping.yaml --report-csv report.csv --json
```

**Exit Codes**

| Code | Description |
|------|-------------|
| `0` | All checks passed |
| `1` | One or more checks failed |

---

#### byo-check

Automated BYO Prometheus & Alertmanager integration verification (replaces manual curl + jq steps).

**Purpose**: Verify BYO environment tenant label injection, threshold-exporter scrape, Rule Pack loading, and Alertmanager routing configuration.

**Syntax**

```bash
da-tools byo-check <target> [options]
```

**Required Parameters**

| Parameter | Description | Values |
|-----------|-------------|--------|
| `<target>` | Verification target | `prometheus` / `alertmanager` / `all` |

**Options**

| Option | Description | Default |
|--------|-------------|---------|
| `--prometheus <URL>` | Prometheus Query API URL | `http://localhost:9090` |
| `--alertmanager <URL>` | Alertmanager API URL | `http://localhost:9093` |
| `--json` | JSON structured output (for CI) | false |

**Checks**

| Target | Checks |
|--------|--------|
| `prometheus` | Connection health, tenant label injection (Step 1), threshold-exporter scrape (Step 2), Rule Pack loading (Step 3), recording rules output, vector matching |
| `alertmanager` | Connection ready, tenant routing, inhibit_rules, active alerts, silences |

**Examples**

```bash
da-tools byo-check prometheus --prometheus http://prometheus:9090
da-tools byo-check alertmanager --alertmanager http://alertmanager:9093
da-tools byo-check all --json
```

**Exit Codes**

| Code | Description |
|------|-------------|
| `0` | All checks passed |
| `1` | One or more checks failed |

---

#### federation-check

Multi-cluster federation integration verification (automates federation-integration.md §6 manual steps).

**Purpose**: Verify edge cluster external_labels and federate endpoint, central cluster edge metrics reception and recording rules, end-to-end cross-cluster alert state.

**Syntax**

```bash
da-tools federation-check <target> [options]
```

**Required Parameters**

| Parameter | Description | Values |
|-----------|-------------|--------|
| `<target>` | Verification mode | `edge` / `central` / `e2e` |

**Options**

| Option | Description | Default |
|--------|-------------|---------|
| `--prometheus <URL>` | Prometheus URL (central for e2e, or target for edge/central) | `http://localhost:9090` |
| `--edge-urls <URLS>` | Comma-separated edge Prometheus URLs (required for e2e mode) | (none) |
| `--json` | JSON structured output (for CI) | false |

**Three-Mode Checks**

| Mode | Checks |
|------|--------|
| `edge` | Prometheus health, external_labels (with cluster label), tenant label, federate endpoint |
| `central` | Prometheus health, edge metrics reception, threshold-exporter, recording rules, alert rules |
| `e2e` | All edge checks + central checks + cross-cluster vector matching |

**Examples**

```bash
da-tools federation-check edge --prometheus http://edge-prometheus:9090
da-tools federation-check central --prometheus http://central-prometheus:9090
da-tools federation-check e2e --prometheus http://central:9090 --edge-urls http://edge-1:9090,http://edge-2:9090
da-tools federation-check central --prometheus http://central:9090 --json
```

**Exit Codes**

| Code | Description |
|------|-------------|
| `0` | All checks passed |
| `1` | One or more checks failed |

---

#### grafana-import

Grafana dashboard import tool (via ConfigMap sidecar auto-mount).

**Purpose**: Automates the full workflow of Grafana dashboard JSON → Kubernetes ConfigMap → sidecar discovery.

**Syntax**

```bash
da-tools grafana-import [options]
```

**Options**

| Option | Description | Default |
|--------|-------------|---------|
| `--dashboard <FILE>` | Dashboard JSON file path | (none) |
| `--dashboard-dir <DIR>` | Import all *.json files in directory | (none) |
| `--name <NAME>` | ConfigMap name (auto-generated if omitted) | (auto) |
| `--namespace <NS>` | Kubernetes namespace | `monitoring` |
| `--verify` | Verify existing dashboard ConfigMaps | false |
| `--dry-run` | Preview kubectl commands without executing | false |
| `--json` | JSON structured output | false |

**Modes**

| Mode | Description |
|------|-------------|
| Single import | `--dashboard <file>` imports one dashboard |
| Batch import | `--dashboard-dir <dir>` imports all JSON files in directory |
| Verify mode | `--verify` checks existing dashboard ConfigMaps |

**Examples**

```bash
da-tools grafana-import --dashboard k8s/03-monitoring/dynamic-alerting-overview.json --namespace monitoring
da-tools grafana-import --dashboard-dir k8s/03-monitoring/ --namespace monitoring
da-tools grafana-import --verify --namespace monitoring
da-tools grafana-import --dashboard overview.json --dry-run
```

**Exit Codes**

| Code | Description |
|------|-------------|
| `0` | Success |
| `1` | Import failed or verification found issues |

---

#### alert-quality

Analyze Alertmanager history to identify problem alerts. 4 quality metrics (Noise / Stale / Latency / Suppression), 3 grades (GOOD / WARN / BAD), per-tenant weighted scoring.

**Usage**

```bash
da-tools alert-quality --prometheus <URL> [--alertmanager <URL>] [--period <DURATION>] [--tenant <NAME>] [--json] [--markdown] [--ci] [--min-score <N>]
```

**Parameters**

| Parameter | Description | Default |
|-----------|-------------|---------|
| `--prometheus` | Prometheus URL (required) | - |
| `--alertmanager` | Alertmanager URL (for suppression data) | - |
| `--period` | Analysis period | `30d` |
| `--tenant` | Filter to specific tenant | all |
| `--json` | JSON output | - |
| `--markdown` | Markdown output | - |
| `--ci` | CI mode: exit 1 if any BAD alert | - |
| `--min-score` | CI minimum score threshold | `0` |

**Examples**

```bash
# Basic quality report
da-tools alert-quality --prometheus http://prometheus:9090

# Specific tenant, Markdown output
da-tools alert-quality --prometheus http://prometheus:9090 --tenant db-a --markdown

# CI gate (fail below score 60)
da-tools alert-quality --prometheus http://prometheus:9090 --ci --min-score 60
```

**Exit Codes**

| Code | Description |
|------|-------------|
| `0` | Success (CI mode: all alerts meet quality threshold) |
| `1` | CI mode: BAD alerts found or score below threshold |

---

#### alert-correlate

Analyze Alertmanager alerts using time-window clustering, compute correlation scores, and infer root causes. Supports both online (Prometheus API) and offline (JSON file) modes.

**Usage**

```bash
da-tools alert-correlate --prometheus <URL> [--window <MINUTES>] [--lookback <DURATION>] [--min-score <FLOAT>] [--json] [--markdown] [--ci]
da-tools alert-correlate --input <FILE> [--window <MINUTES>] [--min-score <FLOAT>] [--json]
```

**Arguments**

| Argument | Description | Default |
|----------|-------------|---------|
| `--prometheus <URL>` | Prometheus endpoint (online mode) | `$PROMETHEUS_URL` |
| `--input <FILE>` | Alertmanager JSON file (offline mode) | — |
| `--window <MINUTES>` | Time window size in minutes | `10` |
| `--lookback <DURATION>` | Lookback duration | `1h` |
| `--min-score <FLOAT>` | Minimum correlation score threshold | `0.3` |
| `--json` | JSON output | — |
| `--markdown` | Markdown report output | — |
| `--ci` | CI mode (exit 1 if critical clusters found) | — |

**Examples**

```bash
# Basic usage — query current Prometheus alerts
da-tools alert-correlate --prometheus http://prometheus:9090

# Offline analysis from JSON file
da-tools alert-correlate --input alerts.json --window 15

# CI gate — fail if critical alert clusters found
da-tools alert-correlate --prometheus http://prometheus:9090 --ci
```

**Exit Codes**

| Code | Description |
|------|-------------|
| `0` | Success (CI mode: no critical alert clusters) |
| `1` | CI mode: critical severity alert clusters found |

---

#### drift-detect

Compare multiple config-dir directories (from different clusters or GitOps branches) and detect unexpected configuration drift. Uses SHA-256 manifests for directory-level comparison.

**Usage**

```bash
da-tools drift-detect --dirs <DIR1>,<DIR2>[,<DIR3>...] [--labels <L1>,<L2>,...] [--ignore-prefix <PREFIX>] [--json] [--markdown] [--ci]
```

**Arguments**

| Argument | Description | Default |
|----------|-------------|---------|
| `--dirs <LIST>` | Comma-separated config directories (at least 2) | — |
| `--labels <LIST>` | Labels for each directory | `dir-1,dir-2,...` |
| `--ignore-prefix <PREFIX>` | File prefixes treated as expected drift | `_cluster_,_local_` |
| `--json` | JSON output | — |
| `--markdown` | Markdown report output | — |
| `--ci` | CI mode (exit 1 on unexpected drift) | — |

**Examples**

```bash
# Compare two cluster configs
da-tools drift-detect --dirs cluster-a/conf.d,cluster-b/conf.d --labels prod-a,prod-b

# Three-cluster pairwise comparison, JSON output
da-tools drift-detect --dirs a/conf.d,b/conf.d,c/conf.d --json

# CI gate — fail on unexpected drift
da-tools drift-detect --dirs staging/conf.d,prod/conf.d --ci
```

**Exit Codes**

| Code | Description |
|------|-------------|
| `0` | No unexpected drift |
| `1` | CI mode: unexpected drift detected |

---

#### cardinality-forecast

Analyze per-tenant time series cardinality growth and predict limit breach. Uses pure Python linear regression (no numpy dependency).

**Usage**

```bash
da-tools cardinality-forecast --prometheus <URL> [--lookback <DURATION>] [--limit <N>] [--warn-days <N>] [--tenant <NAME>] [--json] [--markdown] [--ci]
```

**Parameters**

| Parameter | Description | Default |
|-----------|-------------|---------|
| `--prometheus` | Prometheus URL (required) | - |
| `--lookback` | Lookback period | `30d` |
| `--limit` | Cardinality limit | `500` |
| `--warn-days` | Warning days before limit | `7` |
| `--tenant` | Filter to specific tenant | all |
| `--json` | JSON output | - |
| `--markdown` | Markdown output | - |
| `--ci` | CI mode: exit 1 if any critical risk found | - |

**Examples**

```bash
# Basic forecast report
da-tools cardinality-forecast --prometheus http://prometheus:9090

# Custom limit and warning days
da-tools cardinality-forecast --prometheus http://prometheus:9090 --limit 1000 --warn-days 14

# CI gate
da-tools cardinality-forecast --prometheus http://prometheus:9090 --ci
```

**Risk Levels**

| Level | Condition |
|-------|-----------|
| `critical` | Predicted to hit limit within `--warn-days` |
| `warning` | Growing trend but not yet reaching warning threshold |
| `safe` | Stable or declining trend |

#### config-history

Config snapshot & history tracking — records each change to conf.d/ in `.da-history/`, providing git-independent lightweight version control.

```bash
da-tools config-history --config-dir <PATH> <action>
```

**Subcommands**

| Subcommand | Purpose | Parameters |
|------------|---------|------------|
| `snapshot` | Create config snapshot | `-m <message>` (optional) |
| `log` | Show snapshot history | `--limit N` (optional) |
| `show` | Show snapshot details | `<id>` |
| `diff` | Compare two snapshots | `<id_a> <id_b>` |

**Examples**

```bash
# Create snapshot
da-tools config-history --config-dir conf.d/ snapshot -m "Adjust MariaDB thresholds"

# View history
da-tools config-history --config-dir conf.d/ log --limit 5

# Compare snapshots 1 and 2
da-tools config-history --config-dir conf.d/ diff 1 2
```

---

### Adoption & Initialization

#### init

Initialize Dynamic Alerting integration skeleton in a customer repo. Generates CI/CD pipelines, conf.d/ directory, Kustomize overlays, and pre-commit configuration.

```bash
da-tools init [--ci <github|gitlab|both>] [--tenants <list>] [--rule-packs <list>] [--deploy <kustomize|helm|argocd>] [-o <dir>] [--non-interactive] [--dry-run]
```

**Parameters**

| Parameter | Description | Default |
|-----------|-------------|---------|
| `--ci` | CI/CD platform | `both` |
| `--tenants` | Comma-separated tenant names | `db-a,db-b` (interactive mode) |
| `--rule-packs` | Comma-separated Rule Packs | `mariadb,kubernetes` (interactive mode) |
| `--deploy` | Deployment method | `kustomize` |
| `--non-interactive` | Skip interactive prompts (requires `--tenants`) | — |
| `--dry-run` | Show files that would be generated without writing | — |
| `--force` | Overwrite existing `.da-init.yaml` | — |

**Examples**

```bash
# Interactive mode
da-tools init

# Non-interactive mode
da-tools init --ci github --tenants prod-db,staging-db --rule-packs mariadb,redis,kubernetes --non-interactive

# Dry-run
da-tools init --ci both --tenants db-a --dry-run
```

#### gitops-check

GitOps Native Mode readiness validation — checks Git repo accessibility, local config structure, and git-sync sidecar deployment status.

```bash
da-tools gitops-check <subcommand> [options]
```

**Subcommands**

| Subcommand | Purpose | Parameters |
|------------|---------|------------|
| `repo` | Validate Git repo accessibility and branch existence | `--url <git-url> [--branch main]` |
| `local` | Validate local clone's conf.d/ structure | `--dir <path>` |
| `sidecar` | Check K8s git-sync sidecar deployment readiness | `[--namespace monitoring]` |

**Examples**

```bash
# Validate Git repo
da-tools gitops-check repo --url git@github.com:example/configs.git

# Validate local config structure
da-tools gitops-check local --dir /data/config/conf.d

# Check sidecar deployment
da-tools gitops-check sidecar --namespace monitoring --json
```

---

### Operator + Federation Tools

#### operator-generate

Generate Kubernetes Operator CRDs (PrometheusRule, AlertmanagerConfig, ServiceMonitor) from Rule Packs and Tenant configuration.

**Purpose**: Dynamic alert rule and routing deployment in Prometheus Operator clusters; multi-cluster config management for Federation scenarios.

**Syntax**

```bash
da-tools operator-generate --rule-packs-dir <dir> --config-dir <dir> [options]
```

**Required Parameters**

| Parameter | Description |
|-----------|-------------|
| `--rule-packs-dir <DIR>` | Rule Pack directory path |
| `--config-dir <DIR>` | Tenant configuration directory path |

**Options**

| Option | Description | Default |
|--------|-------------|---------|
| `--namespace <NS>` | Target K8s namespace | `monitoring` |
| `--output <FILE>` | Output to file | stdout |
| `--split` | Generate individual CRD files (split by Rule Pack) | false |
| `--include-servicemonitor` | Also generate ServiceMonitor CRD | false |
| `--dry-run` | Preview only | false |
| `--apply` | Apply directly to Kubernetes | false |

**Examples**

```bash
# Output CRD YAML to file
da-tools operator-generate --rule-packs-dir rule-packs/ --config-dir conf.d/ -o crds.yaml

# Split output and apply directly
da-tools operator-generate --rule-packs-dir rule-packs/ --config-dir conf.d/ --split --apply --namespace monitoring
```

---

#### operator-check

Verify Operator CRD deployment status in a Prometheus Operator cluster, checking 5 indicators and generating a diagnostic report.

**Purpose**: Operator integration health check; deployment integrity verification; fault diagnosis.

**Syntax**

```bash
da-tools operator-check [options]
```

**Options**

| Option | Description | Default |
|--------|-------------|---------|
| `--namespace <NS>` | K8s namespace to explore | `monitoring` (auto-discover) |
| `--json` | JSON format output | false |

**Checks Performed**

1. PrometheusRule deployed
2. AlertmanagerConfig deployed
3. ServiceMonitor bound
4. Prometheus scraping
5. Alerts firing correctly

**Examples**

```bash
# Check monitoring namespace
da-tools operator-check --namespace monitoring

# JSON output (for CI gate)
da-tools operator-check --json
```

---

#### rule-pack-split

Split Rule Packs into edge (Part 1) and central (Parts 2+3) layers for Federation Scenario B.

**Purpose**: Multi-cluster Federation scenarios; separate edge and central deployment.

**Syntax**

```bash
da-tools rule-pack-split --rule-packs-dir <dir> [options]
```

**Required Parameters**

| Parameter | Description |
|-----------|-------------|
| `--rule-packs-dir <DIR>` | Rule Pack directory path |

**Options**

| Option | Description | Default |
|--------|-------------|---------|
| `--output-dir <DIR>` | Output directory | `./split-output` |
| `--scenario` | Federation scenario (A / B) | `B` |

**Output Structure**

```
split-output/
├── edge/           (Part 1 - edge)
│   └── part-1-*.yaml
├── central/        (Parts 2+3 - central)
│   ├── part-2-*.yaml
│   └── part-3-*.yaml
└── mapping.json    (edge → central mapping)
```

**Examples**

```bash
# Scenario B hierarchical split
da-tools rule-pack-split --rule-packs-dir rule-packs/ --scenario B --output-dir federation-split/
```

---

### Configuration Generation Tools

#### generate-routes

Generate Alertmanager route + receiver + inhibit_rules fragment (or complete ConfigMap) from tenant YAML.

**Purpose**: GitOps configuration management; auto-generate alert routing and notification receivers.

**Syntax**

```bash
docker run --rm \
  -v <config_dir>:/etc/config:ro \
  [-v <output>:/data/output] \
  [-v <base_config>:/data/base.yaml:ro] \
  ghcr.io/vencil/da-tools:v2.3.0 \
  generate-routes --config-dir <path> [options]
```

**Required Parameters**

| Parameter | Description |
|-----------|-------------|
| `--config-dir <PATH>` | Tenant configuration directory |

**Options**

| Option | Description | Default |
|--------|-------------|---------|
| `--output <FILE>` | Output to file | stdout |
| `--output-configmap` | Output complete Kubernetes ConfigMap YAML | false |
| `--base-config <FILE>` | Custom Alertmanager base config (for --output-configmap) | built-in default |
| `--dry-run` | Show preview without writing | false |
| `--validate` | Validate only, don't output | false |
| `--apply` | Apply directly to Kubernetes (requires kubectl) | false |
| `--yes` | Skip confirmation prompt with --apply | false |
| `--policy <DOMAINS>` | Webhook domain allowlist (comma-separated; empty=unrestricted) | (unrestricted) |

**Output**

**Fragment Mode** (no `--output-configmap`):
YAML fragment containing route, receivers, inhibit_rules.

**ConfigMap Mode** (`--output-configmap`):
Complete Kubernetes ConfigMap YAML with global, route, receivers, inhibit_rules, ready for `kubectl apply`.

**Examples**

```bash
# Generate fragment (preview)
docker run --rm \
  -v $(pwd)/conf.d:/etc/config:ro \
  ghcr.io/vencil/da-tools:v2.3.0 \
  generate-routes --config-dir /etc/config --dry-run

# Generate fragment to file
docker run --rm \
  -v $(pwd)/conf.d:/etc/config:ro \
  -v $(pwd)/output:/data/output \
  ghcr.io/vencil/da-tools:v2.3.0 \
  generate-routes --config-dir /etc/config \
    -o /data/output/alertmanager-routes.yaml

# Generate complete ConfigMap
docker run --rm \
  -v $(pwd)/conf.d:/etc/config:ro \
  -v $(pwd)/output:/data/output \
  ghcr.io/vencil/da-tools:v2.3.0 \
  generate-routes --config-dir /etc/config --output-configmap \
    -o /data/output/alertmanager-configmap.yaml

# With custom base config
docker run --rm \
  -v $(pwd)/conf.d:/etc/config:ro \
  -v $(pwd)/base-alertmanager.yaml:/data/base.yaml:ro \
  -v $(pwd)/output:/data/output \
  ghcr.io/vencil/da-tools:v2.3.0 \
  generate-routes --config-dir /etc/config --output-configmap \
    --base-config /data/base.yaml \
    -o /data/output/alertmanager-configmap.yaml

# Direct kubectl apply
docker run --rm --kubeconfig=$HOME/.kube/config \
  -v $(pwd)/conf.d:/etc/config:ro \
  ghcr.io/vencil/da-tools:v2.3.0 \
  generate-routes --config-dir /etc/config --apply --yes
```

**Exit Codes**

| Code | Description |
|------|-------------|
| `0` | Success |
| `1` | Config validation failed |
| `2` | kubectl operation failed (--apply mode) |

---

#### patch-config

ConfigMap partial update tool with preview (--diff) and direct application support.

**Purpose**: Quick threshold adjustment during operations; avoid full ConfigMap redeployment.

**Syntax**

```bash
docker run --rm \
  [-v <config_dir>:/etc/config:ro] \
  ghcr.io/vencil/da-tools:v2.3.0 \
  patch-config [<tenant> <metric> <value> | --diff] [options]
```

**Required Parameters**

Choose one mode:

1. **Update Mode**: `<tenant> <metric> <value>`
2. **Preview Mode**: `--diff`

**Options**

| Option | Description | Default |
|--------|-------------|---------|
| `--namespace <NS>` | K8s namespace | `monitoring` |
| `--configmap <CM>` | ConfigMap name | `threshold-config` |
| `--dry-run` | Show changes without applying | false |
| `--yes` | Skip confirmation prompt | false |

**Output**

Preview or confirmation message.

**Examples**

```bash
# Preview current ConfigMap and changes
docker run --rm \
  -v $(pwd)/conf.d:/etc/config:ro \
  ghcr.io/vencil/da-tools:v2.3.0 \
  patch-config --diff

# Update single metric
docker run --rm \
  ghcr.io/vencil/da-tools:v2.3.0 \
  patch-config db-a mysql_connections 100 --dry-run

# Apply update
docker run --rm \
  ghcr.io/vencil/da-tools:v2.3.0 \
  patch-config db-a mysql_connections 100 --yes
```

**Exit Codes**

| Code | Description |
|------|-------------|
| `0` | Success |
| `1` | Invalid ConfigMap or parameters |

---

### Filesystem Tools

#### scaffold

Generate new tenant configuration (interactive or non-interactive).

**Purpose**: Quickly create tenant configs; supports multiple DB types and defaults.

**Syntax**

```bash
docker run --rm -it \
  -v <output_dir>:/data/output \
  ghcr.io/vencil/da-tools:v2.3.0 \
  scaffold [options]
```

**Options**

| Option | Description | Default |
|--------|-------------|---------|
| `--non-interactive` | Non-interactive mode (requires --tenant, etc.) | false |
| `--tenant <NAME>` | Tenant ID | (interactive prompt) |
| `--db <LIST>` | Comma-separated DB type list | (interactive prompt) |
| `--namespaces <LIST>` | Comma-separated K8s namespace list | (interactive prompt) |
| `--output <DIR>` | Output directory | `./` |

**Supported DB Types**

- `mariadb` / `mysql`
- `postgresql`
- `redis`
- `mongodb`
- `elasticsearch`
- `kubernetes`
- `jvm`
- `nginx`

**Output**

- `<tenant>.yaml` — Tenant configuration file
- `_defaults.yaml` — Platform defaults (on first creation)
- `scaffold-report.txt` — Summary report

**Examples**

```bash
# Interactive generation
docker run --rm -it \
  -v $(pwd)/output:/data/output \
  ghcr.io/vencil/da-tools:v2.3.0 \
  scaffold --output /data/output

# Non-interactive generation (CI/CD)
docker run --rm \
  -v $(pwd)/output:/data/output \
  ghcr.io/vencil/da-tools:v2.3.0 \
  scaffold --non-interactive \
    --tenant db-c \
    --db mariadb,redis \
    --namespaces ns-db-c \
    --output /data/output
```

**Exit Codes**

| Code | Description |
|------|-------------|
| `0` | Success |
| `1` | Invalid input or I/O failed |

---

#### migrate

Convert legacy Prometheus rules to dynamic format (AST engine).

**Purpose**: Large-scale rule migration; automate early preparation work.

**Syntax**

```bash
docker run --rm \
  -v <input_file>:/data/input.yml:ro \
  [-v <output_dir>:/data/output] \
  ghcr.io/vencil/da-tools:v2.3.0 \
  migrate <input_file> [options]
```

**Required Parameters**

| Parameter | Description |
|-----------|-------------|
| `<input_file>` | Input legacy rules YAML file |

**Options**

| Option | Description | Default |
|--------|-------------|---------|
| `--output <DIR>` | Output directory | `./migration_output/` |
| `--dry-run` | Show report only, don't generate files | false |
| `--triage` | Triage mode: output only CSV report | false |
| `--interactive` | Ask user when uncertain | false |
| `--no-prefix` | Disable custom_ prefix (not recommended) | false |
| `--no-ast` | Force old regex engine | false |

**Output**

**Standard Mode**:

- `migration_output/tenant-config.yaml` — Extracted thresholds
- `migration_output/platform-recording-rules.yaml` — Recording rules
- `migration_output/platform-alert-rules.yaml` — Alert rules
- `migration_output/migration-report.txt` — Detailed report
- `migration_output/triage-report.csv` — Rules requiring manual review
- `migration_output/prefix-mapping.yaml` — Metric prefix mapping

**Triage Mode**:

- Output only `triage-report.csv` (for manual review)

**Examples**

```bash
# Preview report (dry run)
docker run --rm \
  -v $(pwd)/my-rules.yml:/data/input.yml:ro \
  ghcr.io/vencil/da-tools:v2.3.0 \
  migrate /data/input.yml --dry-run

# Convert and output triage report
docker run --rm \
  -v $(pwd)/my-rules.yml:/data/input.yml:ro \
  -v $(pwd)/output:/data/output \
  ghcr.io/vencil/da-tools:v2.3.0 \
  migrate /data/input.yml --triage -o /data/output

# Complete conversion (with manual review)
docker run --rm \
  -v $(pwd)/my-rules.yml:/data/input.yml:ro \
  -v $(pwd)/output:/data/output \
  ghcr.io/vencil/da-tools:v2.3.0 \
  migrate /data/input.yml -o /data/output
```

**Exit Codes**

| Code | Description |
|------|-------------|
| `0` | Success |
| `1` | Invalid input file or I/O failed |

---

#### validate-config

One-stop configuration validation: YAML format, schema, routing, policy, version consistency.

**Purpose**: CI/CD gate check; verify config completeness before deployment.

**Syntax**

```bash
docker run --rm \
  -v <config_dir>:/etc/config:ro \
  ghcr.io/vencil/da-tools:v2.3.0 \
  validate-config --config-dir <path> [options]
```

**Required Parameters**

| Parameter | Description |
|-----------|-------------|
| `--config-dir <PATH>` | Tenant configuration directory |

**Options**

| Option | Description | Default |
|--------|-------------|---------|
| `--policy <DOMAINS>` | Webhook domain allowlist | (unrestricted) |
| `--ci` | CI mode (exit code for CI/CD) | false |

**Checks Performed**

- YAML file format (parseable)
- Schema validation (required keys, correct types)
- Routing rule validation (group_wait/group_interval/repeat_interval in allowed range)
- Policy checks (webhook domains)
- Tenant name consistency

**Output**

Validation result summary (pass/fail list).

**Examples**

```bash
# Basic validation
docker run --rm \
  -v $(pwd)/conf.d:/etc/config:ro \
  ghcr.io/vencil/da-tools:v2.3.0 \
  validate-config --config-dir /etc/config

# CI mode (strict exit code)
docker run --rm \
  -v $(pwd)/conf.d:/etc/config:ro \
  ghcr.io/vencil/da-tools:v2.3.0 \
  validate-config --config-dir /etc/config --ci

# Check webhook domain allowlist
docker run --rm \
  -v $(pwd)/conf.d:/etc/config:ro \
  ghcr.io/vencil/da-tools:v2.3.0 \
  validate-config --config-dir /etc/config \
    --policy "webhook.company.com,slack.com"
```

**Exit Codes**

| Code | Description |
|------|-------------|
| `0` | All validations pass |
| `1` | Validation failed (one or more checks) |

---

#### offboard

Offboard tenant configuration and related resources.

**Purpose**: Cleanup when tenant lifecycle ends.

**Syntax**

```bash
docker run --rm \
  -v <config_dir>:/etc/config:rw \
  [-v <output>:/data/output] \
  ghcr.io/vencil/da-tools:v2.3.0 \
  offboard <tenant> [options]
```

**Required Parameters**

| Parameter | Description |
|-----------|-------------|
| `<tenant>` | Tenant ID |

**Options**

| Option | Description | Default |
|--------|-------------|---------|
| `--config-dir <PATH>` | Tenant config directory | `./conf.d` |
| `--backup <DIR>` | Backup directory | `./offboarded/` |
| `--cleanup-rules` | Remove associated Alert rules | false |
| `--dry-run` | Preview items to delete | false |

**Output**

Backup tenant config; optionally remove associated Recording/Alert rules.

**Examples**

```bash
# Preview offboard actions
docker run --rm \
  -v $(pwd)/conf.d:/etc/config:ro \
  ghcr.io/vencil/da-tools:v2.3.0 \
  offboard db-old --dry-run

# Execute offboard with backup
docker run --rm \
  -v $(pwd)/conf.d:/etc/config:rw \
  -v $(pwd)/backup:/data/backup \
  ghcr.io/vencil/da-tools:v2.3.0 \
  offboard db-old --backup /data/backup
```

**Exit Codes**

| Code | Description |
|------|-------------|
| `0` | Success |
| `1` | Tenant not found or I/O failed |

---

#### deprecate

Mark metrics as disabled to prevent accidental use.

**Purpose**: Gradually retire old metrics; maintain version compatibility.

**Syntax**

```bash
docker run --rm \
  -v <config_dir>:/etc/config:rw \
  ghcr.io/vencil/da-tools:v2.3.0 \
  deprecate <metric_keys...> [options]
```

**Required Parameters**

| Parameter | Description |
|-----------|-------------|
| `<metric_keys...>` | One or more metric keys (space-separated) |

**Options**

| Option | Description | Default |
|--------|-------------|---------|
| `--config-dir <PATH>` | Tenant config directory | `./conf.d` |
| `--reason <TEXT>` | Deprecation reason (annotation) | (none) |
| `--dry-run` | Preview changes | false |

**Output**

Add or update metric key with `enabled: false` flag in _defaults.yaml.

**Examples**

```bash
# Mark multiple metrics as disabled
docker run --rm \
  -v $(pwd)/conf.d:/etc/config:rw \
  ghcr.io/vencil/da-tools:v2.3.0 \
  deprecate old_metric_1 old_metric_2 \
    --reason "Replaced by new_metric; migration complete"
```

**Exit Codes**

| Code | Description |
|------|-------------|
| `0` | Success |
| `1` | Invalid config directory |

---

#### lint

Check Custom Rule governance compliance (based on `custom_` prefix rules).

**Purpose**: CI/CD lint check; ensure custom rules follow naming conventions.

**Syntax**

```bash
docker run --rm \
  -v <rules_dir>:/data/rules:ro \
  ghcr.io/vencil/da-tools:v2.3.0 \
  lint <path...> [options]
```

**Required Parameters**

| Parameter | Description |
|-----------|-------------|
| `<path...>` | One or more file or directory paths |

**Options**

| Option | Description | Default |
|--------|-------------|---------|
| `--strict` | Strict mode: elevate warnings to errors | false |
| `--json-output` | Structured JSON output | false |

**Checks Performed**

- Metric names start with `custom_` prefix
- Recording rule name format
- Label usage consistency

**Examples**

```bash
# Check single file
docker run --rm \
  -v $(pwd)/my-custom-rules.yaml:/data/rules/my-rules.yaml:ro \
  ghcr.io/vencil/da-tools:v2.3.0 \
  lint /data/rules/my-rules.yaml

# Check entire directory
docker run --rm \
  -v $(pwd)/rule-packs:/data/rules:ro \
  ghcr.io/vencil/da-tools:v2.3.0 \
  lint /data/rules --strict
```

**Exit Codes**

| Code | Description |
|------|-------------|
| `0` | All pass |
| `1` | Violations found (warning level) |
| `2` | Violations found (error level; --strict mode) |

---

#### onboard

Analyze existing Alertmanager or Prometheus config, output migration hints.

**Purpose**: Incorporate existing monitoring configs; reduce manual migration work.

**Syntax**

```bash
docker run --rm \
  -v <config_file>:/data/config.yml:ro \
  [-v <output>:/data/output] \
  ghcr.io/vencil/da-tools:v2.3.0 \
  onboard <config_file> [options]
```

**Required Parameters**

| Parameter | Description |
|-----------|-------------|
| `<config_file>` | Alertmanager or Prometheus config file |

**Options**

| Option | Description | Default |
|--------|-------------|---------|
| `--alertmanager-config <FILE>` | Alertmanager config file (alternate location) | (positional) |
| `--output <FILE>` | Output hints JSON | stdout |

**Output**

JSON format migration hints (`onboard-hints.json`), including:
- Detected receiver types and endpoints
- Recommended tenant groupings
- Initial threshold suggestions

**Examples**

```bash
# Analyze Alertmanager config
docker run --rm \
  -v $(pwd)/alertmanager.yaml:/data/config.yml:ro \
  -v $(pwd)/output:/data/output \
  ghcr.io/vencil/da-tools:v2.3.0 \
  onboard /data/config.yml -o /data/output/onboard-hints.json
```

**Exit Codes**

| Code | Description |
|------|-------------|
| `0` | Success |
| `1` | Invalid config file |

---

#### analyze-gaps

Compare custom rules with Rule Pack, find duplicates/gaps.

**Purpose**: Evaluate Rule Pack coverage; decide if custom rule can be deleted.

**Syntax**

```bash
docker run --rm \
  -v <config_dir>:/etc/config:ro \
  ghcr.io/vencil/da-tools:v2.3.0 \
  analyze-gaps --config <path> [options]
```

**Required Parameters**

| Parameter | Description |
|-----------|-------------|
| `--config <PATH>` | Tenant config file or directory |

**Options**

| Option | Description | Default |
|--------|-------------|---------|
| `--output <FILE>` | Output to CSV or JSON | stdout |
| `--json-output` | JSON format | false |

**Output**

CSV list where each row represents a custom rule and its Rule Pack coverage relationship.

**Examples**

```bash
# Analyze coverage gaps
docker run --rm \
  -v $(pwd)/conf.d:/etc/config:ro \
  ghcr.io/vencil/da-tools:v2.3.0 \
  analyze-gaps --config /etc/config/db-a.yaml
```

**Exit Codes**

| Code | Description |
|------|-------------|
| `0` | Success |
| `1` | Invalid config file |

---

#### config-diff

Compare two config directories (conf.d), output blast radius report.

**Purpose**: GitOps PR review; quickly assess config change impact scope.

**Syntax**

```bash
docker run --rm \
  -v <old_dir>:/data/old:ro \
  -v <new_dir>:/data/new:ro \
  ghcr.io/vencil/da-tools:v2.3.0 \
  config-diff --old-dir <path> --new-dir <path> [options]
```

**Required Parameters**

| Parameter | Description |
|-----------|-------------|
| `--old-dir <PATH>` | Old config directory |
| `--new-dir <PATH>` | New config directory |

**Options**

| Option | Description | Default |
|--------|-------------|---------|
| `--json-output` | Structured JSON output | false |
| `--summary-only` | Only output summary, not detailed changes | false |

**Change Classifications**

| Classification | Meaning | Impact |
|---|---|---|
| `tighter` | Threshold decreased | May increase alerts |
| `looser` | Threshold increased | May decrease alerts |
| `added` | New metric key | New alert coverage |
| `removed` | Metric key removed | Lost alert coverage |
| `toggled` | enable ↔ disable | Enable or disable alert |
| `modified` | Complex value change | Manual review needed |

**Output**

Markdown format report with per-tenant change tables and summary statistics.

**Examples**

```bash
# Compare two directories
docker run --rm \
  -v $(pwd)/conf.d-old:/data/old:ro \
  -v $(pwd)/conf.d-new:/data/new:ro \
  ghcr.io/vencil/da-tools:v2.3.0 \
  config-diff --old-dir /data/old --new-dir /data/new

# JSON output (for CI consumption)
docker run --rm \
  -v $(pwd)/conf.d-old:/data/old:ro \
  -v $(pwd)/conf.d-new:/data/new:ro \
  ghcr.io/vencil/da-tools:v2.3.0 \
  config-diff --old-dir /data/old --new-dir /data/new --json-output
```

**Exit Codes**

| Code | Description |
|------|-------------|
| `0` | Success |
| `1` | Invalid directory |

---

#### evaluate-policy

Declarative policy engine — evaluate tenant configuration compliance using built-in DSL, zero external dependencies.

**Usage**

```bash
da-tools evaluate-policy --config-dir <PATH> [--policy <FILE>] [--json] [--ci]
```

**Parameters**

| Parameter | Description | Default |
|-----------|-------------|---------|
| `--config-dir` | Path to conf.d/ directory (required) | - |
| `--policy` | Path to standalone policy file (top-level `policies:` key) | `_policies` in `_defaults.yaml` |
| `--json` | JSON output | - |
| `--ci` | CI mode: exit 1 if any error-level violations found | - |

**Supported Operators**

`required`, `forbidden`, `equals`, `not_equals`, `gte`, `lte`, `gt`, `lt`, `matches`, `one_of`, `contains`

**Examples**

```bash
# Evaluate default policies
da-tools evaluate-policy --config-dir conf.d/

# Use standalone policy file
da-tools evaluate-policy --config-dir conf.d/ --policy policies/production.yaml

# CI gate
da-tools evaluate-policy --config-dir conf.d/ --ci
```

**Exit Codes**

| Code | Description |
|------|-------------|
| `0` | No error-level violations |
| `1` | CI mode: error-level violations found |

#### opa-evaluate

OPA (Open Policy Agent) policy evaluation bridge — converts tenant configs to OPA input JSON, evaluates via OPA REST API or local binary, and returns results compatible with evaluate-policy format.

**Usage**

```bash
da-tools opa-evaluate --config-dir <PATH> [options]
```

**Parameters**

| Parameter | Description | Default |
|-----------|-------------|---------|
| `--config-dir` | Path to conf.d/ directory (required) | - |
| `--opa-url` | OPA REST API endpoint | - |
| `--opa-binary` | Local OPA binary path | `opa` |
| `--policy-path` | Path to .rego policy file(s) | - |
| `--dry-run` | Show input JSON without calling OPA | - |
| `--json` | JSON format output | - |

**Examples**

```bash
# Evaluate via OPA REST API
da-tools opa-evaluate --config-dir conf.d/ --opa-url http://localhost:8181

# Use local OPA binary
da-tools opa-evaluate --config-dir conf.d/ --opa-binary /usr/local/bin/opa --policy-path policies/

# Dry-run: show OPA input JSON only
da-tools opa-evaluate --config-dir conf.d/ --dry-run
```

---

#### threshold-recommend

Threshold recommendation engine — recommends optimal thresholds based on historical Prometheus P50/P95/P99 percentiles, with Noise Score integration for direction adjustment.

**Usage**

```bash
da-tools threshold-recommend --config-dir <PATH> [--prometheus <URL>] [--tenant <NAME>] [--lookback <DURATION>] [--min-samples <N>] [--dry-run] [--json] [--markdown]
```

**Parameters**

| Parameter | Description | Default |
|-----------|-------------|---------|
| `--config-dir` | Path to conf.d/ directory (required) | - |
| `--prometheus` | Prometheus Query API URL | `$PROMETHEUS_URL` or `http://localhost:9090` |
| `--tenant` | Analyze only this tenant (omit for all) | all |
| `--lookback` | Historical data lookback period | `7d` |
| `--min-samples` | Minimum sample count threshold (below = LOW confidence) | `100` |
| `--dry-run` | Show PromQL queries without executing | - |
| `--json` | JSON output | - |
| `--markdown` | Markdown table output | - |

**Confidence Levels**

| Level | Sample Count |
|-------|-------------|
| HIGH | ≥ 1000 |
| MEDIUM | ≥ 100 (or `--min-samples`) |
| LOW | < 100 |

**Examples**

```bash
# Recommend thresholds for all tenants
da-tools threshold-recommend --config-dir conf.d/ --prometheus http://prometheus:9090

# Specific tenant, 14-day lookback
da-tools threshold-recommend --config-dir conf.d/ --prometheus http://prometheus:9090 --tenant db-a --lookback 14d

# Dry-run: show PromQL only
da-tools threshold-recommend --config-dir conf.d/ --dry-run
```

#### test-notification

Multi-channel notification connectivity testing — verify reachability of all configured receivers and report status.

**Usage**

```bash
da-tools test-notification --config-dir <PATH> [--tenant <NAME>] [--dry-run] [--json] [--ci] [--timeout <SEC>] [--rate-limit <SEC>]
```

**Parameters**

| Parameter | Description | Default |
|-----------|-------------|---------|
| `--config-dir` | Path to conf.d/ directory (required) | - |
| `--tenant` | Test only this tenant (omit to test all) | all |
| `--dry-run` | Validate URL format only, do not send | - |
| `--json` | JSON output | - |
| `--ci` | CI mode: exit 1 if any receiver fails | - |
| `--timeout` | Connection timeout per receiver in seconds | `10` |
| `--rate-limit` | Seconds to wait between each test | `0.5` |

**Supported Receiver Types**

`webhook`, `slack`, `teams`, `pagerduty`, `rocketchat`, `email` (SMTP connectivity check)

**Examples**

```bash
# Test all tenant receivers
da-tools test-notification --config-dir conf.d/

# Test a specific tenant
da-tools test-notification --config-dir conf.d/ --tenant db-a

# Dry-run (URL validation only)
da-tools test-notification --config-dir conf.d/ --dry-run

# CI gate
da-tools test-notification --config-dir conf.d/ --ci
```

**Exit Codes**

| Code | Description |
|------|-------------|
| `0` | All receivers reachable (or non-CI mode) |
| `1` | CI mode: one or more receivers unreachable |

#### explain-route

Routing merge pipeline debugger — shows the four-layer routing merge expansion per tenant (ADR-007): `_routing_defaults` → `routing_profiles` → tenant `_routing` → `_routing_enforced`.

**Usage**

```bash
da-tools explain-route --config-dir <PATH> [--tenant <NAME>...] [--show-profile-expansion] [--json]
```

**Parameters**

| Parameter | Description | Default |
|-----------|-------------|---------|
| `--config-dir` | Config directory path | (required) |
| `--tenant` | Show only specified tenant(s) (repeatable) | (all) |
| `--show-profile-expansion` | Show all routing profile expansions and references | `false` |
| `--json` | Output in JSON format | `false` |

**Examples**

```bash
# Show routing merge expansion for all tenants
da-tools explain-route --config-dir conf.d/

# Show only specific tenant
da-tools explain-route --config-dir conf.d/ --tenant db-a

# Show profile reference map (which profiles are used by whom)
da-tools explain-route --config-dir conf.d/ --show-profile-expansion

# JSON output (for pipeline integration)
da-tools explain-route --config-dir conf.d/ --json
```

---

#### discover-mappings

Auto-discover 1:N instance-tenant mappings — scrapes exporter `/metrics` endpoints or queries the Prometheus API, parses partition label candidates (schema, tablespace, datname, etc.), ranks by suitability, and generates an `_instance_mapping.yaml` draft (ADR-006).

**Usage**

```bash
da-tools discover-mappings --endpoint <URL> [-o <FILE>] [--json]
da-tools discover-mappings --prometheus <URL> --instance <INST> [--job <JOB>] [-o <FILE>] [--json]
```

**Parameters**

| Parameter | Description | Default |
|-----------|-------------|---------|
| `--endpoint` | Exporter /metrics URL to scrape directly | (mutually exclusive with --prometheus) |
| `--prometheus` | Prometheus API URL | (mutually exclusive with --endpoint) |
| `--instance` | Instance label in Prometheus | (used with --prometheus) |
| `--job` | Job label in Prometheus (narrows query) | (optional) |
| `-o`, `--output` | Output file path (defaults to stdout) | stdout |
| `--json` | Output in JSON format | `false` |

**Examples**

```bash
# Direct exporter scrape
da-tools discover-mappings --endpoint http://mariadb-exporter:9104/metrics

# Query via Prometheus API
da-tools discover-mappings --prometheus http://prometheus:9090 --instance mariadb-exporter:9104

# Output to file
da-tools discover-mappings --endpoint http://mariadb-exporter:9104/metrics -o mapping-draft.yaml

# JSON output
da-tools discover-mappings --endpoint http://mariadb-exporter:9104/metrics --json
```

**Exit Codes**

| Code | Description |
|------|-------------|
| `0` | Successfully discovered partition labels and generated mapping draft |
| `1` | Connection failed or no suitable partition labels found |

---

## Environment Variables

| Variable | Purpose | Default | Description |
|----------|---------|---------|-------------|
| `PROMETHEUS_URL` | Prometheus endpoint URL | `http://localhost:9090` | Fallback for `--prometheus`; localhost inside container refers to the container itself, use correct network config |

--8<-- "docs/includes/prometheus-url-config.en.md"

---

## Docker Quick Reference

### As Kubernetes Job

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: da-tools-check
  namespace: monitoring
spec:
  template:
    spec:
      containers:
        - name: da-tools
          image: ghcr.io/vencil/da-tools:v2.3.0
          env:
            - name: PROMETHEUS_URL
              value: "http://prometheus.monitoring.svc.cluster.local:9090"
          args: ["check-alert", "MariaDBHighConnections", "db-a"]
      restartPolicy: Never
  backoffLimit: 0
```

---

## FAQ

### Q: How to use da-tools in CI/CD?

**A**: Use `--ci` flag; exit code 0 = success, non-zero = fail. See each command's `--help`.

### Q: How to specify multiple metrics for validate?

**A**: Use `--mapping` CSV file (format: `old_rule,new_rule`). See [validate](#validate) command documentation.

### Q: What's the difference between blind-spot and analyze-gaps?

**A**:
- **blind-spot**: Cross-reference "cluster infrastructure vs tenant config", find exporters without tenant config (blind spots).
- **analyze-gaps**: Cross-reference "custom rules vs Rule Pack", evaluate Rule Pack coverage.

They're complementary; run both after migration.

### Q: How to safely execute cutover?

**A**:
1. Run `validate --auto-detect-convergence` to confirm convergence
2. Run `cutover --dry-run` to preview steps
3. Run `cutover` to execute cutover
4. Run `diagnose` + `batch-diagnose` to verify health

---

## Version Compatibility

| da-tools Version | Platform Version | Notes |
|---|---|---|
| v1.13.0 | v1.13.0 | DX Automation tools (shadow-verify + byo-check + federation-check + grafana-import) |
| v1.12.0 | v1.12.0 | Rule Pack expansion (JVM + Nginx) |
| v1.11.0 | v1.11.0 | Cutover + Blind-spot + Config-diff + Maintenance-scheduler |
| v1.10.0 | v1.10.0 | Generate-routes --output-configmap |

---

## Further Resources

| Document | Content |
|----------|---------|
| [getting-started/for-platform-engineers.en.md](getting-started/for-platform-engineers.en.md) | Platform Engineer Quick Start |
| [migration-guide.en.md](migration-guide.en.md) | Migration Steps Explained |
| [troubleshooting.en.md](troubleshooting.en.md) | Troubleshooting |
| [architecture-and-design.en.md](architecture-and-design.en.md) | Architecture & Design Principles |

## Related Resources

| Resource | Relevance |
|----------|-----------|
| ["da-tools CLI Reference"](./cli-reference.md) | ⭐⭐⭐ |
| ["Threshold Exporter API Reference"](api/README.en.md) | ⭐⭐⭐ |
| ["da-tools Quick Reference"] | ⭐⭐⭐ |
| ["Grafana Dashboard Guide"] | ⭐⭐ |
| ["Troubleshooting and Edge Cases"] | ⭐⭐ |
| ["Performance Analysis & Benchmarks"] | ⭐⭐ |
| ["BYO Alertmanager Integration Guide"] | ⭐⭐ |
| ["Bring Your Own Prometheus (BYOP) — Existing Monitoring Infrastructure Integration Guide"] | ⭐⭐ |
