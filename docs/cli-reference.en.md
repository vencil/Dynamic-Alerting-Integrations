---
title: "da-tools CLI Reference"
tags: [cli, reference, da-tools, tools]
audience: [platform-engineer, sre, devops, tenant]
version: v2.9.0
lang: en
---

# da-tools CLI Reference

> **Language / 語言：** **English (Current)** | [中文](./cli-reference.md)
>
> **Audience**: Platform Engineers, SREs, DevOps, Tenants
> **Container Image**: `ghcr.io/vencil/da-tools:v2.9.0`
> **Version**: (synced with platform version)

da-tools is a portable CLI container that bundles validation, migration, configuration, and operational tools for the Dynamic Alerting platform. This document is a complete reference for all subcommands.

---

## Quick jump by your role

This reference covers tools for several roles — **you don't need to read it end to end.** Find your role and go straight to the command subset you use most.

| Your role | Commands you use most | Jump to |
|---|---|---|
| **Platform Engineer**<br>Deploy & operate the platform | `init` · `gitops-check` · `operator-generate` · `operator-check` · `validate-config` · `byo-check` · `federation-check` | [Command Categories](#command-categories) |
| **SRE / On-call**<br>Day-to-day diagnosis & alerting | `diagnose` · `batch-diagnose` · `check-alert` · `alert-quality` · `alert-correlate` · `drift-detect` · `cardinality-forecast` | [Prometheus API Tools](#prometheus-api-tools) |
| **DevOps / GitOps**<br>CI integration & drift defense | `validate-config` · `config-diff` · `rule-pack-diff` · `backtest` · `config-history` · `drift-detect` | [Filesystem Tools](#filesystem-tools) |
| **Tenant**<br>Self-service threshold management | `scaffold` · `patch-config` · `validate-config` · `tenant-verify` · `explain-route` · `threshold-recommend` | [Configuration Generation Tools](#configuration-generation-tools) |
| **Domain Expert**<br>Rule quality governance | `lint` · `analyze-gaps` · `alert-quality` · `evaluate-policy` · `opa-evaluate` · `migrate` · `parser` | [Filesystem Tools](#filesystem-tools) |

> Most commands support `--help` and `--json` (for CI gates). See [Command Reference](#command-reference) below for full parameters.

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
docker pull ghcr.io/vencil/da-tools:v2.9.0

# Local build (development)
cd components/da-tools/app && ./build.sh v1.11.0
```

### View Help

```bash
docker run --rm ghcr.io/vencil/da-tools:v2.9.0 --help
docker run --rm ghcr.io/vencil/da-tools:v2.9.0 --version
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

**Dispatcher-level exit codes (`da-tools` itself)** — a different layer from the per-command exit-code tables below:

| Situation | Exit code | Notes |
|-----------|-----------|-------|
| `--help` / `--version` / no arguments | `0` | Usage and version go to stdout |
| **Unknown subcommand** | `2` | Caller error (since [#1406](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1406); was `1`). All messages go to stderr, stdout is empty |
| **Tool script missing from the image** | `2` | Same; stderr names the missing script and the paths searched |
| The subcommand's own exit code | Passed through unchanged | The tool runs as `__main__` in the same process, no remapping — see each command's exit-code table and the SSOT [`_lib_exitcodes.py`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/scripts/tools/_lib_exitcodes.py) |
| The subcommand raises an uncaught exception (traceback) | `1` | The Python interpreter's default; the dispatcher deliberately has no `try/except` (that would break pass-through). This is the residual collision with the "findings" `1` — a `1` with no report on stdout and a `Traceback` (or the tool's own one-line error) on stderr is this row |

> ⚠️ The first two rows used to return `1`, colliding with the per-subcommand "findings" `1` — `docker run … da-tools:<moving tag> <subcommand>` looked, in CI, exactly like "the tool ran fine and found something" (with an empty report on stdout) whenever a subcommand was renamed or the image did not ship it yet. Consumers should treat `2` as "the tool did not run" and never fold it into `1`.

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
| `init` | Project skeleton generation (CI/CD + conf.d + Kustomize overlays) | `--tenants <list>` with any of `--ci` / `--rule-packs` / `--deploy`, or interactive mode (no flags) |
| `gitops-check` | GitOps Native Mode readiness validation (repo / local / sidecar) | `<subcommand>` |
| `state-reconcile` | Migration state directory declarative reconciliation (schema_version validation + manifest rebuild) | `--state-dir <dir>` (default `.da/state`) |
| `rule-pack-diff` | Mechanical diff between two Rule Pack versions (added / removed / breaking label schema) | `--from <v1.yaml> --to <v2.yaml>` |
| `silencer-drift-check` | AM silence drift audit against v2 rule pack (offline, eats amtool dump) | `--silences-file <json> --rule-source <path>` |

### Operator + Federation Tools

| Command | Purpose | Minimum Parameters |
|---------|---------|-------------------|
| `operator-generate` | Rule Packs + Tenant config → PrometheusRule / AlertmanagerConfig / ServiceMonitor CRD YAML | `--rule-packs-dir <dir>` |
| `operator-check` | Operator CRD deployment status verification (5 checks + diagnostic report) | (auto-discover or `--namespace <ns>`) |
| `runtime-audit` | Read-only Git rule-packs ↔ Prometheus runtime reconciliation (#747; MISSING / UNHEALTHY / ORPHAN) | `--prometheus <url>` or `--runtime-json <file>` |
| `rule-pack-split` | Rule Pack hierarchical split (edge Part 1 + central Parts 2+3), Federation Scenario B | `--rule-packs-dir <dir>` |
| `fed-key` | Generate / rotate the federation JWT signing keypair (ADR-020 IV-2l): private-key Secret manifest → stdout, public JWKS → file | (none, bootstrap default) / `--rotate --existing-jwks <file>` |

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
| `deprecate` | Deprecate metrics: delete their keys from `defaults:` / `optional_overrides:` / tenant files | `<metric_keys...>` |
| `lint` | Check Custom Rule governance compliance | `<path...>` |
| `onboard` | Analyze existing Alertmanager/Prometheus config for migration | `<config_file>` or `--alertmanager-config <file>` |
| `analyze-gaps` | Compare custom rules with Rule Pack coverage | `--tenant-config <path>` |
| `config-diff` | Directory-level config diff (GitOps PR review) | `--old-dir <dir> --new-dir <dir>` |
| `evaluate-policy` | Policy-as-Code DSL evaluation engine | `--config-dir <dir>` |
| `opa-evaluate` | OPA Rego policy evaluation bridge (OPA integration) | `--config-dir <dir>` |
| `guard` | Dangling Defaults Guard wrapper (v2.8.0); shells out to the `da-guard` Go binary | `defaults-impact --config-dir <dir>` |
| `batch-pr` | Migration Batch PR Pipeline wrapper (v2.8.0); shells out to the `da-batchpr` Go binary | `apply\|refresh\|refresh-source [flags]` |
| `parser` | PromRule parser wrapper (v2.8.0); shells out to the `da-parser` Go binary; strict-PromQL portability check + dialect classification | `import\|allowlist [flags]` |
| `tenant-verify` | Print tenant effective config + merged_hash (v2.8.0; incremental migration playbook rollback checklist) | `<tenant-id> [--conf-d <dir>] [--expect-merged-hash <hash>]` or `--all --json` |
| `test-notification` | Multi-channel notification connectivity testing | `--config-dir <dir>` |
| `threshold-recommend` | Threshold recommendation engine (historical P50/P95/P99) | `--config-dir <dir>` + `--prometheus <url>` |
| `threshold-govern` | Threshold governance loop: recommend→gate→open per-tenant proposed-PR via tenant-api (#656) | `--config-dir <dir>` + `--prometheus <url>` + `--apply` |
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
| `1` | Only an uncaught exception (traceback) returns 1 — the command has no violation exit (inactive / pending / firing are all 0) |
| `2` | Caller error: Prometheus API unreachable or errored (stdout carries an `{"error": ...}` JSON), or arguments argparse rejects |

---

#### diagnose

Health check for a single tenant: the MariaDB Pod's status, the exporter's `mysql_up`, the operational mode (maintenance / silent), and, when `--config-dir` is given, the profile and inheritance chain.

**Purpose**: Quick single-tenant check after cutover or while troubleshooting. ⚠️ The Pod and exporter checks are written for MariaDB. The Pod check looks for an `app=mariadb` Pod in the namespace named after the tenant; it needs `kubectl` and cluster access, and the da-tools image does not ship `kubectl`. The exporter check queries `mysql_up{instance="<tenant>"}`. The tool first reads the tenant's `db_type` from Prometheus via `tenant_expected_exporter{tenant="<tenant>"}`; that series exists only when the tenant declares `_metadata.db_type`. Both checks run only when `db_type` is `mariadb`. For any other database, or a tenant that declares none, they are listed under `skipped` with a reason and `status` is `unchecked`: neither `healthy` nor `error`. A MariaDB tenant that does not declare `db_type` therefore skips them too; declare `mariadb` in `_metadata.db_type` to have them checked. ⚠️ The v2.9.0 image still has the old behavior: it ignores `db_type`, and every non-MariaDB tenant comes back `status: error` (`Pod not found`). <!-- image-caveat: v2.9.0 -->

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
| `--config-dir <PATH>` | Tenant config directory; profile and inheritance chain are looked up only when given | (none) |
| `--show-inheritance` | Print only the full inheritance-chain resolution (requires `--config-dir`) | false |
| `--json` | JSON output. Output is always JSON; the flag is accepted for compatibility | true |

The Pod is always looked up in the namespace named after the tenant; there is no option to choose another namespace.

**Output**

One line of JSON. When healthy it has just two fields:

```json
{"status": "healthy", "tenant": "db-a"}
```

When something is wrong it lists `issues` and recent error logs:

```json
{"status": "error", "tenant": "db-a", "issues": ["Pod not found", "Prometheus query failed (http://localhost:9090)"], "recent_logs": []}
```

A tenant in maintenance or silent mode gets an extra `operational_mode`; with `--config-dir` you also get `profile` (when set) and `inheritance_chain`. When the Pod and exporter checks are skipped there is an extra `skipped`:

```json
{"status": "unchecked", "tenant": "db-b", "skipped": [{"check": "pod", "reason": "db_type=postgresql; the Pod and exporter checks are written for MariaDB (app=mariadb, mysql_up)"}, {"check": "exporter", "reason": "db_type=postgresql; the Pod and exporter checks are written for MariaDB (app=mariadb, mysql_up)"}]}
```

**Examples**

```bash
da-tools diagnose db-a --config-dir ./conf.d
```

**Exit Codes**

| Code | Description |
|------|-------------|
| `0` | `status: healthy`, or `status: unchecked` (the Pod and exporter checks were skipped and nothing else was wrong) |
| `1` | `status: error`: the Pod is missing or not Running, or `mysql_up` is not 1 |
| `2` | Caller error: bad arguments (missing tenant, or `--show-inheritance` without `--config-dir`); a failed Prometheus query (the output is still the `status: error` JSON, `issues` contains `Prometheus query failed`, and the code is 2 even alongside other issues); or the Pod check needs to run but `kubectl` is not in the environment (one line on stderr, no JSON) |

⚠️ The v2.9.0 image does not have these exit codes yet: it returns `0` whether `status` is `healthy` or `error`, and ends with a Python traceback (rc=1) when `kubectl` is missing. On v2.9.0, read the output's `status`. <!-- image-caveat: v2.9.0 -->

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

Unified JSON report with summary of all tenant checks. `unchecked` tenants are counted in `unchecked_count`, in neither `healthy_count` nor `issue_count`; `health_score` is computed over checked tenants only and is `null` when every tenant is `unchecked`. The text report lists them in a separate `Unchecked Tenants` section with the skip reason. ⚠️ The v2.9.0 image has no `unchecked` status or field. <!-- image-caveat: v2.9.0 -->

**Examples**

```bash
da-tools batch-diagnose --workers 10
da-tools batch-diagnose --tenants db-a,db-b,db-c --output report.json
```

**Exit Codes**

| Code | Description |
|------|-------------|
| `0` | All tenants healthy |
| `1` | One or more tenant checks failed |
| `2` | Caller error: bad arguments, or the output path given to `-o/--output` cannot be written (#1641) |

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
| `--duration <SEC>` | Observation duration in seconds | `600` |
| `--interval <SEC>` | Sampling interval in seconds | `15` |
| `--metrics <LIST>` | Comma-separated metric list (empty=all) | (all) |
| `-o, --output-dir <DIR>` | Output directory; both CSVs are written there (see Output below), created if missing | `baseline_output` |
| `--dry-run` | Only show metrics to observe, don't sample | false |

**Output**

The statistical summary is printed to stdout; two CSVs are also written under `--output-dir`: `baseline-<tenant>-timeseries.csv` (raw samples) and `baseline-<tenant>-summary.csv` (one line per metric: min / max / avg / p50 / p90 / p95 / p99 and the recommended thresholds). ⚠️ There is no single-file output flag — `--output <FILE>` is accepted by argparse as an abbreviation of `--output-dir`, so it creates a **directory** named `<FILE>`.

Metrics observed and the tenant key each suggestion goes to:

| Metric | Unit | Suggested key |
|--------|------|---------------|
| `connections` | connections | `mysql_connections` |
| `cpu` | % of limit (the tenant's highest container) | Compared with the `container_cpu` platform default; no threshold value (see below) |
| `memory` | % of limit (the tenant's highest container) | Compared with the `container_memory` platform default; no threshold value (see below) |
| `slow_queries` | per minute | No tenant key: `MariaDBHighSlowQueries` compares against a fixed value |
| `disk_io` | KiB/s | No tenant key: no rule pack alert reads this measurement |

`cpu` / `memory` divide cAdvisor usage by the kube-state-metrics limit directly, the same arithmetic as the rule pack's `tenant:container_{cpu,memory}_percent:by_container`, so kube-state-metrics is required. A container without a limit gives no value (for CPU the rule pack falls back to node share there; baseline does not). `cpu` / `memory` are bounded (100% means OOMKill or throttling), so they are not given a p95×1.2 / p99×1.5 threshold: once p99 passes about 67% that formula yields a >100 threshold that can never fire. Instead they are compared with the platform default (read from scaffold; today `container_cpu` 80, `container_memory` 85). If p99 is below the default, the report says the default fits and needs no override. If p99 has reached the default, it says the default would fire routinely and the limit should be raised first, with the limit multiplier that would put p99 at 90% of the default. Other metrics print suggestions as `patch-config <tenant> <key> <value>`; a measurement with no tenant key prints only the observed value and the reason. ⚠️ The v2.9.0 image still has the old behavior: `cpu` is % of one core, `memory` is MiB, and every suggestion is spelled `mysql_<metric>`. No alert reads `mysql_memory` / `mysql_disk_io` / `mysql_slow_queries`, and `mysql_cpu` is really the threads_running threshold (later renamed `mysql_threads_running`). Do not copy its patch-config lines on v2.9.0. <!-- image-caveat: v2.9.0 -->

**Examples**

```bash
da-tools baseline --tenant db-a --duration 1800 --interval 30 -o baseline_out
```

**Exit Codes**

| Code | Description |
|------|-------------|
| `0` | Success. ⚠️ A Prometheus connection / query failure is **not** an error — failed samples are recorded as empty, the report and CSVs are still written, still this code |
| `1` | Only an uncaught exception (traceback) returns 1 — this command has no violation exit; measured with `--interval 0` (`duration // interval` raises `ZeroDivisionError`). ⛔ An unwritable output path has been `2` since #1789 and no longer lands here |
| `2` | Caller error: none of the `--metrics` names is one the tool knows (the error lists the accepted ones), the required `--tenant` missing, arguments argparse rejects, or `-o/--output-dir` cannot be written (measured: the parent is a file; a CSV the tool must create inside it is already a directory) — one `ERROR: cannot …` line naming `-o/--output-dir`, no traceback (#1789) |

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
   Reads the `prefix-mapping.yaml` that `da-tools migrate` writes to its output directory. CSV is not accepted: a CSV file fails with a traceback while loading and exits 1. The file looks like this (taken from a real `migrate` run):
   ```yaml
   custom_mysql_global_status_threads_connected:
     original_metric: mysql_global_status_threads_connected
     alert_name: MySQLTooManyConnections
     golden_match: null
     golden_rule: null
     old_query: max by(tenant) (mysql_global_status_threads_connected)
     new_query: tenant:custom_mysql_global_status_threads_connected:max
   ```
   Each entry becomes one comparison pair. The new query `new_query` is the recording rule migrate generated. The old query `old_query` is the original rule's left-hand side, aggregated per tenant the same way; rate-style rules keep their `rate()` (for example `sum by(tenant) (rate(mysql_global_status_slow_queries[5m]))` against `tenant:custom_mysql_global_status_slow_queries:sum`). Both sides have the same units, so the values agree once the recording rule is loaded and evaluating. If the raw series carry no `tenant` label, the old query yields an extra group with no tenant, reported as missing on the new side. Entries the dictionary sends to a golden rule (those with `golden_rule`) get no recording rule from migrate and no such fields; validate skips them and lists them on stderr.
   Files from an older migrate have no `old_query`/`new_query`. validate then warns on stderr and falls back to "raw metric vs `tenant:<key>:max`"; rate-style and `:sum` pairs cannot be compared from such a file, so regenerate it with a current migrate. ⚠️ The v2.9.0 image still ships the old migrate and validate behavior. <!-- image-caveat: v2.9.0 -->

2. **Query Mode**: `--old <query> --new <query>`
   Directly specify two PromQL expressions.

**Options**

| Option | Description | Default |
|--------|-------------|---------|
| `--watch` | Continuous monitoring mode (compare every N seconds) | false |
| `--interval <SEC>` | Monitoring interval in seconds | `60` |
| `--rounds <N>` | Number of monitoring rounds. ⚠️ `0` is not infinite — it runs **no rounds at all** (`for i in range(rounds)`) | `10` |
| `--tolerance <RATIO>` | Allowed deviation as a **ratio**, not a percentage: `0.01` = 1% | `0.001` |
| `--auto-detect-convergence` | Auto-detect convergence and output readiness JSON | false |
| `-o, --output-dir <DIR>` | Output directory; `validation-report.csv` (and, in watch mode, `cutover-readiness.json`) are written there | `validation_output` |

**Output**

`<output-dir>/validation-report.csv`, one line per rule (old value, new value, difference %, convergence status); the summary is also printed to stdout. ⚠️ There is no single-file output flag — `--output <FILE>` is accepted by argparse as an abbreviation of `--output-dir`, so it creates a **directory** named `<FILE>`.

With `--watch --auto-detect-convergence`, `cutover-readiness.json` is additionally written into the same directory for the `cutover` command (`--convergence-output <FILE>` overrides the path); single-shot mode (no `--watch`) never writes this file.

**Examples**

```bash
da-tools validate --mapping migration_output/prefix-mapping.yaml
da-tools validate --mapping migration_output/prefix-mapping.yaml --watch --interval 60 --rounds 1440
da-tools validate --mapping migration_output/prefix-mapping.yaml --watch --auto-detect-convergence -o ./validation_output
```

**Exit Codes**

| Code | Description |
|------|-------------|
| `0` | Every pair matches; with `--watch --auto-detect-convergence`, converged |
| `1` | A mismatch, or a value found on only one side (`old_missing` / `new_missing`); with `--watch --auto-detect-convergence`, still not converged after `--rounds` |
| `2` | Caller error: bad arguments, no comparison pairs in the mapping, a Prometheus connection or query failure (ignored once converged), or the output path given to `-o/--output-dir` / `--convergence-output` cannot be written (#1641) |

⚠️ The v2.9.0 image does not have these exit codes yet. It returns `2` only for bad arguments and `0` for everything else, and it still prints "🎉 可以安全切換" (safe to cut over) when Prometheus is unreachable. On v2.9.0, do not gate on the exit code; read the mismatch / missing counts in the summary instead. <!-- image-caveat: v2.9.0 -->

---

#### cutover

Shadow Monitoring one-click cutover (the last step of the migrate / shadow flow): stop the shadow monitor Job, delete the old Recording Rules, remove the shadow label and the Alertmanager intercept, then confirm the tenant's threshold metrics exist.

**Purpose**: Final migration step, automates complete cutover workflow. Applies only to the migrate / shadow flow; the objects it deletes and edits (the `shadow-monitor` Job, the `prometheus-rules-old` ConfigMap, the `migration_status` label) are created by that flow.

**Syntax**

```bash
da-tools cutover --readiness-json <FILE> --tenant <name> [options]
```

**Required Parameters**

| Parameter | Description |
|-----------|-------------|
| `--readiness-json <FILE>` | `cutover-readiness.json` produced by `validate_migration --auto-detect-convergence` |
| `--tenant <NAME>` | Tenant ID (used by the post-cutover health check) |

**Options**

| Option | Description | Default |
|--------|-------------|---------|
| `--dry-run` | Print the `kubectl` commands it would run, change nothing | false |
| `--force` | Proceed even when the readiness JSON says `ready: false`; `--readiness-json` is still required | false |
| `--namespace <NS>` | K8s namespace | `monitoring` |
| `--json-output` | Also print a JSON report on stdout | false |

**Automated Steps**

0. Read the readiness JSON: stop if it is unreadable or missing fields; stop if `ready` is false (unless `--force`)
1. `kubectl delete job shadow-monitor`
2. `kubectl delete configmap prometheus-rules-old`
3. `kubectl label configmap prometheus-rules migration_status-`
4. `kubectl label configmap alertmanager-config migration_status-`
5. Query `count(user_threshold{tenant="<tenant>"})` to confirm the tenant's threshold metrics exist

There is no rollback option; when a step fails the tool points to the manual rollback in `shadow-monitoring-sop.md` §7.2.

**Examples**

```bash
da-tools cutover --readiness-json cutover-readiness.json --tenant db-a --dry-run
da-tools cutover --readiness-json cutover-readiness.json --tenant db-a
da-tools cutover --readiness-json cutover-readiness.json --tenant db-a --force
```

**Exit Codes**

| Code | Description |
|------|-------------|
| `0` | Cutover successful |
| `1` | Readiness says not ready (without `--force`), or a cutover step failed |
| `2` | Caller error: a required argument is missing, the readiness JSON is unreadable or missing fields, Prometheus is unreachable, or `kubectl` is not found |

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
da-tools blind-spot --config-dir ./conf.d
da-tools blind-spot --config-dir ./conf.d --exclude-jobs node-exporter,kube-state-metrics
da-tools blind-spot --config-dir ./conf.d --json-output
```

**Exit Codes**

| Code | Description |
|------|-------------|
| `0` | Success (regardless of blind spots) |
| `1` | Prometheus connection failed |
| `2` | Caller error: a file under `--config-dir` cannot be read (content not UTF-8 or not valid YAML; the message names the file, #1654) |

---

#### maintenance-scheduler

Evaluate the `_state_maintenance.recurring[]` schedules and create an Alertmanager silence for every tenant currently inside a maintenance window.

**Purpose**: Automate scheduled maintenance windows; designed to run as a K8s CronJob every 5 minutes.

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
| `--alertmanager <URL>` | Alertmanager base URL; silences are only created when this is given, otherwise it just reports | (none) |
| `--pushgateway <URL>` | Push the run's results to a Pushgateway (skipped with `--dry-run`) | (none) |
| `--dry-run` | Report only, create no silences | false |
| `--json-output` | Also print one line `{"created", "skipped", "errors", "mode"}` on stdout; `mode` is `apply`, `dry-run` or `report-only`, and in the last two `created` is the number it *would* create | false |

Cron expressions are always read as **UTC**; an option to choose a timezone is not implemented yet. For 02:00 Taipei time every day, write `0 18 * * *`. The output is not a silence YAML file either: the tool calls the Alertmanager API directly.

**Output**

stderr lists whether each schedule is currently inside its window and ends with a summary line: `Summary: N created, N skipped, N errors` when it really creates silences, `Summary: N in window (report only …)` without `--alertmanager`, and `Summary: N would be created (dry run …)` with `--dry-run`. A created silence matches `tenant="<tenant>"` and `alert_source=""`, is created by `da-tools/maintenance-scheduler`, carries the schedule's `reason` as its comment, and ends when the window ends; if the window already has a silence that lasts until the window ends, it counts as skipped; if that silence would expire before the window ends, the tool extends it to the window's end (stderr prints `Extended silence …`) and counts the extension as created. ⚠️ `--dry-run` does not read Alertmanager's existing silences, so ones that already exist are counted as "would be created" too. The v2.9.0 image still prints `N created` in both cases and has no `mode` field, although nothing is created. <!-- image-caveat: v2.9.0 -->

**Examples**

```bash
da-tools maintenance-scheduler --config-dir ./conf.d --dry-run
da-tools maintenance-scheduler --config-dir ./conf.d --alertmanager http://alertmanager:9093
```

**Exit Codes**

| Code | Description |
|------|-------------|
| `0` | Success (silences created, already present, or not needed) |
| `1` | At least one silence could not be created |
| `2` | Caller error: `--config-dir` does not exist, `croniter` is missing, or a file under it cannot be read (content not UTF-8 or not valid YAML; the message names the file, #1654) |

---

#### backtest

Execute historical backtest of PR threshold changes.

**Purpose**: Validate threshold adjustment impacts; estimate expected alert changes from PR.

**Syntax**

```bash
da-tools backtest [--git-diff | --config-dir <dir> --baseline <dir>] [options]
```

**Required Parameters**

Choose one mode:

1. **Git Diff Mode**: `--git-diff`
   (Run inside Git repo, auto-detect changes)
   ⚠️ Needs `git` on PATH and, as the working directory, the directory that contains `conf.d/` (the tool runs `git diff HEAD~1 -- conf.d/` relative to the cwd; under the customer layout that is the repo root). Inside a container, mount the directory that contains `conf.d/` and `-w` into the mount point (e.g. `-v $(pwd):/workspace -w /workspace`). The `ghcr.io/vencil/da-tools` image **does not ship git**, so `git diff` cannot run inside it — run this mode where git is installed

2. **Directory Comparison Mode**: `--config-dir <dir> --baseline <dir>`
   (Compare two config versions)

**Options**

| Option | Description | Default |
|--------|-------------|---------|
| `--lookback <DURATION>` | Historical lookback window, `<number><d\|h\|m>` (e.g. `7d` / `24h`). A value that does not match that shape (including a bare `7` or the empty string) ⇒ exit code 2 listing the accepted shape (#1625; it used to be silently treated as `7d`) | `7d` |
| `--output <FILE>` | Output to JSON or CSV | stdout |

**Output**

Comparison report showing impact of threshold changes on historical data (potential alerts increased/decreased).

**Examples**

```bash
# Git Diff mode — the image has no git, so run the script on a host checkout
# (same invocation as this repo's .github/workflows/backtest.yaml), from the
# directory that contains conf.d/
python3 scripts/tools/ops/backtest_threshold.py --git-diff \
  --prometheus http://prometheus.monitoring.svc.cluster.local:9090 \
  --lookback 7d --skip-if-unavailable

# Directory comparison mode
da-tools backtest --config-dir ./conf.d-new --baseline ./conf.d-old --lookback 7d
```

**Exit Codes**

| Code | Description |
|------|-------------|
| `0` | Success |
| `1` | At least one threshold change was rated HIGH risk (review before merging); an unreachable Prometheus or a git that cannot run is not 1, see below |
| `2` | Caller error: `--lookback` supplied but unusable (not `<number><d\|h\|m>`, #1625); `--git-diff` supplied but git cannot run (git not installed, not inside a git work tree, no HEAD~1) — ⛔ do not switch to `--config-dir` to go green, that compares two trees, not your PR; the output path given to `-o/--output` / `--markdown-output` cannot be written (#1641); `--lookback` supplied but unusable (not `<number><d\|h\|m>`, #1625); `--git-diff` supplied but git cannot run (git not installed, not inside a git work tree, no HEAD~1) — ⛔ do not switch to `--config-dir` to go green, that compares two trees, not your PR; a conf.d file whose content cannot be read (not UTF-8 or not valid YAML; the message names the file, #1654) |

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
| `2` | Caller error: Prometheus unreachable or a query failed in `preflight` (including `all`; the check is still listed as FAIL, but the exit code is 2, not 1), an I/O error reading `--report-csv`, or arguments argparse rejects. ⚠️ A `--report-csv` that does not exist is **not** 2 — the CSV analysis is simply skipped. ⚠️ Running `runtime` alone with Prometheus unreachable is **neither** 2 nor 1: both queries fail without producing any check, so the result is `Overall: PASS` and exit code 0 |

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
| `2` | Caller error: Prometheus or Alertmanager unreachable, or a query / rules / status API call failed (the check is still listed as FAIL, but the exit code is 2, not 1); or arguments argparse rejects |

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
| `2` | Caller error: `e2e` without `--edge-urls`, a target other than edge / central / e2e; edge / central Prometheus unreachable or a config / query / rules API call failed (the check is still listed as FAIL, but the exit code is 2, not 1) |

---

#### fed-key

Generate / rotate the federation JWT signing keypair (ADR-020 IV-2l).

**Purpose**: tenant-api signs federation tokens with an RS256 private key; federation-gateway verifies them against the matching public key's JWKS. This tool generates an RSA keypair and emits the two downstream artifacts: the private key → a Kubernetes Secret manifest (stdout, never written to disk); the public key → a JWKS file. Each public key's `kid` is its RFC 7638 JWK thumbprint, which matches the `kid` header tenant-api computes by construction.

**Syntax**

```bash
da-tools fed-key [options]
```

**Options**

| Option | Description | Default |
|--------|-------------|---------|
| `--rotate` | Rotation mode: merge the new public key into `--existing-jwks` (old + new coexist) | false |
| `--existing-jwks <PATH>` | Current JWKS file to merge into (required with `--rotate`) | (none) |
| `--jwks-out <PATH>` | JWKS output path | `./federation-jwks.json` |
| `--namespace <NS>` | Namespace for the Secret manifest | `monitoring` |
| `--secret-name <NAME>` | Secret name | `tenant-federation-signing-key` |
| `--secret-key <KEY>` | Data key inside the Secret | `federation-signing-key.pem` |
| `--key-bits <N>` | RSA modulus size (tenant-api rejects < 2048) | `2048` |

**Examples**

```bash
# First bootstrap: private key applied directly, JWKS written to ./federation-jwks.json
da-tools fed-key | kubectl apply -f -

# Planned rotation: new public key merged into the existing JWKS (rotation order: see runbook)
da-tools fed-key --rotate --existing-jwks federation-jwks.json \
  --jwks-out federation-jwks.json > new-signing-key.secret.yaml
```

> Full rotation / emergency-replacement procedure: [`federation-key-rotation-runbook.md`](internal/federation-key-rotation-runbook.md).

**Exit Codes**

| Code | Description |
|------|-------------|
| `0` | Key generated |
| `1` | Only an uncaught exception (traceback) returns 1 — this command has no violation exit; measured when `--existing-jwks` points at a JSON **array** (a top level that is not an object). ⛔ An unwritable output path has been `2` since #1789 and no longer lands here |
| `2` | Caller error: `--jwks-out` cannot be written (measured: its directory does not exist) — one `ERROR: cannot …` line naming `--jwks-out`, no traceback (#1789); `openssl` not on PATH, timed out or failed; `--existing-jwks` unreadable, not a JWKS document (no `keys` array) or already holding the same kid; `--rotate` without `--existing-jwks`, `--key-bits` < 2048; stdout is a terminal (refuses to print the private-key Secret to a tty — pipe it to `\| kubectl apply -f -`) |

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
| `1` | `--verify` found issues (a ConfigMap whose dashboard JSON is invalid). ⚠️ Import-mode failures are all 2, not 1; `kubectl` not on PATH is an uncaught exception (traceback, rc 1, in both import and `--verify`) |
| `2` | Caller error: `--dashboard` file missing or not valid JSON, `--dashboard-dir` missing or holding no `*.json`; `kubectl create / apply / label` failed during import; `kubectl get` failed or its output unparsable under `--verify`; none of the three mode flags given |

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
| `2` | Caller error: `--period` unparsable, `--tenant` containing characters other than alphanumerics / underscore / hyphen, the required `--prometheus` missing, or arguments argparse rejects. ⚠️ An unreachable Prometheus is **not** 2 — failed queries count as no data and the report is still printed (rc 0; under `--ci` the score / BAD count decide 1) |

---

#### alert-correlate

Analyze Alertmanager alerts using time-window clustering, compute correlation scores, and infer root causes. Supports both online (Prometheus API) and offline (JSON file) modes.

**Usage**

```bash
da-tools alert-correlate --prometheus <URL> [--window <DURATION>] [--lookback <DURATION>] [--min-score <FLOAT>] [--json] [--markdown] [--ci]
da-tools alert-correlate --input <FILE> [--window <DURATION>] [--min-score <FLOAT>] [--json]
```

**Arguments**

| Argument | Description | Default |
|----------|-------------|---------|
| `--prometheus <URL>` | Prometheus endpoint (online mode) | `$PROMETHEUS_URL` |
| `--input <FILE>` | Alertmanager JSON file (offline mode) | — |
| `--window <DURATION>` | Time window size (Prometheus duration, e.g. `5m`). ⚠️ A bare number does **not** parse (`10` → `None`); the unit is required | `5m` |
| `--lookback <DURATION>` | Lookback duration | `24h` |
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
| `2` | Caller error: `--window` unparsable or ≤ 0, or arguments argparse rejects. ⚠️ An unreachable Alertmanager/Prometheus is **not** 2 — a WARN is printed and the run continues with zero alerts (rc 0); an `--input` file that does not exist is an uncaught exception (traceback, rc 1) |

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
| `2` | Caller error: any `--dirs` directory missing, fewer than 2 directories in configmap mode, not exactly 1 directory in operator mode, `--labels` count not matching `--dirs`; in operator mode `kubectl` not on PATH, timed out (30s), exited non-zero or returned non-JSON; arguments argparse rejects |

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
| `--lookback` | Lookback period, `<number><d\|h\|m\|s>` (e.g. `30d` / `4h`). A value that does not match that shape or is not positive (including a bare `30`, the empty string, `0s`) ⇒ exit code 2 listing the accepted shape (#1625; it used to be silently treated as `30d`) | `30d` |
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

In `conf.d/`, init owns only two kinds of path: `_defaults.yaml` and `<tenant>.yaml` at the **root**. A requested tenant that **may** already be declared by **another file** of yours is **skipped, and that file is left untouched**; stderr and the summary name it (e.g. "db-c may already be declared by conf.d/team.yaml — conf.d/db-c.yaml was not generated") and the exit code stays 0. Likewise `_defaults.yaml` is not written when the root already has a defaults carrier in another spelling (e.g. `_defaults.yml`). "May declare" is judged by **content**, never by filename, and is deliberately generous: the keys of a `tenants:` mapping, or the tenant id appearing as a standalone token anywhere in the file (inside quotes, in comments, in flow/JSON style, in UTF-16 and other encodings, or written with YAML escapes such as `"db\x2dc"`) all count; a file that cannot be read or decoded, or that carries an explicit tag (`!!binary` …), counts as possibly naming every requested tenant, and the skip notice says why and how to make it readable to init. ⚠️ A `!` starting a word inside a comment or a string (e.g. `# !Important`, `"wow !!"`) counts as a tag too and makes that file name every tenant — an accepted over-match. Such a file does not trigger the coexistence refusal below: if init's own `conf.d/<t>.yaml` already exists **and itself declares t**, init rewrites it as usual and names the file for you to check with guard; an own file that exists but does not declare t (a placeholder, say) counts as no own file — t is skipped and that file is not rewritten. ⚠️ **init does not check whether the exporter can read that file** — to confirm the skipped tenant really is declared, run [`da-tools guard defaults-impact --config-dir <conf.d>`](#guard) (it scans the whole tree the way the exporter does and fails on a duplicate; the file should be listed under Scanned files in its report). A customer file that names no requested tenant draws no comment (init is not a validator). init **refuses with rc 1 and writes nothing** (`.da-init.yaml` included), under `--dry-run` too, when: its own `conf.d/<t>.yaml` already exists while another file **concretely** names t (a `tenants:` key or an id token; e.g. `db-c.yaml` and `db-c.yml` — the exporter rejects a tree that declares a tenant twice, and which file to keep is your call); `_defaults.yaml` sits beside a defaults carrier in another spelling; the `conf.d/<t>.yaml` it would overwrite may also declare another tenant this run was asked for (after the overwrite that tenant would be declared nowhere); the `conf.d/<t>.yaml` it would overwrite also declares a tenant this run was **not** asked for (e.g. `db-a.yaml` declares db-a and db-z and you run `--tenants db-a`: the rewrite would make db-z's declaration disappear — move db-z into a file of its own such as `conf.d/db-z.yaml`, or add db-z to `--tenants` too), or init cannot list every tenant that file declares (PyYAML cannot parse it) — `--force` is refused just the same; or any path it would write has, at **any level** below the output directory, an existing entry that differs only in case (e.g. `Conf.D/` for `conf.d/` — one path on a case-insensitive filesystem).

⚠️ With `--deploy kustomize`, a skipped tenant whose carrier is not at the `conf.d` root (e.g. `conf.d/prod/db-c.yaml`) is **not** in the generated ConfigMap (`configMapGenerator.files` is flat; `make configmap-assemble` also takes top-level files only, see [GitOps deployment §3](integration/gitops-deployment.en.md#3-configmap-assembly)); init names it in a WARN line on stderr and in the summary, rc stays 0.

```bash
da-tools init [--ci <github|gitlab|both>] [--tenants <list>] [--rule-packs <list>] [--deploy <kustomize|helm>] [-o <dir>] [--non-interactive] [--dry-run] [--force]
```

**Parameters**

| Parameter | Description | Default |
|-----------|-------------|---------|
| `--ci` | CI/CD platform | `both` |
| `--tenants` | Comma-separated tenant names. If `--ci` / `--rule-packs` / `--deploy` is given without `--tenants`: without `--non-interactive`, a terminal stdin is **asked for the tenant names only** (no default; every other flag is used as given; the answer is split like `--tenants`, so `a,,b` is refused either way); with `--non-interactive`, or off a terminal (CI, scripts) it is **rc 2 and nothing is written**, `--dry-run` included. A `--tenants` that names no tenant (`''`, `' , '`) counts as not given. init no longer fills in example tenants | none; the fully interactive mode (no flags) offers `db-a,db-b` as the prompt default |
| `--rule-packs` | Comma-separated Rule Packs | `mariadb,kubernetes` (interactive mode) |
| `--deploy` | Deployment method | `kustomize` |
| `--non-interactive` | Skip interactive prompts (requires `--tenants`) | — |
| `--dry-run` | Show files that would be generated without writing | — |
| `--force` | Re-run in an initialised directory: **rewrites every generated file**, including `conf.d/_defaults.yaml` and each `conf.d/<tenant>.yaml` (hand edits are lost). ⚠️ **Exception: it never rewrites an existing root `.gitlab-ci.yml`** — that may be the customer's own pipeline, so it is left alone on every run (and there is therefore no in-tool way to regenerate it); ⚠️ if an own file also declares a tenant this run was not asked for, `--force` is refused too (see above); ⚠️ **nor any conf.d carrier init did not write**: tenants your other files may declare, and defaults in another spelling, are still skipped and named, and a coexisting pair is still refused (see above) | — |

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
da-tools gitops-check local --dir conf.d

# Check sidecar deployment
da-tools gitops-check sidecar --namespace monitoring --json
```

---

#### state-reconcile

Declarative reconciliation of the migration state directory — scans `.da/state/*.json`, validates `schema_version`, and rebuilds `.da/manifest.json` from the filesystem. Replaces the manual jq workflow described in [troubleshooting-checklist §schema_version drift / manifest drift](integration/troubleshooting-checklist.en.md) ([issue #405](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/405) Category A).

```bash
da-tools state-reconcile [options]
```

**Main Parameters**

| Parameter | Purpose | Default |
|-----------|---------|---------|
| `--state-dir <dir>` | Directory with per-cluster state files | `.da/state` |
| `--manifest-path <path>` | Manifest file path | `.da/manifest.json` |
| `--dry-run` | Report needed changes without writing | (off) |
| `--ci` | **Pair with `--dry-run`** for a check-only CI gate (exit 1 when changes would be needed). Unresolvable drift always exits 1 regardless of `--ci`. Using `--ci` alone (no `--dry-run`) still applies changes | (off) |
| `--json` | Emit JSON structured report | text mode |

**Exit Codes**

| Code | Description |
|------|-------------|
| `0` | State directory consistent (or changes applied successfully) |
| `1` | Unresolvable schema drift (including a state file that cannot be read or lacks `schema_version`); or `--ci` with `--dry-run` detecting pending changes |
| `2` | Caller error: arguments argparse rejects (unknown flags etc.). ⚠️ A missing `--state-dir` is **not** 2 — a warning is printed and it is treated as empty (manifest rebuilt with 0 states, rc 0; 1 under `--ci --dry-run` because a rebuild is pending) |

**Why a single declarative command, not micro-commands**

Schema migration (upgrade state files) and manifest rebuild (regenerate from filesystem) are the same kind of problem — repairing the state directory to a consistent state. Splitting them into two commands forces users to remember the execution order. This tool's one-liner ("make .da/ consistent") covers both. Schema migrations are registered in `MIGRATIONS` (empty for v1.0; add an entry when v1.1 ships).

**Examples**

```bash
# Basic: detect and repair
da-tools state-reconcile

# Custom location
da-tools state-reconcile --state-dir custom/states/ --manifest-path custom/manifest.json

# Dry-run to preview changes
da-tools state-reconcile --dry-run

# CI gate: exit 1 on drift
da-tools state-reconcile --ci --dry-run

# Automation-friendly JSON
da-tools state-reconcile --json
```

---

#### rule-pack-diff

Mechanical diff between two Rule Pack versions — walks `groups[*].rules[*]` `alert` / `record` entries and classifies into added / removed / modified / breaking-label-schema. Replaces the "manually cross-reference CHANGELOG + git diff" workflow in [staged-adoption-guide §7.3](scenarios/staged-adoption-guide.en.md) ([issue #405](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/405) Category D).

```bash
da-tools rule-pack-diff --from <v1.yaml> --to <v2.yaml> [options]
```

**Main Parameters**

| Parameter | Purpose |
|-----------|---------|
| `--from <path>` | Older Rule Pack YAML path |
| `--to <path>` | Newer Rule Pack YAML path |
| `--json` | Emit JSON structured report |
| `--ci` | Exit 1 on breaking changes (CI gate). Breaking = alert removed, label key added/removed, label value change, alert↔record kind swap |

**Breaking vs informational changes**

| Change | Breaking? |
|---|---|
| Alert removed / renamed | ⚠️ breaking (silencer matchers on alertname silently miss) |
| Label key added / removed | ⚠️ breaking (matcher key absent / extra) |
| Label value change (e.g. `severity: warning → critical`) | ⚠️ breaking (equality matchers miss) |
| Alert ↔ record kind swap | ⚠️ breaking (silencers don't apply to recording rules) |
| PromQL `expr` change | 📝 informational (semantic equivalence undecidable) |
| Annotation change | 📝 informational |
| `for:` duration change | 📝 informational |

**Examples**

```bash
# Compare two versions extracted from git
git show v1.0.0:rule-packs/rule-pack-mariadb.yaml > v1.yaml
git show v2.0.0:rule-packs/rule-pack-mariadb.yaml > v2.yaml
da-tools rule-pack-diff --from v1.yaml --to v2.yaml

# CI gate: block merge on breaking changes
da-tools rule-pack-diff --from v1.yaml --to v2.yaml --ci

# Automation-friendly JSON
da-tools rule-pack-diff --from v1.yaml --to v2.yaml --json
```

**Exit codes**

| Code | Meaning |
|------|---------|
| 0 | Diff completed (no breaking changes; or no `--ci` so breaking still exits 0) |
| 1 | `--ci` mode detected breaking changes |
| 2 | Caller error (file missing / parse failure / bad arg) |

---

#### silencer-drift-check

Alertmanager silence drift audit against the v2 rule pack — consumes an `amtool silence query -o json` dump + a rule pack source, lists silences whose matchers no longer match **any** alert in the rule source. Replaces the manual `jq + comm` workflow in [troubleshooting-checklist §1.3.2](integration/troubleshooting-checklist.en.md) ([issue #405](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/405) Category B).

**Offline-first design**: the tool does NOT connect to AM directly — avoiding VPN / Ingress / Auth-proxy boundary pitfalls. It runs in CI / GitHub Action environments without any AM network connectivity.

```bash
da-tools silencer-drift-check --silences-file <silences.json> --rule-source <path> [options]
```

**Main Parameters**

| Parameter | Purpose |
|-----------|---------|
| `--silences-file <path>` | Path to `amtool silence query -o json` output (required) |
| `--rule-source <path>` | Rule pack YAML file or directory (recursive `*.yaml`/`*.yml`; files without a `groups:` root such as `_defaults.yaml` are silently skipped) |
| `--include-inactive` | Include expired / pending silences. Default: only active silences are checked |
| `--json` | Emit machine-readable JSON instead of human-readable text |
| `--ci` | Exit 1 on orphan silences (CI gate) |

**Full matcher semantics**

The tool implements all four AM matcher combinations (not just `alertname=`):

| `isEqual` | `isRegex` | Semantics |
|---|---|---|
| true | false | `label == value` |
| false | false | `label != value` |
| true | true | `label =~ value` (Go regex, **fullmatch**) |
| false | true | `label !~ value` |

A silence matches an alert iff **all matchers** match the alert's label set. Label set includes the implicit `alertname=<name>` plus the rule's `labels:` block. **Absent labels are treated as empty strings** (AM convention).

**Match coverage reporting**

A silence fails in two opposite directions: an orphan matches nothing and suppresses nothing; an over-broad matcher (a dropped label, a `.*` left in) matches far more than intended and suppresses alerts nobody meant to silence. Orphan detection only sees the first, so the report also lists what each silence actually matches:

| Output | Content |
|--------|---------|
| Text "Match coverage" section | Rules matched per in-scope silence, widest first; the name preview shows at most 3 and spells out `+N more` |
| `coverage[]` under `--json` | One entry per in-scope silence, four fields: `silence_id`; `matchers`, echoed verbatim so a consumer can see what produced the count without re-reading the input file; `matched_rules` (rule definitions); and the deduped `matched_alertnames` (**not truncated**) |
| Summary line / `counts.widest_match` | The largest match count across in-scope silences |

⚠️ `matched_rules` counts rule definitions in the rule source, NOT alerts currently firing — the latter needs live Prometheus / Alertmanager state, which this tool deliberately does not reach for.

Every file-sourced string in the text output (alertname, silence id, matcher value, comment, author) has its control characters escaped to `\x1b` / `\u202e` form before printing. The rule pack comes from whatever path `--rule-source` names (tenant self-service Custom Alerts included) and the silence dump from anyone with Alertmanager write access; a raw ESC or CR in either rewrites the line on a terminal or in a CI log, making one name *display* as a different one. The escaping happens in the text renderer only — `--json` keeps the original strings, since `json.dumps` already escapes control characters losslessly and the machine-readable copy must stay faithful.

**Exit codes**

| Code | Meaning |
|------|---------|
| 0 | No orphan silences (or orphans present but no `--ci`) |
| 1 | `--ci` mode detected orphans |
| 2 | Caller error (file missing / JSON parse failure / `--rule-source` empty) |

**Examples**

```bash
# Step 1: grab silences in a network-accessible environment
amtool silence query -o json --alertmanager.url=http://<am>:9093 > silences.json

# Step 2: offline comparison (no AM access needed)
da-tools silencer-drift-check --silences-file silences.json --rule-source rule-packs/

# CI gate: block merge on orphan silences
da-tools silencer-drift-check --silences-file silences.json --rule-source rule-packs/ --ci

# Automation-friendly JSON
da-tools silencer-drift-check --silences-file silences.json --rule-source rule-packs/ --json
```

⛔ **Do not wire `--ci` over a live silence dump** (the CI-gate example above targets a dump captured by hand at cutover). Measured 2026-08-27: all 122 alerts in `rule-packs/` carry a templated `tenant:` label (122 templated / 0 literal), while every silence `maintenance_scheduler.py` creates carries a literal `tenant=<id>` matcher ⇒ this platform's own scheduler-created silences all land in the existing templated-label false positive (see the tool's Known limitations), and the gate would fail on **every scheduled maintenance window**.

---

### Operator + Federation Tools

#### operator-generate

Generate Kubernetes Operator CRDs (PrometheusRule, AlertmanagerConfig, ServiceMonitor) from Rule Packs and Tenant configuration.

**Purpose**: Dynamic alert rule and routing deployment in Prometheus Operator clusters; GitOps-friendly CRD YAML generation.

**Syntax**

```bash
da-tools operator-generate [options]
```

**Options**

| Option | Description | Default |
|--------|-------------|---------|
| `--rule-packs-dir <DIR>` | Rule Pack directory path | `rule-packs/` |
| `--config-dir <DIR>` | Tenant configuration directory path | `conf.d/` |
| `--output-dir <DIR>` | Write CRDs into this directory. ⚠️ **Writing requires both: this flag set _and_ no `--dry-run`**; if either fails, everything goes to **stdout** and no file is written | none |
| `--namespace <NS>` | Target K8s namespace | `monitoring` |
| `--api-version <VER>` | AlertmanagerConfig API version (`v1alpha1` / `v1beta1`) | `v1beta1` |
| `--components <COMP>` | Components to generate (`all` / `rules` / `alertmanager` / `servicemonitor`) | `all` |
| `--receiver-template <TYPE>` | Receiver template type (`slack` / `pagerduty` / `email` / `teams` / `opsgenie` / `webhook`) | — |
| `--secret-name <NAME>` | K8s Secret name (receiver credential reference); use with `--receiver-template` | `da-{tenant}-{type}` |
| `--secret-key <KEY>` | Key name inside the K8s Secret | inferred from the receiver type |
| `--selector-label <KEY=VALUE>` | Label added to the PrometheusRule and ServiceMonitor (repeatable; overrides a default of the same key) so Prometheus's `ruleSelector` / `serviceMonitorSelector` matches. Use `release=<name>` when your Helm release is not named `kube-prometheus-stack` | PrometheusRule: `prometheus=kube-prometheus`, `release=kube-prometheus-stack`; ServiceMonitor: `release=kube-prometheus-stack` |
| `--gitops` | GitOps mode (sorted keys, no timestamps) | false |
| `--dry-run` | Print output instead of writing files | false |
| `--json` | Output the result report as JSON | false |
| `--kustomize` | Also generate `kustomization.yaml`. ⚠️ Without `--output-dir` it is **mixed into the stdout stream**, and a `Kustomization` is not accepted by `kubectl apply -f -` | false |

**Examples**

```bash
# Basic: CRDs go to stdout (no files written) and can be applied directly
da-tools operator-generate --rule-packs-dir rule-packs/ --config-dir conf.d/ | kubectl apply -f -

# To write files, name the directory explicitly (#1582: writing is opt-in)
da-tools operator-generate --rule-packs-dir rule-packs/ --config-dir conf.d/ --output-dir ./operator-crds

# GitOps mode + Slack receiver
da-tools operator-generate \
  --config-dir conf.d/ \
  --output-dir ./operator-crds \
  --receiver-template slack \
  --gitops

# PagerDuty + custom Secret
da-tools operator-generate \
  --receiver-template pagerduty \
  --secret-name org-pd-secret \
  --secret-key routing-key

# AlertmanagerConfig only
da-tools operator-generate --components alertmanager --receiver-template email

# Dry-run JSON report — stdout is a single JSON document:
#   {"crds": [...], "kustomization": {...}|null, "summary": {...}}
# (progress/summary messages go to stderr, so it pipes cleanly into jq)
da-tools operator-generate --dry-run --json | jq '.crds | length'
da-tools operator-generate --dry-run --json | jq -r '.crds[].metadata.name'
```

---

#### migrate-to-operator

Read existing ConfigMap-based Prometheus rules and produce equivalent CRD YAML files with a migration checklist.

**Purpose**: Migrate from ConfigMap native format to Operator native CRD; GitOps-friendly conversion; pre-migration validation and preview.

**Syntax**

```bash
da-tools migrate-to-operator [options]
```

**Options**

| Option | Description | Default |
|--------|-------------|---------|
| `--source-dir <DIR>` | Directory containing ConfigMap YAML files | (required) |
| `--config-dir <DIR>` | Tenant config directory | `conf.d` |
| `--output-dir <DIR>` | Write CRDs and the checklist into this directory. ⚠️ **Writing requires all three: this flag set, no `--dry-run`, and no `--checklist-only`** (the same discriminators as the `--json` envelope below); if any fails, everything goes to **stdout** and no file is written | none |
| `--namespace <NS>` | Target K8s namespace | `monitoring` |
| `--receiver-template <TYPE>` | Receiver type (`slack` / `pagerduty` / `email` / `teams` / `opsgenie` / `webhook`) | — |
| `--secret-name <NAME>` | K8s Secret name | — |
| `--secret-key <KEY>` | Key within K8s Secret | — |
| `--dry-run` | Preview without writing files | false |
| `--checklist-only` | Generate migration checklist only | false |
| `--json` | JSON output mode | false |

> ⚠️ **There are three top-level `--json` shapes. They are selected in this order** (#1582, all 8 combinations measured):
> 1. `--checklist-only` — **outranks every other flag** (including `--dry-run` and `--output-dir`) ⇒ **checklist envelope** (adds `checklist` / `status`; `prometheus_rules` is a **count**);
> 2. otherwise `--dry-run` **or** no `--output-dir` ⇒ **preview envelope** (`metadata` / `errors`; `prometheus_rules` is a **list of CRDs**);
> 3. otherwise (`--output-dir` set, no `--dry-run`, no `--checklist-only`) ⇒ **write report** (`configmap_files` / `rule_groups` / `tenants` / `total_crds`; `prometheus_rules` is a **count**).
>
> ⛔ The key names overlap but the types differ. Consumers must branch in that order — **not on `--output-dir` alone**: `--output-dir DIR --dry-run` yields the preview envelope, not the report.

**Examples**

```bash
# Basic: the checklist and the CRDs go to stdout (nothing is written)
da-tools migrate-to-operator --source-dir configmaps/

# To write files, name the directory explicitly (#1582: writing is opt-in)
da-tools migrate-to-operator --source-dir configmaps/ --output-dir ./migration-output

# Preview migration plan
da-tools migrate-to-operator --source-dir configmaps/ --dry-run

# With receiver configuration
da-tools migrate-to-operator --source-dir configmaps/ \
  --receiver-template slack --secret-name da-slack

# Checklist only
da-tools migrate-to-operator --source-dir configmaps/ --checklist-only

# JSON report mode
da-tools migrate-to-operator --source-dir configmaps/ --json
```

#### Rollback Procedures

To revert from Operator deployment back to ConfigMap mode:

**Step 1: Stop the Operator path**
```bash
# Delete PrometheusRule CRDs
kubectl delete prometheusrules -n monitoring -l app.kubernetes.io/part-of=dynamic-alerting

# Delete AlertmanagerConfig CRDs (if present)
kubectl delete alertmanagerconfigs -n monitoring -l app.kubernetes.io/part-of=dynamic-alerting
```

**Step 2: Restore ConfigMap path**
```bash
# Switch Helm values
helm upgrade threshold-exporter ./charts/threshold-exporter \
  --set rules.mode=configmap

# Verify ConfigMap rules have been reloaded
kubectl logs -n monitoring deployment/threshold-exporter | grep "SHA256"
```

**Step 3: Verify**
```bash
# Confirm alerts are firing normally
da-tools drift-detect --dirs conf.d --mode configmap
promtool query instant 'count(ALERTS{alertstate="firing"})'
```

> ⚠️ After rollback, CRD files generated by Operator remain in the `operator-manifests/` directory and can be re-applied at any time.

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

#### runtime-audit

A **read-only** hard comparison between the rules DECLARED in Git
(`rule-packs/rule-pack-*.yaml`) and the rules Prometheus has actually LOADED
(`GET /api/v1/rules`). Covers the **runtime leg** that the #711/#714 PR-time
drift gates do not.

**Use cases**: incident-time reconciliation button (run after `kubectl
port-forward` — zero new infra); scheduled / CI gate (`--ci`); reload-fail /
hand-edited-configmap / orphan-rule detection.

> **Boundary**: read-only, **no self-healing** — detect → report (exit code) →
> a human decides. Self-healing / a standing reconciliation Operator is
> explicitly rejected (avoids machine-writes-human-plane + the observer-paradox
> recursed one level up). Aligns with the #631/#643/#652 idiom. See
> [custom-rule-governance.md §7.1](custom-rule-governance.en.md).

**Syntax**

```bash
da-tools runtime-audit (--prometheus <url> | --runtime-json <file>) [options]
```

**Options**

| Option | Description | Default |
|--------|-------------|---------|
| `--prometheus <URL>` | Prometheus API URL, e.g. `http://localhost:9090` | (none) |
| `--runtime-json <FILE>` | Saved `/api/v1/rules` JSON (offline / fixture) | (none) |
| `--rule-packs-dir <DIR>` | Rule pack YAML directory | `rule-packs/` |
| `--strict-orphan` | Promote ORPHAN (stale loaded rule) to a `--ci` gate failure too | false |
| `--json` | JSON output | false |
| `--ci` | Exit 1 on drift (MISSING/UNHEALTHY; ORPHAN with `--strict-orphan`) | false |

**Finding categories**

1. **MISSING** — declared in Git but not loaded (reload fail / projected-volume lag / hand-deleted configmap)
2. **UNHEALTHY** — loaded but `health != ok` (carries `lastError`; series-observation cannot tell this apart from "metric legitimately absent")
3. **ORPHAN** — still loaded but no longer declared in Git (stale; scoped to DECLARED groups so unrelated infra rules are not flagged)

> **⚠️ Limitation (avoiding false positives)**: `declared` = **every** `rule-pack-*.yaml` in `--rule-packs-dir`. Platform packs can be selectively enabled (projected-volume `optional`); if a deployment loads only a subset, **the disabled packs are reported as MISSING**. Lever: point `--rule-packs-dir` at a directory holding only the enabled packs (the ORPHAN direction is unaffected — it is scoped to declared groups). Also: `unknown` health (the transient post-reload, not-yet-evaluated state) is **not** flagged UNHEALTHY — only a confirmed `err` is.

**Examples**

```bash
# Incident diagnosis (port-forward to 9090 first)
da-tools runtime-audit --prometheus http://localhost:9090

# CI / scheduled gate
da-tools runtime-audit --prometheus http://localhost:9090 --ci

# Offline fixture (no live cluster)
da-tools runtime-audit --runtime-json rules.json --json
```

---

#### rule-pack-split

Split Rule Packs into edge (Part 1) and central (Parts 2+3) layers for Federation Scenario B.

**Purpose**: Multi-cluster Federation scenarios; separate edge and central deployment.

**Syntax**

```bash
da-tools rule-pack-split [--rule-packs-dir <dir>] [options]
```

**Options**

| Option | Description | Default |
|--------|-------------|---------|
| `--rule-packs-dir <DIR>` | Rule Pack directory path | `rule-packs/` |
| `--output-dir <DIR>` | Output directory | `split-output/` |
| `--operator` | Output PrometheusRule CRD YAML instead | false |
| `--namespace <NS>` | Namespace for the CRDs | `monitoring` |
| `--gitops` | GitOps mode (sorted keys, reproducible output) | false |
| `--dry-run` | Write no files | false |
| `--json` | Print the report as JSON | false |

The tool only performs the Scenario B edge / central split; an option to choose the Federation scenario is not implemented yet. The output format is chosen with `--operator` / `--gitops`.

**Output Structure**

```
split-output/
├── edge-rules/       (Part 1: normalization recording rules)
│   └── rule-pack-<db>.yaml
└── central-rules/    (Parts 2+3: threshold normalization and alerts)
    └── rule-pack-<db>.yaml
```

When a group name lacks a `-normalization` / `-threshold-normalization` / `-alerts` suffix, the tool routes its rules one by one by data locality (recording rules to edge, alerts to central) and prints a WARN line.

**Examples**

```bash
# Scenario B hierarchical split
da-tools rule-pack-split --rule-packs-dir rule-packs/ --output-dir federation-split/
```

---

### Configuration Generation Tools

#### generate-routes

Generate Alertmanager route + receiver + inhibit_rules fragment (or complete ConfigMap) from tenant YAML.

**Purpose**: GitOps configuration management; auto-generate alert routing and notification receivers.

**Syntax**

```bash
da-tools generate-routes --config-dir <path> [options]
```

**Required Parameters**

| Parameter | Description |
|-----------|-------------|
| `--config-dir <PATH>` | Tenant configuration directory |

**Options**

| Option | Description | Default |
|--------|-------------|---------|
| `--output <FILE>` | Output to file. **Read only by render (default) and `--output-configmap`, and not together with `--dry-run`**; with `--validate` / `--apply` / `--dry-run` it is a caller error (exit 2), never a silent no-write (#1650) | stdout |
| `--output-configmap` | Output complete Kubernetes ConfigMap YAML | false |
| `--base-config <FILE>` | Custom Alertmanager base config. **Only `--output-configmap` reads it**; supplying it in any other mode is a caller error (exit 2), not a silent no-op | built-in default (**only when the flag is omitted**; a supplied value that is unreadable / not valid YAML / not a mapping exits 2 rather than falling back) |
| `--dry-run` | Show preview without writing. **Not read by `--validate` / `--apply`** (exit 2) | false |
| `--validate` | Validate only, don't output. Any tenant file in conf.d that cannot be parsed → exit 1, with or without `--strict` (#1460). Once every other check passes, it also assembles the complete config on the **built-in default base** and hands it to `amtool` (see "Alertmanager validation" below; #2260) | false |
| `--apply` | Apply directly to Kubernetes (requires kubectl) | false |
| `--namespace <NS>` | Namespace of the ConfigMap. **Read only by `--apply` / `--output-configmap`**; exit 2 in any other mode | `monitoring` |
| `--configmap <NAME>` | ConfigMap name. **Read only by `--apply` / `--output-configmap`**; exit 2 in any other mode | `alertmanager-config` |
| `--yes` | Skip confirmation prompt with --apply. **Read only by `--apply`**; exit 2 in any other mode | false |
| `--policy <FILE>` | **Path** to a policy YAML holding an `allowed_domains:` list (omit for no constraint). ⚠️ This takes a file path, not a comma-separated domain list; a value that cannot be read exits 2 (#1556) | (unrestricted) |

**Output**

Every mode first prints one stdout line, `Config files: N read, M skipped (<names>)` (#1460) — N / M come from the structured record, not from the stderr WARN lines; when M > 0 and a skipped file is a tenant file, the run produces nothing further.

**Fragment Mode** (no `--output-configmap`):
YAML fragment containing route, receivers, inhibit_rules.

**ConfigMap Mode** (`--output-configmap`):
Complete Kubernetes ConfigMap YAML with global, route, receivers, inhibit_rules, ready for `kubectl apply`.

**Alertmanager validation (#2219, #2260)**: when `amtool` is on PATH, `--output-configmap` and `--apply` run `amtool check-config` on the exact `alertmanager.yml` about to be written / applied, and a rejected config is neither written nor applied; without `amtool` a `NOTICE: ... was NOT validated by Alertmanager` line goes to stderr and nothing else changes. `--validate`, once every other check passes and before it prints `OK`, also assembles the generated config on the **built-in default base** (not your `--base-config` — `--validate` never reads it) and runs `amtool check-config` on that: a rejection exits 1, `amtool` failing on its own exits 2; without `amtool` a NOTICE line says so and the exit code is unchanged. A passing `--validate` therefore says nothing about your own base — run `--output-configmap --base-config` for that. Fragment mode (not a complete config) is not validated this way. The da-tools image bundles `amtool`, taken from the same Alertmanager image the deployment manifest pins (same tag and digest; #2294), so inside the image this validation runs by default. ⚠️ The v2.9.0 image does not bundle `amtool`, and its `--validate` does not go through `amtool` <!-- image-caveat: v2.9.0 -->

**Receiver names must be unique (#2279)**: the tenant id has no restricted character set, so tenant `<t>`'s `routes[0]` receiver (`tenant-<t>-route-0`) can have the same name as the main receiver of a tenant called `<t>-route-0` (likewise `-override-<n>`). When two sources generate a receiver of the same name, every mode exits 1, writes nothing and applies nothing, with or without `--strict`, and the message names both sources. With `--output-configmap --base-config`, a base receiver named like a generated receiver also exits 1 and writes nothing (otherwise the base one would replace the conf.d one); the platform's fixed `custom-alerts-firehose` / `watchdog-heartbeat` / `synthetic-receiver` / `sentinel-sinkhole` let a base definition win by design and do not count. `--apply` still replaces a same-named receiver in the cluster with this run's. ⚠️ The v2.9.0 image exits 0 in both cases <!-- image-caveat: v2.9.0 -->

**Examples**

```bash
da-tools generate-routes --config-dir ./conf.d --dry-run
da-tools generate-routes --config-dir ./conf.d -o alertmanager-routes.yaml
da-tools generate-routes --config-dir ./conf.d --output-configmap -o alertmanager-configmap.yaml
da-tools generate-routes --config-dir ./conf.d --apply --yes
```

**Exit Codes**

| Code | Description |
|------|-------------|
| `0` | Success |
| `1` | Config validation failed; **or conf.d holds a tenant file that could not be parsed / read** (bad YAML, not UTF-8, top level not a mapping, a directory named `x.yaml`) — refused in every mode, with or without `--strict`, and the file is named on stdout (#1460); **or the `amtool` on PATH rejected the config `--output-configmap` / `--apply` was about to write / apply** — nothing written, nothing applied (#2219); **or it rejected the config `--validate` assembled on the built-in base** (#2260); **or two sources generate a receiver of the same name** (every mode, with or without `--strict`), or the `--output-configmap` base has a receiver named like a generated one (#2279); **or the assembly violates a platform invariant** (e.g. a base inhibit rule that would let a tenant silence a platform alert) — reported as `FAIL:`, no longer a traceback (#2260) |
| `2` | Caller error: **the tool could not do its job because of how it was invoked or its environment** — not because your config violates something. Reaching it today (non-exhaustive): `--policy` / `--base-config` supplied but unusable (not a file, unreadable, not valid YAML, top level not a mapping); `--base-config` used in a mode other than `--output-configmap`; **`-o` / `--dry-run` / `--namespace` / `--configmap` / `--yes` used in a mode that never reads them** (the message names the flag and the mode and gives a remedy argparse accepts; #1650); the `-o` output path cannot be written; `--apply` without `--yes` where stdin cannot be read; and kubectl / cluster operations failing (#1556, #1616, #1617); `amtool` on PATH but not runnable, timed out, or failing without a rejection verdict; Alertmanager's `/-/reload` failing after `--apply` (before v2.10.0 a WARN at exit 0; #2219). ⚠️ **This row is the v2.10.0 contract**; the `v2.9.0` image pinned at the top of this page returns 0 or 1 for most of them <!-- image-caveat: v2.9.0 --> |

---

#### patch-config

ConfigMap partial update tool with preview (--diff) and direct application support.

**Purpose**: Quick threshold adjustment during operations; avoid full ConfigMap redeployment.

**Syntax**

```bash
python3 scripts/tools/ops/patch_config.py [--diff] [--json] [--exporter-namespace NS] [--exporter-selector LABELS] [--exporter-port PORT] [--reload-timeout SECONDS] [--poll-interval SECONDS] <tenant> <metric> <value>
```

Reads and patches `threshold-config` in the `monitoring` namespace; needs `kubectl` on PATH and a working kubeconfig. `<value>` is a concrete value, `default` (delete the tenant's key) or `disable`. Apply (no `--diff`) also needs `list pods` and `get pods/proxy` in the exporter's namespace (why, and what that grants: [Cross-tenant ConfigMap hardening §2.2](cross-tenant-configmap-hardening.en.md)).

**Options**

| Parameter | Description |
|-----------|-------------|
| `--diff` | Preview only, apply nothing |
| `--json` | stdout carries one JSON document: the preview under `--diff`, apply's outcome otherwise; all other output goes to stderr |
| `--exporter-namespace` | Namespace of the exporter pods apply verifies on (default from the chart) |
| `--exporter-selector` | Label selector of those pods (default from the chart) |
| `--exporter-port` | Container port name or number of their HTTP listener (default from the chart) |
| `--reload-timeout` | Deadline (seconds) that ends the wait for every pod to serve the written bytes, checked between polling passes |
| `--poll-interval` | Seconds between reads of each pod's `/api/v1/config/identity` (an older exporter's `Last reload`) meanwhile |

**Locating the tenant**: the key patched is **the one** YAML key in `threshold-config` whose `tenants:` mapping declares the tenant (whatever its name, casing or `.yml` spelling), and the patch is written back to that key. The `_defaults` key is matched in any casing, `.yaml` or `.yml`. When no key declares the tenant, a concrete value creates `<tenant>.yaml` in the multi-file layout and goes into `config.yaml` in the legacy layout. `default` is a no-op when the tenant does not set that metric (exit `0`), as is a value the target already holds as its source text; a tenant block it leaves empty is kept.

**Reading**: a scalar is its source text, never type-converted (`010` is `010`).

**Refused (exit `2`, nothing written)**: two or more keys declaring the tenant; no readable key declaring the tenant while some key cannot be read (the message names those keys); a merge key `<<` on the path being read; a ConfigMap whose `data` is not a mapping; two `_defaults` keys, or neither a `_defaults` nor a `config.yaml` key; a tenant block that is not a mapping; a key to rewrite that declares more than one tenant (except `config.yaml` in the legacy layout); a key to create that starts with `.` or `_`. ⚠️ The rewrite re-serialises the key's other values with YAML 1.1 types and drops its comments.

**Post-write verification (apply)**: new bytes equal to the key's current bytes ⇒ nothing is written or waited for, exit `0`. Otherwise it first reads, through `kubectl get --raw …/pods/<pod>:<port>/proxy/…` (GET only) on every Running pod not being deleted, the pod's [`/api/v1/config/identity`](api/README.en.md) (`config_hash`: which bytes it serves; `parse_failed`: the keys it left out because they did not parse) and `/metrics`. After the patch it computes, from the ConfigMap the patch returned, the `config_hash` an exporter serving exactly that version reports (the keys the exporter reads — not dot-prefixed, a `.yaml` / `.yml` extension in any case — in key order, each SHA-256'd, the hex digests concatenated and SHA-256'd again; for a pod in single-file mode the SHA-256 of `config.yaml`) and waits until every pod reports it; `/metrics` is then read between two identity reads and kept only if both show the same install, else read again (bounded; past the bound it counts as a timeout). It then compares every `user_*` series on each pod; parse failures are judged on `parse_failed` after the write: the patched key in it, or another key that was not in it before ⇒ failure, another key already in it before ⇒ warning only. ⇒ Success means every pod serves the whole ConfigMap at the version this write produced, byte for byte, and did not leave the patched key out as unparseable; whether those bytes then do what was meant (e.g. a non-numeric value falling back to the default, an unknown key ignored) is not covered — see `patch-config --help`. A pod whose exporter answers the identity path with 404 (an older exporter) is verified the older way (wait for `/api/v1/config` `Last reload` to change, compare `da_config_parse_failure_total`), with a note on stderr and `pods.<pod>.identity` set to `unavailable (404)` in `--json` (`checked` otherwise). For a key starting with `_` (`_silent_mode`, `_profile`, ...) the target tenant's own series are not judged, only listed on stderr (and in `--json`). A mismatch, a timeout, an unreachable pod on the way, Ctrl-C / SIGTERM or an unexpected error after the write ⇒ the old bytes are patched back (a key that did not exist is removed) and it exits non-zero; The write and the rollback are both conditional on the ConfigMap's `resourceVersion`: changed since it was read ⇒ nothing is written; the patched key changed by someone else before the rollback ⇒ no rollback; verification also requires the ConfigMap to still be at the version the write produced. If the patch call itself fails, the ConfigMap is re-read to tell whether it landed. The exact rules and known residuals: `patch-config --help`.

**`--diff`**: says only what would be written (`after`) and whether apply would write (`changed`, apply's own verdict — `false` when apply would send no patch). **It does not show the current value**: under `--json`, `before` is always `null`, and the text output prints one line saying to check the exporter instead (reading one key's current value here would mean redoing the exporter's key→series mapping in Python, which [#1950](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1950) ruled out). For `default`, `after.state` is `key removed`; what applies once the key is gone is not inferred.

**`--json`**: stdout is one JSON document on every terminal path (except when stdout is closed, or a signal ends the process before anything is written); `--json --help` exits `0` with `status: "help"`. On exit `2` it carries the preview keys with empty values plus `status: "caller_error"` and `reason` (`bad_arguments` / `configmap_shape` / `configmap_changed` / `kubectl_failed` / `unexpected_error`). Apply's document keeps the preview keys (`before` / `after` are `null`) and adds `status` (`no-op` / `applied` / `verify-failed-rolled-back` / `timeout` / `unreachable` / `rollback-failed` / `interrupted-rolled-back` / `error-rolled-back` / `state-unknown` / `overwritten-by-another-writer`), `exit_code`, `written` (the ConfigMap holds the new bytes as it exits; `null` when unknown), `rolled_back`, `message`, and `pods.<pod>` with `identity` (`checked` / `unavailable (404)`), `problems` / `warnings` / `target_changes` (the target tenant's changed series; `before` / `after` `null` means absent).

**Examples**

```bash
python3 scripts/tools/ops/patch_config.py --diff db-a mysql_connections 100
python3 scripts/tools/ops/patch_config.py --diff --json db-a mysql_connections 100 | jq .changed
python3 scripts/tools/ops/patch_config.py db-a mysql_connections 100
python3 scripts/tools/ops/patch_config.py --json db-a mysql_connections 100 | jq .status
```

**Exit Codes**

| Code | Description |
|------|-------------|
| `0` | Success (verified on every exporter pod), including a `default` or same-bytes no-op |
| `1` | Post-write verification failed (another tenant's series changed, the target tenant changed beyond the bound, a new parse failure, ...); rolled back |
| `2` | Caller error, **nothing written**: `kubectl` could not run or exited non-zero (e.g. not on PATH, cluster unreachable, ConfigMap missing, no permission), any refusal above, the ConfigMap changed after it was read (`reason: configmap_changed`; re-run), arguments argparse rejects, or an unexpected exception before the write |
| `3` | Some pod was not serving the written bytes when `--reload-timeout` ran out (its `config_hash` differs; an older exporter: no reload seen); rolled back |
| `4` | Exporter unreachable (no pod matches the selector, pods/proxy failed, an unexpected response shape): before the write nothing was written, after it the write was rolled back |
| `5` | The rollback itself failed: the ConfigMap may still hold the new bytes; fix it by hand |
| `6` | Ctrl-C / SIGTERM or an unexpected error after the write; rolled back (interrupted before the write, it ends as it normally would, nothing written) |
| `7` | What the ConfigMap now holds cannot be told (not readable, or neither version), or another writer changed the patched key after this write (not rolled back); check it by hand |

---

### Filesystem Tools

#### scaffold

Generate new tenant configuration (interactive or non-interactive).

**Purpose**: Quickly create tenant configs; supports multiple DB types and defaults.

**Syntax**

```bash
da-tools scaffold [options]
```

**Options**

| Option | Description | Default |
|--------|-------------|---------|
| `--non-interactive` | Non-interactive mode (requires --tenant, etc.) | false |
| `--tenant <NAME>` | Tenant ID | (interactive prompt) |
| `--db <LIST>` | Comma-separated DB type list | (interactive prompt) |
| `--namespaces <LIST>` | Comma-separated K8s namespace list | (interactive prompt) |
| `-o, --output-dir <DIR>` | Output directory | `scaffold_output` |

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
- `_defaults.yaml` — Platform defaults. ⚠️ **Overwritten on every run** if the directory already has one — do not point `--output-dir` at a `conf.d/` whose platform defaults you have already tuned; scaffold into a staging directory and move only the tenant file
- `scaffold-report.txt` — Summary report

**Examples**

```bash
da-tools scaffold                                     # interactive
da-tools scaffold --non-interactive --tenant db-c --db mariadb,redis
```

**Exit Codes**

| Code | Description |
|------|-------------|
| `0` | Success |
| `1` | Only an uncaught exception (traceback on stderr); "invalid input" is 2, not 1 |
| `2` | Caller error: bad arguments, an unsupported `--db` type, `--non-interactive` without `--tenant` or `--db`, or the output path given to `-o/--output-dir` cannot be written (#1641) |

---

#### migrate

Convert legacy Prometheus rules to dynamic format (AST engine).

**Purpose**: Large-scale rule migration; automate early preparation work.

**Syntax**

```bash
da-tools migrate <input_file> [options]
```

**Required Parameters**

| Parameter | Description |
|-----------|-------------|
| `<input_file>` | Input legacy rules YAML file |

**Options**

| Option | Description | Default |
|--------|-------------|---------|
| `-o, --output-dir <DIR>` | Output directory | `./migration_output/` |
| `--dry-run` | Show report only, don't generate files | false |
| `--triage` | Triage mode: output only CSV report | false |
| `--interactive` | Ask user when uncertain | false |
| `--no-prefix` | Disable custom_ prefix (not recommended) | false |
| `--no-ast` | Force old regex engine | false |

**Output**

**Standard Mode**:

- `migration_output/tenant-config.yaml` — Extracted thresholds
- `migration_output/platform-recording-rules.yaml` — Recording rules
- `migration_output/platform-alert-rules.yaml` — Alert rules. The alerts read per-tenant recording rules, so `$labels` only carries `tenant`. A label the original annotations or labels reference is replaced by its value when the original expression pins it to one value with `=` (for example `queue="order-processing"`). Any other label (for example `instance`) is rewritten to `$labels.tenant`; annotations add "（原為 instance，已依租戶聚合）", and a comment above the alert plus the report list the rewritten labels. (The v2.9.0 image does not rewrite them; those references render as empty strings.) <!-- image-caveat: v2.9.0 -->
- `migration_output/migration-report.txt` — Detailed report
- `migration_output/triage-report.csv` — Rules requiring manual review
- `migration_output/prefix-mapping.yaml` — Metric prefix mapping
- `migration_output/defaults-snippet.yaml` — a `defaults:` snippet to merge into `_defaults.yaml`. threshold-exporter only emits declared keys, so the values in `tenant-config.yaml` have no effect until it is merged. The values come from the original rules; once declared, the warning tier applies to every tenant. A critical tier paired with a warning cannot be declared in defaults: each tenant that wants it sets `<key>_critical` in its own file; a legacy rule with only a critical tier reads the base row, so its value is in the snippet (the v2.9.0 image does not produce this file yet) <!-- image-caveat: v2.9.0 -->

**Triage Mode**:

- Output only `triage-report.csv` (for manual review)

**Examples**

```bash
da-tools migrate ./my-rules.yml --dry-run
da-tools migrate ./my-rules.yml --triage
da-tools migrate ./my-rules.yml -o migration_output/
```

**Exit Codes**

| Code | Description |
|------|-------------|
| `0` | Success |
| `1` | Invalid input file |
| `2` | Caller error: bad arguments, or the output path given to `-o/--output-dir` cannot be written (#1641) |

---

#### validate-config

One-stop configuration validation: YAML format, schema, routing, policy, version consistency.

**Purpose**: CI/CD gate check; verify config completeness before deployment.

**Syntax**

```bash
da-tools validate-config --config-dir <path> [options]
```

**Required Parameters**

| Parameter | Description |
|-----------|-------------|
| `--config-dir <PATH>` | Tenant configuration directory |

**Options**

| Option | Description | Default |
|--------|-------------|---------|
| `--policy <FILE>` | **Path** to a policy YAML holding an `allowed_domains:` list (omit for no constraint). ⚠️ A supplied value that cannot be used now exits 2 instead of being skipped — not a file, unreadable, not UTF-8, not valid YAML, or not a mapping at the top level (#1556). ⚠️ The v2.9.0 image still has the old behaviour | (unrestricted) <!-- image-caveat: v2.9.0 --> |
| `--rule-packs <PATH>` | Path to the `rule-packs/` directory for the custom rule lint. ⚠️ A supplied value that cannot be used exits 2; **omitting it removes the `custom_rules` row from the report entirely** (#1556) | (check not run) |
| `--policy-dsl <FILE>` | Path to a standalone Policy-as-Code DSL file (top-level `policies:` key). ⚠️ A supplied value that cannot be used exits 2 (same five shapes as `--policy`); before the fix its output was byte-identical to passing no flag at all (#1556) | (only `_policies` in `_defaults.yaml`) |
| `--version-check` | Also run the version consistency check | false |
| `--json` | Output results as JSON (for CI consumption) | false |
| `--strict` | Escalate domain-policy (ADR-007) violations from WARN to FAIL (matches CI `generate-routes --strict`) | false |

**Checks Performed**

- YAML file usability (parses, decodes as UTF-8, mapping at top level). ⚠️ **The last two landed after v2.9.0**: on the image you have, a non-UTF-8 file or a non-mapping top level raises a traceback with zero bytes on stdout <!-- image-caveat: v2.9.0 -->
- **Quoting of string fields** (`yaml_quoting`): FAIL when a value in a field the JSON Schema types as a string (enum included) is written unquoted and PyYAML reads it as a boolean, number or null — `channel: yes` is `True` to PyYAML but the string `"yes"` to the exporter and Alertmanager, so the tools disagree about the same file. Each finding names the file, line and field path; the fix is to quote the value (`channel: "yes"`). Tenant thresholds are string fields too, so `mysql_connections: 70` is reported — write `"70"`. Tenant files are held to `tenant-config.schema.json`, `_defaults*` files to `platform-defaults.schema.json` (whose `_routing_defaults` / `_routing_enforced` follow the tenant schema's routing definitions); other `_*` files are not read. Which words are ambiguous is PyYAML's own resolver's verdict (so `y` / `n`, which PyYAML reads as strings, are not reported) and which fields are strings is the schema's (#2164). ⚠️ **Not present in the v2.9.0 image** <!-- image-caveat: v2.9.0 -->
- Schema validation (required keys, correct types). A `_routing_enforced.enabled` that is not a YAML boolean (`n`, `'yes'`, `~` …) is FAIL: the platform-enforced (NOC) route is NOT enabled, and `generate-routes --validate` fails on the same line (#2164). ⚠️ In the v2.9.0 image such a value enabled NOC routing when it was a non-empty string <!-- image-caveat: v2.9.0 -->
- Routing rule validation (group_wait/group_interval/repeat_interval in allowed range). FAIL when two sources generate a receiver of the same name, decided by the same predicate as `generate-routes --validate` (#2279). ⚠️ The v2.9.0 image reports PASS for a duplicate receiver name <!-- image-caveat: v2.9.0 -->
- Policy checks (webhook domains) — **this row only appears when `--policy` is given**
- Custom rule lint (deny-list over `rule-packs/`) — **only when `--rule-packs` is given**
- Profile references (does a tenant's `_profile` name a profile that exists)
- Version consistency — **only when `--version-check` is given**
- Policy-as-Code DSL evaluation (`_policies` in `_defaults.yaml`, or `--policy-dsl`)
- **Tenant declaration uniqueness**: FAIL when one tenant id is declared by **two files**. ⚠️ The exporter answers that state by **rejecting the entire config dir** (`DuplicateTenantError`), so the consequence is not "that one tenant loses alerting" — **every tenant in the tree does**, at deploy or restart time, after CI has passed. The usual cause is an editor leaving a `db-a.yml` beside `db-a.yaml`, but the predicate is "one id, two files": `archive/db-a.yaml` is caught the same way (#1577). ⚠️ **Not present in the v2.9.0 image**: the same tree reports `Result: PASS` / exit 0 there <!-- image-caveat: v2.9.0 -->
- **Root defaults** (`root_defaults`): checks `defaults:` in the **root** `_defaults.yaml` the way the exporter decodes it, and FAILs on two things. First, values: the exporter decodes the root `defaults:` as `map[string]float64`, so **a value that does not decode as a number** (`"70"`, `disable`, a mapping, a list, a boolean, a date …) makes it **drop the whole `defaults:` block** — every platform threshold — while the load still reports success; and **an empty value** (`k:`, `~`, `null`) decodes as **0**, a 0 threshold sent to every tenant that does not set its own value. To declare a key without a platform value, list it under `optional_overrides:` instead. The verdict is the same yaml.v3 mirror the `deprecate` carrier health check uses (#1414). Second, routing: `_routing` or any `_routing`-prefixed key under `defaults:` FAILs whatever its value. `defaults:` is for numeric thresholds; routing defaults go in the top-level `_routing_defaults:` block. ⚠️ A `_routing` mapping there costs more than routing: the exporter reads the root `defaults:` as numbers only, so it drops **the whole block — every platform threshold** — while the load still reports success; and the route generator never reads `defaults:` either. A `_defaults.yaml` in a subdirectory is not judged by this row (#2291). ⚠️ **Not present in the v2.9.0 image** <!-- image-caveat: v2.9.0 -->

⛔ **The report's own rows are authoritative** (the `Total: N checks` line). This list previously carried an item called "Tenant name consistency" and **no check does that** — measured: a file named `hotel.yaml` declaring tenant `totally-different` passes all six rows at exit 0 — while four checks that do run were missing from it. The conditional rows above do not silently pass when their flag is omitted: the row is absent.

**Output**

Validation result summary (pass/fail list).

**JSON output (`--json`)**: stdout is exactly one JSON document whose top level is an **array**; each element is one row of the report (one check). Required keys are on every row; an optional key appears only when its condition below holds, and **when it does not, the key is absent** (not an empty array or `null`) — read optional keys with `.get()` / `// empty`, never by direct indexing.

| Key | Required / optional | Type | When it appears, and what it means |
|-----|---------------------|------|------------------------------------|
| `check` | required | string | Check name (`yaml_syntax`, `schema`, `routes` …; the rows actually printed are authoritative) |
| `status` | required | string | `pass` / `warn` / `fail` (lower case) |
| `details` | required | string[] | The row's detail lines; may be empty |
| `caller_error` | required | bool | This FAIL comes from the caller (paths, environment, a prerequisite tool), not the config itself; exit code `2` is derived from it (see the exit-code table below) |
| `unusable_files` | optional | string[] | **Only on the `yaml_syntax` row**, and only when some file could not be read: the files that row names |
| `skipped_unusable_files` | optional | string[] | When some file could not be read, on **every other row that reads `--config-dir`**: this row's answer excludes these files. Rows that do not read the config tree (e.g. `versions`, `custom_rules`) do not get it |
| `skipped_nested_files` | optional | string[] | When the row's flat reader actually skipped files in subdirectories (see [Hierarchical conf.d](#hierarchical-confd) below) |
| `suggested_action` | optional | string | When `status` is not `pass`: the suggested next step |
| `docs_link` | optional | string | Always paired with `suggested_action`: URL of the relevant page |

⚠️ "Which files could not be read" lives under two keys: `unusable_files` on the `yaml_syntax` row, `skipped_unusable_files` on the other rows; on a healthy tree neither exists. The key set is pinned by `tests/shared/test_json_stdout_contract.py` — a key not listed above turns it red (#1653).

When `--config-dir` is not a directory, stdout under `--json` is still a document of the same shape: a single row with `check: "config_dir"`, `status: "fail"`, `caller_error: true`; exit code `2`, and stderr still prints `ERROR: config-dir not found: …`. ⚠️ On the v2.9.0 image this path leaves stdout empty <!-- image-caveat: v2.9.0 -->

**Examples**

```bash
da-tools validate-config --config-dir ./conf.d
da-tools validate-config --config-dir ./conf.d --policy ./policy.yaml
```

**Exit Codes**

| Code | Description |
|------|-------------|
| `0` | No FAIL (there may be WARNs — including a row that did not cover files in subdirectories, see [Hierarchical conf.d](#hierarchical-confd) below; read the `Result:` line) |
| `1` | Validation failed (one or more checks), or a file under `--config-dir` could not be read. ⚠️ An unreadable path passed on the command line (`--policy` / `--rule-packs`) is `2`, not this code |
| `2` | Caller error (arguments, paths, environment) — not a problem with your config. ⚠️ The v2.9.0 image does not distinguish this code <!-- image-caveat: v2.9.0 --> |

##### Hierarchical conf.d

**Hierarchical `conf.d/` (config files in subdirectories)**: the schema, routes, policy and Policy-as-Code rows get their tenants from a **flat** reader (top level of `--config-dir` only), while the exporter reads the whole tree. When a row's read **actually skipped** files in subdirectories, that row **does not report PASS**: a PASS becomes WARN, and whatever its status it gains a line naming the skipped files (the first 5, the rest as `(+N more)`; the full list is that row's `skipped_nested_files` in `--json`). What counts as skipped depends on the read: a flat tenant reader skips **every** config file below the top level; the lookup of the root `_defaults.yaml` (where Policy-as-Code takes `_policies` from) skips only the `_defaults.yaml` files in subdirectories. So whenever a subdirectory holds a `_defaults.yaml` (the standard ADR-017 tree), the Policy-as-Code row is WARN naming that file even with no `_policies` anywhere — "no policies" was concluded without opening it. Which rows are affected is observed at run time, not a hard-coded list; a row that answers without reaching a reader (e.g. a policy file with no `allowed_domains`) keeps its PASS. ⚠️ Not observed: a root file opened by name — `profiles` reads only the root `_profiles.yaml`. To have these rows check the files in subdirectories, run once per subdirectory with `--config-dir <subdir>`, or flatten the tree; neither reproduces the exporter's per-level `_defaults.yaml` inheritance. ⛔ **The exit code does not carry this signal**: WARN is still `0` (this repo's own conf.d has an `examples/` subdirectory) — read `Result:` or `--json`, not the exit code, to learn whether every row covered every file (#1652). ⚠️ Not in the v2.9.0 image: the same tree reports `[PASS] routes  0 routes` / `Result: PASS` there <!-- image-caveat: v2.9.0 -->

---

#### offboard

Offboard tenant configuration and related resources.

**Purpose**: Cleanup when tenant lifecycle ends.

**Syntax**

```bash
da-tools offboard <tenant> [options]
```

**Required Parameters**

| Parameter | Description |
|-----------|-------------|
| `<tenant>` | Tenant ID |

**Options**

| Option | Description | Default |
|--------|-------------|---------|
| `--config-dir <PATH>` | Tenant config directory. ⚠️ The default points at a repo-internal path that does not exist in the image — pass it explicitly | `components/threshold-exporter/config/conf.d` |
| `--execute` | **Actually perform the change** (default is pre-check / preview only, nothing is written) | false |

Without `--execute` it is already a preview; there is no separate dry-run flag.

**Output**

A pre-check report: where the tenant file is, whether any other file references the tenant, and the metrics it has set. A config file that cannot be parsed is named and fails the pre-check (the cross-reference check cannot see inside it). With `--execute` it deletes `<config-dir>/<tenant>.yaml` directly, **without a backup** (back it up yourself or rely on git), and does **not** touch Recording / Alert rules (an option to clean up rules is not implemented yet); at the end it reminds you to also remove the `tenant=<tenant>` routing from Alertmanager.

**Examples**

```bash
# Pre-check only by default (writes nothing)
da-tools offboard db-old --config-dir ./conf.d
# Actually offboard
da-tools offboard db-old --config-dir ./conf.d --execute
```

**Exit Codes**

| Code | Description |
|------|-------------|
| `0` | Success: the pre-check passed or only warned (e.g. a cross-file reference); with `--execute` the tenant file was deleted |
| `1` | The pre-check failed (a ❌ item such as the tenant file not found or a config file that cannot be parsed, named in the report), with or without `--execute`; or I/O failed (#2179) |
| `2` | Caller error: only arguments argparse rejects (missing tenant positional, unknown flag) |

---

#### deprecate

Deprecate metrics: remove `<m>`, `<m>_critical`, `custom_<m>`, `custom_<m>_critical` from `defaults:` / `optional_overrides:` / tenant files; on the tenant plane also the dimensional keys `<name>{…}` of those four names.

**Purpose**: Gradually retire old metrics; maintain version compatibility.

**Syntax**

```bash
da-tools deprecate <metric_keys...> [options]
```

**Required Parameters**

| Parameter | Description |
|-----------|-------------|
| `<metric_keys...>` | One or more metric keys (space-separated) |

**Options**

| Option | Description | Default |
|--------|-------------|---------|
| `--config-dir <PATH>` | Tenant config directory. ⚠️ The default points at a repo-internal path that does not exist in the image — pass it explicitly | `components/threshold-exporter/config/conf.d` |
| `--execute` | **Actually perform the change** (default is pre-check / preview only, nothing is written) | false |
| `--plane {root,subtree}` | Whether this `--config-dir` is the conf.d root or one subtree carrier below the exporter's `-config-dir`. `root` runs the carrier health check on every `_`-prefixed file at this level (whether the exporter can read the values under `defaults:`, judged the way yaml.v3 does); `subtree` skips it (string values are legal on the subtree plane and take effect). The tool cannot infer this, so the default is fail-closed | `root` |

**Output**

Deletes those keys from `defaults:` and `optional_overrides:` (the declared tier, names only) in `_defaults.yaml` / `.yml`, and from the non-`_`-prefixed tenant files in the flat directory, naming each removed key and its old value; an emptied `defaults:` / `optional_overrides:` is dropped entirely; a carrier holding none of them is named and left untouched. It does **not** write `disable`: `defaults:` is `map[string]float64`, so a string there makes the exporter drop the whole root carrier ([#1787](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1787)). When the carrier health check (see `--plane`) finds a file the exporter cannot read, the whole run degrades to a preview: nothing is written, rc 1; a `_`-prefixed file the tool's own pure-Python parser cannot read degrades the run the same way (the message says the exporter can read it); an empty value under `defaults:` only warns. The tool does not write into subdirectories, but the completeness rescan looks down into them: residue in a subtree's own `_defaults.yaml` or a tenant file's `tenants:` block clears by running `--plane subtree` on that subtree as the printed guidance says; the `tenants:` / `profiles:` blocks of root-level `_`-prefixed files are out of reach and any residue there has to be removed by hand; blocks the exporter does not read or drops only warn. ⚠️ NOT GUARDED: the write-back re-serialises the whole file (comments outside the header are removed, scalars in YAML 1.1 spellings change type); legacy spellings from the exporter's alias table and the value shapes of the other blocks are outside the health check (tracking entry: #1822).

**Examples**

```bash
# Deprecate several metrics
docker run --rm \
  --user $(id -u):$(id -g) \
  -v $(pwd)/conf.d:/etc/config:rw \
  ghcr.io/vencil/da-tools:v2.9.0 \
  deprecate old_metric_1 old_metric_2 \
    --config-dir /etc/config \
    --execute
```

**Exit Codes**

| Code | Description |
|------|-------------|
| `0` | Success |
| `1` | Deprecation incomplete; every cause is printed in the output. ⚠️ Preview mode reaches the same verdict on the same tree |
| `2` | Invalid config directory, or a carrier could not be written back |

---

#### lint

Check tenant-authored Prometheus rule files against the platform governance policy (deny-list).

**Purpose**: CI/CD lint check; the guard rail for Tier 3 custom rules before they reach the platform (see [Custom Rule Governance](custom-rule-governance.en.md) §4).

**Syntax**

```bash
da-tools lint <path...> [options]
```

**Required Parameters**

| Parameter | Description |
|-----------|-------------|
| `<path...>` | One or more file or directory paths |

**Options**

| Option | Description | Default |
|--------|-------------|---------|
| `--policy <FILE>` | Policy file (`custom-rule-policy.yaml`) | built-in policy |
| `--ci` | Exit 1 when there is any ERROR-level violation | false |

WARNs are never escalated to ERRORs, and JSON output is not implemented yet; in CI use `--ci`, the output is one `ERROR:` / `WARN:` line per finding.

**Checks Performed** (built-in policy defaults; override with `--policy`)

- Denied functions: `holt_winters`, `predict_linear`, `quantile_over_time`
- Denied patterns: the match-all `=~".*"` and `without(tenant)`
- Required label: `tenant`
- Range vectors at most `1h`; rule-group `interval` at most `60s`
- WARN when an `owner` or `expiry` label is missing

**Examples**

```bash
da-tools lint ./my-custom-rules.yaml
# In CI always pass --ci: without it, even ERROR-level violations exit 0
da-tools lint ./rule-packs --ci
```

**Exit Codes**

| Code | Description |
|------|-------------|
| `0` | No ERROR-level violations. ⚠️ **Without `--ci`, ERROR-level violations still exit `0`**; WARN level never affects the exit code |
| `1` | ERROR-level violations found, in `--ci` mode |
| `2` | Caller error: `--policy` was supplied but is unusable (not a file / unreadable / not valid YAML / top level not a mapping); a scan target does not exist (the message names it, #1618). ⛔ Do not go green by dropping `--policy` or the path — that silently lints against the built-in policy, or treats an unscanned target as clean |

---

#### onboard

Reverse-analyze an existing Alertmanager config, Prometheus rule files and scrape config, and write migration CSVs, suggested snippets and `onboard-hints.json`. Each of the three inputs drives one phase; at least one is required. No positional arguments are read.

**Purpose**: Incorporate existing monitoring configs; reduce manual migration work.

**Syntax**

```bash
da-tools onboard [--alertmanager-config <FILE>] [--rule-files '<GLOB>'] \
  [--scrape-config <FILE>] [options]
```

**Options**

| Option | Description | Default |
|--------|-------------|---------|
| `--alertmanager-config <FILE>` | Phase 1: Alertmanager config — the config file itself, or a ConfigMap YAML. ⚠️ The ConfigMap key must be `alertmanager.yml`; any other key (e.g. `alertmanager.yaml`, common with prometheus-operator) and Secrets fail to parse, rc=2 | — |
| `--rule-files '<GLOB>'` | Phase 2: glob of Prometheus rule files (`**` supported). ⚠️ Quote it: unquoted, the shell expands it first — several matches make only the first the value and the rest `unrecognized arguments` (rc=2); a single match exits 0 having analysed only that file (e.g. bash without globstar treats `**` as `*`) | — |
| `--scrape-config <FILE>` | Phase 3: Prometheus scrape config | — |
| `--tenant-label <NAME>` | Tenant label name | `tenant` |
| `-o, --output-dir <DIR>` | Output **directory** (a file name yields a directory of that name) | `onboard_output` |
| `--dry-run` | Analyze only, write nothing, print a per-phase summary | false |
| `--json` | Print one JSON report on stdout instead (for CI); phase files are still written, `onboard-hints.json` is not | false |

**Output**

Progress and the `Found N tenant route(s)` / `SKIP` lines go to stderr. Under `--output-dir`, depending on the inputs given:

| Path | Phase | Content |
|------|-------|---------|
| `phase1-routing/routing-summary.csv` | Phase 1 | Per tenant route: receiver type, `group_wait` / `group_interval` / `repeat_interval`, severity-dedup verdict |
| `phase1-routing/<tenant>.yaml` | Phase 1 | Routing snippet to merge into `conf.d/<tenant>.yaml` |
| `phase2-rules/migration-plan.csv` | Phase 2 | Per alert rule: metric, threshold, operator, suggested aggregation, and whether it converts automatically (`perfect` / `complex` / `unparseable`) |
| `phase2-rules/_defaults-suggestion.yaml` | Phase 2 | Defaults inferred from the rule thresholds, to merge into `conf.d/_defaults.yaml`. ⚠️ Thresholds of `complex` rules are included too — check them against `migration-plan.csv` before merging; not written when every rule is `unparseable` |
| `phase3-scrape/scrape-analysis.yaml`, `<job>-relabel-suggestion.yaml` | Phase 3 | Whether each job maps to a tenant, and suggested `relabel_configs` |
| `onboard-hints.json` | Phase 1 (plus DB types inferred in Phase 2) | Tenant list, routing hints, and DB types — ⚠️ every DB type Phase 2 infers is attached to **every** tenant (one shared union), so `scaffold --from-onboard` enables the same packs for all tenants; not written when Phase 1 finds no tenant route, or under `--dry-run` / `--json` |

**Examples**

```bash
# Alertmanager only
da-tools onboard --alertmanager-config ./alertmanager.yaml -o onboard_output

# Alertmanager plus rule files; quote the glob
da-tools onboard --alertmanager-config ./alertmanager.yaml \
  --rule-files './rules/*.yaml' -o onboard_output
```

**Exit Codes**

| Code | Description |
|------|-------------|
| `0` | At least one phase produced results (Phase 1 with no tenant route counts; it just writes no `onboard-hints.json`) |
| `1` | No phase produced results, e.g. the `--rule-files` glob matched no file, or `--scrape-config` has no `scrape_configs` |
| `2` | Caller error: none of the three inputs given, bad arguments, or the output path given to `-o/--output-dir` cannot be written (#1641); an input file cannot be read or parsed (content not UTF-8 or not valid YAML; the message names the file, #1654) |

---

#### analyze-gaps

Compare custom rules with Rule Pack, find duplicates/gaps.

**Purpose**: Evaluate Rule Pack coverage; decide if custom rule can be deleted.

**Syntax**

```bash
da-tools analyze-gaps (--tenant-config <FILE> | --config-dir <DIR>) [options]
```

**Required Parameters**

| Parameter | Description |
|-----------|-------------|
| `--tenant-config <PATH>` | Single tenant config file (use `--config-dir <DIR>` for a whole directory). ⚠️ Do not shorten it to `--config`: argparse resolves that to `--config-dir`, and a file path there answers "no custom_ metrics" with rc 0 |

**Options**

| Option | Description | Default |
|--------|-------------|---------|
| `-o, --output <FILE>` | Also write the JSON report to a file | (none) |
| `--json` | Print only JSON on stdout | false |
| `--metric-dictionary <FILE>` | Metric dictionary; exit code 2 if given but the file does not exist | `metric-dictionary.yaml` beside the tool (image) or one level up (`scripts/tools/` in the repo) |

If the dictionary is in neither default location, one `WARN` line goes to stderr and matching falls back to name prefix and token overlap (`match_type: "prefix"`, `confidence: 0.7`). ⚠️ The v2.9.0 image is not affected (the dictionary sits beside the tool), but running that version's code directly in the repo as `python3 scripts/tools/ops/analyze_rule_pack_gaps.py` finds no dictionary and does not warn; pass `--metric-dictionary scripts/tools/metric-dictionary.yaml`. <!-- image-caveat: v2.9.0 -->

**Output**

A text report grouped by Rule Pack, listing which raw metric each `custom_` metric maps to and how, e.g. `custom_mysql_global_status_threads_connected -> mysql_global_status_threads_connected (exact, 100%)`, ending with how many can move to a Rule Pack. With no `custom_` metrics in the tenant config it prints only `No custom_ metrics found in tenant configs.`. With `--json` it is an array with one entry per `custom_` metric, carrying `tenant`, `custom_metric`, `original_metric`, `current_value`, `best_match_pack`, `match_type`, `confidence`, `recommendation` and more.

**Examples**

```bash
da-tools analyze-gaps --tenant-config ./conf.d/db-a.yaml
```

**Exit Codes**

| Code | Description |
|------|-------------|
| `0` | Success |
| `2` | Caller error: bad arguments; a `--config-dir` / `--tenant-config` / `--metric-dictionary` path that does not exist (the message names the flag); the output path given to `-o/--output` cannot be written (#1641); an input file cannot be read (content not UTF-8 or not valid YAML; the message names the file, #1654). ⚠️ The v2.9.0 image returns `0` for an input path that does not exist, treating it as having no `custom_` metrics <!-- image-caveat: v2.9.0 --> |

---

#### config-diff

Compare two config directories (conf.d), output blast radius report.

**Purpose**: GitOps PR review; quickly assess config change impact scope.

**Syntax**

```bash
da-tools config-diff --old-dir <path> --new-dir <path> [options]
```

**Required Parameters**

| Parameter | Description |
|-----------|-------------|
| `--old-dir <PATH>` | Old config directory |
| `--new-dir <PATH>` | New config directory |

**Options**

| Option | Description | Default |
|--------|-------------|---------|
| `--format {markdown,json}` | Output format | `markdown` |
| `--json-output` | Same as `--format json` | false |

An option to print only the summary is not implemented yet; the report's last line is the summary (`Summary: N tenant(s) changed, N metric change(s)`).

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
da-tools config-diff --old-dir ./conf.d-old --new-dir ./conf.d-new
da-tools config-diff --old-dir ./conf.d-old --new-dir ./conf.d-new --json-output
```

**Exit Codes**

| Code | Description |
|------|-------------|
| `0` | No configuration changes |
| `1` | Changes detected — the signal CI uses to tell that this PR touched config |
| `2` | Caller error: directory missing, input unreadable, or the run did not complete |

> ⚠️ **`1` means "there are changes", not "it failed".** A CI step that calls
> this command bare will fail whenever the command is working correctly.
> A consumer-side snippet you can copy (`set +e` / `rc=$?`) is in
> [GitOps CI integration §2.3 Stage 2: Generate](scenarios/gitops-ci-integration.en.md);
> the same contract is restated in
> [GitOps deployment integration](integration/gitops-deployment.en.md).

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
| `2` | Caller error: `--policy` supplied but not a file (including the empty string) / `--config-dir` does not exist / `_defaults.yaml` or a tenant file whose content cannot be read (not UTF-8 or not valid YAML; the message names the file, #1654). ⛔ Do not go green by dropping `--policy` — that evaluates without your policy file (#1651) |

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
| `--opa-binary` | Local OPA binary path (the da-tools image has no `opa`; inside the container use `--opa-url`) | `opa` |
| `--policy-path` | Path to .rego policy file(s) | - |
| `--dry-run` | Show input JSON without calling OPA | - |
| `--json` | JSON format output | - |

**Examples**

```bash
# Evaluate via OPA REST API
da-tools opa-evaluate --config-dir conf.d/ --opa-url http://localhost:8181

# Dry-run: show OPA input JSON only
da-tools opa-evaluate --config-dir conf.d/ --dry-run
```

---

#### guard

Dangling Defaults Guard (v2.8.0). Python wrapper that shells out to the `da-guard` Go binary to validate a `conf.d/` tree across schema / routing / cardinality (routing includes domain policies).

**Usage**

```bash
da-tools guard <subcommand> [flags]
```

**Subcommands**

| Subcommand | Description |
|---|---|
| `defaults-impact` | Run deepMerge → guard checks against every tenant under conf.d/ (or the `--scope` subdirectory); emit Markdown / JSON report |
| `served-values` | Print, as JSON, the values the exporter's `/metrics` serves per tenant ([#2115](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2115)); Python readers call it through `scripts/tools/_lib_tenant_values.py` |

**Binary resolution order**

1. `--da-guard-binary <path>` (explicit override)
2. `$DA_GUARD_BINARY` env var
3. `da-guard` on `$PATH`

If none resolves, prints install hints (download from `tools/v*` release / `cd components/threshold-exporter/app && go build -o /usr/local/bin/da-guard ./cmd/da-guard`).

**`defaults-impact` key flags**

| Flag | Default | Description |
|---|---|---|
| `--config-dir <path>` | (required) | conf.d/ root |
| `--scope <path>` | whole tree | Limit to this subdirectory (CI uses dirname of changed `_defaults.yaml`) |
| `--required-fields <a,b,c>` | empty | CSV of dotted-path fields every tenant must have; a plain field is judged against the effective config, a `_routing` or `_routing.`-prefixed field against the resolved routing (see Routing checks below) |
| `--cardinality-limit <n>` | the root `_defaults.yaml`'s `max_metrics_per_tenant` (unset = 500; negative = no check) | Per-tenant predicted-metric ceiling; an explicit value overrides, `0` disables |
| `--cardinality-warn-ratio <r>` | 0.8 | Warn-tier ratio (0 < r < 1) |
| `--baseline-config-dir <path>` | empty | The same conf.d before the change (CI passes the PR's merge-base); when the root `_defaults.yaml`'s `max_metrics_per_tenant` is raised or disabled, the report opens with a notice. Never affects the exit code |
| `--format md\|json` | md | Output format |
| `--output <path>` | stdout | Write report to file (parent dir must exist) |
| `--warn-as-error` | false | Treat warnings as errors for exit code |

**Exit codes**

| Code | Meaning |
|---|---|
| 0 | clean — no error-tier findings (warnings don't block unless `--warn-as-error`) |
| 1 | guard found errors — block merge / commit |
| 2 | caller error (bad flags, path missing, scope outside root, binary missing) |
| 3 | files the exporter drops whole when it loads the tree, plus files da-guard itself cannot decode, limited to those that bear on this run (files in `--scope`, and `_`-prefixed files in the directories above it); independent of `--cardinality-limit`. The report and stderr list them (relative to `--config-dir`); a run may list only the first one, so re-run after fixing. Takes precedence over 1 and replaces the "vacuously safe" 0. The contract test `TestExitThree_NamesExactlyTheFilesTheExporterDrops` is authoritative (#2123, #2179) |

**Routing checks ([#2280](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2280))**

The routing checks look at each tenant's **resolved** routing, merged from the same three layers the route generator (`generate-routes`) uses: the root `_routing_defaults` → the routing profile named by `_routing_profile` → the tenant's own `_routing`, a shallow merge per top-level key, then `{{tenant}}` replaced with the tenant id. `_routing_enforced` takes no part. Platform files are read from the `--config-dir` root only (as the generator does), never from `--scope`. The tenant layer is what the generator reads ([#2291](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2291)): the tenant file's own `_routing` / `_routing_profile` over the same keys of the root platform files' `tenants.<id>` entry (a key the tenant file writes replaces the platform's whole). The merged effective config is **not** read, so a `_routing` in a defaults block or a threshold profile is never judged as the tenant's routing; it is reported as `routing_in_unread_location`. A `--required-fields` entry that is `_routing` or starts with `_routing.` is judged against the resolved routing too; every other field still reads the effective config. The main receiver, every `overrides` entry and every ADR-007 `routes` entry get the same shape checks, and their receiver types are judged against `_domain_policy.yaml`: `forbidden_receiver_types` and `allowed_receiver_types` are separate tests, so one receiver can break both.

| Finding kind | Severity | Trigger |
|---|---|---|
| `invalid_route_entry` | error | `routes` is not a list, or an entry the generator skips (not a mapping, an unsupported key such as `continue` / `match_re`, a missing or empty `match`, an invalid label, a value that is not a non-empty string); Field is `routes` or `routes[i]` |
| `domain_policy_violation` | error | the main receiver / `overrides[i]` / `routes[i]` type breaks a domain policy; the message names the domain, the constraint and the layer the value came from |
| `unknown_routing_profile` | warn | `_routing_profile` names a profile nothing defines (whitespace-only counts) |
| `domain_policy_unusable` | error | a `_domain_policy.yaml` structure that cannot be used (e.g. `tenants` is not a list); empty tenant, only the checks that depend on it are skipped |
| `routing_profiles_unusable` | warn | `routing_profiles:` is not a mapping; empty tenant |
| `routing_defaults_routes_ignored` | error | `_routing_defaults` carries `routes` (they belong in a profile or the tenant); both readers drop them before the merge, and the generator's `--validate` fails on it too |
| `routing_in_unread_location` | error | a `_routing` or `_routing_*` key where the generator never reads it: inside the `defaults:` block of any `_defaults.yaml` (root or nested), at the top level of a `_defaults.yaml` with no `defaults:` wrapper (top-level `_routing_defaults` / `_routing_enforced` in the ROOT one is the documented spelling and is not reported), or inside a profile of a root platform file's `profiles:`. The exporter merges it into the effective config, but no route is rendered from it. Empty tenant; Field is `<file>:<key path>` (e.g. `_profiles.yaml:profiles.p1._routing`); the message points to the root's `_routing_defaults`, `_routing_profiles.yaml`, or the tenant's own file. ⚠️ In the **root** `_defaults.yaml`, a `defaults:` block carrying `_routing*` fails the exporter's decode and the file is dropped whole, so that spelling shows up as exit 3 (`parse_failed`), not as this finding; only a nested `defaults:` block produces it. Separately, a tenant with `_routing: disable` is still reported `missing_required` under `--required-fields _routing*`, and the message says the routing is explicitly disabled |

None of these puts a file on the exit-3 list; a platform file whose syntax the exporter cannot read is still named once, by exit 3.

**`served-values`**

| Flag | Default | Description |
|---|---|---|
| `--config-dir <path>` | (required) | conf.d/ root |
| `--at <RFC3339>` | now | Instant to resolve at (schedule windows, `expires`, silence / maintenance end times all follow it) |

The values come from the exporter's own load and resolvers; the subcommand judges nothing itself. JSON output: `parse_failed` (files the exporter's load skips whole; `[]` when none) and `tenants`, each with `values` (every threshold key `/metrics` emits a row for, canonical name → value, plus the reserved keys as the exporter's resolvers read them at `--at`), `severities` (each threshold key's severity label) , `unserved` (keys of the tenant's merged config absent from `values`, switched-off ones included, value as written) and `dropped` (keys whose row `/metrics` drops because the exporter cannot build its series; the reason for each dropped row). Which rows are kept is decided by `Gather` over a private registry holding the same collectors the exporter's `/metrics` serves. Exit codes: 0 ok; 2 caller error, the exporter rejects the whole tree (e.g. a tenant declared in two files), or `Gather` fails (e.g. two keys produce one series; the exporter's `/metrics` then answers 500 as a whole), with the reason — and the keys where they can be named — on stderr; 3 a file was skipped whole — the JSON is still written and names it in `parse_failed`. Any string in the output that is not valid UTF-8 is exit 2 too (JSON cannot carry it), even where the exporter only drops that row and `/metrics` still answers 200. Exit 2 follows `Gather` over the same collectors production `/metrics` serves. Served means served to a UTF-8-negotiated scrape (the Prometheus 3 default); a scrape with legacy or underscores escaping may see labels such as `{a-b}` and `{a.b}` collide in the text output. `dropped` is keyed by the canonical spelling, `unserved` by the spelling as written.

**Examples**

```bash
# Validate the whole conf.d/ tree against a required-fields list
da-tools guard defaults-impact --config-dir conf.d/ --required-fields cpu,memory

# CI hook: limit to the subtree of the changed _defaults.yaml (ceiling read from the root _defaults.yaml)
da-tools guard defaults-impact --config-dir conf.d/ \
    --scope conf.d/db/

# JSON output for downstream PR comment poster
da-tools guard defaults-impact --config-dir conf.d/ \
    --format json --output guard-report.json

# What /metrics serves per tenant, at a given instant
da-tools guard served-values --config-dir conf.d/ --at 2026-07-01T03:00:00Z
```

**Scope simplification**: `da-guard` ships a *current-working-tree* validator (reads conf.d/ from disk as-is). Equivalent to a "delta-aware" model in CI / pre-commit flows because by the time the tool runs, the proposed change is already on disk. Speculative simulation is out of scope (handled per-tenant by the `/simulate` endpoint). The full design rationale and three-layer check explainer live in `components/threshold-exporter/README.md` (outside the MkDocs site — open from GitHub).

---

#### batch-pr

Migration Batch PR Pipeline (v2.8.0). Python wrapper that shells out to the `da-batchpr` Go binary; takes a customer's PromRule corpus through the full "emit → open PRs → review → Base merge → tenant rebase → optional data-layer hot-fix" loop.

**Usage**

```bash
da-tools batch-pr <subcommand> [flags]
```

**Subcommands**

| Subcommand | Purpose |
|---|---|
| `apply` | Open (or update) tenant chunk PRs from a Plan + profile-builder emit output. Uses Hierarchy-Aware chunking (Base Infrastructure PR + per-domain tenant chunks). |
| `refresh` | After the Base PR merges, run `git rebase --onto <merged-sha>` against every `Blocked by` tenant branch; conflicts surface in `refresh-report.md`. |
| `refresh-source` | When a parser bug fix changes the emission for a known set of source rules, rewrite the affected files in the corresponding tenant PRs and push the patch (data-layer hot-fix). |

**Binary resolution order**

1. `--da-batchpr-binary <path>` (explicit override)
2. `$DA_BATCHPR_BINARY` env var
3. `da-batchpr` on `$PATH`

If not found, prints install hints (download from a `tools/v*` release / `cd components/threshold-exporter/app && go build -o /usr/local/bin/da-batchpr ./cmd/da-batchpr`).

**`apply` flags (most common)**

| Flag | Default | Purpose |
|---|---|---|
| `--plan <path>` | (required) | Plan JSON (serialised from `BuildPlan`). |
| `--emit-dir <dir>` | (required) | profile-builder emit output directory; CLI walks it + runs AllocateFiles bucketing. |
| `--repo <owner/name>` | (required) | GitHub repo. |
| `--workdir <dir>` | (required) | Local clone (CWD for git ops). |
| `--base-branch <name>` | `main` | Branch the new PRs target. |
| `--branch-prefix <p>` | batchpr default | Custom branch prefix. |
| `--commit-author "Name <email>"` | git config | Commit author. |
| `--dry-run` | false | Run orchestration without git or GitHub API calls. |
| `--inter-call-delay-ms <n>` | 0 | Per-item delay (softens GitHub secondary rate limits). |
| `--report <path>` | `-` (stdout) | Markdown report. |
| `--result-json <path>` | empty (skip) | JSON ApplyResult; `-` = stdout, empty = skip. Skip is the default to avoid gluing markdown + JSON when `--report` also defaults to stdout. |

**`refresh` / `refresh-source` flags (most common)**

| Flag | Default | Purpose |
|---|---|---|
| `--input <path>` | `-` (stdin) | RefreshInput / RefreshSourceInput JSON. |
| `--workdir <dir>` | (required) | Local clone. |
| `--patches-dir <dir>` | (required, refresh-source only) | `<dir>/<pr-number>/<file-paths>` layout; CLI loads files into each target's Files map. |
| `--report <path>` | `-` | Markdown report. |
| `--result-json <path>` | empty (skip) | Same contract as `apply`. |

**Exit codes**

| Code | Meaning |
|---|---|
| 0 | Clean — every target succeeded or was skipped acceptably (closed/merged PR / no-change / dry-run). |
| 1 | Per-target failures, or refresh produced conflicts (a CI hook should NOT treat conflicts as a green run). |
| 2 | Caller error (bad flags, JSON parse failure, missing path, missing binary). |

**Examples**

```bash
# Open PRs: push profile-builder emit output into the customer repo
da-tools batch-pr apply \
    --plan plan.json --emit-dir ./emit/ \
    --repo vencil/customer --workdir ./customer-repo

# After Base PR merges: rebase tenant branches onto the new main HEAD
da-tools batch-pr refresh \
    --input refresh.json --workdir ./customer-repo

# Parser bug fix: re-emit the 200 affected source rules into existing tenant PRs
da-tools batch-pr refresh-source \
    --input refresh-source.json --patches-dir ./patches/ \
    --workdir ./customer-repo
```

**Honest scope (v2.8.0 v1)**: JSON-input-first is the v1 contract (machine + automation friendly); convenience flags (`--base-merged-sha N`, `--source-rule-ids id1,id2,id3`) are deferred to a future polish. The Python wrapper shells out using the same pattern as `guard`; binaries ship via `tools/v*` Releases and the da-tools docker image.

---

#### parser

MetricsQL-as-Superset PromRule parser (v2.8.0). The Python wrapper shells out to the `da-parser` Go binary, which parses customer `PrometheusRule` CRD YAML into a canonical `ParsedRule` JSON record per rule, annotated with dialect (`prom` / `metricsql` / `ambiguous`), the list of VM-only functions used, and `prom_compatible: bool` (computed via the upstream `prometheus/promql/parser`).

**Usage**

```bash
da-tools parser <subcommand> [flags]
```

**Subcommands**

| Subcommand | Description |
|---|---|
| `import` | Parse PrometheusRule YAML → JSON ParseResult; optional `--validate-strict-prom` (default on) plus `--fail-on-non-portable` / `--fail-on-ambiguous` portability gates |
| `allowlist` | Print the embedded VM-only function allowlist (text or json format); useful for independent audits and custom lints |

**Binary resolution order**

1. `--da-parser-binary <path>` (explicit override)
2. `$DA_PARSER_BINARY` environment variable
3. `da-parser` on `$PATH`

If none resolves, prints install hints (download from `tools/v*` release / `cd components/threshold-exporter/app && go build -o /usr/local/bin/da-parser ./cmd/da-parser`).

**`import` key flags**

| Flag | Default | Description |
|---|---|---|
| `--input <path>` | (required) | PrometheusRule YAML path; `-` reads from stdin |
| `--output <path>` | `-` (stdout) | JSON ParseResult output path |
| `--generated-by <stamp>` | `da-parser@<version>` | Stamped into `Provenance.GeneratedBy` (e.g. CI job ID) |
| `--validate-strict-prom` | true | Run `prometheus/promql/parser` per rule (v2.8.0 default — anti-vendor-lock-in) |
| `--fail-on-non-portable` | false | Exit 1 if any rule has `prom_compatible=false` (auto-implies `--validate-strict-prom`) |
| `--fail-on-ambiguous` | false | Exit 1 if any rule has `dialect=ambiguous` |

**`allowlist` key flags**

| Flag | Default | Description |
|---|---|---|
| `--format` | `text` | `text` (one fn per line) or `json` (includes `metricsql_version` + sorted functions array) |

**Exit codes**

| Code | Meaning |
|---|---|
| 0 | Parse OK; no portability gate failures |
| 1 | `--fail-on-non-portable` or `--fail-on-ambiguous` gate triggered |
| 2 | Caller error (bad flags, malformed YAML, missing path, binary missing) |

**Examples**

```bash
# 1. Basic import: customer PromRule CRD → JSON ParseResult
da-tools parser import --input prom-rules.yaml > parsed.json

# 2. "Pure-PromQL only" conservative path: any VM-only function fails the run
da-tools parser import --input prom-rules.yaml --fail-on-non-portable

# 3. Pipe-friendly stdin path (CI workflow + helm/kustomize render chain)
helm template ... | da-tools parser import --input -

# 4. Print the VM-only function allowlist for the pinned metricsql version
da-tools parser allowlist --format json
```

**ParsedRule schema highlights** (v2.8.0):

| Field | Type | Description |
|---|---|---|
| `dialect` | `prom` / `metricsql` / `ambiguous` | Inferred from metricsql AST + VM-only function set |
| `prom_portable` | bool | Convenience flag for dialect == prom (no VM-only functions) |
| `prom_compatible` | bool | Strict mode: `prometheus/promql/parser` also accepts the expression. Tighter than `prom_portable` |
| `vm_only_functions` | []string | Sorted list of VM-only functions used in this rule |
| `analyze_error` | string | metricsql parse error (populated when `dialect=ambiguous`) |
| `strict_prom_error` | string | `prometheus/promql/parser` error (populated when `PromCompatible=false`) |
| `source_rule_id` | string | `<source-file>#groups[i].rules[j]` — reverse-lookup key for `da-batchpr refresh --source-rule-ids` |
| `provenance` | object | `generated_by` / `source_file` / `parsed_at` / `source_checksum` (shared across the rule batch) |

**Anti-vendor-lock-in promise**: when a customer corpus passes `--fail-on-non-portable` cleanly, every rule is also evaluable on a vanilla Prometheus server. Validity rests on `vm_only_functions.yaml`'s version pin matching the metricsql dep in go.mod — the **freshness CI gate** (`vm_only_functions_freshness_test.go`) ensures upstream version bumps don't silently miss new functions.

---

#### tenant-verify

Print a tenant's effective config + `merged_hash` (v2.8.0). Supports the verification checklist in `docs/scenarios/incremental-migration-playbook.md` §Emergency Rollback Procedures item 6: after a rollback wave, every tenant's `merged_hash` must return to the pre-Base-PR snapshot. Reuses `describe_tenant.py`'s `ConfDScanner` for inheritance + canonical-hash; this tool is a thin CLI ergonomics layer (terse output + exit code).

```bash
# Print effective config + merged_hash for one tenant
da-tools tenant-verify db-fin-a --conf-d conf.d/

# Snapshot pre-Base-PR state (save before the rollback wave)
da-tools tenant-verify --all --conf-d conf.d/ --json > pre-base.json

# Compare after rollback: exit 0 = pass, exit 2 = mismatch
da-tools tenant-verify db-fin-a --conf-d conf.d/ \
    --expect-merged-hash 0123456789abcdef
```

**Flags**

| Flag | Default | Description |
|---|---|---|
| `<tenant-id>` | — | Required unless `--all` is given |
| `--all` | false | Verify every tenant under conf.d/ |
| `--conf-d <PATH>` | `conf.d` | Path to the conf.d/ directory |
| `--expect-merged-hash <H>` | empty | Compare actual `merged_hash` against this value; exits 2 on mismatch |
| `--json` | false | Emit JSON (for piping to jq diff) |

**Exit codes**

| Code | Meaning |
|---|---|
| 0 | Tenant exists and is declared by exactly one file; if `--expect-merged-hash` supplied, it matched. `--all`: no tenant is declared more than once |
| 1 | Usage / IO error (missing tenant_id, conf-d not found, `--all` + `--expect-*` mutually exclusive, etc.) |
| 2 | Tenant not found, `--expect-merged-hash` mismatch, OR **duplicate declaration** (the same tenant in two or more files) (this is the incremental migration playbook checklist item 6 stop-signal). `--all`: any tenant is declared more than once |

**Duplicate declaration** ([#2093](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2093)): when one tenant is declared by several files, the tool computes no hash — the scanner keeps just one of them, chosen by filename order, so hashing it would let item 6 pass falsely whenever the stray file sorts first. Single-tenant mode (with or without `--expect-merged-hash`) exits 2 with JSON `{"tenant_id": ..., "error": "duplicate", "files": [...], "detail": ...}` (`files` sorted, conf.d-relative paths); the human output lists each file as `declared in: <file>`. `--all` reports that tenant as an error entry of the same shape (no `merged_hash`), still reports every other tenant, and exits 2; the human `# total:` line counts verified and duplicate-declared (not verified) tenants separately. Fix: delete the extra declaration so the tenant lives in exactly one file, then re-run (`validate-config`'s `tenant_uniqueness` reports the same state).

---

#### threshold-recommend

Threshold recommendation engine — recommends optimal thresholds based on historical Prometheus P50/P95/P99 percentiles, with Noise Score integration for direction adjustment.

**Usage**

```bash
da-tools threshold-recommend --config-dir <PATH> [--prometheus <URL>] [--tenant <NAME>] [--lookback <DURATION>] [--min-samples <N>] [--dry-run] [--json] [--markdown] [--export-patch]
da-tools threshold-recommend --generate-observed-map
```

**Parameters**

| Parameter | Description | Default |
|-----------|-------------|---------|
| `--config-dir` | Path to conf.d/ directory (required except with `--generate-observed-map`) | - |
| `--prometheus` | Prometheus Query API URL | `$PROMETHEUS_URL` or `http://localhost:9090` |
| `--tenant` | Analyze only this tenant (omit for all) | all |
| `--lookback` | Historical data lookback period | `7d` |
| `--min-samples` | Minimum sample count threshold (below = LOW confidence) | `100` |
| `--dry-run` | Show PromQL queries without executing | - |
| `--json` | JSON output | - |
| `--markdown` | Markdown table output | - |
| `--export-patch` | Output an applyable conf.d override fragment (#720 STAGE-1); only keys with \|delta\|≥5% and a mapping | - |
| `--generate-observed-map` | Regenerate the observed-map from rule-packs (#719); does not need `--config-dir` | - |

> **#720 STAGE-1 (`--export-patch`)**: emits a `tenants:`-rooted conf.d override (only keys with an actionable recommendation; within-margin / skipped keys are listed as comments). The operator reviews it, merges it into the matching `conf.d/<tenant>.yaml`, and opens a PR → the existing `backtest.yaml` CI posts the old-vs-new firing-count risk report (the STAGE-1 value basis). The tool does **not** edit conf.d in place (the heavier in-place ruamel round-trip is deferred — #721).
>
> **#719 data source**: recommendations come from the observed recording rule each threshold key is actually compared against in its rule-pack alert (via `scripts/tools/ops/metric_observed_map.yaml`), NOT the configured `user_threshold`. Keys that are unmapped / version-aware / pending manual resolution are fail-loud skipped with a reason. The observed-map is produced by `--generate-observed-map` and guarded by a CI drift-check. Regeneration is **merge-preserve** (#916): a still-valid human-resolved `observed_series` survives rule-pack edits (an invalidated pick falls back to needs_review; a removed key is dropped), and the summary reports preserved/demoted/dropped counts with details on stderr WARN.
>
> **#916 lower-bound (`<`) three states**: a lower-bound threshold (a hit-ratio / availability **floor**, where rot means LOWERING the floor) is no longer blanket-skipped but classified by code-level allowlists into three states:
> - **`percentile-lower`** (recommendable): an opted-in floor key (e.g. `db2_bufferpool_hit_ratio`) runs a dedicated **P5 floor engine** — bucket by complete UTC day, take the daily-P5, and `min(daily-median, pooled)` to resist contamination; 4-dp `ROUND_FLOOR` (floor so rounding never loosens); rot is measured in **miss-rate (complement space `1-value`)**. Guardrails in order: **relaxation (lowering the floor) is always `force_manual` — no magnitude exemption** → clamp (never tighten tolerated miss-rate below 25% of current) → a tighten within a 10%-miss margin is no-change. Thin samples or a current/candidate outside (0,1) are `force_manual` too.
> - **`not-applicable`** (by-design skip): expected-value invariants / topology floors / integer-count floors (e.g. `kafka_active_controllers` / `kafka_broker_count` / `rabbitmq_consumers`) — a percentile recommendation is meaningless, so governance treats it as complete and reports an INFO count.
> - **`needs_review`** (unclassified): a `<` key in neither allowlist, fail-loud pending manual classification.
>
> The mode is authority-guarded by the drift-check: a hand-edited map that stamps `percentile-lower` / `not-applicable` onto a non-allowlisted key is a hard CI error. **Lower-bound deltas are miss-rate** — the export-patch / governance tables tag them `+X% miss` so the direction is never misread. An estimator-divergence gate (pooled-P5's miss-rate ≥ 1.5× the daily-median-P5's) blocks the false auto-tighten when a recurring trough sits ABOVE current (otherwise a weekly false page) and defers it to a human.
>
> **Lower-bound lookback guidance**: a percentile-lower key needs **≥5 full UTC days**; the default `--lookback 7d` leaves only ~6 full days after dropping the two partial boundaries — marginal and fragile to scrape gaps, so **use `--lookback 14d` for lower-bound keys** (with fewer than 5 full days the engine `force_manual`s fail-loud rather than emitting garbage).

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

# JSON output
da-tools threshold-recommend --config-dir conf.d/ --prometheus http://prometheus:9090 --json
```

---

#### threshold-govern

Threshold governance loop (Renovate-for-thresholds, #656) — turns `threshold-recommend`'s output into an **active loop**: gate on rot magnitude, then open one approvable per-tenant proposed-PR via the tenant-api (single-writer, ADR-011/023) instead of a nudge. **Dry-run by default** — pass `--apply` to actually open PRs.

**Usage**

```bash
# Dry-run (default): print which tenants would get a PR; no writes
da-tools threshold-govern --config-dir <PATH> [--prometheus <URL>] [--min-delta-pct <N>] [--json]

# Actually open per-tenant governance PRs
da-tools threshold-govern --config-dir <PATH> --prometheus <URL> --apply \
  --tenant-api-url <URL> --identity-groups <RBAC_GROUP>
```

**Parameters**

| Parameter | Description | Default |
|-----------|-------------|---------|
| `--config-dir` | conf.d/ directory path (required) | - |
| `--prometheus` | Prometheus Query API URL | `$PROMETHEUS_URL` or `http://localhost:9090` |
| `--tenant` | Analyze only this tenant | all |
| `--lookback` | Historical lookback period | `7d` |
| `--min-samples` | Minimum sample count | `100` |
| `--min-delta-pct` | Governance gate: only open a PR when \|delta%\| >= this | `25` |
| `--max-prs` | Max PRs opened per run (anti-flood / alert-fatigue budget) | `10` |
| `--apply` | Actually open PRs via tenant-api (default: dry-run, no writes) | - |
| `--tenant-api-url` | tenant-api base URL (required with `--apply`) | `$TENANT_API_URL` |
| `--identity-email` | X-Forwarded-Email (PR git author / governance identity) | `threshold-governance@platform.local` |
| `--identity-groups` | X-Forwarded-Groups (an RBAC group with write perm; required for direct `--apply`) | `$DA_GOVERN_GROUPS` |
| `--auth-token` | Bearer token (oauth2-proxy mode; or `$DA_GOVERN_TOKEN`) | - |
| `--auth-token-file` | Path to a Bearer token file (K8s audience-bound projected SA token, read at call time; or `$DA_GOVERN_TOKEN_FILE`) | - |
| `--throttle-seconds` | Seconds to pause between opened PRs | `2` |
| `--json` | JSON output | - |

> **Gate**: only recommendations with \|delta\| >= `--min-delta-pct` AND confidence ∈ {HIGH, MEDIUM} (never act on a thin sample — anti-broken-window), and **lower-bound `force_manual` is excluded** (a floor recommendation that would relax / is out-of-domain / rests on a thin sample never auto-opens a PR). **Dedup**: the tenant-api returns 409 when a PR for that tenant is already open → treated as already-in-flight and skipped, so re-runs don't spam duplicates. **Channel isolation**: the PUT carries `X-DA-Write-Source: threshold-governance` → the PR gets its own label / title / source instead of masquerading as a tenant-manager UI edit, and never touches the alert plane. The read-modify-write surgically replaces only the recommended value lines (comments preserved → clean PR diff). Recommendation logic / data source are reused verbatim from `threshold-recommend` (Day-N observed recording rule, #719).
>
> **#916 lower-bound integration**: a `percentile-lower` tighten recommendation (raising the floor) opens a PR through the gate as usual; but `force_manual` (relax / domain / sample / estimator divergence) is listed in a separate **manual-review section** (with its `guardrail_reason` + miss delta) and `not-applicable` is an **INFO** line (governance complete) — both counted in the summary, with `force_manual` / `not_applicable` arrays and counts in the JSON output. The governance table also tags lower-bound changes with `+X% miss`.
>
> **`--min-delta-pct` space asymmetry**: this knob compares a **value-space** delta for upper-bound keys but a **miss-rate-space** delta for lower-bound keys — the same number is far more sensitive in one than the other (25% miss on a 0.95 floor ≈ 1.3% value). The `5.0` floor on `--min-delta-pct` is the upper-bound 5% value margin (the lower-bound no-change margin is 10% miss), so it is a necessary-not-sufficient bound for lower-bound keys. Pair lower-bound keys with `--lookback 14d` (and a weekly govern cadence for lower-bound coverage).

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
| `2` | Caller error: a file under `--config-dir` cannot be read (content not UTF-8 or not valid YAML; the message names the file, #1654) |

#### explain-route

Routing merge pipeline debugger — shows the four-layer routing merge expansion per tenant (ADR-007): `_routing_defaults` → `routing_profiles` → tenant `_routing` → `_routing_enforced`.

`overrides` and `routes` are not listed in the final merged result but under "Effective sub-routes" after it: every sub-route and receiver the generator actually renders, in match order (`overrides` → `routes`), plus the entries the generator skipped, each with its reason ([#2245](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2245)). In `--json`, each tenant gains `sub_routes` and `skipped_sub_routes`; `final` is still the raw merged config.

**Usage**

```bash
da-tools explain-route --config-dir <PATH> [--tenant <NAME>...] [--show-profile-expansion] [--json]
da-tools explain-route --config-dir <PATH> --tenant <NAME> --trace [--alertname <NAME>] [--severity <LEVEL>] [--label <KEY=VALUE>...] [--json]
```

**Parameters**

| Parameter | Description | Default |
|-----------|-------------|---------|
| `--config-dir` | Config directory path | (required) |
| `--tenant` | Show only specified tenant(s) (repeatable) | (all) |
| `--show-profile-expansion` | Show all routing profile expansions and references | `false` |
| `--trace` | Trace mode: simulate one alert's routing path (requires `--tenant`) | `false` |
| `--alertname` | Alert name to trace (with `--trace`) | `GenericAlert` |
| `--severity` | Alert severity to trace (with `--trace`) | `warning` |
| `--label` | Extra alert label for the trace, as `KEY=VALUE` (repeatable; read only with `--trace`) | (none) |
| `--json` | Output in JSON format | `false` |

The `--trace` alert labels are built from `--alertname`, `--severity`, `--tenant` and `--label`; an `overrides` `metric_group` or a `routes` `match` key can only be supplied through `--label`, otherwise the trace always lands on the main receiver. `--label` splits on the first `=` (the value may contain `=` and may be empty); a missing `=`, a key that is not a valid label name, a key of `alertname` / `severity` / `tenant` (use the matching flag instead), a repeated key, or `--label` without `--trace` are all rejected with exit code `2` ([#2264](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2264)).

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

# Trace which receiver an alert carrying metric_group lands on (pass an overrides metric_group or a routes match key via --label)
da-tools explain-route --config-dir conf.d/ --tenant demo-tenant --trace --alertname HighConnectionCount --label metric_group=connections
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
| `2` | Caller error: bad arguments, or the output path given to `-o/--output` cannot be written (#1641) |

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
          image: ghcr.io/vencil/da-tools:v2.9.0
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

**A**: Point `--mapping` at the `prefix-mapping.yaml` generated by `da-tools migrate` (CSV is not accepted). See [validate](#validate) command documentation.

### Q: What's the difference between blind-spot and analyze-gaps?

**A**:
- **blind-spot**: Cross-reference "cluster infrastructure vs tenant config", find exporters without tenant config (blind spots).
- **analyze-gaps**: Cross-reference "custom rules vs Rule Pack", evaluate Rule Pack coverage.

They're complementary; run both after migration.

### Q: How to safely execute cutover?

**A**: `cutover` applies only to the migrate / shadow flow (see [Shadow Monitoring Cutover](scenarios/shadow-monitoring-cutover.en.md)):
1. Run `validate_migration --watch --auto-detect-convergence`, which writes `cutover-readiness.json` once the values converge
2. Run `da-tools shadow-verify convergence --readiness-json <file>` to confirm convergence
3. Run `da-tools cutover --readiness-json <file> --tenant <tenant> --dry-run` to preview the `kubectl` commands it would run
4. Drop `--dry-run` to execute the cutover
5. Run `da-tools batch-diagnose` for each tenant's health (`diagnose` only checks MariaDB Pods; confirm other tenant types with `check-alert`)

There is no rollback option; if it fails, roll back by hand per `shadow-monitoring-sop.md` §7.2.

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
