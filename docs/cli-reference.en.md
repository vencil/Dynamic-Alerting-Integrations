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
| The subcommand raises an uncaught exception (traceback) | `2` | Internal error (since [#3090](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3090); was the interpreter's default `1`, colliding with "findings"). stderr carries the `Traceback`, and its last line is `ERROR: unexpected internal error (<exception type>): <message>`; stdout the tool wrote before the crash is kept. With `--json`, stdout also carries one envelope: `"status": "caller_error"`, `"reason": "internal_error"` and `"error": "<the same ERROR line>"`, plus the keys of the tool's early-exit report (zeroed) where it has one ([#3094](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3094)). The envelope is always a JSON object, even for tools whose normal output is an array (`validate-config`, `analyze-gaps`, …); `threshold-recommend --export-patch` writes YAML to stdout and gets no envelope. There is none when the crash happens before the arguments are parsed or after the tool already wrote to stdout; where `config-diff` and `operator-generate` catch the exception themselves, the code is still `2` and the envelope's `reason` is `compare_failed` / `generate_failed` respectively ([#3115](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3115); stdout used to be empty); `tenant-verify` prints no such envelope. The dispatcher only catches `Exception`: a tool's own `sys.exit(N)` and Ctrl-C still pass through unchanged. The one exception is `tenant-verify`: its `2` means "verification failed", so its crash stays `1` (see that section) |
| The subcommand exits `2` (caller error) with `--json` | `2` | stdout carries exactly one envelope: `"status": "caller_error"`, `"reason"` naming the cause (`served_values_unavailable` da-guard missing or failing, `yaml_file_unreadable` an input YAML that cannot be read, `output_unwritable` an output that cannot be written, `observed_map_unavailable` the observed-map is unusable; `caller_error` where the tool names none), and `"error"` the `ERROR:` line on stderr (where no cause is named, the last stderr line starting with `ERROR:`, else the last line; line breaks and tabs fold to one space, control and format characters become `?`), plus the keys of the tool's early-exit report (zeroed) where it has one. Nothing is added when the tool already printed its own document ([#3115](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3115); many of these paths used to leave stdout empty). Arguments argparse rejects still get no envelope; neither does `threshold-recommend --export-patch` without `--generate-observed-map`, whose stdout is a YAML fragment, so a caller error leaves stdout empty |
| The reader closed the pipe (`… \| head -c1`, `… \| true`), fd 1 was closed during the run, or stdout cannot be written | `2` | When a write to stdout fails (mid-run or at the final flush), the tool ends at `2` whatever code it was about to exit with — a crash included, and also when the tool exits with a named reason while handling that error, unless it passes its own exit code; another error raised while handling it is still a crash or the tool's own reason — with one stderr line `ERROR: stdout was closed before all output was written`, or `ERROR: cannot write to stdout: <reason>` when it is not the reader leaving (a full disk, `/dev/full`). This used to be the interpreter's own `120`, and a large write was reported as `unexpected internal error (BrokenPipeError)` ([#3124](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3124)). Only stdout's own write failure counts: the same error from a socket, another file or stderr is still a crash or the tool's own reason. Output fully sent before the reader closed keeps its exit code. With `PYTHONUNBUFFERED=1` only write failures that reach the tool are caught; a failure the tool swallows itself (such as `--help`) is not, and the exit code stays. `tenant-verify`: `1`. A `BrokenPipeError` that is not stdout's (a socket) is still a crash. A stdout the shell closed before the tool started (`>&-`) is not covered: the output is dropped, the exit code is the tool's own, and there is no envelope |

The exit-code table in each command section lists the main triggers and is **not exhaustive**: when bad input makes a tool raise an uncaught exception, a Python tool ends at the `2` in the row above (`tenant-verify`: `1`), and a Go tool (`guard`, `batch-pr`, `parser`) panics at `2` too; tables do not list these one by one. The same holds when a script under `scripts/tools/` is run directly rather than through `da-tools`: as long as it loads `_lib_exitcodes` (most tools do, indirectly via `_lib_io` / `_lib_python`), a crash after that is `2`; an error while importing before it (e.g. PyYAML missing) is still `1`. An interactive session (`python -i`) is not affected.

> ⚠️ The first two rows used to return `1`, colliding with the per-subcommand "findings" `1` — `docker run … da-tools:<moving tag> <subcommand>` looked, in CI, exactly like "the tool ran fine and found something" (with an empty report on stdout) whenever a subcommand was renamed or the image did not ship it yet. Consumers should treat `2` as "the tool did not run" and never fold it into `1`.

#### conf.d refusal

**A path on the conf.d nothing can be read from: one refusal shared by the conf.d tools** ([#3206](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3206), [ADR-036](adr/036-single-parser-effective-config.en.md) §7 step 9): a tool that reads `--config-dir` (or `--conf-d`, `--sources`, or an existing conf.d at `--output`) first takes the run's whole-tree index (`da-guard parse-tree`), before it reads content or writes anything. When any path is one `da-guard` validation names (`verdict` `unservable`: the exporter cannot read or decode it — a YAML syntax error, several documents in one file, a dangling or looping symlink, a FIFO, a directory that cannot be listed; or `tree_refused`: the route generator refuses the whole tree on it — a symlink to a directory, a directory with a config file's name such as `x.yaml/`), the tool prints one `ERROR: <path>: <reason>` line per path on stderr (**all of them, none elided**) and a last line naming the tree, then exits `2` and writes nothing; with `--json`, stdout holds one envelope with `reason` `conf_d_not_servable` and `refused_paths`, every named path with its verdict (`[{"path": …, "verdict": "unservable" | "tree_refused"}, …]`, all of them, as on stderr). `tenant-verify` exits with its input-error code `1` (its `2` means a verification mismatch). Several tools used to print a `WARN` / `NOTE` for such paths and exit `0` (`configmap-assemble`, `assemble_config_dir`, `init`, `offboard`, `deprecate`, `migrate_conf_d` on `x.yaml/`, …); they all refuse now. The exceptions are the tools whose verdict on the tree is their output: `validate-config` (exit `1`), `generate-routes`, `check_routing_profiles`, `check_confd_schema` and `config-diff` name such paths themselves and fail (see their sections); `da-tools guard` itself still exits `3` / `4`. `assemble_config_dir --validate` was removed: every file it called an ERROR (a top level that is not a mapping, a parse error) is now stopped by this refusal before assembly, and passing the flag is a usage error (exit 2). A tool that writes into an output directory that already exists — `scaffold`, `onboard` and `migrate` (`-o`, every mode, `--triage` and `--scrape-config` alone included) and `da-assembler` (`--config-dir`) — takes the same refusal on that directory before its first write; a directory that does not exist yet, an empty one, or one holding only files no reader reads is written as before. ⛔ It judges the output directory AS the tree: a directory inside a larger conf.d (`-o conf.d/sub`) is judged as its own tree, and what lies beside it is not looked at — pass the conf.d root to have the whole conf.d judged. The `da-assembler` controller judges the tree once per reconcile cycle and, when refused, logs every path as `ERROR`, skips the cycle's writes and deletes, and keeps running (`--once` exits `2`); ⚠️ a watch event skipped during a refused cycle is not replayed, and a watch ended by `410 Gone` is not re-listed ([#3318](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3318)). ⚠️ Not on this refusal yet, moving in the next step (β2, #3206): `config_history`, `drift_detect` and `run_chaos_soak` (they still list conf.d files themselves); `migrate_to_operator` (it filters ConfigMap dump files by name).

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
| `backtest` | Backtest PR threshold changes against historical data | `--config-dir` + `--baseline` or `--tenant` + `--metric` + `--old-value`/`--new-value` |
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
| `migration-audit` | Migration audit: read a conf.d with PyYAML and with `da-guard parse-tree`, list where they differ (ADR-036 §8) | `--config-dir <dir>` |

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
| `generator-service` | Generator container service: judges whether conf.d with writes applied passes `generate-routes --validate --strict`, over a Unix socket (ADR-036 §3) | `--socket <path> --repo <dir> --config-subdir <path> --work-dir <dir>` |

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
| `explain-keys` | Per key: source, readers, verdict and what a `disable` does; `--assert` checks it (ADR-038 decision 9) | `--config-dir <dir>` |
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

**Options**

| Option | Description | Default |
|--------|-------------|---------|
| `--prometheus <URL>` | Prometheus Query API URL. The `da-tools` image fills it in from `$PROMETHEUS_URL` when the flag is not given | `$PROMETHEUS_URL`, else `http://localhost:9090` |
| `--json` | JSON output. Output is always JSON; the flag is accepted for compatibility | true |

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
| `1` | Only an uncaught exception (traceback) returns 1 — the command has no violation exit (inactive / pending / firing are all 0) | <!-- datools-cmd-ignore: only a traceback gives 1, no exit to trace -->
| `2` | Caller error: Prometheus API unreachable, errored or answered in the wrong shape (stdout carries an `{"error": ...}` JSON), or arguments argparse rejects. A wrong shape is no `data`, `alerts` not an array, or a label value that is not a string; no `data` used to be reported as `inactive` at rc 0 ([#3133](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3133)) |

---

#### diagnose

Health check for a single tenant: the MariaDB Pod's status, the exporter's `mysql_up`, the operational mode (maintenance / silent), and, when `--config-dir` is given, the profile and inheritance chain.

When `--config-dir` is given and that conf.d holds a path da-guard validation names, this tool refuses with `2` before any check, each path named (the [shared refusal](#confd-refusal), [#3206](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3206)).

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
| `--prometheus <URL>` | Prometheus Query API URL. The `da-tools` image fills it in from `$PROMETHEUS_URL` when the flag is not given | `$PROMETHEUS_URL`, else `http://localhost:9090` |

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

When an issue was found but the `kubectl logs` call that fetches the logs does not answer in time, `recent_logs` is empty and an extra `recent_logs_error` says why; the exit code follows the issue itself ([#2968](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2968)). A tenant in maintenance or silent mode gets an extra `operational_mode`, which is `unknown` when a mode query was not answered ([#3133](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3133)); with `--config-dir` you also get `profile` (when set) and `inheritance_chain`. When the Pod and exporter checks are skipped there is an extra `skipped`:

```json
{"status": "unchecked", "tenant": "db-b", "skipped": [{"check": "pod", "reason": "db_type=postgresql; the Pod and exporter checks are written for MariaDB (app=mariadb, mysql_up)"}, {"check": "exporter", "reason": "db_type=postgresql; the Pod and exporter checks are written for MariaDB (app=mariadb, mysql_up)"}]}
```

**Inheritance chain (`--config-dir`)**: whether the tenant exists, the profile it is bound to and the layer each key comes from are `da-guard effective`'s answer (tenant-api's `/effective`), and every value is what the exporter serves (`da-guard served-values`, i.e. `/metrics`), no longer a Python re-reading of the YAML ([#2526](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2526), [#2547](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2547)). The whole tree is read (tenants in sub-directories and every level's `_defaults.yaml` count). `--show-inheritance`'s `resolved` holds the values `/metrics` serves (a key it serves no row for, e.g. switched off or not declared at the root, shows its `/effective` value); `chain` is where each value was written and what was written. When the two differ (e.g. an unparseable `2024-02-30` served as the default, an expired `expires:`) the output does not say why; `da-guard served-values`' stderr does. Each `chain` layer lists only the keys it wins with (an overridden default is not listed), and a root platform file's `tenants:` entry is a layer of its own, `platform`. `profile_name` is the profile the exporter actually binds: `null` for a reference to a profile that does not exist, said on stderr. `declared` is still the root `_defaults.yaml`'s `optional_overrides:` (names as written). A tenant not in the tree prints `{"error": ...}` with a line on stderr, exit 0. Needs da-guard (shipped in the image; `make da-guard-build` in a checkout); when it is missing, a file the exporter drops or cannot read, or da-guard fails (served-values refusing the tree, or the two outputs disagreeing on the tenants, included), no partial answer is given: exit 2 with da-guard's stderr. ⚠️ The v2.9.0 image still reads conf.d in Python and needs no da-guard. <!-- image-caveat: v2.9.0 -->

**Examples**

```bash
da-tools diagnose db-a --config-dir ./conf.d
```

**Exit Codes**

| Code | Description |
|------|-------------|
| `0` | `status: healthy`, or `status: unchecked` (the Pod and exporter checks were skipped and nothing else was wrong) |
| `1` | `status: error`: the Pod is missing or not Running, or `mysql_up` is not 1 |
| `2` | Caller error: bad arguments (missing tenant, or `--show-inheritance` without `--config-dir`); a failed Prometheus query, the maintenance / silent mode queries included (those used to read as `normal` at exit code `0`; `operational_mode` is then `unknown`, [#3133](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3133)) (the output is still the `status: error` JSON, `issues` contains `Prometheus query failed`, and the code is 2 even alongside other issues); the Pod check needs to run but `kubectl` is not in the environment, or `kubectl` does not answer in time (120s limit per call; either way one line on stderr, [#2968](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2968)); or, with `--config-dir`, da-guard is missing, a file the exporter drops or cannot read, or da-guard fails (one `ERROR:` line plus da-guard's stderr). What stdout carries depends on the case: a missing tenant is rejected by argparse and leaves stdout empty; a failed Prometheus query prints the `status: error` JSON above; the rest (`--show-inheritance` without `--config-dir`, `kubectl` missing or not answering, the da-guard ones) print one `"status": "caller_error"` envelope, with `reason` `served_values_unavailable` for the da-guard ones and `caller_error` for the rest ([#3115](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3115); stdout used to be empty) |

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
| `--timeout <SEC>` | Deadline in seconds for one tenant's check: a tenant still being checked then is recorded as `error` (`timed out after …s`) and the batch stops waiting for it. Must be above 0, fractions allowed. The default outlasts diagnose's up to four Prometheus queries (10s limit each), so a stuck Prometheus is still reported as such ([#2954](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2954)). The deadline reaches every call inside the check: `kubectl` is stopped slightly before it (`min(0.5s, --timeout/4)`), so no process is left behind after the batch ends (not guaranteed with a very short `--timeout`, below about 0.5s, or on a heavily loaded machine), and past the deadline no `kubectl` is started and no Prometheus query is sent ([#2968](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2968)) | `60` |
| `-o, --output <FILE>` | Also write the JSON report to a file; the report still goes to stdout | (none) |
| `--json` | Print only JSON on stdout, no text report | false |
| `--dry-run` | Only list tenants, don't run checks | false |
| `--namespace <NS>` | K8s namespace (for auto-discover) | `monitoring` |
| `--prometheus <URL>` | Prometheus Query API URL. The `da-tools` image fills it in from `$PROMETHEUS_URL` when the flag is not given | `$PROMETHEUS_URL`, else `http://localhost:9090` |

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
| `0` | Every tenant is `healthy` or `unchecked` (including `--dry-run`) |
| `1` | One or more tenant checks failed (see each tenant's `status` and `issues` in the output) |
| `2` | Caller error: bad arguments (including `--workers` below 1 and `--timeout` not above 0), a failed Prometheus query, a missing `kubectl`, a `kubectl` that does not answer in time, or a check not finished within `--timeout` for any tenant (takes precedence over `1`, as `diagnose` alone does), or the output path given to `-o/--output` cannot be written (#1641) |

⚠️ The v2.9.0 image does not have these exit codes yet: a failed tenant check and a failed Prometheus query both return `0`. On v2.9.0, read each tenant's `status` in the output. <!-- image-caveat: v2.9.0 -->

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
| `--duration <SEC>` | Observation duration in whole seconds above 0 (`0`, a negative value or a fraction exits `2`; less than `--interval` takes no sample at all and exits `2` too, `--dry-run` included) | `600` |
| `--interval <SEC>` | Sampling interval in whole seconds above 0 (`0`, a negative value or a fraction exits `2`) | `15` |
| `--metrics <LIST>` | Comma-separated metric list (empty=all) | (all) |
| `-o, --output-dir <DIR>` | Output directory; both CSVs are written there (see Output below), created if missing | `baseline_output` |
| `--dry-run` | Only show metrics to observe, don't sample | false |
| `--prometheus <URL>` | Prometheus Query API URL. The `da-tools` image fills it in from `$PROMETHEUS_URL` when the flag is not given | `$PROMETHEUS_URL`, else `http://localhost:9090` |

**Output**

The statistical summary is printed to stdout; two CSVs are also written under `--output-dir`: `baseline-<tenant>-timeseries.csv` (raw samples) and `baseline-<tenant>-summary.csv` (one line per metric: min / max / avg / p50 / p90 / p95 / p99 and the recommended thresholds; when a sampling query was not answered, that line's `note` reads 「查詢失敗 N/M 次，本列不完整」 (queries failed N of M times, the line is incomplete), [#3133](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3133)). ⚠️ There is no single-file output flag — `--output <FILE>` is accepted by argparse as an abbreviation of `--output-dir`, so it creates a **directory** named `<FILE>`.

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
| `0` | Success: every sampling query was answered |
| `1` | Only an uncaught exception (traceback) returns 1 — this command has no violation exit. ⛔ `--interval 0` (formerly `duration // interval` raising `ZeroDivisionError`) has exited `2` at parse time since #2966, and an unwritable output path has been `2` since #1789; neither lands here any more | <!-- datools-cmd-ignore: only a traceback gives 1, no exit to trace -->
| `2` | Caller error: a Prometheus query of any sample was not answered (unreachable, an HTTP error, an `error` status, a malformed answer) — the failed points are recorded as empty, the report and CSVs are still written, and stderr carries one `ERROR: Prometheus query failed for N sample(s)` line (it used to be `0`, [#3133](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3133)); none of the `--metrics` names is one the tool knows (the error lists the accepted ones), the required `--tenant` missing, arguments argparse rejects, or `-o/--output-dir` cannot be written (measured: the parent is a file; a CSV the tool must create inside it is already a directory) — one `ERROR: cannot …` line naming `-o/--output-dir`, no traceback (#1789) |

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
| `--mapping <FILE>` | Mapping mode: path to `prefix-mapping.yaml` (see above) | (none) |
| `--old <QUERY>` | Query mode: PromQL of the old Recording Rule (with `--new`) | (none) |
| `--new <QUERY>` | Query mode: PromQL of the new Recording Rule (with `--old`) | (none) |
| `--prometheus <URL>` | Prometheus Query API URL. The `da-tools` image fills it in from `$PROMETHEUS_URL` when the flag is not given | `$PROMETHEUS_URL`, else `http://localhost:9090` |
| `--watch` | Continuous monitoring mode (compare every N seconds) | false |
| `--interval <SEC>` | Monitoring interval in seconds (>= 0, fractions allowed; `0` = no pause between rounds, capped by `--rounds`; a negative value, `nan` or `inf` exits `2`) | `60` |
| `--rounds <N>` | Number of monitoring rounds. ⚠️ `0` is not infinite — it runs **no rounds at all** (`for i in range(rounds)`) | `10` |
| `--tolerance <RATIO>` | Allowed deviation as a **ratio**, not a percentage: `0.01` = 1%. Must be a finite number ≥ 0; `inf`, `nan` or a negative value exits 2 | `0.001` |
| `--auto-detect-convergence` | Auto-detect convergence and output readiness JSON | false |
| `-o, --output-dir <DIR>` | Output directory; `validation-report.csv` (and, in watch mode, `cutover-readiness.json`) are written there | `validation_output` |
| `--stability-window <N>` | Consecutive all-match rounds `--auto-detect-convergence` needs to call it converged | `5` |
| `--convergence-output <FILE>` | Where the readiness JSON is written (`--watch --auto-detect-convergence`) | `cutover-readiness.json` under `-o/--output-dir` |

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
| `2` | Caller error: bad arguments, no comparison pairs in the mapping, a Prometheus connection or query failure, or a malformed answer (an unreadable sample value used to be reported as `old_missing` / `new_missing` at exit code `1`, [#3133](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3133)) (`--watch` looks at the last round only; ignored once converged), or the output path given to `-o/--output-dir` / `--convergence-output` cannot be written (#1641) |

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
| `--prometheus <URL>` | Prometheus Query API URL. The `da-tools` image fills it in from `$PROMETHEUS_URL` when the flag is not given | `$PROMETHEUS_URL`, else `http://localhost:9090` |

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
| `2` | Caller error: a required argument is missing, the readiness JSON does not exist, is not valid JSON, or is missing fields, Prometheus is unreachable or its answer is malformed, or `kubectl` is not found. A malformed answer used to read by its shape: `status` other than `success` as "no threshold metrics", exit 1; a `NaN` count or no `resultType` as a pass, exit 0; `data: null` crashed ([#3133](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3133)) |

---

#### blind-spot

Scan Prometheus cluster's active targets, cross-reference with tenant config to find blind spots (exporter present but no tenant config).

When the conf.d holds a path da-guard validation names, this tool refuses with `2` before it reads or writes anything, each path named (the [shared refusal](#confd-refusal), [#3206](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3206)).

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
| `--prometheus <URL>` | Prometheus Query API URL. The `da-tools` image fills it in from `$PROMETHEUS_URL` when the flag is not given | `$PROMETHEUS_URL`, else `http://localhost:9090` |

**Output**

Presented in three sections:
- **Covered**: Exporters with corresponding tenant config
- **Blind Spots**: Exporters without tenant config
- **Unrecognized**: Jobs with unidentifiable DB type

"Corresponding tenant config" means the thresholds the exporter's `/metrics` actually serves for the tenant (read through `da-guard served-values`, #2115): a value written in `defaults:`, a platform file's `tenants:`, the tenant file or a subdirectory counts, and so does a tenant in a subdirectory; a key switched off with `disable`, or one with no default that is never served, does not. A file with no `tenants:` is not a tenant; stderr prints one `WARN` per such file. A file or sub-directory the exporter cannot read (e.g. no permission, a dangling symlink, a sub-directory it cannot list) ends the run with exit 2, the `ERROR` line naming the file (or directory) and the reason (`stat_error` / `read_error` / `walk_error` / `special_file`); the exception is a symlink to a directory, which the exporter never follows (a k8s ConfigMap's nested paths are mounted as such links): it is only skipped and does not change the exit code. Whatever da-guard prints on stderr is passed on to stderr line by line, each line behind the prefix `da-guard|` (two leading spaces, one trailing) and with control characters escaped (a newline inside a file name makes da-guard print two lines, which cannot be told apart here, but neither starts a line). Needs da-guard (shipped in the image; when running from the repo, via `$DA_GUARD_BINARY` or `$PATH`).

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
| `1` | Only an uncaught exception (traceback) returns 1 |
| `2` | Caller error: Prometheus's targets could not be read (unreachable, errored or answered in the wrong shape; one `ERROR: Prometheus query failed: …` line on stderr — it used to be a WARN, every DB reported as a blind spot at rc 0, [#3133](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3133)); a file under `--config-dir` that the exporter fails to parse and skips whole (e.g. content not UTF-8 or not valid YAML), or a file or sub-directory the exporter cannot read (e.g. no permission, a dangling symlink; a symlink to a directory excepted) — the `ERROR` line names the file (or directory), and what da-guard prints on stderr (the exporter's reasons among it) follows line by line, each line behind a fixed prefix (see below); a tree the exporter rejects (e.g. one tenant declared in two files); or da-guard not found / failing |

On exit 2, every line below the `ERROR` line starts with the prefix `da-guard|` (two leading spaces, one trailing; the same prefix as the stderr passed on in a successful run); an `exit 3` among them is da-guard's own exit code, this tool exits 2.

`walk_error` does not depend on whether the directory holds config files: any sub-directory under `--config-dir` that the running user cannot list (e.g. a `docs/` without permission, or `lost+found` when conf.d is the root of an ext4 volume and the tool runs as non-root) makes this tool exit 2; a directory whose name starts with `.` (e.g. `.git`) does not, as the exporter's load never enters it. Point `--config-dir` at a sub-path that does not contain that directory, or change the permissions so the running user can list it.

---

#### maintenance-scheduler

Evaluate the `_state_maintenance.recurring[]` schedules and create an Alertmanager silence for every tenant currently inside a maintenance window.

When the conf.d holds a path da-guard validation names, this tool refuses with `2` before it reads or writes anything, each path named (the [shared refusal](#confd-refusal), [#3206](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3206)).

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

**Where the schedules are read from**: each tenant's `_state_maintenance` is what `da-guard effective` (tenant-api `/effective`) resolves, not a Python re-read of the YAML ([#2751](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2751)): one written in a root platform file's `tenants:` or in a tenant file under a subdirectory counts the same as one in a root tenant file; a top-level `_state_maintenance` in `_defaults.yaml` does not (the exporter does not inherit it either). ⚠️ Behavior change: a root tenant file without `tenants:` that writes `_state_maintenance` at its top level used to be read as a schedule for the tenant named after the file; like the exporter, it is now not read (a WARN on stderr names the file; exit code 0). da-guard is required (bundled in the image; `make da-guard-build` in the repo); when da-guard is not found, the exporter cannot read a file, or da-guard fails (including a `--config-dir` with no `.yaml` in it, and a tenant id declared in two files: `duplicate tenant ID`), the run is not treated as "no schedules": it exits 2 and passes on da-guard's stderr. ⚠️ The v2.9.0 image still reads conf.d in Python and does not see schedules in a platform file's `tenants:` or in subdirectories. <!-- image-caveat: v2.9.0 -->

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
| `1` | At least one silence could not be created or extended |
| `2` | Caller error: `--config-dir` does not exist, a recurring schedule exists but `croniter` is not installed, or a file under it cannot be read (content not UTF-8 or not valid YAML; the message names the file, #1654); or `--alertmanager` is given (without `--dry-run`), a schedule is in its window, and the existing silences cannot be read (unreachable, still failing after the retries, or a malformed answer; with `--json-output` stdout holds an `alertmanager_query_error` envelope) — no silence is created then. Unreadable silences used to read as "none exist", so the silences were created anyway, possibly twice, exit 0 ([#3133](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3133)) |

---

#### backtest

Execute historical backtest of PR threshold changes.

When the conf.d holds a path da-guard validation names, this tool refuses with `2` before it reads or writes anything, each path named (the [shared refusal](#confd-refusal), [#3206](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3206)); both trees (`--config-dir` and `--baseline`). This path still writes `-o` and `--markdown-output`: a not-run report with `status` `caller_error`, `reason` `conf_d_not_servable`, and `refused_paths` listing the named paths (the same document on stdout under `--json`; the markdown says why and lists each path). This path's `reason` used to be `served_values_unavailable`.

**Purpose**: Validate threshold adjustment impacts; estimate expected alert changes from PR.

**Syntax**

```bash
da-tools backtest [--config-dir <dir> --baseline <dir> | --tenant <id> --metric <key> [--old-value <v>] [--new-value <v>]] [options]
```

**Required Parameters**

Choose one mode:

1. **Directory Comparison Mode**: `--config-dir <dir> --baseline <dir>`
   (Compare two config versions)
   Both trees are read by the exporter (`da-guard served-values`, da-guard required; #2119), and the values `/metrics` actually serves are compared per tenant and per `/metrics` series (its name and full label set), not per threshold key ([#3111](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3111)): a change in the platform file, a `_defaults.yaml`, a profile or a sub-directory counts; tenants come from each file's `tenants:`, and a file without `tenants:` serves nothing, so editing it is no change. A series or tenant served on one side only (switched off, added, removed, moved to another severity) has no value on the other. Values are compared over the whole UTC day: each old/new pair a series takes is one row; one that does not hold all day carries `window`, the list of every part of the day it holds in (UTC `HH:MM-HH:MM`). The Prometheus backtest is not restricted to those windows: it always covers the whole `--lookback`. The severity is a label of the series and decides which alert compares a row ([#3060](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3060)): the same value moved to another severity (`70` → `70:critical`) is two rows — the warning series is no longer served (`70` → no value: that alert is silenced) and the critical one starts being served (no value → `70`: newly enabled). The same series served by another key (e.g. `90:critical` rewritten as `disable` plus `<base>_critical: "90"`) is no change. A row's `metric` is the key of the alert comparing it (`<metric_key>_critical` for a critical row; a dimensioned row adds its dimensions, e.g. `mysql_connections_critical{db="a"}`, `--tenant` too), and `keys` lists the threshold keys serving it on either side in the parts of the day the row holds in; when `keys` is not just `metric` itself, the text and Markdown reports add `(served by: ...)` to the row. The severity suffix may only be `warning` or `critical`, in any letter case (`70:Critical` is critical); any other suffix (`70:high`, the empty `70:`) makes the whole value unreadable, and the exporter serves the platform default with severity warning ([#3071](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3071)): `70` → `70:high` is a value change from 70 to the default, and two unreadable values (`70:high` → `60:high`) are no change. `--tenant` does not backtest such a value the exporter cannot read (`value '...': this backtest cannot read it the way the exporter does`). Which alert compares a row is read from the row's `/metrics` series ([#2750](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2750)), not from a key's name: a row with severity critical (a `<base>_critical` whose `<base>` is declared in the root `defaults:` — even when the tenant switches `<base>` itself off with `disable` — and a key a window overrides to `N:critical`) is compared by the `<metric_key>_critical` alert, a row with severity warning by the `<metric_key>` alert; a row of any other severity (the rules select `severity="warning"` / `"critical"` only; an exporter since #3071 serves no such row, and one appears only when `DA_GUARD_BINARY` names an older da-guard, which served `60:high` as written, as `severity="high"`) is compared by no alert and is not backtested (`severity '...' is selected by no threshold rule`) — and a `<base>_critical` whose `<base>` the root `defaults:` does not declare is served as a warning row of metric `<x>_critical`, which no alert selects, so it is not backtested; a retired alias's twin (`legacy_twin: true`) is not a series of its own, since data under the retired name is not this threshold's data. A row whose alert key has no observed-map entry and is not KNOWN_DEFERRED (e.g. mongodb's critical row: it has no `_critical` alert) is compared by no shipped alert, and is listed but not backtested. The alert's threshold is the `max by(tenant)` of every row of one metric and severity, dimensioned rows included: when the tenant also serves (in either tree, at any time of the day) a dimensioned row of the same metric and severity, the changed base value is not what the alert compares, and the change is not backtested (`dimensioned rows share this alert's threshold (max by tenant)`). Dimensioned keys (a series with `dimensions` / `dimensions_regex` in served-values, e.g. `metric{db="a"}`) are listed but not queried in Prometheus, marked `not_backtested` (`backtest: skipped: dimensioned key`). The text and Markdown reports say how many were not backtested, and why. Custom-alert recipes (`_custom_alerts`) are not part of this backtest: each tree's custom-alert rows per tenant as the exporter serves them are read with `da-guard served-values` (`_custom_alerts`; `_platform.yaml` and files in sub-directories included; a recipe's shape, window, `for:`, name, mode, severity and value are all in its rows), together with the number of rows the recipes ask for before `max_metrics_per_tenant` cuts them (`custom_alert_rows`) and a non-empty `_custom_alerts` list the exporter refuses whole (`unserved`; an empty list counts as none written), and the tenants where they differ (a recipe added, changed or removed, or a change to a list refused whole) are listed in a `NOTE:` on stderr and in the Markdown recipe block; a tenant only moved to another file with the same recipes is not ([#3111](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3111)). Three cases are not named. First, an entry the exporter refuses while it serves the rest of the list (an unknown recipe, or one with the shape and severity of an earlier entry): neither side serves it and `unserved` does not hold it; da-guard prints only a `custom alert "…" rejected` line on stderr (relayed line by line to the tool's stderr), its JSON has no field to compare, and the tool does not parse stderr. Second, a top-level `_custom_alerts` in a `_defaults.yaml` (root or sub-directory): the custom-alert compiler reads it and every tenant below it inherits it, the exporter does not serve it, and `served-values` does not hold it. Third, a changed value inside a recipe that `max_metrics_per_tenant` cuts: `custom_alert_rows` counts the rows before the cut but does not show the ones cut (adding or removing a cut recipe changes the count and is still named). For these three see `config-diff` (top-level keys of `_`-prefixed files compared key by key) and the `custom_alert_*` rows of `da-guard check`.

2. **Single-change Mode**: `--tenant <id> --metric <key> [--old-value <v>] [--new-value <v>]`
   (Reads no config file; backtests the one change given on the command line, each value read by the exporter — `da-guard served-values` over the shipped platform defaults, da-guard required, [#3140](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3140))
   `--tenant` requires `--metric` and at least one of `--old-value` / `--new-value`, otherwise exit 2; the tenant id must be a valid DNS-1123 label. How values are read: see "What the backtest queries" below. `--tenant` and `--config-dir` are mutually exclusive: giving both is refused by argument parsing (exit code 2).

**What the backtest queries** ([#3017](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3017), [#3002](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3002))

- One Prometheus query per change: the recording rule the key's alert compares against the threshold (`observed_series` in [`metric_observed_map.yaml`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/scripts/tools/ops/metric_observed_map.yaml)), with `{tenant="<tenant>"}`. Breaches are counted the way the alert compares: a `>` alert counts points above the threshold, a `<` alert (a lower bound, e.g. `db2_bufferpool_hit_ratio`, `kafka_broker_count`) counts points below it. It used to count every breach as `>` and try three guessed names in turn — `<key>`, `tenant:<key>:max`, `<key>{namespace=...}` — none of which is what the alert compares for nearly half of the keys.
- `--tenant` gives the key and values as written: a retired spelling goes through the alias table first (e.g. `mysql_cpu` → `mysql_threads_running`). A key containing `{` is not backtested (`dimensioned key`). Values are read by the exporter ([#3140](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3140): the tool writes a tree with only this tenant and this key, rooted at the shipped platform defaults — the threshold registry's `defaults` tier and its `optional_overrides:` tier, a key the registry does not know declared without a default — runs `da-guard served-values`, and reads the value, severity and `keys[]` `reason` / `refused` it gives, with no judgement of its own in Python): the number before the last `:`, the severity after it (`70:critical`; none means warning), and a `<base>_critical` key's whole value is the number. The edit becomes `/metrics` rows by the rule directory comparison applies ([#3111](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3111)): each side is the row of its severity (a critical row is compared by the `<key>_critical` alert), so `70` → `95:critical` is two rows (the warning row `70` → no value, the critical row no value → `95`), and `70` → `70` is no change; a row's `metric` is its alert key, its values the numbers alone, and `keys` the key given. A row no shipped alert compares (its alert key has no observed-map entry and is not KNOWN_DEFERRED, e.g. mongodb has no `_critical` alert) is listed but not backtested, so mongodb's `70` → `70:critical` is the warning alert silenced (MEDIUM) plus a critical row not backtested. A value the exporter cannot read (served-values' `refused: unparsed`, e.g. `7O`, `70:high`, `disable:critical`) is one row as written, not backtested; `1_000` and `0x1p4`, two spellings the exporter does read, are still not backtested here (exit codes unchanged). A non-finite number (`NaN`, `Inf`, `-Inf`, `infinity`, with or without `:critical`) is refused by the exporter ([#3140](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3140)), and is read here as what the exporter serves instead: for a base key the shipped platform default (at severity warning; a deployment whose root `_defaults.yaml` writes another value shows only when two trees are compared with `--config-dir`), for a `<key>_critical`, dimensional or `optional_overrides:` key no row — which is not a switch-off: a new side with no row because its value is refused is judged `HIGH` (message `Value refused by the exporter (non_finite): no row is served — all alerts silenced`, exit 1), while `disable` is the "Metric disabled" `MEDIUM`; a key the registry does not know is one row as written, not backtested. A `NOTE:` line on stderr says what was used. Directory comparison follows the same rule: a row gone from the PR tree while a key serving it is refused there as non-finite in those parts of the day (served-values' `--schedules` segment `reason: value_non_finite`, or `keys[].reason` when it is served in no part of the day) is `HIGH`; a value refused as unreadable (`value_unparsed`, unquoted `.nan` / `.inf` included, which the exporter reads as a string) and `disable` stay `MEDIUM`. ⚠️ **Behaviour change**: such a value used to be backtested as NaN / ±Inf (NaN never breaches, often HIGH, exit 1); a base key is now judged at the default, so with the same data the exit code can go from 1 to 0; `--tenant` now needs da-guard (exit 2, `served_values_unavailable`, when it is not found).
- These changes are `not_backtested` (risk `UNKNOWN`, no effect on the exit code, Prometheus not queried), with `backtest: skipped: <reason>` and message `Not backtested: <reason>`: the observed-map has no entry for the key (no shipped alert compares it; KNOWN_DEFERRED keys such as `container_cpu`, whose alerts compare a version-aware recording rule the observed-map does not record, get their own reason); the entry cannot be replayed (needs review, a composite alert with no single series, an unresolved direction, not a per-tenant series); a tenant id holding a character a PromQL selector cannot take as written (`"`, `\`, a control character, or bytes that are not UTF-8 — in directory comparison the tenant is the exporter's `tenants:` key, which the exporter does not hold to the tenant id rule; it used to build invalid PromQL and end as `query_error`, exit code 2).
- ⚠️ Known approximation: what is replayed is the alert's comparison operand, not the alert — `for:`, maintenance filters and the other conditions of a composite AND alert are not modeled, so the count is "points at which the comparison holds", not how many times the alert would actually fire. `--tenant` sees only the one key it is given: not whether the same series is also served by another key, not whether the tenant serves dimensioned rows of the same metric (the threshold's `max by(tenant)` is not modeled), and not whether `<base>` is declared in the root `defaults:`.

**`--git-diff` was removed ([#3111](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3111))**

`--git-diff` used to take `HEAD~1`'s `conf.d/` out itself and compare it; now the caller checks the PR's base out and compares the two trees in directory comparison mode. A call that still passes `--git-diff` ends with exit code 2 (under `--json` the envelope's `reason` is `git_diff_removed`), and the message names the commands to use instead.

```bash
# Old (removed)
python3 scripts/tools/ops/backtest_threshold.py --git-diff --prometheus <url>

# Local: check the previous commit out at another path, then compare the two conf.d/
git worktree add /tmp/backtest-base HEAD~1
python3 scripts/tools/ops/backtest_threshold.py --config-dir conf.d \
  --baseline /tmp/backtest-base/<repo-path>/conf.d --prometheus <url>
git worktree remove /tmp/backtest-base   # if the directory was deleted directly: git worktree prune

# CI (GitHub Actions): check the PR base out into its own path
#   - uses: actions/checkout@v6
#     with:
#       ref: ${{ github.event.pull_request.base.sha }}
#       path: .backtest-base
#       persist-credentials: false
python3 scripts/tools/ops/backtest_threshold.py --config-dir conf.d \
  --baseline "$GITHUB_WORKSPACE/.backtest-base/<repo-path>/conf.d" --prometheus <url>
```

`<repo-path>` is the path from the repository root to the directory holding `conf.d/` (`.` under the customer layout, where `conf.d/` is at the repository root). The base tree must hold at least one config file the exporter reads (`.yaml` / `.yml`, not hidden); otherwise da-guard refuses it and the exit code is 2 (`served_values_unavailable`). A PR that adds the first `conf.d/` has no base to compare against: this repo's `backtest.yaml` checks whether the base checkout has a `conf.d/` and, when it has none, skips the backtest and says so in the PR comment.

**Options**

| Option | Description | Default |
|--------|-------------|---------|
| `--config-dir <DIR>` | Directory Comparison mode: the config directory after the change (with `--baseline`) | (none) |
| `--baseline <DIR>` | Directory Comparison mode: the config directory before the change (with `--config-dir`) | (none) |
| `--tenant <ID>` | Single-change mode: the tenant id (requires `--metric` and at least one of `--old-value` / `--new-value`) | (none) |
| `--metric <KEY>` | Single-change mode: the threshold key (with `--tenant`) | (none) |
| `--old-value <VALUE>` | Single-change mode: the old threshold value, read the way conf.d is (e.g. `70`, `70:critical`; with `--tenant`) | (none) |
| `--new-value <VALUE>` | Single-change mode: the new threshold value (with `--tenant`) | (none) |
| `--prometheus <URL>` | Prometheus Query API URL. The `da-tools` image fills it in from `$PROMETHEUS_URL` when the flag is not given | `$PROMETHEUS_URL`, else `http://localhost:9090` |
| `--lookback <DURATION>` | Historical lookback window, `<number><d\|h\|m>` (e.g. `7d` / `24h`). A value that does not match that shape (including a bare `7` or the empty string) ⇒ exit code 2 listing the accepted shape (#1625; it used to be silently treated as `7d`) | `7d` |
| `-o, --output <FILE>` | Also write the JSON report to a file, the same document `--json` would print on this run (when it is written: see Output below); the text report still goes to stdout | (none) |
| `--json` | Print only JSON on stdout, no text report | false |
| `--markdown-output <FILE>` | Also write the Markdown report to a file (for PR comments) | (none) |
| `--skip-if-unavailable` | Only "Prometheus unreachable" becomes exit 0; every other caller error (see the exit-code table below) still exits 2 | false |

**Output**

Comparison report showing impact of threshold changes on historical data (potential alerts increased/decreased).

The firing percentage is over every sample in the lookback, NaN included: the alert is silent at a NaN step, so the percentage is the share of the whole lookback. Every `analyzed` / `no_data` result carries `query`, the exact PromQL sent; the text report prints `query: ...` under the message, and the Markdown table has a `Query` column (`—` for a `not_backtested` change, which sends none).

A change with no data at all, or with only NaN samples, is `no_data` (risk `UNKNOWN`, no effect on the exit code); the message is `No historical data found in Prometheus` for the former and `Only NaN samples in lookback window` for the latter ([#2963](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2963); all-NaN used to be reported as analyzed). There is one query, so no other query to fall back to. `+Inf` / `-Inf` are real samples and compare as usual.

When Prometheus is reachable but the query itself fails (HTTP 400 / 503, a timeout, an `error` status), or answers HTTP 200 with `success` in a shape that is not a matrix (no `data`, `resultType` other than `matrix`, `result` not a list, a row that is not an object or has neither `values` nor `histograms`, a sample that is not `[time, value]`, a sample value it cannot read (for example `"abc"`; before, that point was skipped silently at exit code 0, [#3213](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3213)); a native-histogram-only row is still skipped; [#3074](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3074) — no `data` / a row without `values` or `histograms` used to be reported as `no_data` or the row skipped, exit code 0, and the other shapes ended as an internal error without writing `-o`), the change is `query_error` (risk `UNKNOWN`), with message `Prometheus query failed: <error>` (for an HTTP error, the status line such as `HTTP Error 400: Bad Request`, not the `error` in Prometheus's response body; for a wrong shape, `malformed Prometheus response: <what is wrong>`) (control characters replaced with `?`, truncated when long), and the other changes still run ([#3059](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3059); it used to be reported as `no_data` with exit code 0). The report carries a `query_error` count; the text report prints `Query error: N` and the Markdown says how many changes were not measured. When any change is `query_error`, the `--json` top level carries `status: "partial"` and `reason: "query_error"` (a run that measured every change carries no `status`, as before), and the exit code is 2.

`-o/--output` and `--markdown-output` are written on every terminal path ([#3089](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3089)), except for: an argument error that ends the run before the output paths are checked (`--lookback` unusable, `--tenant` missing its arguments or not valid, no change source, the removed `--git-diff` given), and an output path that itself cannot be written; an uncaught exception (rc 2, [#3090](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3090)) and a write that fails after the check passed are not guaranteed to write them either. Do not read the output files when the exit code is 2: they may hold a previous run's content. When `-o` and `--markdown-output` name the same file, the Markdown, written last, replaces the JSON. `-o` holds the same JSON `--json` would print on that run: the full report, or the `skipped` / `no_changes` / `caller_error` envelope. When no backtest ran, `--markdown-output` holds a short note under the same heading as the full report, reading `Backtest not run: <reason>`, where the reason is Prometheus being unavailable, no threshold changes, or ``caller error `<reason>` ``; when a tenant's custom-alert recipes changed, the recipe block follows it as before. Before this, only the full-report path wrote `-o`, and the `--skip-if-unavailable` and no-changes paths wrote the markdown only when a tenant declared recipes. CI decides whether to update the PR comment by whether the file is non-empty, so the comment stayed at the previous run's result.

**Examples**

```bash
# Directory comparison mode
da-tools backtest --config-dir ./conf.d-new --baseline ./conf.d-old --lookback 7d
```

**Exit Codes**

| Code | Description |
|------|-------------|
| `0` | Success |
| `1` | At least one threshold change was rated HIGH risk (review before merging), and no change is `query_error`; an unreachable Prometheus or a failed query is not 1, see below |
| `2` | Caller error: Prometheus unreachable without `--skip-if-unavailable`; `--lookback` supplied but unusable (not `<number><d\|h\|m>`, #1625); the removed `--git-diff` given, with any other valid flag (the message names the commands to use instead; under `--json` the `reason` is `git_diff_removed`, [#3111](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3111)); the output path given to `-o/--output` / `--markdown-output` cannot be written (#1641) — checked after the argument checks and before conf.d, the observed-map or Prometheus are read (parent missing, not a directory or not writable, or the path itself a directory or a file that cannot be written), with `--skip-if-unavailable` and with no threshold changes too ([#3089](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3089); those two cases used to exit 0 and write nothing), and under `--json` the envelope's `reason` is `output_unwritable`; a conf.d file whose content cannot be read (not UTF-8 or not valid YAML; the message names the file, #1654); `--config-dir` without `--baseline` (`reason` `baseline_missing`); in directory comparison mode, da-guard missing, failing or stale, or either tree holding a file the exporter cannot read or no config file at all (with `--skip-if-unavailable` too, `reason` `served_values_unavailable`, #2119; an unreadable file under `--config-dir` used to be `conf_file_unreadable` — since #3111 da-guard names it on either side); the observed-map (`metric_observed_map.yaml`) missing, unreadable, without keys or with a mistyped entry (with `--skip-if-unavailable` too; under `--json` the envelope's `reason` is `observed_map_unavailable`, #3017); `--tenant` without `--metric` or without either value, or no change source given (with `--skip-if-unavailable` too, #3059); `--tenant` not a valid tenant id (ADR-035's DNS-1123 label: lowercase letters, digits and `-`, 1–63 characters, starting and ending with a letter or digit; with `--skip-if-unavailable` too, [#3066](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3066) — `a"b` used to build an invalid PromQL query and exit 0); any change that is `query_error` — this outranks HIGH's 1, and holds with `--skip-if-unavailable` too, since Prometheus is reachable (#3059). Under `--json` those three argument errors and an unreachable Prometheus print a `caller_error` envelope, with `reason` `tenant_args_missing`, `no_change_source`, `invalid_tenant_id` and `prometheus_unavailable` respectively; `query_error` prints the full report (`status: "partial"`) |

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
| `--alertmanager <URL>` | Alertmanager API URL. Given and not read (unreachable, errored or answered in the wrong shape), the shadow-route check FAILs and the exit code is 2; not given, the default is read and not answering stays a skip ([#3133](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3133)). The wrong shape includes a missing `config` or `config.original` (required by the AM v2 API): such an answer used to read as an empty config, a warn "No migration_status matcher found", exit code 0 | `http://localhost:9093` |
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
| `1` | One or more checks failed (a `--mapping` that does not exist counts too: it is listed as FAIL, not as a caller error) |
| `2` | Caller error: Prometheus unreachable, a query failed, answered in the wrong shape, or answered a count that is not a finite number in `preflight` or `runtime` (each including `all`; the check is still listed as FAIL, but the exit code is 2, not 1), an I/O error reading `--report-csv` in `runtime` (including `all`; `convergence` run alone skips that error), or arguments argparse rejects. ⚠️ A `--report-csv` that does not exist is **not** 2 — the CSV analysis is simply skipped. A failed maintenance / silent mode query in `runtime` used to produce no check, so the result was `Overall: PASS` and exit code 0 ([#3133](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3133)). An `--alertmanager` given and not read (unreachable, errored or answered in the wrong shape) is 2 as well; not given, the default is read and not answering stays a skip |

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
| `2` | Caller error: Prometheus or Alertmanager unreachable, or a query / rules / status API call failed, answered in the wrong shape, or answered a count that is not a finite number (the check is still listed as FAIL, but the exit code is 2, not 1); or arguments argparse rejects. Every step's query counts: a failed Step 3b or E2E query used to produce no check; a failed Step 4 / 5 follow-up query used to be a warn; Alertmanager alerts / silences that could not be read used to be a warn, and an alerts answer that is not an array passed; a Step 3 rules answer with `status: error` or no `groups` used to read as "no rule groups", exit code 1; an Alertmanager status answer missing `config` or `config.original` (required by the AM v2 API), whose `config` is null or not an object, whose `original` is not a string, or whose body is not an object used to read as "no tenant routing" (warn, exit code 0) ([#3133](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3133)) |

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
| `2` | Caller error: `e2e` without `--edge-urls`, a target other than edge / central / e2e; edge / central Prometheus unreachable, a config / query / rules API call failed, a query answered in the wrong shape, or a count that is not a finite number (the check is still listed as FAIL, but the exit code is 2, not 1). Every query counts: the central fallback query (no cluster label) used to read a failure as "none found", exit code 1; a failed recording-rules or e2e cross-cluster query used to produce no check; an edge config API answer with `status: error` or no `data.yaml` used to read as "no external_labels", exit code 1; a central alert-rules answer with `status: error` or no `groups` used to read as "0 alert rules" (warn, exit code 0) ([#3133](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3133)). `/federate` is a probe: not reaching it stays a warn and does not change the exit code |

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
| `1` | Only an uncaught exception (traceback) returns 1 — this command has no violation exit; measured when `--existing-jwks` points at a JSON **array** (a top level that is not an object). ⛔ An unwritable output path has been `2` since #1789 and no longer lands here | <!-- datools-cmd-ignore: only a traceback gives 1, no exit to trace -->
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
| `--alertmanager` | Alertmanager URL (for suppression data). When given and it does not answer, that is not 0 suppressed: see the exit codes | - |
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
| `2` | Caller error: `--period` unparsable, `--tenant` containing characters other than alphanumerics / underscore / hyphen, the required `--prometheus` missing, or arguments argparse rejects; and a failed Prometheus `ALERTS` query (unreachable, non-2xx HTTP, `status` other than success, or an unparsable response — HTTP 200 but not JSON or not an object, e.g. a proxy returning HTML; with or without `--ci`, #3065 — it used to count as no data, rc 0; or `success` in a shape that is not a matrix, e.g. no `data` or a row without `values` or `histograms`, [#3074](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3074) — no `data` / a row without `values` or `histograms` used to be rc 0); and an `--alertmanager` that was given and did not answer (unreachable, an HTTP error, a body that is not a list of alert objects; [#3073](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3073) — it used to count as 0 suppressed, rc 0, and under `--ci --min-score` let through a run that should have been rc 1). No report is printed then and stderr carries `Error: <Prometheus\|Alertmanager> query failed: <raw>`; `--json` prints `{"status": "caller_error", "reason": "prometheus_query_error" (`alertmanager_query_error` for Alertmanager), "error": "<raw>", ...}` (the remaining keys follow the normal report, zeroed). Tell them apart with `da-tools alert-quality --prometheus <URL> --json \| jq '.status // "ok"'` |

**Sample values** ([#3213](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3213)): every `ALERTS` sample is read once, by the lib. A value it cannot read (for example `"abc"`, or not a string) is a malformed answer, exit code `2` (`prometheus_query_error`). `+Inf` / `-Inf` / `NaN` are no state and are left out of the grades; how many were dropped is in `summary.non_finite_samples_dropped` of `--json` (and the same field on each alert) and on one line of the text report.

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
| `-i, --input <FILE>` | Alertmanager JSON file (offline mode) | — |
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
| `1` | CI mode: a cluster containing a critical alert found (a cluster needs at least 2 alerts; a single critical alert gives 0) |
| `2` | Caller error: `--window` unparsable or ≤ 0, or arguments argparse rejects. Prometheus's alerts not read (unreachable, errored or answered in the wrong shape) is 2 as well — it used to print a WARN and go on with zero alerts at rc 0 ([#3133](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3133)). The Alertmanager URL is derived from `--prometheus` (`:9090` → `:9093`); when it does not answer, one `NOTICE` line goes to stderr and Prometheus is read instead; an `--input` file that does not exist is an uncaught exception (traceback, rc 1) |

---

#### drift-detect

Compare multiple config-dir directories (from different clusters or GitOps branches) and detect unexpected configuration drift. Uses SHA-256 manifests for directory-level comparison.

What is compared is each directory's top-level config files, listed from da-guard's index of that directory (the exporter's walk); config files in subdirectories do not enter the manifest, and stderr names them. So da-guard is required (`$DA_GUARD_BINARY`, then `$PATH`; the da-tools image ships one); without it the exit code is `2`. When any directory holds a path da-guard validation names, this tool refuses with `2` before comparing, each path named (the [shared refusal](#confd-refusal), [#3206](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3206)).

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
| `--mode <MODE>` | `configmap`: compare several directories (`--dirs` needs at least 2); `operator`: compare one local directory with the PrometheusRule CRDs in the cluster (`--dirs` takes exactly 1) | `configmap` |
| `--kubeconfig <FILE>` | kubeconfig path (operator mode only) | — |
| `--context <NAME>` | Kubernetes context name (operator mode only) | — |
| `--namespace <NS>` | Monitoring namespace (operator mode only) | `monitoring` |

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
| `2` | Caller error: any `--dirs` directory missing, a directory holding a path da-guard validation names, no usable da-guard, fewer than 2 directories in configmap mode, not exactly 1 directory in operator mode, `--labels` count not matching `--dirs`; in operator mode `kubectl` not on PATH, timed out (30s), exited non-zero or returned non-JSON; arguments argparse rejects |

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

**Exit Codes**

| Code | Description |
|------|-------------|
| `0` | Success (CI mode: no critical risk) |
| `1` | CI mode: critical risk found |
| `2` | Caller error: bad arguments, or no cardinality data obtained. The `--json` `reason` separates the two cases: a failed Prometheus query (unreachable, non-2xx HTTP, `status` other than success, or an unparsable response — HTTP 200 but not JSON or not an object, e.g. a proxy returning HTML; or `success` in a shape that is not a matrix, e.g. no `data` or a row without `values` or `histograms`, [#3074](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3074) — no `data` used to be reported as `no_cardinality_data` (rc 2) and a row without `values` or `histograms` skipped (rc 0)) ⇒ `"status": "caller_error"`, `"reason": "prometheus_query_error"`, plus `"error": "<raw>"` (#3065; it used to be `"status": "no_data"`, `no_cardinality_data` like the next case); a successful query with no `tenant_threshold_*` series ⇒ `"status": "no_data"`, `"reason": "no_cardinality_data"`. `da-tools cardinality-forecast --prometheus <URL> --json \| jq -r '.reason // "ok"'` |

**Sample values** ([#3213](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3213)): every sample is read once, by the lib. A value it cannot read is a malformed answer, exit code `2` (`prometheus_query_error`). `+Inf` / `-Inf` / `NaN` are left out of the regression; how many were dropped is in `summary.non_finite_samples_dropped` of `--json` and in the text report. A tenant whose samples are all non-finite is not listed; when that holds for every tenant the run is `no_cardinality_data` (`2`).

#### config-history

Config snapshot & history tracking — records each change to conf.d/ in `.da-history/`, providing git-independent lightweight version control.

`snapshot` records the conf.d's top-level config files, listed from da-guard's index of the tree (the exporter's walk); config files in subdirectories do not enter the snapshot, and stderr names them. So `snapshot` requires da-guard (`$DA_GUARD_BINARY`, then `$PATH`; the da-tools image ships one); without it the exit code is `2`. When the conf.d holds a path da-guard validation names, `snapshot` refuses with `2` before it writes anything, each path named (the [shared refusal](#confd-refusal), [#3206](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3206)). `log` / `show` / `diff` read `.da-history/` only.

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

**Options**

| Option | Description | Default |
|--------|-------------|---------|
| `--config-dir <PATH>` | The conf.d directory (required; goes before the subcommand) | (none) |
| `-m, --message <TEXT>` | `snapshot`: the snapshot message | (empty) |
| `--limit <N>` | `log`: show only the last N entries (`0` shows all) | (all) |

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

In `conf.d/`, init owns only two kinds of path: `_defaults.yaml` and `<tenant>.yaml` at the **root**. A requested tenant that **may** already be declared by **another file** of yours is **skipped, and that file is left untouched**; stderr and the summary name it (e.g. "db-c may already be declared by conf.d/team.yaml — conf.d/db-c.yaml was not generated") and the exit code stays 0. Likewise `_defaults.yaml` is not written when the root already has a defaults carrier in another spelling (e.g. `_defaults.yml`). "May declare" is judged by **content**, never by filename: the keys of a `tenants:` mapping are declarations. When the conf.d holds a path init cannot read (see the next paragraph for how it reads — a file that does not decode, a dangling symlink, a directory with a config file's name), init refuses before it writes any file, naming each one, rc 2 (the [shared refusal](#confd-refusal); `--dry-run` too); such a file used to count as possibly declaring every requested tenant. ⚠️ **init does not check whether the exporter can read that file** — to confirm the skipped tenant really is declared, run [`da-tools guard defaults-impact --config-dir <conf.d>`](#guard) (it scans the whole tree the way the exporter does and fails on a duplicate; the file should be listed under Scanned files in its report). A readable customer file that names no requested tenant draws no comment (init is not a validator). init **refuses with rc 1 and writes nothing** (`.da-init.yaml` included), under `--dry-run` too, when: its own `conf.d/<t>.yaml` already exists while another file it can read declares t as a `tenants:` key (e.g. `db-c.yaml` and `db-c.yml` — the exporter rejects a tree that declares a tenant twice, and which file to keep is your call); `_defaults.yaml` sits beside a defaults carrier in another spelling; the `conf.d/<t>.yaml` it would overwrite may also declare another tenant this run was asked for (after the overwrite that tenant would be declared nowhere); the `conf.d/<t>.yaml` it would overwrite also declares a tenant this run was **not** asked for (e.g. `db-a.yaml` declares db-a and db-z and you run `--tenants db-a`: the rewrite would make db-z's declaration disappear — move db-z into a file of its own such as `conf.d/db-z.yaml`, or add db-z to `--tenants` too) — `--force` is refused just the same; or any path it would write has, at **any level** below the output directory, an existing entry that differs only in case (e.g. `Conf.D/` for `conf.d/` — one path on a case-insensitive filesystem).

**Reading, and the confirmation before a write (ADR-036 §2, #2906)**: which tenants another file declares is `da-guard parse-tree`'s answer (the yaml.v3 reading, the exporter's own — no longer approximated by a text search); a file parse-tree cannot read (a syntax error, a duplicate key, a tag outside its allowed set, not UTF-8, ...) counts as possibly declaring every requested tenant. Before any file is written, the generated `conf.d/_defaults.yaml` and `conf.d/<tenant>.yaml` are read back by parse-tree at the paths they are written to and must hold exactly what was generated — otherwise exit `1` and no file is written. da-guard is needed whenever `conf.d/` exists or a conf.d file is written (`$DA_GUARD_BINARY`, then `$PATH`; the da-tools image ships it); without it, exit `2`.

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
| `-o, --output-dir <DIR>` | Output root directory | `.` (current directory) |
| `--namespace <NS>` | Kubernetes monitoring namespace | `monitoring` |
| `--da-tools-image <IMAGE>` | The da-tools image the generated CI pipelines and pre-commit config use; an empty value exits 2 | the image of the da-tools release running this init |
| `--config-source <SOURCE>` | Config source: only `configmap` is supported; `git` (GitOps Native Mode) is temporarily unsupported and exits 2 ([#1349](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1349)) | `configmap` |

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

When the conf.d holds a path da-guard validation names, this tool refuses with `2` before it reads or writes anything, each path named (the [shared refusal](#confd-refusal), [#3206](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3206)).

```bash
da-tools gitops-check <subcommand> [options]
```

**Subcommands**

| Subcommand | Purpose | Parameters |
|------------|---------|------------|
| `repo` | Validate Git repo accessibility, branch existence, and that `--path` is in that branch | `--url <git-url> [--branch main] [--path configs/]` |
| `local` | Validate local clone's conf.d/ structure | `--dir <path>` |
| `sidecar` | Check K8s git-sync sidecar deployment readiness | `[--namespace monitoring]` |

**Options**

| Option | Description | Default |
|--------|-------------|---------|
| `--url <GIT-URL>` | `repo`: the Git repository URL (required) | (none) |
| `--branch <BRANCH>` | `repo`: the branch to check | `main` |
| `--path <PATH>` | `repo`: the config path inside the repository | `configs/` |
| `--dir <PATH>` | `local`: the local config directory (required) | (none) |
| `--namespace <NS>` | `sidecar`: K8s namespace | `monitoring` |
| `--json` | Accepted by all three subcommands: JSON output | false |
| `--ci` | Accepted by all three subcommands: only a failure exits 1, a warning exits 0 (without it a warning exits 1 too) | false |

**How `local` reads** (ADR-036 step 4, [#2906](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2906)): which files there are, and what each holds, always come from `da-guard parse-tree` (yaml.v3, the exporter's file selection). The root defaults carrier is the one the exporter reads (any case, `.yaml` first); tenant files are counted over the **whole tree** (subdirectories too), and every file the exporter reads (`_` platform files included) that cannot be read is listed in `parse_errors` and fails the check. It needs da-guard (`$DA_GUARD_BINARY`, else `$PATH`); without one, or when it fails, the exit code is 2 and there is no verdict. ⚠️ **Breaking**: it used to look at the root only — tenant files in subdirectories were neither counted nor checked, and an unreadable `_` platform file did not fail it.

**Exit Codes**

| Code | Description |
|------|-------------|
| `0` | Passed; with `--ci` a warning is 0 too. Only `sidecar` gives warnings (kubectl unavailable, or the git-sync Secret / sidecar partly or entirely missing); a `repo` `--path` the server cannot verify (it does not support `git archive --remote`) is only recorded as `config_path_verified: false` and still passes |
| `1` | A failure: `repo` cannot reach the repository, the branch does not exist (as git resolves it: `--branch main` is `refs/heads/main`, never `refs/heads/feature/main`; `heads/x` and `refs/heads/x` both name `refs/heads/x`; a tag is not a branch), or the server answers that `--path` is not in that branch (without `--path`, the default `configs/` is what is checked; a server answering in another language falls back to "cannot verify"); `local` has no root defaults carrier, or a file that cannot be read (`parse_errors`). Without `--ci` a warning is 1 too |
| `2` | No verdict: no subcommand given (help is printed), arguments argparse rejects (unknown subcommand, missing `--url` / `--dir`, unknown flag); `local --dir` naming no directory (`reason: dir_not_found`), or a path it cannot tell is one, e.g. under a parent the caller cannot enter (`reason: dir_not_accessible`); `local` finds no usable `da-guard parse-tree` (`$DA_GUARD_BINARY`, else `$PATH`) or it fails; an unexpected exception in the tool |

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
| `2` | Caller error: arguments argparse rejects (unknown flags etc.). ⚠️ A missing `--state-dir` is **not** 2 — it is treated as empty (text mode prints a warning, `--json` only sets `state_dir_missing: true` in the report; manifest rebuilt with 0 states, rc 0; 1 under `--ci --dry-run` because a rebuild is pending) |

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
| 1 | `--ci` mode detected orphans or malformed silences |
| 2 | Caller error (file missing / JSON parse failure / `--rule-source` an empty directory, etc.) |

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

#### migration-audit

Migration audit ([ADR-036](adr/036-single-parser-effective-config.en.md) §8, [#2898](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2898)). conf.d YAML is moving to one reader in Go (yaml.v3, `da-guard parse-tree`); run this on your own tree before the switch to see what changes. It reads the conf.d twice — with today's route generator reader (PyYAML, the same file selection and loader) and with `da-guard parse-tree` — and **lists the differences without judging them**.

```bash
da-tools migration-audit --config-dir <dir> [--json] [--da-guard-binary <path>]
```

**Main parameters**

| Parameter | Purpose |
|------|------|
| `--config-dir <dir>` | conf.d root (required) |
| `--json` | Print one JSON document (schema `da-tools.migration-audit/v1`) on stdout |
| `--da-guard-binary <path>` | da-guard path; otherwise `$DA_GUARD_BINARY`, then `da-guard` on `$PATH` (the da-tools image ships it) |
| `--timeout <seconds>` | How long da-guard may run (default 600) |

**What it lists**

| List (`--json` key) | Content |
|------|------|
| `values` | Values the two readers read differently: file, line, path, both readings, a suggested spelling (e.g. `on`: write `true` for a boolean, `"on"` for a string). `rewrites` lists each replacement (`meaning`, `text`) and `span` where the value is written (line and columns, its tag included, its anchor not); every replacement makes both readers read the same, and none is lossy (an integer past int64 or an item of a `tenants:` list gets the quoted string only); a non-finite number gets no number spelling, only the quoted string — a threshold must be finite, so `.inf` / `.nan` are no longer suggested ([#3140](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3140)) |
| `keys` | Keys only one side has |
| `becomes_unreadable` | Files read today that `parse-tree` refuses, with the `kind` (one of the closed list of 13). ⚠️ Always empty since #3206: a tree holding such a file refuses the whole audit with `2`, each one named (the [shared refusal](#confd-refusal)); the key stays, the schema is unchanged |
| `becomes_readable` | Files refused today that `parse-tree` reads, with today's error and what yaml.v3 reads |
| `selection` | Files only one side reads |
| `still_unreadable` | Files both refuse. ⚠️ Always empty, as above |
| `files_compared` | Files both read and compared value by value (the text output names them on its second line), so "no difference" and "nothing compared" read apart |

Lines come from PyYAML's reading: a value is at the line it is written on, a key at its own line; a value reached through an alias or a merge is at the line of its anchor or of the merged mapping's definition — changing that line changes every place that uses it. A key only yaml.v3 has is at its parent's key line.

A policy's `tenants: [010, 1e3, 0777]` is listed as a difference (today the source text `"010"`, `"1e3"`, `"0777"`; yaml.v3 reads 8, 1000, 511): that is the expected result of ADR-036 §2 (the semantic layer judges by type and converted value only); quote them to keep the text.

**Without da-guard** it fails (exit 2) and says why; it never falls back to parsing the YAML itself.

**Exit codes**

| Code | Meaning |
|------|------|
| 0 | Both readers read the tree the same way |
| 1 | A difference is listed |
| 2 | Caller error (`--config-dir` missing or unreadable, no da-guard, da-guard failed or wrote no readable parse-tree document); with `--json`, stdout is still one JSON document with `status: "caller_error"` and a `reason` |

```bash
da-tools migration-audit --config-dir conf.d/
da-tools migration-audit --config-dir conf.d/ --json | jq '.values[] | {file, line, suggestion}'
```

---

### Operator + Federation Tools

#### operator-generate

Generate Kubernetes Operator CRDs (PrometheusRule, AlertmanagerConfig, ServiceMonitor) from Rule Packs and Tenant configuration.

When the conf.d holds a path da-guard validation names, this tool refuses with `2` before it reads or writes anything, each path named (the [shared refusal](#confd-refusal), [#3206](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3206)).

**Purpose**: Dynamic alert rule and routing deployment in Prometheus Operator clusters; GitOps-friendly CRD YAML generation.

**Syntax**

```bash
da-tools operator-generate [options]
```

**Options**

| Option | Description | Default |
|--------|-------------|---------|
| `--rule-packs-dir <DIR>` | Rule Pack directory path | `rule-packs/` |
| `--config-dir <DIR>` | Tenant configuration directory path. One AlertmanagerConfig per tenant; the tenants are the keys of each tenant file's `tenants:` across the whole tree (subdirectories included), read through `da-guard parse-tree` — **not file names**: the tenant in `team-a.yaml` holding `tenants: {db-a: …}` is `db-a`, and a flat-format file with no `tenants:` yields no tenant. ⚠️ A tenant file parse-tree cannot read (each named with its kind), one tenant declared in two files, or no usable da-guard ⇒ exit `2` and no CRD at all (`--json` envelope `reason`: `tenant_set_unknown`). A tenant id that is not RFC 1123 is warned about and skipped | `conf.d/` |
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

When the conf.d holds a path da-guard validation names, this tool refuses with `2` before it reads or writes anything, each path named (the [shared refusal](#confd-refusal), [#3206](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3206)).

**Purpose**: Migrate from ConfigMap native format to Operator native CRD; GitOps-friendly conversion; pre-migration validation and preview.

**Syntax**

```bash
da-tools migrate-to-operator [options]
```

**Options**

| Option | Description | Default |
|--------|-------------|---------|
| `--source-dir <DIR>` | Directory containing ConfigMap YAML files. Its top-level YAML files are read, listed by da-guard's list-only walk of the directory (`parse-tree --list-only`), so da-guard is required (exit `2` without it); files in subdirectories are not read, and stderr names them; top-level entries that are not read (a dot-prefixed hidden file or directory, a dangling symlink, a FIFO, ...) are each named, with the reason, on one `WARNING:` line. This directory is not a conf.d, so the conf.d refusal does not apply: a file that cannot be parsed gets a WARNING and is skipped | (required) |
| `--config-dir <DIR>` | Tenant config directory. Tenants are found as in `operator-generate`: the keys of each tenant file's `tenants:` across the whole tree (through `da-guard parse-tree`), not file names. ⚠️ An unreadable tenant file, one tenant declared in two files, or no usable da-guard ⇒ exit `2` (`--json` envelope `reason`: `tenant_set_unknown`). A tenant id that is not RFC 1123 goes into `issues` and is skipped | `conf.d` |
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
| `--rule-packs-dir <DIR>` | Rule Pack YAML directory; the PrometheusRule check compares the number loaded in the cluster with the `rule-pack-*.yaml` files here | `rule-packs/` |
| `--prometheus <URL>` | Prometheus API URL; the exporter target health check runs only when it is given, and is skipped otherwise. Given and its targets not read (unreachable, errored or answered in the wrong shape), that check FAILs and the exit code is 2 — it used to be a warn ([#3133](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3133)) | (none) |
| `--kubeconfig <FILE>` | kubeconfig path | (none) |
| `--ci` | Exit 1 when any check fails (without it a failure exits 0; kubectl unavailable, or `--prometheus` given and its targets not read, is always 2 — the latter [#3133](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3133)) | false |

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

**Exit codes**: `0` no drift, or no `--ci`; `1` drift with `--ci`; `2` caller error: the rule-packs dir is missing or empty, a rule pack fails to parse, no runtime source is given, or the `/api/v1/rules` answer cannot be read (unreachable, an error, or `data` / `data.groups` / a group / a rule of the wrong type; a `--runtime-json` file is checked the same way). `data: null` used to read as "no rule loaded", reporting every declared rule MISSING ([#3133](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3133)).

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

**How it reads conf.d** (ADR-036 step 4, [#2906](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2906)): always through `da-guard parse-tree` (yaml.v3), the exporter's own reading; an unquoted `yes` / `no` / `on` / `off` is a string. It needs da-guard: `$DA_GUARD_BINARY`, else `da-guard` on `$PATH` (the da-tools image ships one; in the repo, `make da-guard-build`); without one, or when it fails, the exit code is 2 and nothing is read — and the same when a file is nested so close to yaml.v3's limit (10,000 levels) that this tool cannot decode `parse-tree`'s JSON (measured: arrays nested 9,999 deep; 9,990 deep are read, and past the limit the file is a `syntax_error`). A file `parse-tree` cannot read (its `kind`: [ADR-036](adr/036-single-parser-effective-config.md) §2) is skipped whole and the refusal names the `kind`. `validate-config`, `policy-engine`, `explain-route` and `notification-tester` read conf.d through the generator and need da-guard too.

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
| `-o, --output <FILE>` | Output to file. **Read only by render (default) and `--output-configmap`, and not together with `--dry-run`**; with `--validate` / `--apply` / `--dry-run` it is a caller error (exit 2), never a silent no-write (#1650) | stdout |
| `--output-configmap` | Output complete Kubernetes ConfigMap YAML | false |
| `--base-config <FILE>` | Custom Alertmanager base config. **Only `--output-configmap` reads it**; supplying it in any other mode is a caller error (exit 2), not a silent no-op | built-in default (**only when the flag is omitted**; a supplied value that is unreadable / not valid YAML / not a mapping exits 2 rather than falling back) |
| `--dry-run` | Show preview without writing. **Not read by `--validate` / `--apply`** (exit 2) | false |
| `--validate` | Validate only, don't output. Any tenant file in conf.d that cannot be parsed → exit 1, with or without `--strict` (#1460). Once every other check passes, it also assembles the complete config on the **built-in default base** and hands it to `amtool` (see "Alertmanager validation" below; #2260) | false |
| `--strict` | Report these as ERROR with exit code 1 (CI runs `--strict`): domain-policy (ADR-007) violations (WARN without it); a `routes[i].match` value or `overrides[i].alertname` / `metric_group` that yaml.v3 reads as a non-string (an unquoted `8`, `true`, `2001-12-15`, `~`, ...; #2431, #2906; `yes` / `on` / `1:30` are strings; fix: quote it. Without it a routes entry is skipped with a WARN and an override is rendered via `str()`, e.g. `alertname="True"`; a falsy one — `0`, `false` — counts as no matcher: the override is skipped and `--validate` fails on it too); a `group_by` element that is not a non-empty string (an unquoted `8`, `true`, ...), repeats a label, or is `...` alongside other labels (#2503; without it the element is dropped with a `WARN … skipping` and the rest rendered, no `group_by` at all when nothing is left); a tenant `_routing` that is neither a mapping nor a disabling string, or a `_routing_defaults` that is not a mapping (#2341; without it a `WARN … skipping`: the tenant renders no route / the level contributes nothing, and `--validate` fails too); on the `--apply` / `--output-configmap` merge, an inhibit rule whose `equal:` label is not presence-gated (#1132, WARN without it) | false |
| `--apply` | Apply directly to Kubernetes (requires kubectl) | false |
| `--namespace <NS>` | Namespace of the ConfigMap. **Read only by `--apply` / `--output-configmap`**; exit 2 in any other mode | `monitoring` |
| `--configmap <NAME>` | ConfigMap name. **Read only by `--apply` / `--output-configmap`**; exit 2 in any other mode | `alertmanager-config` |
| `--yes` | Skip confirmation prompt with --apply. **Read only by `--apply`**; exit 2 in any other mode | false |
| `--policy <FILE>` | **Path** to a policy YAML holding an `allowed_domains:` list (omit for no constraint). ⚠️ This takes a file path, not a comma-separated domain list; a value that cannot be read exits 2 (#1556); so does a file with no `allowed_domains` key (empty, comments only, misspelled key) or a list with a non-string entry — write `allowed_domains: []` to say "no constraint" explicitly (#1649). Optional `min_generator_version: "X.Y.Z"`: exit 2 when the da-tools that runs is older (or cannot read its own version) — raise the da-tools version; a value not of the form `X.Y.Z` is exit 2 too (#2901). ⚠️ The v2.9.0 image still has the old behaviour | (unrestricted) <!-- image-caveat: v2.9.0 --> |
| `--findings-json <PATH>` | Also write the findings this run **prints** (each warning-stream and refusal line) as JSON to PATH; the shape is under "Structured findings" below. Read in every mode; written on every exit (refusals, caller errors, usage errors and exceptions included), to a temp file renamed into place, so PATH never keeps a previous run's document. stdout, stderr and the exit code are unchanged; a PATH that cannot be written exits 2 (#2766). ⚠️ The v2.9.0 image has no such flag <!-- image-caveat: v2.9.0 --> | (not written) |

**Output**

Every mode first prints one stdout line, `Config files: N read, M skipped (<names>)` (#1460) — N / M come from the structured record, not from the stderr WARN lines; when M > 0 and a skipped file is a tenant file, the run produces nothing further.

**Hierarchical conf.d ([#2326](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2326))**: the whole tree is read (the exporter's walk rules: hidden directories skipped, a directory holding only a README contributes nothing), and a tenant at any depth gets a route. Semantics in [ADR-017 Decision 9](adr/017-defaults-yaml-inheritance-dual-hash.en.md#9-routing-settings-are-inherited-level-by-level-along-directories): `_routing_defaults` comes from any root `_` file, then from the top level of the defaults carrier (`_defaults.yaml` / `.yml`) of every subdirectory on the tenant's path, a shallow merge per top-level key, deeper wins; `_routing_profiles.yaml` and `_domain_policy.yaml` may sit in a subdirectory and apply to that subtree only, with policies at every level judged additively. In **every mode** these exit 2 and produce and write nothing: `_routing_enforced` in any file below the root (an unselected defaults carrier excepted, see "One defaults carrier per directory" below); `receiver` or `overrides` written as null in a subdirectory level's `_routing_defaults`; one routing-profile name defined in two files (root `.yaml` beside `.yml` included). One tenant id declared by two tenant files exits 1 in every mode (`FAIL: N tenant(s) declared in more than one file`). A subtree policy whose `tenants:` names a tenant outside the subtree is an ERROR under `--strict` (exit 1) and a WARN otherwise; that entry is not enforced. The `tenants:` block of a platform file below the root is still read by no plane and only WARNs. `_routing_defaults` in a `_` file below the root that is not the defaults carrier is not read, and `--validate` exits 1 naming the file; a tenant entry with a null body (`t:` with nothing under it) counts as a declaration too. ⚠️ The v2.9.0 image reads the top level only: a tenant in a subdirectory gets no route, at exit 0 <!-- image-caveat: v2.9.0 -->

**One defaults carrier per directory ([#2906](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2906))**: when one directory (the root or any subdirectory) holds more than one defaults carrier spelling (`_defaults.yaml` beside `_defaults.yml`, or spellings differing in case), every plane reads one of them (`.yaml` first, the exporter's rule; [#1674](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1674)) and ignores the other whole. The generator prints, for each file not selected, `WARN: <file> is not read: … only <the file read> is read, on every plane (#1674); merge them into one file, skipping`; `--validate` (with or without `--strict`) and `validate-config`'s `schema` row exit 1 / FAIL on it; render, `--dry-run`, `--output-configmap` and `--apply` produce output as before, exit code unchanged; a write through tenant-api is therefore a 422 `GENERATOR_FINDINGS` until the two are merged. The generator does not open that file: whatever it holds (`_routing_enforced`, broken YAML) the result is the same, and no other finding is reported for it. The finding kind is `unselected_defaults_carrier`, the name of `da-guard parse-tree`'s `not_read` reason. Which file is not read is always parse-tree's `not_read`: a tool that walks the directory itself skips that file and names it — `check_routing_profiles` prints `skipped <N> file(s) no reader reads (da-guard parse-tree's not_read): …` on stdout, and `validate-config`'s `defaults_wrapper` / `schedule_null` rows add `not checked, no reader reads: …` to their detail. Fix: merge the two into one file. ⚠️ **Stricter**: such a tree used to print one multi-carrier WARN on stderr with `--validate` at exit 0; an unselected subdirectory spelling carrying `_routing_enforced` exited 2 in every mode, now only `--validate` exits 1. ⚠️ The v2.9.0 image does not report this, `--validate` exits 0: it reads both root spellings and no subdirectory at all <!-- image-caveat: v2.9.0 -->

**Tenant ids ([ADR-035](adr/035-tenant-id-single-source.en.md))**: a tenant id must be a DNS-1123 label — 1-63 lowercase letters, digits and `-`, starting and ending with a letter or digit (the rule is written once, as `definitions.tenantId` of `tenant-config.schema.json`). If conf.d declares even one invalid id (empty, upper case, `_`, `.`, a space, or longer than 63 characters), **every mode** (render, `--dry-run`, `--output-configmap`, `--apply`, `--validate`, with or without `--strict`) exits 1 and writes or applies nothing; the message starts `FAIL: N invalid tenant id(s)`, names each id and cites the rule. The tenant is not merely skipped: skipping it would deploy a config without it, and its alerts would fall through to the root receiver, which in the shipped base is the empty `default`. Fix: rename the tenant; quote an all-digit id. ⚠️ The v2.9.0 image does not check tenant ids and renders them <!-- image-caveat: v2.9.0 -->

**Boolean and enum fields are judged by type and value only ([#2906](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2906))**: when one of the fields below has the wrong type or a value the generator does not know, **every mode** (render, `--dry-run`, `--output-configmap`, `--apply`, `--validate`, with or without `--strict`) exits 1 and writes or applies nothing; the message starts `FAIL: N config problem(s) — nothing was generated, written or applied:` and prints one `ERROR (field type): …` line each, and `validate-config`'s `schema` row FAILs too, as does its `routes` row (since #3284; it used to show the route count of a run that generates nothing). A boolean field takes a boolean only: `_routing_enforced.enabled` (null is refused too; leaving the key out still means not enabled), a domain policy's `constraints.require_critical_escalation`, a receiver's `send_resolved`, email `require_tls` and webhook `http_config.proxy_from_environment` (for the last four null means unset; a receiver is judged wherever it is written — a tenant's `_routing`, an `overrides` / `routes` entry, `_routing_defaults`, a routing profile, `_routing_enforced` — and a profile no tenant references is judged too). A quoted `"true"`, `"yes"` or `"off"` is a type error and is no longer turned into the boolean it spells. A tenant's `_severity_dedup` takes `enable` / `disable` (also the exporter's `enabled` / `on` / `true` and `disabled` / `off` / `false`, trimmed, any case), a boolean (`true` is enable, `false` disable) or null (the default, enable); any other value (`"no"`, `"yes"`, `""`, a number) used to be read as enable and is now refused. An unquoted `enabled: yes`, `send_resolved: off` or `_severity_dedup: no` is refused too: the generator reads yaml.v3 through `da-guard parse-tree`, where they are strings. `group_by` takes a list or null only ([#3284](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3284)): a string (`group_by: alertname`), a number, a boolean or a mapping is refused and is never read as a one-label list — the generator used to treat it as not written, so the route had no `group_by` and inherited its parent's grouping, at exit 0 even under `--strict`. Every place the generator reads `group_by` is judged: a tenant's `_routing` (tenant file and a root platform file's `tenants:` block) — main route and `overrides` / `routes` entries —, every level's `_routing_defaults` — main route and `overrides` entries —, a routing profile (with its `overrides` / `routes` entries, judged even when no tenant references it), and `_routing_enforced`. null still means not written (the parent's applies) and `[]` still renders no `group_by`; for the list's elements see the `--strict` row (#2503). Fix: write `group_by: [alertname]`. The finding kind is `routing_enforced_enabled_invalid`, `domain_policy_unusable`, `invalid_receiver_field`, `severity_dedup_invalid` or `routing_group_by_invalid` (`group_by`), by field. A domain policy that cannot be enforced as written refuses the whole tree by the same rule ([#3116](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3116)): every case reported as `domain_policy_unusable`, judged in one place — the policy or its `constraints` is not a mapping; `tenants` is not a list or holds an entry that is not a tenant id; `forbidden_receiver_types` / `allowed_receiver_types` / `enforce_group_by` is not a list, or `enforce_group_by` holds an entry that is not a non-empty string (the rule for a `group_by` entry; a mapping entry used to stop the generator at rc 2); a receiver-type list holds an entry that is not a receiver type the generator renders (`email`, `pagerduty`, `rocketchat`, `slack`, `teams`, `webhook`; a misspelt `webhok`, `opsgenie`, an unquoted `yes`, `1`, `~`, a mapping all count); `max_repeat_interval` / `min_group_wait` does not read as a duration (`banana`, `-1h`); `require_critical_escalation` is not a boolean; the whole `_domain_policy.yaml` cannot be used (a syntax error, not UTF-8, a top level or `domain_policies:` that is not a mapping); a `domain_policies:` block in a file other than `_domain_policy.yaml` / `.yml` (`_defaults.yaml`, a tenant file, any other `_` file, at any depth; [#3126](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3126)) — that block is never read whatever it holds; the message names the file and says to move the block into the `_domain_policy.yaml` of the same directory (one in a subdirectory applies to that subtree only), and it used to be blocked under `--strict` only, a single WARN line otherwise. The message names each cause and its fix. Except for `require_critical_escalation`, these used to block under `--strict` only: without it the constraint, the policy, or every policy of the file was silently off at exit 0; a misspelt allowed type made every receiver a violation without saying the list itself was wrong. Null takes effect as nothing and is accepted, as the schema allows: a null policy, `constraints` or constraint value is no constraint, `tenants: null` applies the policy to no tenant, and a `domain_policies:` with no value is no policy (those two used to be judged unusable). Fix: write a boolean as an unquoted `true` / `false`, `_severity_dedup` as `enable` or `disable`, and correct a domain policy as each message says. ⚠️ The v2.9.0 image still accepts these forms <!-- image-caveat: v2.9.0 -->

**Routing blocks take no file-reference key ([#3257](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3257))**: when a tenant `_routing`, a routing profile, `_routing_defaults` (its dropped `routes` included), `_routing_enforced`, or routing written where no reader reads it (under `defaults:`, a threshold profile's `_routing`) has, anywhere in the block (receivers, every level under `http_config` and header names included; only the label names under `match` are not judged), a key ending in `_file` or `_ref` or a key named `files` (`bearer_token_file`, `basic_auth.password_file`, `tls_config.key_file`, `oauth2.client_secret_ref`, the `files` list of `http_headers`, …), **every mode** exits 1 and writes or applies nothing — the same refusal as the paragraph above (`ERROR (field type): … sets '<path>': a key ending in _file or _ref, or a 'files' list, …`): such a key makes Alertmanager read a file inside its own container or look for a secret manager it does not have. The check is a rule on the key name, not a list of fields; the schema states the same rule as `definitions.fileReferenceKey`. Write the credential's value itself; the platform's own credentials go on the platform's own receivers in `--base-config`, which this rule does not read. Separately, when the `global` section of `--base-config` sets a credential a receiver inherits — any `smtp_auth_*` (an email receiver without auth inherits it and sends it to the smarthost the tenant names) or `basic_auth` / `authorization` / `bearer_token` / `bearer_token_file` / `oauth2` under `global.http_config`, or an `http_headers` header there with a non-empty `secrets` / `files` list (a receiver without `http_config` inherits it and sends it to the tenant's URL) — `--output-configmap` exits 1 before reading conf.d and writes nothing, and `--apply` exits 1 on the cluster's config and applies nothing. To migrate, move the credential onto the platform's own receivers in the base config (for example the platform's email receiver carries its own `auth_password`) and keep no inheritable credential in `global`. A TLS client certificate and key (`tls_config`, `smtp_tls_config`) are not refused: the private key only proves the certificate's identity in the mTLS handshake and is never sent. ⚠️ The v2.9.0 image checks neither and renders as before <!-- image-caveat: v2.9.0 -->

**Keys the generator does not read ([#2960](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2960))**: a tenant `_routing`, a routing profile, `_routing_defaults` (root and directory carriers), `_routing_enforced`, and the receivers, `overrides` entries and `routes` entries' receivers under them are read for a fixed set of keys (`receiver`, `group_by`, `group_wait` / `group_interval` / `repeat_interval`, `overrides`, `routes`; an override also `alertname` / `metric_group`; `_routing_enforced` also `enabled` and `match`, but not `overrides` / `routes`; a receiver the fields of its type). Any other key — an Alertmanager field such as `continue`, `mute_time_intervals`, `active_time_intervals` or `matchers`, or a typo such as `recevier` or `enabeld` — leaves the output exactly as if it were absent, so each one is named in a `WARN: <where>: '<key>' is not read by the route generator (…)` line with what kind of field it is or a "did you mean" hint; `--validate` and `validate-config` exit 1 on that line, while render, `--dry-run`, `--output-configmap` and `--apply` still write and keep their exit code. Keys are read as written (`1:` and `on:` count), a null key does not; nothing under `http_config` is looked at — it is passed to Alertmanager as written. The key sets are what the generator reads, and the JSON schema is pinned to the same sets; the finding kind is `routing_key_ignored`. da-guard does not judge these keys: a tree with routing content is only listed as undetermined evidence (exit 4). A write through tenant-api is judged by the generator and answered with 422 `GENERATOR_FINDINGS` (a batch patch that sets a `_routing*` key the generator does not read is refused by tenant-api itself first). A `routes` entry's own keys are still that entry's check (the entry is skipped). Keys starting with `_routing` are judged by exact name too ([#2983](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2983)): a tenant's config is read for `_routing` and `_routing_profile` only, the top level of a `_` file for `_routing_defaults` and `_routing_enforced` only; any other `_routing*` key — a misspelt `_routing_profle` or `_routing_defualts`, a `_routing_defaults` in the wrong place (in a tenant), or the flat `_routing_receiver_type` that is no longer read — prints `WARN: <tenant or top level in <file>>: '<key>' is not a key the route generator reads (…)`, with the same verdict as above. A spelling that does not start with `_routing` (`_routng`) still gets only the `unknown reserved key` WARN, which blocks nothing. ⚠️ The v2.9.0 image names none of these keys and `--validate` exits 0 <!-- image-caveat: v2.9.0 -->

**`_routing_enforced.match` takes a list only ([#2989](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2989))**: `match` is a list of Alertmanager matcher strings (`['severity="critical"']`). Written as the `routes[i].match` mapping (`{severity: critical}`), a single string, a number or a boolean, or as a list with an element that is not a non-empty string (as yaml.v3 reads it: `[1]`, `[~]`, `[true]`, `['']`, `[{severity: critical}]`), the `_routing_enforced` block is not enabled: no enforced route is rendered (none is expanded for the `{{tenant}}` shape either), the generator prints `WARN: _routing_enforced in <file>: 'match' must be a list of Alertmanager matcher strings … skipping`, and `--validate` and `validate-config` exit 1; render, `--dry-run`, `--output-configmap` and `--apply` still write and keep their exit code, and this block then produces no NOC route (rather than one that receives everything), so always run `--validate` before deploying. A disabled block (`enabled: false`) is not judged. An enabled `_routing_enforced` in an earlier root `_` file (name order) stays in force. Omitting `match` or writing null still means no matcher. Before, such a `match` was dropped without a word and the NOC route received every alert. ⚠️ The v2.9.0 image still drops it silently and `--validate` exits 0 <!-- image-caveat: v2.9.0 -->

**Duration syntax ([#2490](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2490))**: `group_wait` / `group_interval` / `repeat_interval` (tenant `_routing`, overrides, `routes` entries, `_routing_defaults`, `_routing_enforced`, routing profiles) are read the way Alertmanager reads them — whole numbers with units, largest unit first, each unit at most once (`y`, `w`, `d`, `h`, `m`, `s`, `ms`), or a bare `0`; the rule is written once, as `definitions.duration` of `tenant-config.schema.json`. `1h30m`, `90m` and `1d` are used (then clamped by the guardrails); a fraction (`1.5h`), units out of order (`30m1h`) or repeated (`1h1h`), `ns` / `us`, a sign, surrounding spaces or a value past about 292 years makes Alertmanager refuse the whole config, so the generator renders the platform default in its place and prints `WARN: … invalid <param> '<value>' …`, and `--validate` and `validate-config` exit 1 on that line; render and `--dry-run` still write (with the default) and keep their exit code. ⚠️ The v2.9.0 image turns `1h30m` into the platform default and hands `1.5h` to Alertmanager verbatim, `--validate` exiting 0 either way <!-- image-caveat: v2.9.0 -->

**Structured findings (`--findings-json`, [#2766](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2766))**: the top level holds `schema` (`da-tools.findings/v1`), `generator_version` (the da-tools version, null when unreadable), `config_dir`, `exit_code` (the exit code the run actually ends with), `validate` / `strict` (whether the run had that flag; some `blocks: strict` findings are produced only under `--strict` (`domain_policies:` written in a file the generator reads no policy from, for one — [#3126](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3126)), so their absence without the flag is no evidence; a domain policy violation is listed as a WARN either way) and `findings`. Each finding carries `kind` (a stable snake_case id, spelled as the kind of the same meaning in the [§guard](#guard) table — that table keeps the routing kinds as definitions only, da-guard no longer emits them; a `_routing_profile` naming an undefined profile is still `unclassified`, not the table's `unknown_routing_profile`; `unclassified` means not classified yet, so only its text can be relied on), `severity` (the SARIF level: `error` / `warning` / `note`), `blocks` (when it fails the generator: `always` in every mode, `strict` under `--strict`, `validate` under `--validate`, `never`), `tenant` / `policy` / `file` (relative to `--config-dir`) / `field` (null when not known) and `message` (the printed line, leading indentation stripped). A blocking finding whose `tenant` is null affects the whole config, not "no tenant". Only warning-stream and refusal lines this run printed are listed (a few WARN lines the loader prints directly are not): a refused run lists its refusal, and the errors `--validate` prints a second time are not repeated. The one exception is `run_failed`: when the exit code is non-zero but no finding blocks in this run's mode (an amtool rejection, a refused assembly, a caller error), the document adds that one finding itself (`error`, `always`) and the reason is in stderr, so a reader of the findings alone never takes a failed run for a pass. ⚠️ The v2.9.0 image has no such output <!-- image-caveat: v2.9.0 -->

**Fragment Mode** (no `--output-configmap`):
YAML fragment containing route, receivers, inhibit_rules.

**ConfigMap Mode** (`--output-configmap`):
Complete Kubernetes ConfigMap YAML with global, route, receivers, inhibit_rules, ready for `kubectl apply`.

**Alertmanager validation (#2219, #2260)**: when `amtool` is on PATH, `--output-configmap` and `--apply` run `amtool check-config` on the exact `alertmanager.yml` about to be written / applied, and a rejected config is neither written nor applied; without `amtool` a `NOTICE: ... was NOT validated by Alertmanager` line goes to stderr and nothing else changes. `--validate`, once every other check passes and before it prints `OK`, also assembles the generated config on the **built-in default base** (not your `--base-config` — `--validate` never reads it) and runs `amtool check-config` on that: a rejection exits 1, `amtool` failing on its own exits 2; without `amtool` a NOTICE line says so and the exit code is unchanged. A passing `--validate` therefore says nothing about your own base — run `--output-configmap --base-config` for that. Fragment mode (not a complete config) is not validated this way. The da-tools image bundles `amtool`, taken from the same Alertmanager image the deployment manifest pins (same tag and digest; #2294), so inside the image this validation runs by default. ⚠️ The v2.9.0 image does not bundle `amtool`, and its `--validate` does not go through `amtool` <!-- image-caveat: v2.9.0 -->

**WARN when the root receiver has no integration (#2660)**: when the `alertmanager.yml` that `--output-configmap` / `--apply` is about to write / apply has a root route whose receiver has no non-empty `*_configs` (or names no defined receiver), stderr gets one line `WARN: the root route's receiver '<name>' has no integration …` — alerts no child route claims (platform self-monitoring alerts included) end there and notify no one; see [BYO integration guide §11](integration/byo-alertmanager-integration.en.md#11-delivering-platform-self-monitoring-alerts) for wiring them. What is judged is the `--base-config` root receiver (without it, the built-in base's empty `default`, so the default run always prints it), or the cluster's under `--apply`. Advisory only: not an error, not escalated by `--strict`, exit code unchanged; render and `--validate` never print it. ⚠️ The v2.9.0 image does not print it <!-- image-caveat: v2.9.0 -->

**When the platform alert probe set is degraded (#1533)**: the check that a tenant-triggered inhibit rule cannot silence a platform self-monitoring alert probes every alert in `configmap-rules-platform.yaml` (beside the tool in the image, under `k8s/03-monitoring/` in a checkout). When that file is not found, cannot be read, or yields no platform alert, the probe set falls back to a built-in 6 entries and stderr gets one `WARN: platform alert identity probe set degraded …` line. When the file is only partly read (a malformed group / rule was skipped, or one of the built-in 6 names is missing from it), the probe set is what was read plus whichever of the built-in 6 entries it lacks, stderr gets `WARN: platform alert identity probe set is INCOMPLETE …`, and it counts as degraded too (`--validate` then prints `Probe set: DEGRADED, shipped platform alert identities only partly read …`). If, in that state, the inhibit rules supplied by you — the `--output-configmap` `--base-config`, or the cluster's existing config under `--apply` — include a tenant-triggered rule (its source matchers require `tenant`), the run exits 2 and writes / applies nothing: the invariant could not be verified, so it is not a verdict on your config. The message names the cause and the offending `inhibit_rules[<i>]`. Without such a rule the run continues as before with only the WARN; generated rules do not count, nor does any rule whose target likewise requires `metric_group` (e.g. a copy of them in the base) — no platform alert carries that label — and neither does a rule whose target already excludes platform alerts (e.g. `alert_source=""`, the fix the violation message recommends). `--validate` keeps its exit code and adds one stdout line `Probe set: DEGRADED to the 6-entry built-in fallback …`. Not tied to `--strict`. ⚠️ The v2.9.0 image only prints the WARN, exits 0 and writes / applies <!-- image-caveat: v2.9.0 -->

**Routing checks moved from da-guard ([#2904](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2904))**: [ADR-036](adr/036-single-parser-effective-config.md) takes routing and policy judgements away from da-guard, so the checks only da-guard had are reported here, under the kind names da-guard used (defined in the [§guard](#guard) table): `routing_in_unread_location` — a `_routing` or `_routing_*` key where this tool never reads it (inside the `defaults:` block of any level's selected `_defaults.yaml`, or inside a threshold profile of a root `_` file's `profiles:` — there `_severity_dedup` and any key starting with `_routing` count ([#2914](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2914)); a `_routing*` key the generator does not read at the top level of a `_` file is `routing_key_ignored` instead, see "Keys the generator does not read" above): a `WARN: <file>: <key path> is not rendered … skipping` line, and `--validate` exits 1 — `--validate` only: `--strict` without it (a render such as `--output-configmap --strict`) prints the WARN and still writes. A `defaults:` mapping is the wrapper whatever its keys (`defaults: {1: 5}` too), as the exporter reads it; `routing_profiles_unusable` — `routing_profiles:` in `_routing_profiles.yaml` is not a mapping: a WARN that does not block; `duplicate_override_matcher` — `overrides[i]` has the same matcher as an earlier override, which catches those alerts first, so it never takes effect; `redundant_override_receiver` — `overrides[i].receiver` is identical to the main receiver. The last two judge the resolved routing and are WARN lines that never block, in any mode. For a receiver, `conflicting_receiver_field` (both fields of an `exactly one of` group set) now names the extra field in `field` (e.g. `receiver.routing_key`). Whether `http_config.proxy_url` parses as a URL is left to Alertmanager's parser (`amtool`, bundled in the da-tools image): without `amtool` on PATH (e.g. the tools installed with pip) `--validate --strict` does not judge whether `proxy_url` parses, and a malformed proxy URL passes. ⚠️ For this tool `routing_in_unread_location` is **stricter**: the same tree used to pass `--validate` with 0. ⚠️ The v2.9.0 image has none of these <!-- image-caveat: v2.9.0 -->

**Receiver names must be unique (#2279)**: a tenant id may contain `-`, so tenant `<t>`'s `routes[0]` receiver (`tenant-<t>-route-0`) can have the same name as the main receiver of a tenant called `<t>-route-0` (likewise `-override-<n>`). When two sources generate a receiver of the same name, every mode exits 1, writes nothing and applies nothing, with or without `--strict`, and the message names both sources. With `--output-configmap --base-config`, a base receiver named like a generated receiver also exits 1 and writes nothing (otherwise the base one would replace the conf.d one); the platform's fixed `custom-alerts-firehose` / `watchdog-heartbeat` / `synthetic-receiver` / `sentinel-sinkhole` let a base definition win by design and do not count. `--apply` still replaces a same-named receiver in the cluster with this run's. ⚠️ The v2.9.0 image exits 0 in both cases <!-- image-caveat: v2.9.0 -->

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
| `1` | Config validation failed; **or conf.d holds a tenant file that could not be parsed / read** (bad YAML, not UTF-8, top level not a mapping, a directory named `x.yaml`) — refused in every mode, with or without `--strict`, and the file is named on stdout (#1460); **or the `amtool` on PATH rejected the config `--output-configmap` / `--apply` was about to write / apply** — nothing written, nothing applied (#2219); **or it rejected the config `--validate` assembled on the built-in base** (#2260); **or two sources generate a receiver of the same name** (every mode, with or without `--strict`), or the `--output-configmap` base has a receiver named like a generated one (#2279); **or the assembly violates a platform invariant** (e.g. a base inhibit rule that would let a tenant silence a platform alert) — `--output-configmap` / `--validate` report it as `FAIL:` (#2260), and so does `--apply` when the cluster's existing config violates one, applying nothing (#2506); **or one tenant id is declared by two tenant files** (every mode, see "Hierarchical conf.d" above); **or conf.d declares an invalid tenant id** (every mode, with or without `--strict`; see "Tenant ids" above; ADR-035); **or `--validate` meets a duration Alertmanager does not accept** (see "Duration syntax" above; #2490); **or a routing key the generator does not read** (see "Keys the generator does not read" above; #2960, #2983) |
| `2` | Caller error: **the tool could not do its job because of how it was invoked or its environment** — not because your config violates something. Reaching it today (non-exhaustive): `--policy` / `--base-config` supplied but unusable (not a file, unreadable, not valid YAML, top level not a mapping); `--base-config` used in a mode other than `--output-configmap`; **`-o` / `--dry-run` / `--namespace` / `--configmap` / `--yes` used in a mode that never reads them** (the message names the flag and the mode and gives a remedy argparse accepts; #1650); the `-o` output path cannot be written; `--apply` without `--yes` where stdin cannot be read; and kubectl / cluster operations failing (#1556, #1616, #1617); `amtool` on PATH but not runnable, timed out, or failing without a rejection verdict; Alertmanager's `/-/reload` failing after `--apply` (before v2.10.0 a WARN at exit 0; #2219); a conf.d tree whose shape the routing plane refuses (the three exit-2 cases under "Hierarchical conf.d" above, message starting `ERROR: N routing-tree error(s)`; #2326); the platform alert probe set is degraded while a tenant-triggered inhibit rule you supplied (base config or cluster config) could not be verified — nothing written, nothing applied (see "When the platform alert probe set is degraded" above; #1533). ⚠️ **This row is the v2.10.0 contract**; the `v2.9.0` image pinned at the top of this page returns 0 or 1 for most of them <!-- image-caveat: v2.9.0 --> |

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

**Locating the tenant**: the key patched is **the one** YAML key in `threshold-config` whose `tenants:` mapping declares the tenant (whatever its name, casing or `.yml` spelling), and the patch is written back to that key. The `_defaults` key is matched in any casing, `.yaml` or `.yml`. When no key declares the tenant, a concrete value creates `<tenant>.yaml` in the multi-file layout and goes into `config.yaml` in the legacy layout. `default` is a no-op when the tenant does not set that metric (exit `0`), as is a value the target already holds as its source text (the cell is left as it is, type and spelling kept: `'50'` typed as `50` stays a string); a no-op still goes through `da-guard write`'s judgement, so on a key validation names it is refused (`read_back_mismatch`). A tenant block it leaves empty is kept. A key holding only comments is edited as an empty one, the comments kept first.

**Reading**: a scalar is its source text, never type-converted (`010` is `010`).

**Refused (exit `2`, nothing written)**: two or more keys declaring the tenant; no readable key declaring the tenant while some key cannot be read (the message names those keys); a ConfigMap whose `data` is not a mapping; two `_defaults` keys, or neither a `_defaults` nor a `config.yaml` key; a tenant block that is not a mapping; a key to rewrite that declares more than one tenant (except `config.yaml` in the legacy layout); a key to create that starts with `.` or `_`; a rewrite `da-guard write` refuses (next paragraph). A key the exporter cannot read and validation names (e.g. a tenant value that is a list) can still be patched: setting that value to a valid one repairs it, but a rewrite that leaves the key unreadable is refused. The rewrite touches only the value it changes: the key's other nodes, its comments, flow style (`{k: v}`) and how each scalar is spelled (`010`, `12:30`, `!!float 1`, `~`) stay as they were; only the layout is redone (2-space indentation, one space before a trailing comment, blank lines may go). ⚠️ The value written: canonical decimal (`50`, `-1`, `0.5`) is written as a number, anything else as a double-quoted string (`"70:critical"`, `"010"`, `"disable"`); it used to be a string always (`'50'`).

**Reading, and the write ([ADR-036](adr/036-single-parser-effective-config.en.md) §2, #2906 / [#3206](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3206))**: who declares the tenant, what its block holds and whether to write are all taken from `da-guard parse-tree` (the ConfigMap's keys read as the flat directory the exporter mounts, the yaml.v3 way: `yes:` and `on:` are string keys, a `<<` merge is expanded). The text of the rewrite is Go's too: the ConfigMap's keys are laid out in a private temporary directory (the same flat directory), `da-guard write --dry-run` is run on the key to change, as an edit (as a create for a new `<tenant>.yaml`), and the patch carries the text it answers. When da-guard refuses (e.g. the re-encode would also change another tenant sharing an anchor: `not_round_trippable`; validation would still name the text to be written: `read_back_mismatch`) or answers a caller error, nothing is written (exit `2`, `reason: write_refused`, the message carries da-guard's reason and words); a key parse-tree cannot read (a duplicate key, a syntax error, more than one document, a tag outside its allowed set, ...) counts as unreadable (`reason: configmap_shape`). No usable da-guard (`$DA_GUARD_BINARY`, then `$PATH`) is exit `2`, `reason: da_guard_unavailable`.

**Post-write verification (apply)**: new bytes equal to the key's current bytes ⇒ nothing is written or waited for, exit `0`. Otherwise it first reads, through `kubectl get --raw …/pods/<pod>:<port>/proxy/…` (GET only) on every Running pod not being deleted, the pod's [`/api/v1/config/identity`](api/README.en.md) (`config_hash`: which bytes it serves; `parse_failed`: the keys it left out because they did not parse) and `/metrics`. After the patch it computes, from the ConfigMap the patch returned, the `config_hash` an exporter serving exactly that version reports (the keys the exporter reads — not dot-prefixed, a `.yaml` / `.yml` extension in any case — in key order, each SHA-256'd, the hex digests concatenated and SHA-256'd again; for a pod in single-file mode the SHA-256 of `config.yaml`) and waits until every pod reports it; `/metrics` is then read between two identity reads and kept only if both show the same install, else read again (bounded; past the bound it counts as a timeout). It then compares every `user_*` series on each pod; parse failures are judged on `parse_failed` after the write: the patched key in it, or another key that was not in it before ⇒ failure, another key already in it before ⇒ warning only. ⇒ Success means every pod serves the whole ConfigMap at the version this write produced, byte for byte, and did not leave the patched key out as unparseable; whether those bytes then do what was meant (e.g. a non-numeric value falling back to the default, an unknown key ignored) is not covered — see `patch-config --help`. A pod whose exporter answers the identity path with 404 (an older exporter) is verified the older way (wait for `/api/v1/config` `Last reload` to change, compare `da_config_parse_failure_total`), with a note on stderr and `pods.<pod>.identity` set to `unavailable (404)` in `--json` (`checked` otherwise). For a key starting with `_` (`_silent_mode`, `_profile`, ...) the target tenant's own series are not judged, only listed on stderr (and in `--json`). A mismatch, a timeout, an unreachable pod on the way, Ctrl-C / SIGTERM or an unexpected error after the write ⇒ the old bytes are patched back (a key that did not exist is removed) and it exits non-zero; The write and the rollback are both conditional on the ConfigMap's `resourceVersion`: changed since it was read ⇒ nothing is written; the patched key changed by someone else before the rollback ⇒ no rollback; verification also requires the ConfigMap to still be at the version the write produced. If the patch call itself fails, the ConfigMap is re-read to tell whether it landed. The exact rules and known residuals: `patch-config --help`.

**`--diff`**: says only what would be written (`after`) and whether apply would write (`changed`, apply's own verdict — `false` when apply would send no patch). **It does not show the current value**: under `--json`, `before` is always `null`, and the text output prints one line saying to check the exporter instead (reading one key's current value here would mean redoing the exporter's key→series mapping in Python, which [#1950](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1950) ruled out). For `default`, `after.state` is `key removed`; what applies once the key is gone is not inferred.

**`--json`**: stdout is one JSON document on every terminal path (except when stdout is closed, or a signal ends the process before anything is written); `--json --help` exits `0` with `status: "help"`. On exit `2` it carries the preview keys with empty values plus `status: "caller_error"` and `reason` (`bad_arguments` / `configmap_shape` / `write_refused` / `da_guard_unavailable` / `configmap_changed` / `kubectl_failed` / `unexpected_error`). Apply's document keeps the preview keys (`before` / `after` are `null`) and adds `status` (`no-op` / `applied` / `verify-failed-rolled-back` / `timeout` / `unreachable` / `rollback-failed` / `interrupted-rolled-back` / `error-rolled-back` / `state-unknown` / `overwritten-by-another-writer`), `exit_code`, `written` (the ConfigMap holds the new bytes as it exits; `null` when unknown), `rolled_back`, `message`, and `pods.<pod>` with `identity` (`checked` / `unavailable (404)`), `problems` / `warnings` / `target_changes` (the target tenant's changed series; `before` / `after` `null` means absent).

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
| `2` | Caller error, **nothing written**: `kubectl` could not run or exited non-zero while reading the ConfigMap or sending the patch (e.g. not on PATH, cluster unreachable, ConfigMap missing, no permission; a failure listing pods is `4`), any refusal above, the ConfigMap changed after it was read (`reason: configmap_changed`; re-run), arguments argparse rejects, or an unexpected exception before the write |
| `3` | Some pod was not serving the written bytes when `--reload-timeout` ran out (its `config_hash` differs; an older exporter: no reload seen); rolled back |
| `4` | Exporter unreachable (no pod matches the selector, pods/proxy failed, an unexpected response shape): before the write nothing was written, after it the write was rolled back |
| `5` | The rollback itself failed: the ConfigMap may still hold the new bytes; fix it by hand |
| `6` | Ctrl-C / SIGTERM or an unexpected error after the write; rolled back (interrupted before the write, it ends as it normally would, nothing written) |
| `7` | What the ConfigMap now holds cannot be told (not readable, or neither version), or another writer changed the patched key after this write (not rolled back); check it by hand |

#### generator-service

The generator container's service ([ADR-036](adr/036-single-parser-effective-config.en.md) §3, §5, §6, [#2901](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2901)). It runs continuously, and tenant-api asks it before every write whether the whole conf.d, with those writes applied to that base commit, passes `generate-routes --validate --strict`. HTTP/1.1 over a Unix socket only, no network port. The tenant-api chart deploys it as a required sidecar, and tenant-api asks it before committing every write that changes conf.d; how its answers map to tenant-api responses is in [Tenant API hardening §3.9](api/tenant-api-hardening.en.md#39-every-write-asks-the-generator-container-first-2903) ([#2903](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2903)).

```bash
da-tools generator-service --socket <path> --repo <git repo> --config-subdir conf.d --work-dir <dir> [--policy <FILE>]
```

**Main parameters**

| Parameter | Purpose |
|------|------|
| `--socket <path>` | Unix socket to listen on (created with mode 0600; a peer whose uid differs from the service's is closed unanswered; the threat model is in the `generator_service.py` module docstring). An old socket file at the path is replaced; any other kind of file refuses the start (exit 2) |
| `--repo <dir>` | The git repository (a read-only mount is enough). Files always come from git objects (`git ls-tree` plus `git cat-file`), never from the working tree and never through `git archive` |
| `--config-subdir <path>` | conf.d's path inside the repository (`.` is the root; an absolute path or a `..` segment refuses to start, exit 2) |
| `--work-dir <dir>` | Scratch directory for the tree of each judgement; removed when the judgement ends (also when it is killed) |
| `--policy <FILE>` | Deployment policy file (`allowed_domains`, `min_generator_version`), read again for every judgement |
| `--warm-ref <ref>` | The commit warmed at start (default `HEAD`; an empty value turns it off). One that does not resolve is logged and counted, and the service stays usable |

**Other parameters**

| Parameter | Purpose | Default |
|------|------|------|
| `--max-judgement-seconds <SEC>` | Upper bound of one request's time, queue included | `20.0` |
| `--deadline-reserve-seconds <SEC>` | Time kept back for the caller out of its `deadline_ms` | `5.0` |
| `--memory-limit-bytes <BYTES>` | `RLIMIT_DATA` of each judgement process | `201326592` |
| `--cache-max-bytes <BYTES>` | Parse-cache bytes kept for content not in the latest base tree, least recently used dropped first | `33554432` |
| `--max-cache-transfer-bytes <BYTES>` | New parse-cache bytes one judgement may hand back; the rest are dropped | `67108864` |
| `--max-queue <N>` | Requests that may wait; more are rejected as `busy` | `32` |
| `--max-connections <N>` | Open client connections served at once; past it a new connection is answered `busy` and closed | `40` |
| `--max-body-bytes <BYTES>` | Largest request body; larger is 413 | `4194304` |
| `--max-inflight-body-bytes <BYTES>` | Request body bytes being read or queued at once (reserved from `Content-Length` before the body is read); past it a request is answered `busy`. Below `--max-body-bytes` the service refuses to start | `4194304` |
| `--read-timeout-seconds <SEC>` | Time to receive a request's header, and again its body | `10.0` |
| `--max-result-bytes <BYTES>` | Largest result a judgement process may send | `16777216` |
| `--max-writes <N>` | Writes in one request; more is a 400 | `10000` |
| `--warm-seconds <SEC>` | Time limit of one warming run; a request arriving ends it at once | `300.0` |
| `--git <PATH>` | git executable | `git` |
| `--da-guard-binary <PATH>` | da-guard used for the reported version | `$DA_GUARD_BINARY`, else `da-guard` on `$PATH` |

**Endpoints**

| Endpoint | Response |
|------|------|
| `POST /v1/validate` | Body: `base_commit` (a full hex commit id), `writes` (`{path, content_b64}` or `{path, delete: true}`, paths relative to conf.d), `deadline_ms` (the caller's remaining time). Always 200 when there is a verdict: `result` is `pass` (generator exit 0), `findings` (non-zero) or `rejected` (this request only; `reason` is `deadline`, `memory`, `incomplete`, `git_read` or `busy`; `busy` is also the answer when the open connections or the body bytes being read or queued are at their limit, and the body is then not read), with `rc`, `findings` (the generator's `--findings-json` document), `stderr_tail` and `validated` (`base_commit`, the sha256 of each written file, the sha256 of the deployment policy file). A malformed request is 400, and so is a write path that conflicts with the base tree (a parent that is a file or symlink there, a path that is a directory). When the container cannot judge at all, 503: `VALIDATOR_UNAVAILABLE` (`reason`: `amtool_missing`, `generator_too_old`, `internal_error`) or `POLICY_UNAVAILABLE` (the deployment policy file is unusable) |
| `GET /v1/status` | Whether the deployment policy file is usable (with its sha256, error and `min_generator_version`), the generator and da-guard versions, whether amtool is present, `ready` |
| `GET /metrics` | Prometheus text format: cache size (`pinned` marks files in the latest base tree), the latest judgement's peak memory and the judgement process's memory limit, counts by result and by rejection reason, queue depth, versions |

Each judgement runs in a forked process (its own process group, `RLIMIT_DATA` set, the whole group ended when the deadline passes), one at a time; the others queue, and queue time counts against the deadline. Parse results of conf.d files are cached across requests by (SHA-256 of the file's content, parse mode), so when one file of a tree changes only that file is parsed again. The judgement process hands back each parse the moment it finishes, so a judgement ended by its deadline still leaves its completed parses in the cache, and the next request carries on from there.

**Warming**: at start (`--warm-ref`), and after a request whose `base_commit` differs from the current base, the service reads that commit's whole tree in the same kind of process outside any request, only to fill the cache — no verdict, no amtool — with its own time limit (`--warm-seconds`). A request arriving ends the warming run at once and goes first; what it already read is kept. `da_generator_warm_runs_total{outcome}` (`completed`, `preempted`, `deadline`, `failed`) and `da_generator_cache_base_coverage_ratio` (the share of the latest base tree's files already cached; also on the status endpoint) in `/metrics` show how warming goes.

**Exit codes**

| Code | Meaning |
|------|------|
| 0 | Stopped normally after SIGTERM / SIGINT |
| 2 | Caller error: `--repo` is not a git repository, the `--socket` path holds a file that is not a socket or cannot be listened on, `--work-dir` cannot be created, the `RLIMIT_NOFILE` hard limit cannot hold `--max-connections` plus the reserved descriptors, `--max-inflight-body-bytes` below `--max-body-bytes` |

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
| `--catalog` | List the supported exporters and exit | false |
| `--generate-profile <NAME>` | Generate a `_profiles.yaml` skeleton instead, with this profile name; requires `--db` | (none) |
| `--tier <TIER>` | Threshold tier for `--generate-profile`: `prod` or `staging` | `prod` |
| `--from-onboard <FILE>` | Read `onboard-hints.json` and generate a config for each tenant from the onboard results | (none) |
| `--profile <NAME>` | The tenant YAML references this profile (`_profile`) and holds only explicit overrides | (none) |
| `--topology <TOPOLOGY>` | Tenant mapping topology (ADR-006): `1:1`, `N:1` (needs `--namespaces`), `1:N` (generates a mapping hint; needs `--mapping-instance` and `--mapping-filter`); a missing companion only prints a WARN | `1:1` |
| `--mapping-instance <NAME>` | Source instance name for the `1:N` topology (e.g. `oracle-prod-01`) | (none) |
| `--mapping-filter <PROMQL>` | PromQL filter for the `1:N` topology (e.g. `schema=~"app_a_.*"`) | (none) |
| `--silent-mode <MODE>` | Write `_silent_mode`: alerts still fire (the TSDB records them) but no notification is sent; `warning` / `critical` / `all`, `disable` is the same as not writing it | (not written) |
| `--severity-dedup <MODE>` | `disable` writes `_severity_dedup: disable`; `enable` (suppress the warning notification while critical fires) is the default and is not written | `enable` |
| `--routing-receiver <ADDR>` | Alert routing receiver address (a URL or an email list, by `--routing-receiver-type`); `_routing` is written only when it is given | (none) |
| `--routing-receiver-type <TYPE>` | Receiver type: `webhook` / `email` / `slack` / `teams` / `rocketchat` / `pagerduty` | `webhook` |
| `--routing-smarthost <HOST:PORT>` | SMTP smarthost for an email receiver (e.g. `smtp.example.com:587`) | (none; `localhost:25` for an email receiver) |
| `--routing-group-by <LIST>` | Comma-separated `group_by` labels | (none; `alertname,tenant` when `_routing` is written) |
| `--routing-group-wait <DURATION>` | `group_wait` (range 5s–5m) | (none; `30s` when `_routing` is written) |
| `--routing-group-interval <DURATION>` | `group_interval` (range 5s–5m) | (none; `5m` when `_routing` is written) |
| `--routing-repeat-interval <DURATION>` | `repeat_interval` (range 1m–72h) | (none; `4h` when `_routing` is written) |
| `--routing-profile <NAME>` | Write a `_routing_profile` reference (ADR-007), e.g. `team-sre-apac` | (none) |

The tenant-setting flags from `--profile` through `--routing-profile` apply only in non-interactive mode (`--tenant` plus `--db`); interactive mode and `--from-onboard` do not read them.

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
| `1` | Only an uncaught exception (traceback on stderr). Most invalid input is 2, but a broken or wrongly shaped `--from-onboard` JSON currently also ends in a traceback at 1 | <!-- datools-cmd-ignore: only a traceback gives 1, no exit to trace -->
| `2` | Caller error: bad arguments, an unsupported `--db` type, `--non-interactive` without `--tenant` or `--db`, or the output path given to `-o/--output-dir` cannot be written (#1641); a tenant id that is not a DNS-1123 label (`--tenant`, the interactive prompt, or any id in the `--from-onboard` list; [ADR-035](adr/035-tenant-id-single-source.en.md)) — nothing is written; before `_defaults.yaml` and `<tenant>.yaml` (and, for 1:N, `_instance_mapping.yaml`) are written, each is read back by `da-guard parse-tree` at its path in the `-o` directory, and unless it holds exactly what was generated no file is written (`--generate-profile`'s `_profiles.yaml` is confirmed the same way; `--from-onboard` confirms every tenant's files first, and one refusal writes none of them) ([ADR-036](adr/036-single-parser-effective-config.en.md) §2, #2906); no usable da-guard is 2 as well. ⚠️ The v2.9.0 image does not check tenant ids <!-- image-caveat: v2.9.0 --> |

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
| `--prefix <PREFIX>` | Custom metric prefix | `custom_` |
| `--no-dictionary` | Disable heuristic dictionary matching | false |

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
| `1` | Only an uncaught exception (traceback) returns 1. A missing input file or a YAML syntax error is 2, but an empty file, a top level that is not a mapping, or non-UTF-8 content currently ends in a traceback at 1 | <!-- datools-cmd-ignore: only a traceback gives 1, no exit to trace -->
| `2` | Caller error: bad arguments, an input file that cannot be read or is not valid YAML, or the output path given to `-o/--output-dir` cannot be written (#1641); before any file is written, `tenant-config.yaml` and `defaults-snippet.yaml` are read back by `da-guard parse-tree` at the path they are written to, and if either does not hold what was generated (for example a `--prefix` holding ` #`, which turns the line into a comment) no file is written ([ADR-036](adr/036-single-parser-effective-config.en.md) §2, #2906); in the standard mode no usable da-guard is also 2 (`--dry-run` and `--triage` do not need it) |

---

#### validate-config

One-stop configuration validation: YAML format, schema, routing, policy, version consistency.

**Purpose**: CI/CD gate check; verify config completeness before deployment.

**Syntax**

```bash
da-tools validate-config --config-dir <path> [options]
```

**Prerequisite: da-guard** ([#2115](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2115), [#2906](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2906)): every row reads conf.d through `da-guard parse-tree` (ADR-036 step 4); without a usable da-guard (none found, one that fails, one older than this tool) no row runs: the report is one `config_dir` FAIL row and the exit code is `2`, whatever the tree holds. The profiles row also reads the exporter's answer through `da-guard effective`; the policy_dsl row reads the tenants only when there are policy rules (`_policies` or `--policy-dsl`), and with no rules it PASSes as before (`No _policies defined — skipped`). The da-tools image ships it (`/usr/local/bin/da-guard`). To run `python3 scripts/tools/ops/validate_config.py` straight from the repo, first `make da-guard-build` (builds it from this checkout's Go source into `.build/da-guard`; needs Go) and set `DA_GUARD_BINARY=$PWD/.build/da-guard`, or put da-guard on `$PATH`; `make validate-config` knows two sources only: `DA_GUARD_BINARY` when set (used as is, nothing built), else a `.build/da-guard` it builds first (with no Go it prints an ERROR and exits non-zero).

**Required Parameters**

| Parameter | Description |
|-----------|-------------|
| `--config-dir <PATH>` | Tenant configuration directory |

**Options**

| Option | Description | Default |
|--------|-------------|---------|
| `--policy <FILE>` | **Path** to a policy YAML holding an `allowed_domains:` list (omit for no constraint). ⚠️ A supplied value that cannot be used now exits 2 instead of being skipped — not a file, unreadable, not UTF-8, not valid YAML, or not a mapping at the top level (#1556); so does a file with no `allowed_domains` key (empty, comments only, misspelled key) or a list with a non-string entry — write `allowed_domains: []` to say "no constraint" explicitly (#1649). Its `min_generator_version` follows the same rule as `generate-routes`: when this tool is older, the routes and policy rows are caller errors and the exit code is `2` (before, the key was not read and the run judged anyway; [#3142](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3142)). When the `policy` row fails, the suggested fix is to add the receiver's host to `allowed_domains` in this file, or to correct the tenant's receiver URL ([#3208](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3208)). ⚠️ The v2.9.0 image still has the old behaviour | (unrestricted) <!-- image-caveat: v2.9.0 --> |
| `--rule-packs <PATH>` | Path to the `rule-packs/` directory for the custom rule lint. ⚠️ A supplied value that cannot be used exits 2; **omitting it removes the `custom_rules` row from the report entirely** (#1556) | (check not run) |
| `--policy-dsl <FILE>` | Path to a standalone Policy-as-Code DSL file (top-level `policies:` key). ⚠️ A supplied value that cannot be used exits 2 (same five shapes as `--policy`); before the fix its output was byte-identical to passing no flag at all (#1556) | (only `_policies` in `_defaults.yaml`) |
| `--version-check` | Also run the version consistency check | false |
| `--json` | Output results as JSON (for CI consumption) | false |
| `--strict` | Escalate domain-policy (ADR-007) violations from WARN to FAIL (matches CI `generate-routes --strict`); in addition, a matcher value yaml.v3 reads as a non-string (`routes[i].match` values, `overrides[i].alertname` / `metric_group`; #2431, #2906) and a bad `group_by` element (#2503) make the `schema` row FAIL | false |

**Checks Performed**

- YAML file usability (parses, decodes as UTF-8, mapping at top level). ⚠️ **The last two landed after v2.9.0**: on the image you have, a non-UTF-8 file or a non-mapping top level raises a traceback with zero bytes on stdout <!-- image-caveat: v2.9.0 -->
- **Quoting of string fields** (`yaml_quoting`): FAIL when yaml.v3 reads a value in a field the JSON Schema types as a string (enum included) as a boolean, number, timestamp or null — every reader of conf.d reads it that way, through `da-guard parse-tree` ([#2906](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2906)), and the verdict is the tag parse-tree gives the value: `channel: 1e3` is the float 1000, `name: 2024-01-02` a timestamp; `channel: yes` is the string `"yes"` and is not reported (quoted or not, the value read is the same). Each finding names the file and the field path (parse-tree gives no line); the fix is to quote the value, with no tag (`channel: "1e3"`). Tenant thresholds are string fields too, so `mysql_connections: 70` is reported — write `"70"`. A YAML 1.1 boolean word (`yes` / `no` / `on` / `off`, quoted or not) in a field that takes a boolean is listed as WARN: it is a string, which the field refuses (the `json_schema` row FAILs too) — write `true` / `false`. A tag yaml.v3 cannot apply, such as an explicit `!!bool yes`, makes the whole file unreadable, which the `yaml_syntax` row reports. Tenant files are held to `tenant-config.schema.json`, `_defaults*` files to `platform-defaults.schema.json` (whose `_routing_defaults` / `_routing_enforced` follow the tenant schema's routing definitions); the `tenants:` block of a ROOT platform file (any `_` file at the conf.d root the exporter reads: `_defaults*`, `_profiles.yaml`, …) is held to `tenant-config.schema.json` as well ([#2743](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2743)); other `_*` files are not read. Which values are not strings is yaml.v3's tag and which fields are strings is the schema's (#2164). ⚠️ **Not present in the v2.9.0 image** <!-- image-caveat: v2.9.0 -->
- **JSON Schema** (`json_schema`, [#2695](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2695)): tenant files against `tenant-config.schema.json`, `_defaults*` against `platform-defaults.schema.json` and `_routing_profiles.yaml` against `routing-profiles.schema.json`; each `tenants.<id>` entry of a ROOT platform file (any `_` file at the conf.d root the exporter reads: `_defaults*`, `_profiles.yaml`, …) is held to `tenant-config.schema.json`, the definition a tenant file is held to, because the exporter and the route generator merge it as that tenant's own values — named like `_defaults.yaml: … @ /tenants/<id>/_routing/group_by` ([#2743](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2743); a nested platform file's block is read by no plane and is not judged). All of this by calling the verdict of `scripts/tools/lint/check_confd_schema.py` rather than a second copy of it. A violation is FAIL, exit code `1`; each finding names the file, the reason and the field path after `@` — e.g. `_routing.group_by: alertname` (a list is required) or a misspelt key under `_routing_defaults`. The values judged are the ones yaml.v3 reads (through `da-guard parse-tree`, [#2906](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2906)), so `yes` (a string) in a field that takes a boolean FAILs this row. A value already listed by `yaml_quoting` is judged here as if it were quoted, so it is not reported again as a type error and `mysql_connections: 70` appears under `yaml_quoting` only; a value that is wrong even quoted is still reported, e.g. `group_wait: 30` (`"30"` has no unit) or `_silent_mode: true` (`"true"` is not an allowed value). Without the Python package `jsonschema`, or without `check_confd_schema.py` beside the tool (**the da-tools image has neither**), the row is **WARN** (exit code unchanged) with the detail `Not checked against the JSON Schema: …`: the row gave no verdict. A schema file that cannot be found is FAIL, `caller_error: true`, exit code `2`, as for `yaml_quoting`. ⚠️ **Not present in the v2.9.0 image** <!-- image-caveat: v2.9.0 -->
- **Generator key check** (`schema`): the route generator's key check while it reads the config — tenant keys against the declared platform defaults and the reserved keys, plus domain policy. ⚠️ The row name is historical (`--json` consumers depend on it, so it is not renamed); **this row does not apply the JSON Schema** — that is `json_schema` above. A `_routing_enforced.enabled` that is not a boolean (`n`, `'yes'`, `'true'`, `~` …), or another boolean / enum field of the wrong type or with an unknown value, is FAIL: the platform-enforced (NOC) route is NOT enabled, and `generate-routes` refuses the whole tree in every mode (#2164, [#2906](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2906); see [generate-routes](#generate-routes), "Boolean and enum fields"). ⚠️ In the v2.9.0 image such a value enabled NOC routing when it was a non-empty string <!-- image-caveat: v2.9.0 -->
  - A key in a routing block the route generator does not read (`continue`, `mute_time_intervals`, a typo; see "Keys the generator does not read" under `generate-routes`; likewise a `_routing*` key the generator does not read such as `_routing_profle`, #2983) makes this row FAIL, exit 1 (#2960); the row does not need jsonschema, so it applies in the da-tools image too. ⚠️ The v2.9.0 image names none of these keys <!-- image-caveat: v2.9.0 -->
- Routing rule validation (group_wait/group_interval/repeat_interval in allowed range). A duration Alertmanager does not accept (`1.5h`, `30m1h`, `1ns`, …; see "Duration syntax" under `generate-routes`) is a FAIL, exit 1 (#2490). FAIL when two sources generate a receiver of the same name, decided by the same predicate as `generate-routes --validate` (#2279). ⚠️ The v2.9.0 image reports PASS for a duplicate receiver name <!-- image-caveat: v2.9.0 -->
  The `routes` row calls **the same verdict function** as `generate-routes --validate` rather than a copy of it (#2311): a skipped entry, a duplicate receiver name, a generated inhibit rule that would suppress the Watchdog or let a tenant silence a platform alert, a platform invariant refusing the config assembled on the built-in base, and Alertmanager's own parser (`amtool check-config`) rejecting it — any of these is FAIL at exit `1`, the same conclusion `--validate` reaches. ⚠️ One exception: a tree whose tenants generate **no route and no inhibit rule at all** (e.g. only `_severity_dedup: disable`) makes `--validate` print `No valid routes or inhibit rules generated.` at exit `1`, while this row is WARN at exit `0`. With no `amtool` on PATH the row is **WARN** (exit code unchanged) and its details gain `Not validated by Alertmanager: amtool not found on PATH …`: every other check passed, but a value only Alertmanager refuses (e.g. a webhook URL `http://[1]/`) is invisible to this row. An `amtool` on PATH that gives no verdict (cannot run, times out, crashes) is FAIL with `caller_error: true` at exit `2`, as with `--validate`. ⚠️ amtool checks the config assembled on the **built-in default base**, not your own `--base-config`. ⚠️ In the v2.9.0 image this row checks only skipped entries and duplicate receiver names <!-- image-caveat: v2.9.0 -->
- Policy checks (webhook domains) — **this row only appears when `--policy` is given**
- Custom rule lint (deny-list over `rule-packs/`) — **only when `--rule-packs` is given**
- Profile references (does a tenant's `_profile` bind a profile): the verdict is the exporter's own (`da-guard effective`'s `profile`, [#2115](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2115)); tenants are read from the whole tree, a tenant in a subdirectory included, and a profile defined in the `profiles:` of any root platform file counts (not only `_profiles.yaml`). `_profile` is read as written plus inherited: one written in the tenant's own entry (its tenant file, or a root platform file's `tenants:`) that binds nothing is reported as an unknown profile; one inherited from a `_defaults.yaml` is reported as binding nothing (the exporter binds no profile from a defaults file), naming that file. A file with no `tenants:` is not a tenant: the row names it in its details (`not a tenant: <file>: …`, so `--json` carries it too) and its status does not change. A file the exporter drops or cannot read, or a tree it refuses, makes the row FAIL with da-guard's reasons; a `--config-dir` with no config file at all (the exporter refuses to start on it), no da-guard, or da-guard failing itself makes it FAIL with exit code `2` (`1` when the `yaml_syntax` row names other unusable files (its `unusable_files`), as for a missing da-guard; [#2725](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2725)). da-guard failing itself means: it cannot be run (e.g. not executable), exits with a code the subcommand never uses, writes output that is not its JSON or lacks a field this tool reads (e.g. `skipped`), or does not accept the subcommand and flags this tool runs (e.g. a released v2.9.x has no `effective`) — the last judged by the exit code of the same arguments plus `-h`, with no file written and its stderr not read. For these the advice is to use a current or rebuilt da-guard (`make da-guard-build`), not to edit the config. ⚠️ Known limitation: a da-guard subcommand that itself times out or is killed by a signal (e.g. OOM) is not in this list; the row is still attributed to the config tree, exit code `1`. When the subcommand has already exited 2 and the version probe that follows (`-h`) times out or is killed, it is reported as too old (exit code `2`). A threshold profile that writes a routing-layer key fails the row, exit `1`, one line per (file, profile, key); the verdict is the route generator's own [`routing_in_unread_location`](#guard) (`_grar_parse._unread_routing`, the ones inside a profile; which keys and why are in that row; before [#2904](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2904) da-guard's finding of that name), so it covers the `profiles:` of every root platform file and profiles no tenant elects, in a tree with no tenant too ([#2914](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2914); it used to be a WARN for `_profiles.yaml` only). A generator that cannot read the tree fails the row, saying this was not checked (never read as "no finding"). Whether a reader reads a `_` key in a profile is not this row's to judge; the `unread_keys` row's shared verdict reports it, from the registry's profile layer, whether or not a tenant binds the profile or its own value shadows the profile's (`_custom_alerts`, `_silent_mode` and `_metadata` are read in a profile and not reported; a `_profile` in a profile never takes effect and is reported; [#3042](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3042)). ⚠️ The other profile STRUCTURE checks (a profile that is not a mapping, an empty profile: WARN) and the `N profiles defined in _profiles.yaml` count still read `_profiles.yaml` only. ⚠️ The v2.9.0 image reads each tenant file's own `_profile` only, and knows only `_profiles.yaml`; its row WARNs on any `_` key in a profile (`_custom_alerts` included), and it has no `unread_keys` row <!-- image-caveat: v2.9.0 -->
- Version consistency — **only when `--version-check` is given**
- Policy-as-Code DSL evaluation (`_policies` in `_defaults.yaml`, or `--policy-dsl`); rules the engine cannot read as written (see exit code `2` of `evaluate-policy`, #2961) evaluate nothing: from `_defaults.yaml` this row FAILs, exit 1; from the `--policy-dsl` file, top-level keys other than `policies:` included, it is a caller error, exit 2; a rule reads the same values as `evaluate-policy` (thresholds as `/metrics` serves them, see that section, #2115), and when the tenants cannot be read this row FAILs with da-guard's reason (exit code `2` for an empty `--config-dir`, no da-guard, or da-guard failing itself — `1` when the `yaml_syntax` row names other unusable files (its `unusable_files`) — as for Profile references). When `/metrics` serves no threshold at all (e.g. no root `defaults:`), or a threshold value is configured but not served (served-values' `unserved`), the details say so on a line of their own; the verdict is unchanged, threshold rules compare only what `/metrics` serves ([#2725](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2725))
- **Tenant declaration uniqueness**: FAIL when one tenant id is declared by **two files**. ⚠️ The exporter answers that state by **rejecting the entire config dir** (`DuplicateTenantError`), so the consequence is not "that one tenant loses alerting" — **every tenant in the tree does**, at deploy or restart time, after CI has passed. The usual cause is an editor leaving a `db-a.yml` beside `db-a.yaml`, but the predicate is "one id, two files": `archive/db-a.yaml` is caught the same way (#1577). ⚠️ **Not present in the v2.9.0 image**: the same tree reports `Result: PASS` / exit 0 there <!-- image-caveat: v2.9.0 -->
- **Tenant file name** (`tenant_file_name`, [#3157](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3157)): a tenant file's name (without `.yaml`/`.yml`) must be the one tenant id it declares under `tenants:` — tenant-api takes a tenant's id from the file name, the exporter from `tenants:`, so `es.yaml` declaring `es-prod` is one tenant under two ids (RBAC, audit records and portal links do not match its metrics, and tenant-api refuses writes to it as `id_mismatch`). FAIL, exit 1, one line per file naming the declared id and the name to rename it to; a file that declares several tenants also FAILs, with advice to split it into one file per tenant. Files are selected as the exporter selects them: the whole tree (a file in a sub-directory is held to its own name), `_`- and `.`-prefixed files skipped; a file that declares no tenant is not compared; the comparison is case-sensitive. ⚠️ **Stricter**: a tree whose file names differ from their tenant ids passed before; rename each file to `<id>.yaml` (the declared id is what `/metrics` already uses). ⚠️ **Not present in the v2.9.0 image** <!-- image-caveat: v2.9.0 -->
- **Root defaults** (`root_defaults`): checks `defaults:` in the **root** `_defaults.yaml` the way the exporter decodes it, and FAILs on three things. First, values: the exporter decodes the root `defaults:` as `map[string]float64`, so **a value that does not decode as a number** (`"70"`, `disable`, a mapping, a list, a boolean, a date …) makes it **drop the whole `defaults:` block** — every platform threshold — while the load still reports success; and **an empty value** (`k:`, `~`, `null`) is reported as having no value. Before [#2518](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2518) it was served as a 0 threshold; what it changes now depends on the rest of the tree (another spelling, `optional_overrides:`, a `<<:` merge), and this row does not judge it. The verdict is the same yaml.v3 mirror the `deprecate` carrier health check uses (#1414). Second, routing: `_routing` or any `_routing`-prefixed key under `defaults:` FAILs whatever its value. `defaults:` is for numeric thresholds; routing defaults go in the top-level `_routing_defaults:` block. ⚠️ A `_routing` mapping there costs more than routing: the exporter reads the root `defaults:` as numbers only, so it drops **the whole block — every platform threshold** — while the load still reports success; and the route generator never reads `defaults:` either. Third, the wrapper ([#2386](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2386)): when a root `_defaults.yaml`'s `defaults:` is not a mapping (the key is absent, or present with no value), the defaults merge reads the whole document and `/effective` shows its top-level keys, while the exporter's root decode reads thresholds only from a `defaults:` mapping and has no field for the other keys, so `/metrics` does not carry them from this file. The row FAILs and names them when the top level holds any of: thresholds (keys with no `_` prefix) and reserved keys other than `_routing*` ones (e.g. `_severity_dedup`, `_silent_mode`, `_state_*`). Not listed: fields of the exporter's root config struct (e.g. `state_filters`, `max_metrics_per_tenant`); `_routing_defaults` / `_routing_enforced`, which other tools read at the top level; `_metadata`, which the defaults merge drops at every level; `_routing*` keys (the route generator does not read routing from a defaults file; a misplaced one is `routing_in_unread_location`); any other `_` key (e.g. `_x: &x`, a key that only carries a YAML anchor). Measured: a top-level threshold was absent from `/metrics`; a top-level `_severity_dedup: disable` showed `disable` on `/effective` while `/metrics` served `enable`. A `_defaults.yaml` in a subdirectory is not judged by this row (#2291). Any other `_` key under `defaults:` with a number (such as `_metadata: 1`, `_namespaces: 5`) is a WARN: no `_` key is a threshold, the exporter makes no threshold row of one and no other reader reads one under `defaults:`, so it does nothing ([#3045](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3045)); a value that does not decode as a number FAILs under the value rule above and gets no second line. A non-finite number (`.nan`, `.inf`, `-.inf`: yaml.v3 decodes it, but it is no threshold) FAILs too, one line per key: the file loads and the other thresholds are untouched; the exporter only serves no default row for that key (read like an `optional_overrides:` key; da-guard reports `root_default_non_finite`; the verdict is `da-guard parse-tree` giving the `!!float` no `value`). ⚠️ **Behaviour change ([#3140](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3140))**: such a tree used to PASS this row and now FAILs it, exit `0` → `1`. ⚠️ **Not present in the v2.9.0 image** <!-- image-caveat: v2.9.0 -->
- **Defaults wrapper** (`defaults_wrapper`, [#2386](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2386)): a `_defaults.yaml` at any level (in each directory, the one the exporter selects) FAILs when its `defaults:` is a mapping (`{}` included, and a YAML `!!set`, which the exporter decodes as one) while its top level also holds keys that would enter the merged config behind `/effective`; the line names the file and the keys. Those keys are thresholds (keys with no `_` prefix) and reserved keys other than `_routing*` ones (e.g. `_severity_dedup`, `_silent_mode`, `_state_*`). Not listed: fields of the exporter's root config struct (e.g. `state_filters`, `max_metrics_per_tenant`); `_routing_defaults` / `_routing_enforced`, which other tools read at the top level; `_metadata`, which the defaults merge drops at every level; `_routing*` keys (the route generator does not read routing from a defaults file; a misplaced one is `routing_in_unread_location`); any other `_` key (e.g. `_x: &x`, a key that only carries a YAML anchor). With `defaults:` a mapping the defaults merge reads only that mapping, and those top-level keys appear in no tenant's `/effective`; with no `defaults:`, or `defaults:` with no value, the merge reads the whole document, and nothing is reported. Measured on a subtree: wrapped this way, a top-level threshold and `_severity_dedup` were no longer served on `/metrics`; `_silent_mode` and `_state_*` changed `/effective` only (they were not on `/metrics` in any shape). In a subtree file, a reserved key is not told to move under `defaults:` (subtree defaults do not support reserved keys): the line says to delete it from the file and leaves whether to set it in each tenant's own entry to da-guard's `subtree_default_reserved_key` (the suggested action then drops "give `defaults:` no value" too) ([#2388](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2388)). ⚠️ **Not present in the v2.9.0 image** <!-- image-caveat: v2.9.0 -->
- **Nulls in a schedule** (`schedule_null`, [#2708](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2708)): FAILs a threshold written as a schedule with override windows that holds a null (a null window entry, a window's `window:` or `value:` written as null, or a null `default:` beside windows), naming the file, the place and where the null is. Checked in: every tenant file's `tenants:`, each directory's selected `_defaults.yaml` defaults block (the whole document when it has no `defaults:` mapping), and the `tenants:` and `profiles:` of the root `_` files (an unselected defaults carrier excluded); a `_` file below the root that is not a defaults carrier is not checked. A schedule with no window and a null `default:` is not this row's. ⚠️ **Not present in the v2.9.0 image** <!-- image-caveat: v2.9.0 -->
- **Threshold values not served as written** (`values_not_served`, [#2065](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2065)): reads `da-guard effective`'s `not_served` (the exporter's own resolver and build records; this row judges no YAML itself) and FAILs on any of these, one line each naming the file, tenant, key and reason: `value_unparsed` (not a finite number the exporter reads, e.g. `abc`, a list, `7O:critical`, `NaN`, `-Inf` ([#3140](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3140)), `disable:critical` on a threshold row (no disable: write `disable` for the whole key, `<key>_critical: disable` for the critical tier only, [#3141](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3141)), or a severity suffix other than `warning` / `critical` in any letter case, e.g. `70:high` or the empty `70:`, [#3071](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3071); `/metrics` serves the platform default instead), `value_unparsed_dropped` (the same for a `_critical`, dimensional or `optional_overrides:`-declared key, which has no default to fall back to; no series is served), `window_invalid` (a schedule override whose `window:` is not a UTC `HH:MM-HH:MM`, has an hour or minute out of range, is missing, or starts where it ends such as `05:00-05:00`; that window never applies), `value_rejected` (a subtree `_defaults.yaml` value that is not threshold-shaped, e.g. `overrides:` written as a string; the tenants under it keep a shallower level's value), `spelling_duplicate` (the winning layer's mapping writes a threshold under two spellings — a dimensional key with its labels in another order or quoted differently, or both #1231 names, [#2031](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2031)). Threshold keys only (not `_`-prefixed); a reserved key in a subtree defaults file is reported by da-guard's `subtree_default_reserved_key` (a routing key is the route generator's to judge). The set it names is the one da-guard's `value_not_served` refuses (tested); the exporter reports the same set on load and reload as `da_config_values_not_served{reason}` (counted per (tenant, key)) and one WARN line naming tenant and key only. A tree that cannot be read fails like the `profiles` row, with the same advice; a da-guard whose `effective` output lacks `not_served` is named as too old (FAIL, exit `2`). A value not served at all because nothing declares it (served-values' `unserved`, e.g. in a tree with no root `defaults:`) is not this row's, so its PASS does not mean every value is served. What `/metrics` serves does not change. ⚠️ **Behaviour change ([#3071](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3071))**: a tree that writes a severity suffix no rule selects (`70:high`, `70:`) used to PASS this row and now FAILs it, exit `0` → `1`. ⚠️ **Behaviour change ([#3140](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3140))**: a tree whose tenant side or subtree `_defaults.yaml` writes a non-finite number (`"NaN"`, `"-Inf"` …) used to PASS this row and now FAILs it, exit `0` → `1`. ⚠️ **Behaviour change ([#3209](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3209))**: a tree with a `_custom_alerts` entry whose `threshold:` the exporter rejects (`abc`, `NaN`, `70:high` …) FAILs this row too (it used to PASS, exit `0` → `1`), one line per entry naming `_custom_alerts[<index>]`, the entry's name, `refused` and the exporter's reason; the verdict is `da-guard served-values`' `custom_alerts[].refused`, not this row's own; when served-values refuses the whole tree that half is not checked, and a PASS becomes a WARN saying so (never a FAIL). ⚠️ **The v2.9.0 image does not have this check** <!-- image-caveat: v2.9.0 -->
- **Keys no reader reads** (`unread_keys`, [ADR-038](adr/038-unread-keys-judged-by-readers.en.md)): judges, with the shared verdict (`da-guard served-values --disable-effects`, `da-guard effective` and the tree's `_policies`), whether some reader reads each key of each tenant — the same verdict as `da-tools explain-keys`. **Report only**: one WARN line per problem naming the file, the severity it will have once the gates block (`error` / `warning` / `notice`), the kind (`unread`, `disable_noop`, `disable_partial`), the key, the reason and the tenants it reaches (`disable_partial` also names each sibling still served and the file it comes from, and is reported only when that sibling is not written in the tenant's own file: a tenant writing `X: disable` and `X_critical: 90` means to keep the critical tier only, [#3046](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3046)); the same problem in a platform file or profile is one line. A `_` key in a profile is also judged directly from the registry's profile layer, not through tenants: one no reader reads in the profile layer is a `notice` on that file, reason `unregistered` (the registry does not list it), `retired` (a retired key) or `not_read_in_profile` (listed, but not read in a profile, e.g. `_profile`), the same whether or not a tenant binds the profile, with the tenants that bind it, or `no tenant` when none does; a routing-layer key in a profile (such as `_severity_dedup` or `_routing_defaults`) FAILs the `profiles` row and is also a notice here ([#3042](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3042)). The exit code does not change for this row. With no root platform file read, one more line says the threshold keys cannot be judged (`undecidable`). With da-guard missing or failing the row is a WARN "verdict incomplete", not a FAIL. When each gate starts blocking: [#3016](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3016). ⚠️ **The v2.9.0 image does not have this check** <!-- image-caveat: v2.9.0 -->
- **The per-tenant cap on custom alerts** (`custom_alert_cap`, [#3042](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3042)): **FAIL**s when a tenant's own `_custom_alerts` (written in its tenant file, or in its entry under a root platform file's `tenants:`) asks the exporter to serve more than 20 rows. Counted is what da-guard's `custom_alert_over_cap` judges (`da-guard served-values`' `custom_alert_rows`): the `user_threshold` rows the list asks the exporter to serve, before the per-tenant metric limit (`max_metrics_per_tenant`) truncates them — a switched-off recipe serves none and is not counted, an `slo_burn_rate` serves a critical and a warning row and counts 2, an entry dropped as a duplicate is not counted (tenant-api's write check refuses a duplicate outright). A profile's recipes are a shape the tenants using it share and are not counted. One line per tenant over the cap, with the file, the tenant and the rows. With da-guard unavailable or failing the row is a WARN "not counted", never a FAIL. ⚠️ **The v2.9.0 image does not have this check** <!-- image-caveat: v2.9.0 -->

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

**Hierarchical `conf.d/` (config files in subdirectories)**: since [#2326](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2326) the schema, routes and policy rows read the whole tree, as the exporter does (the routing plane's hierarchical semantics: "Hierarchical conf.d" under `generate-routes` above); a tree the routing plane refuses (e.g. `_routing_enforced` in a subdirectory) makes the schema row FAIL, naming it on a line starting `ERROR (routing tree):`. The read that is still **flat** is the Policy-as-Code row's lookup of the root `_defaults.yaml`, while the exporter reads the whole tree. When a row's read **actually skipped** files in subdirectories, that row **does not report PASS**: a PASS becomes WARN, and whatever its status it gains a line naming the skipped files (the first 5, the rest as `(+N more)`; the full list is that row's `skipped_nested_files` in `--json`). The lookup of the root `_defaults.yaml` (where Policy-as-Code takes `_policies` from) skips only the `_defaults.yaml` files in subdirectories. So whenever a subdirectory holds a `_defaults.yaml` (the standard ADR-017 tree), the Policy-as-Code row is WARN naming that file even with no `_policies` anywhere — "no policies" was concluded without opening it. Which rows are affected is observed at run time, not a hard-coded list; a row that answers without reaching a reader (e.g. a policy file with no `allowed_domains`) keeps its PASS. ⚠️ Not observed: a root file opened by name — `profiles`' structure checks read only the root `_profiles.yaml` (the tenants' `_profile` references are judged from the whole tree). The Policy-as-Code row evaluates its tenants from the whole tree ([#2115](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2115)); what its line names is only that `_policies` in those nested `_defaults.yaml` files are not read — put `_policies` in the root carrier (or pass `--policy-dsl`). Do not flatten the tree or point `--config-dir` at a subdirectory: that drops the root defaults the tenants inherit. ⛔ **The exit code does not carry this signal**: WARN is still `0` — read `Result:` or `--json`, not the exit code, to learn whether every row covered every file (#1652). ⚠️ Not in the v2.9.0 image: the same tree reports `[PASS] routes  0 routes` / `Result: PASS` there <!-- image-caveat: v2.9.0 -->

---

#### offboard

Offboard tenant configuration and related resources.

When the conf.d holds a path da-guard validation names, this tool refuses with `2` before it reads or writes anything, each path named (the [shared refusal](#confd-refusal), [#3206](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3206)).

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
| `--state <PATH>` | A copy of tenant-api's state store: the state directory, or a saved `kubectl get configmap tenant-api-state -o yaml` (or `-o json`). Used to list the group memberships, view `filters` and federation subset that reference this tenant ([#2883](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2883)) | — |
| `--skip-state-check` | Allow `--execute` without `--state` (state store references are not checked) | false |

Without `--execute` it is already a preview; there is no separate dry-run flag.

**Output**

A pre-check report: where the tenant file is, whether any other file references the tenant, and the metrics it has set. A config file that cannot be parsed is named and fails the pre-check (the cross-reference check cannot see inside it). With `--execute` it deletes `<config-dir>/<tenant>.yaml` directly, **without a backup** (back it up yourself or rely on git), and does **not** touch Recording / Alert rules (an option to clean up rules is not implemented yet); at the end it reminds you to also remove the `tenant=<tenant>` routing from Alertmanager.

Groups, views and federation subsets live in tenant-api's state store, not in conf.d; a `_groups.yaml` or similar left in conf.d only gets a ⚠️ saying it is no longer read, and is not used as the answer. With `--state` the report lists the state store references and the tenant-api call that removes each; a legacy file left in conf.d whose record the state store lacks is imported by tenant-api at its next start, so its references are listed too (e.g. `PUT /api/v1/groups/{id}` without the member); the tool itself never writes the state store. The AccountID allocation is listed for information only (it is never reused). Without `--state` this item reads "undetermined", and `--execute` runs only with `--skip-state-check` added.

**Examples**

```bash
# Pre-check only by default (writes nothing)
da-tools offboard db-old --config-dir ./conf.d
# Actually offboard (save the state store for --state first)
kubectl -n tenant-api get configmap tenant-api-state -o yaml > tenant-api-state.yaml
da-tools offboard db-old --config-dir ./conf.d --state tenant-api-state.yaml --execute
```

**Reading (ADR-036 §2, #2906)**: which files there are and what each holds (the cross-file reference search included) are `da-guard parse-tree`'s, the yaml.v3 reading. The file deleted is the one whose stem is the tenant AND whose `tenants:`, as parse-tree reads it, declares this tenant and no other; a file that does not declare it, or also declares another tenant, fails the pre-check (deleting it would offboard someone else). A file parse-tree cannot read is named and blocks `--execute`. No usable da-guard is exit `2`.

**Exit Codes**

| Code | Description |
|------|-------------|
| `0` | Success: the pre-check passed or only warned (e.g. a cross-file reference); with `--execute` the tenant file was deleted |
| `1` | The pre-check failed (a ❌ item such as the tenant file not found, or a tenant file that does not declare the tenant or also declares another, named in the report), with or without `--execute`; or I/O failed (#2179). A conf.d holding a path parse-tree cannot read never gets this far: it is refused with `2` before the pre-check (the [shared refusal](#confd-refusal), #3206) |
| `2` | Caller error: arguments argparse rejects (missing tenant positional, unknown flag); a `--state` that cannot be read, is not a ConfigMap document or directory, holds no records, or holds keys that are not tenant-api state records (for example a saved `threshold-config`, or conf.d itself); `--execute` with neither `--state` nor `--skip-state-check`; no usable da-guard |

---

#### deprecate

Deprecate metrics: remove `<m>`, `<m>_critical`, `custom_<m>`, `custom_<m>_critical` from `defaults:` / `optional_overrides:` / tenant files; on the tenant plane also the dimensional keys `<name>{…}` of those four names.

When the conf.d holds a path da-guard validation names, this tool refuses with `2` before it reads or writes anything, each path named (the [shared refusal](#confd-refusal), [#3206](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3206)). That includes a carrier the exporter drops only because of a key this round would delete (for example `old_metric: disable` under `defaults:`): this tool no longer repairs such a tree; fix that file by hand first, then run it again. `--plane subtree` too: it judges by the index of the `--root-dir` tree, so a path outside the subtree refuses it as well; a subtree carrier's valid string values are not affected.

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
| `--plane {root,subtree}` | Whether this `--config-dir` is the conf.d root or one subtree carrier below the exporter's `-config-dir`. `root` runs the carrier health check on every `_`-prefixed file at this level (whether the exporter can read the values under `defaults:`, judged the way yaml.v3 does); `subtree` skips it (string values are legal on the subtree plane and take effect). The tool cannot infer this, so the default is fail-closed; `subtree` needs `--root-dir` | `root` |
| `--root-dir <PATH>` | Required with `--plane subtree`: the exporter's `-config-dir` (the conf.d root); `--config-dir` must be a directory the exporter's walk enters under it, else (or with it on the `root` plane) exit 2. The run's index is that root's whole tree, and the shared refusal is taken on the whole tree ([#3206](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3206)); the writes stay in `--config-dir` | (none) |

**Output**

Deletes those keys from `defaults:` and `optional_overrides:` (the declared tier, names only) in `_defaults.yaml` / `.yml`, and from the non-`_`-prefixed tenant files in the flat directory, naming each removed key and its old value; an emptied `defaults:` / `optional_overrides:` is dropped entirely; a carrier holding none of them is named and left untouched. It does **not** write `disable`: `defaults:` is `map[string]float64`, so a string there makes the exporter drop the whole root carrier ([#1787](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1787)). When the carrier health check (see `--plane`) finds a file the exporter cannot read, the whole run degrades to a preview: nothing is written, rc 1; an empty value under `defaults:` only warns. Reading, and the confirmation before a write ([ADR-036](adr/036-single-parser-effective-config.en.md) §2, #2906): which files there are, what each holds and which keys to delete are all `da-guard parse-tree`'s (the carrier health check judges by the tag yaml.v3 reads: `!!int` / `!!float` is read, `!!null` only warns, anything else drops the file; a file parse-tree cannot read is refused whole); `--execute` writes through `da-guard write` ([#3206](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3206)): every file it rewrites (all metrics merged, one write per file) goes to one `da-guard write` call, judged as a batch, and one refusal (e.g. a symlinked file, a re-encode that would change a node other than the deleted ones) writes not a byte, rc 1, each refusal named at the end; only when all pass are the files written, and a failure part-way (a write error, another writer changing a file meanwhile) is rc 2, the message naming the files already written, which are not rolled back. On `--plane subtree` the edit judges the subtree's paths in the `--root-dir` tree (a subtree carrier's legal string value is written as usual). Where the directory selects another carrier, a defaults spelling no reader reads (a `_defaults.yml` beside `_defaults.yaml`) is not written: if it still holds a key of the metric, that is a residue, rc 1. Before anything is written (preview and `--execute` alike) the tool asks the exporter through `da-guard key-refs` ([#1822](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1822)): a root-level `_`-prefixed file in the `parse_failed` (judged on the root-level `_` files alone, with this run's writes applied, in an otherwise empty temporary directory; when that check cannot answer, the current verdict stands — never a pass) or `unreadable` of the exporter's own load degrades the run the same way (rc 1); keys the exporter normalises onto the metric (legacy spellings from its alias table, `_critical`, dimensional keys in any spelling) are listed and removed exactly like the canonical key, and such spellings in a `_`-prefixed file below the root count as residue; da-guard's stderr is passed on whole (behind a `  da-guard| ` prefix). When `key-refs` cannot answer (any exit 2 from it included), the tool prints 未體檢 ("not checked") with the reason and otherwise behaves, and exits, as before; with no usable da-guard (`$DA_GUARD_BINARY`, then `$PATH`) parse-tree cannot read the tree either: exit 2; Step 3 then also reminds to check the alias table's old spellings. The tool does not write into subdirectories, but the completeness rescan looks down into them: residue in a subtree's own `_defaults.yaml` or a tenant file's `tenants:` block clears by running `--plane subtree` on that subtree as the printed guidance says; the `tenants:` / `profiles:` blocks of root-level `_`-prefixed files are out of reach and any residue there has to be removed by hand; blocks the exporter does not read or drops only warn. The write-back removes only the deprecated nodes: every other node, comment and scalar spelling stays as it was; only the layout is redone (2-space indentation, one space before a trailing comment, blank lines may go); ⚠️ removing items from `optional_overrides:` rewrites the whole list, dropping comments inside it and writing the items as double-quoted strings. ⚠️ NOT GUARDED: which plane a directory is still comes from `--plane` (`subtree` does not use da-guard's verdict on `_`-prefixed files); a file parse-tree cannot read is still refused even where the exporter accepts it.

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
| `2` | Invalid config directory, a write-back failed (part-way: the files already written are named), a caller error from da-guard write, or no usable da-guard |

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
| `1` | ERROR-level violations found, in `--ci` mode (an unreadable rule file or one with broken YAML is recorded as an ERROR too) |
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
| `phase2-rules/_defaults-suggestion.yaml` | Phase 2 | Defaults inferred from the rule thresholds, to merge into `conf.d/_defaults.yaml`. Values are written as numbers (the exporter reads `defaults:` as float64, and a quoted string drops the whole block); a threshold that is not a number is left out of `defaults:` and listed in a comment. ⚠️ Thresholds of `complex` rules are included too — check them against `migration-plan.csv` before merging; not written when every rule is `unparseable` |
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
| `1` | No phase produced results, e.g. the `--rule-files` glob matched no file, or `--scrape-config` has no `scrape_configs`. ⚠️ A `--scrape-config` file that does not exist is 1 too (a missing `--alertmanager-config` is 2) |
| `2` | Caller error: none of the three inputs given, bad arguments, or the output path given to `-o/--output-dir` cannot be written (#1641); an input file cannot be read or parsed (content not UTF-8 or not valid YAML; the message names the file, #1654); a tenant label value in the Alertmanager config that is not a DNS-1123 label ([ADR-035](adr/035-tenant-id-single-source.en.md); `--dry-run` / `--json` too, nothing is written); before any file is written, `phase1-routing/<tenant>.yaml` and `phase2-rules/_defaults-suggestion.yaml` are read back by `da-guard parse-tree` at the path they are written to, and if one does not hold what was generated (for example the string `'1e3'`, written unquoted, which yaml.v3 reads as a number) no file is written ([ADR-036](adr/036-single-parser-effective-config.en.md) §2, #2906); when those files are to be written, no usable da-guard is also 2. ⚠️ Exception: broken YAML embedded in a ConfigMap wrapper is a traceback and returns 1. ⚠️ The v2.9.0 image does not check tenant ids <!-- image-caveat: v2.9.0 --> |

---

#### analyze-gaps

Compare custom rules with Rule Pack, find duplicates/gaps.

When the conf.d holds a path da-guard validation names, this tool refuses with `2` before it reads or writes anything, each path named (the [shared refusal](#confd-refusal), [#3206](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3206)).

**Purpose**: Evaluate Rule Pack coverage; decide if custom rule can be deleted.

**Syntax**

```bash
da-tools analyze-gaps (--tenant-config <FILE> | --config-dir <DIR>) [options]
```

**Required Parameters**

| Parameter | Description |
|-----------|-------------|
| `--tenant-config <PATH>` | Single tenant config file (use `--config-dir <DIR>` for a whole directory). ⚠️ Do not shorten it to `--config`: argparse resolves that to `--config-dir`, and a file path there answers "no custom_ metrics" with rc 0 |
| `--config-dir <DIR>` | Whole tenant config directory (either this or `--tenant-config`); reads the thresholds the exporter actually serves, see below |

**Options**

| Option | Description | Default |
|--------|-------------|---------|
| `-o, --output <FILE>` | Also write the JSON report to a file | (none) |
| `--json` | Print only JSON on stdout | false |
| `--metric-dictionary <FILE>` | Metric dictionary; exit code 2 if given but the file does not exist | `metric-dictionary.yaml` beside the tool (image) or one level up (`scripts/tools/` in the repo) |

`--config-dir` reads the thresholds, and their values, that the exporter's `/metrics` actually serves for each tenant (through `da-guard served-values`, #2115): an inherited `custom_` threshold is listed; a key switched off with `disable`, or one with no default that is never served, is not. A file with no `tenants:` is not a tenant; stderr prints one `WARN` per such file; a file or sub-directory the exporter cannot read (no permission, a dangling symlink, a sub-directory it cannot list) ends the run with exit 2, the `ERROR` line naming the file (or directory) and the reason (`stat_error` / `read_error` / `walk_error` / `special_file`), except a symlink to a directory (the exporter never follows one; it is only skipped and does not change the exit code); whatever da-guard prints on stderr is passed on to stderr line by line, each line behind the prefix `da-guard|` (two leading spaces, one trailing); when the run ends because a file fails to parse or cannot be read, the `exit 3` in the da-guard lines below the `ERROR` line is da-guard's own exit code, this tool exits 2. Needs da-guard (shipped in the image; when running from the repo, via `$DA_GUARD_BINARY` or `$PATH`).

`--tenant-config` reads what the one file says, inheriting nothing from `defaults:` or any other file; with `tenants:` it lists each tenant under it, without one the file name (extension dropped) is the tenant id, and a top level that is not a mapping counts as no keys. It reads the way the exporter's yaml.v3 does (through `da-guard parse-tree`'s single-file mode, #2906): an unquoted `yes` is the string `"yes"`, a date is its source text in the `--json` `current_value`, and a tenant id `010` is `"010"`; a key written twice in one mapping (`123` and `"123"` are one key) or a file with more than one document exits 2 naming the file. It needs da-guard too.

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
| `2` | Caller error: bad arguments; a `--config-dir` / `--tenant-config` / `--metric-dictionary` path that does not exist (the message names the flag); the output path given to `-o/--output` cannot be written (#1641); an input file cannot be read (content not UTF-8 or not valid YAML; for `--tenant-config`, also a duplicate key or more than one document; the message names the file, #1654, #2906); under `--config-dir`, a file the exporter fails to parse and skips whole, or a file or sub-directory it cannot read (no permission, a dangling symlink; a symlink to a directory excepted; the `ERROR` line names the file and da-guard's stderr follows line by line, each line behind a fixed prefix, see the text above), a tree the exporter rejects, or da-guard not found / failing (#2115; `--tenant-config` too, #2906). ⚠️ The v2.9.0 image returns `0` for an input path that does not exist, treating it as having no `custom_` metrics <!-- image-caveat: v2.9.0 --> |

`walk_error` does not depend on whether the directory holds config files: any sub-directory under `--config-dir` that the running user cannot list (e.g. a `docs/` without permission, or `lost+found` when conf.d is the root of an ext4 volume and the tool runs as non-root) makes this tool exit 2; a directory whose name starts with `.` (e.g. `.git`) does not, as the exporter's load never enters it. Point `--config-dir` at a sub-path that does not contain that directory, or change the permissions so the running user can list it.

---

#### config-diff

Compare what two config directories (conf.d) each render to, and output a blast radius report: which tenants each change reaches.

**Purpose**: GitOps PR review; a change to a platform file (`_defaults.yaml`, `_profiles.yaml`, a root `_platform.yaml`, routing defaults) is reported for the tenants it reaches too.

**Syntax**

```bash
da-tools config-diff --old-dir <path> --new-dir <path> [options]
```

**Required Parameters**

| Parameter | Description |
|-----------|-------------|
| `--old-dir <PATH>` | Old config directory (the base). Missing, or holding no config file: a first import, every tenant counts as added |
| `--new-dir <PATH>` | New config directory (the PR) |

**Options**

| Option | Description | Default |
|--------|-------------|---------|
| `--at <RFC3339>` | Instant at which schedules, silences and maintenance windows are read, the same on both sides and printed on the report; in CI pass the base commit's time (`git show -s --format=%cI <base>`) | now |
| `--format {markdown,json}` | Output format | `markdown` |
| `--json-output` | Same as `--format json` | false |

**What it compares** ([ADR-037](adr/037-config-diff-compares-rendered-output.en.md)): config-diff does not read the YAML itself. It runs, on each side, the two programs that turn conf.d into what runs, and compares their output:

- `da-guard served-values --at <time> --schedules --disable-effects`: what the exporter serves for each tenant (thresholds, severities, settings, state filters, custom alerts), and whether each `disable` changes what is served.
- The route generator `generate_alertmanager_routes.py --output-configmap`: the whole Alertmanager configuration. A route node is identified by its path of matchers, and its receiver, `group_by`, `group_wait`, `group_interval`, `repeat_interval` and `continue` are compared; so are receiver definitions and inhibit rules.

Both need da-guard (bundled in the image; in a checkout run `make da-guard-build` first, or set `DA_GUARD_BINARY`).

**Report sections**: Not Computed / Not Compared (when a view has no result), Platform Settings, Tenants Added / Removed, Routing Changes, Custom Alert Changes, Tenant Setting Changes, Keys the Exporter Drops, Other Served Changes, and a threshold table (Threshold Changes). Each section lists a change once, followed by the tenants it reaches: the tenant when it is one, otherwise their count and the first 5 names, so a platform default that reaches 1000 tenants takes one line. Values come from the PR's files: outside the threshold table they are printed inside code spans, and in its cells `|` and line breaks are escaped; a receiver's credential fields are reported as changed with both values masked as `<redacted>`; the same value (a string or a number) in any other field (a YAML anchor / alias, or a copy) is masked too, and the report's head says in how many places. The whole value is matched, and a value of 8 characters or more is also masked inside a longer string; a partial copy, or one rewritten (case, URL encoding), is not.

**Change Classifications** (threshold table)

| Classification | Meaning | Impact |
|---|---|---|
| `tighter` | Threshold decreased | May increase alerts |
| `looser` | Threshold increased | May decrease alerts |
| `added` | New metric key | New alert coverage |
| `removed` | Metric key removed | Lost alert coverage |
| `toggled` | enable ↔ disable (whether the exporter switches the key off: served-values' `keys[].reason` `disabled`) | Enable or disable alert |
| `modified` | Complex value change | Manual review needed |

A threshold key the tenant writes but `/metrics` does not serve (served-values' `unserved`, e.g. its base is in no defaults carrier; a value switched off such as `disable` does not count) is printed in the table with `(not served)` after the value, and its JSON entry carries `not_served` (the sides it is not served on, `"old"` / `"new"`). A side the exporter switches off (served-values' `keys[].reason` `disabled`, however it is spelt, e.g. `OFF`; `disable:critical` on a threshold row is not a switch-off) is printed as `disabled`, and its JSON entry carries `disabled` (a list of sides too); the tool used to read the value itself ([#3140](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3140)). A key served on one side and written but not served on the other is `added` when it starts being served and `removed` when it stops, even with the same number — that is the change that arms or disarms its alert ([#3022](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3022)).

**Platform settings (top-level keys of `_`-prefixed files)**: the `_`-prefixed files on both sides (at any depth) are compared key by key as `da-guard parse-tree` reads them; a change only to comments, indentation, quoting or key order is no change. Each key whose value changed is listed under "Platform Settings", grouped by what the report can say: Rendered when the renders show differences (see the sections below; which of them come from this key is not told apart). A fully covered key that its renderer reads right where it is written (parse-tree's `rendered_by`) and whose renders show no difference is listed as "no effect" and does not affect the exit code ([#2962](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2962)). A partly covered key whose renders show no difference, a key written where its renderer does not read it (such as `defaults` outside a defaults file, or `tenants:` in a `_` file below the root), a key neither render reads (a platform `_custom_alerts` recipe, `instance_tenant_mapping`, `max_metrics_per_tenant`, and keys such as `tenant_orgs` and `groups` in tenant-api's files; for these a value that is not a string is compared by its source text), a file that cannot be read, a `_` file no reader reads (compared byte for byte), and a `_policies` / `domain_policies` written where its check does not read it each raise the exit code to at least 1. `_policies` in the root defaults file and `domain_policies` in `_domain_policy.yaml` are listed as a note only (`validate-config` and the generator's `--validate --strict` check them) and do not affect the exit code. parse-tree itself failing, or its output carrying no `rendered_by` (a da-guard older than this tool), exits 2. The rules are [ADR-037](adr/037-config-diff-compares-rendered-output.en.md) decision 3.

**The Custom Alert section is not a silencing verdict**: it reads the rows the exporter serves. `threshold` is `<value>:<severity>`, and an `op` / `window` / `for` change shows as a `recipe_id` change; a recipe no longer served is listed as removed, with a note read from served-values' `custom_alerts[]` ([#3140](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3140)): no entry of that name is a deletion; every entry of that name with `status` `disabled` is marked **DISABLED (alert silenced)**; every one rejected for its threshold (`refused`: `non_finite` / `unparsed`) says so, and da-guard reports it as `value_not_served`; anything else (another field rejected, a da-guard too old to write `custom_alerts`, the tenant gone) still says a deleted, a disabled and a rejected recipe are not told apart.

**JSON output**: `schema` is `da-tools.config-diff/v1`, beside `at`, `exit_code`, `masked_by_value` (how many places outside a credential field held a credential's value and were masked), `views` (for `served` and `routes`, a `status` of `computed` / `not_computed` / `not_compared` and a `reason`), `tenants_added`, `tenants_removed`, `uncovered_files` (files with platform settings to review) and `platform_settings` (one entry per changed key, and one per file no reader reads or that cannot be read, with `key` null: `file`, `key`, `change`, `status`, `note`, `views`). `views` also has `platform` (parse-tree's reading). Every kind of change is one entry per distinct change with the tenants it reaches (`tenants`): `threshold_changes`, `setting_changes`, `custom_alert_changes`, `dropped_key_changes`, `other_served_changes`, `route_changes` (with `paths`), `receiver_changes` (with `receivers`) and `inhibit_changes`. A platform change that reaches many tenants is one entry. `disable_warnings` (also with `tenants`) lists each `disable` this change brings in that has no effect (`disable: noop`: taking it away changes nothing the exporter serves — often a misspelt key) or only a partial one (`partial`: the key is off, but the `_critical` / dimensional keys in `served_family` are still served); one already in that state on the base side is not listed. The Markdown report has the matching section "`disable` With No or Partial Effect". It is a reminder: on its own it does not make the exit code 1 ([ADR-038](adr/038-unread-keys-judged-by-readers.en.md) decision 8).

⚠️ The v2.9.0 image still behaves the old way: it compares the values each top-level tenant file writes itself, names platform files without comparing them, has no `--at`, and prints the old JSON without `schema` (`metric_diffs`, `profile_diffs`, ...). <!-- image-caveat: v2.9.0 -->

**Examples**

```bash
da-tools config-diff --old-dir ./conf.d-old --new-dir ./conf.d-new
da-tools config-diff --old-dir ./conf.d-old --new-dir ./conf.d-new --at 2026-10-10T03:00:00Z
da-tools config-diff --old-dir ./conf.d-old --new-dir ./conf.d-new --json-output
```

**Exit Codes**

| Code | Description |
|------|-------------|
| `0` | No configuration changes |
| `1` | Changes detected; or a platform setting to review (see Platform settings above: a changed key whose renders show no difference, one neither render reads, one not read where it is written, or a `_` file that cannot be read; a comment- or formatting-only edit, and `_policies` / `domain_policies` where their check reads them, do not count); or the base side's configuration is broken and does not render (that view is Not Compared, so the PR that fixes it is not blocked) |
| `2` | No result: a PR-side file cannot be read (stderr names the file and the exporter's or generator's own reason), the PR side has no config file, da-guard is missing or a tool failed, or bad arguments (for example `--at` that is not RFC3339, a file given as a directory). Except for bad arguments, the report is still printed and says which view was not computed (`da-guard parse-tree` failing is the Platform settings view not computed); an unexpected error in the tool prints one `ERROR:` line on stderr and no report |

> ⚠️ **`1` means "there are changes", not "it failed".** A CI step that calls
> this command bare will fail whenever the command is working correctly.
> A consumer-side snippet you can copy (`set +e` / `rc=$?`) is in
> [GitOps CI integration §2.3 Stage 2: Generate](scenarios/gitops-ci-integration.en.md);
> the same contract is restated in
> [GitOps deployment integration](integration/gitops-deployment.en.md).

---

#### evaluate-policy

Declarative policy engine — evaluate tenant configuration compliance using built-in DSL, zero external dependencies.

When the conf.d holds a path da-guard validation names, this tool refuses with `2` before it reads or writes anything, each path named (the [shared refusal](#confd-refusal), [#3206](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3206)).

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

**Which rules are refused** ([#2987](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2987); the rule and its `when` alike — any one of them exits `2` with nothing evaluated, and the message names the rule, the field and why):

- `required` / `forbidden` test presence only and take no `value` (with no `operator` the rule defaults to `required`, and the `value` would be ignored in silence).
- Every other operator needs a `value`: without one it was compared with null — `not_equals` always held, a `when` never held and skipped the rule, and a main-rule `gte` / `matches` / `contains` reported every tenant.
- The `one_of` `value` must be a non-empty list (a single value is still `[mariadb]`) of scalars; the `value` of `equals` / `not_equals` / `contains` / `matches` and of `gte` / `lte` / `gt` / `lt` is a scalar, and for the last four a finite number or a duration (`5m`); `inf` is not one.
- The `matches` `value` must compile as a regex, and must not be empty, longer than 200 characters, or carry a nested quantifier such as `(a+)+` (the guard refuses two shapes: a quantifier whose count can vary inside a group that can repeat more than once, such as `(a+)+`, `((a+))+`, `(a+){2,}`, `(a?)+`; and a group holding `+` / `*` that itself carries a quantifier, optional groups like `^(\d+)?$` included: rewrite it as `^\d*$` or use `one_of`. A fixed count such as `(a{2})+` is not refused; the engine's ReDoS guard will not run those, so the rule would never match); the `contains` `value` must not be empty.
- A wildcard `target` (with `*`) matches top-level keys only: a dotted one (such as `_metadata.*`) was looked up as a literal path and skipped when not found, and is now refused — name the field (`_metadata.owner`). A wildcard `when` takes only `required` / `forbidden`.

**Which value a rule reads** ([#2115](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2115)): `--config-dir` reads effective values, not the literal tenant files; `target` and `when` read by kind of key:

- **Thresholds** (no `_` prefix): the number the exporter actually serves on `/metrics` (via `da-guard served-values --schedules`). A value written in root `defaults:`, a platform file's `tenants:`, the tenant file or a subdirectory `_defaults.yaml` counts the same, and tenants in subdirectories are evaluated. A scheduled threshold is checked per window and any failing window is a violation (the message names the UTC window); in a window where it is `disable`, only `required` is violated. A key served at no time of day (`disable`, no default) counts as absent: `required` on a key that is written but not served reports "configured (value: …) but the exporter does not serve it", `when` treats it as missing whether its target is exact or a wildcard, and other operators skip it. A `when` with a wildcard target supports only `required` / `forbidden` (a value operator has no meaning over a set of keys). Numbers compare as numbers (`equals: 80` matches a served `80.0`). A `target` in a retired spelling (e.g. `mysql_cpu`) is mapped to the current one through the exporter's alias table; a critical row written as a `<key>_critical` key is compared as `<key>_critical`, while a value written as `"95:critical"` is still compared as `<key>` (that row's severity is critical). `when` on a scheduled threshold holds if it holds in any window.
- **Reserved keys** (`_` prefix, except `_routing`): as written plus inherited (via `da-guard effective`), only the reserved keys a tenant may carry (root-only keys such as `_policies` or `_routing_defaults` are not the tenant's); defaults the exporter fills in itself (`_severity_dedup: enable`, ...) do not count as written. `_metadata` is the exception: `/effective` drops it, so it is taken from `/metrics` (the platform files' `tenants:` and the tenant file merged key by key, [#2370](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2370)), and only the fields left after removing the exporter's empty fill (empty strings, empty lists) count as written — so an explicit `owner: ""` counts as not written (`forbidden` / `not_equals: ""` no longer report it); fields the exporter does not know (e.g. `cost_center`) are not there; when the whole `_metadata` fails to parse (e.g. `tags: foo`) the exporter sends no field and `required _metadata.owner` still reports "not configured or empty" (da-guard gives no structured signal to tell them apart; the reason is only in the da-guard WARN passed on to stderr). Only the tenant reserved keys the exporter knows are evaluated; any other `_` key (e.g. `_foo`) is outside policy's view, so `forbidden: _foo` does not fire.
- **`_routing`**: as the route generator resolves it (`_routing_defaults` level by level, routing profile, tenant `_routing`, merged; `{{tenant}}` substituted).

A file without `tenants:` is not a tenant (one `WARN` per file on stderr); da-guard's stderr is passed on line by line (prefix `da-guard|`). Needs da-guard (shipped in the image; in the repo use `$DA_GUARD_BINARY` or `$PATH`). ⚠️ Known limitation: a tree where root `defaults:` and a tenant both write `X_critical` is refused by da-guard (exit 2) and no rule is evaluated.

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
| `2` | Caller error: bad arguments (including no `--config-dir`) / **rules the engine cannot read as written** ([#2961](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2961); any rule in `_policies` or the `--policy` file with an unknown key (such as `check:`), an unknown `operator` / `severity`, no `target`, an `exclude_tenants` that is not a list of ids (a single string became a substring test), a `when` that is not a mapping or has an unknown key / no `target` / an unknown operator, a rule that is not a mapping, `policies` that is not a list, or an operator whose `value` / `target` does not fit it (see "Which rules are refused" above, #2987) — nothing is evaluated, each rule is named on stderr, and the `--json` `reason` is `policy_rule_invalid`; such rules used to be skipped or read as `required`, and the run passed) / a `--policy` file with a top-level key other than `policies:` (or `_policies:`), such as a misspelt `polices:`, or with both of them (`reason` `policy_file_invalid`; it used to read as "no rules" or read only one, exit 0). ⚠️ The v2.9.0 image skips or coerces these rules <!-- image-caveat: v2.9.0 --> / `--policy` supplied but not a file (including the empty string) / `--config-dir` does not exist / a `--policy` file or `_defaults.yaml` whose content cannot be read (not UTF-8, not valid YAML; the message names the file, #1654) / (tenants are read only when there are policy rules; with none the result is 0) a directory with no config file at all (e.g. an empty directory with `--policy`; was 0), a file the exporter drops as unparseable, a file or subdirectory it cannot read (the `ERROR` line names it, da-guard's stderr below), a tree da-guard refuses, a tree the route generator refuses when a rule reads `_routing`, no da-guard (under `--json` the `reason` is `yaml_file_unreadable` / `da_guard_failed` / `routing_tree_refused`, #2115). ⛔ Do not go green by dropping `--policy` — that evaluates without your policy file (#1651) |

#### opa-evaluate

OPA (Open Policy Agent) policy evaluation bridge — converts tenant configs to OPA input JSON, evaluates via OPA REST API or local binary, and returns results compatible with evaluate-policy format.

When the conf.d holds a path da-guard validation names, this tool refuses with `2` before it reads or writes anything, each path named (the [shared refusal](#confd-refusal), [#3206](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3206)).

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
| `--policy-package` | The rego package; `<package>.violations` is queried, `.` or `/` as separator | `dynamic_alerting.policy` |
| `--ci` | Exit 1 on error-level violations | - |
| `--dry-run` | Show input JSON without calling OPA | - |
| `--json` | JSON format output | - |

**Exit 2 when OPA did not evaluate** ([#2724](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2724)): an unreachable OPA server, an HTTP error or redirect from it (redirects are not followed; point `--opa-url` at OPA's final address), a broken HTTP response, a missing `opa` or one that fails or times out (10 s), an answer that is not JSON, or an undefined `<package>.violations` (no `violations` rule under that package, e.g. a mistyped package or a policy not loaded) all exit 2 with one `ERROR:` line on stderr naming the cause and nothing on stdout (`--json` included). Each item of `violations` must be an object `{"msg", "severity", "tenant", "field"}` whose `severity` is a string (`warning`, compared case-insensitively (ASCII only), is warning-level; every other string, and an absent `severity`, is error-level); an item that is not an object (e.g. the set of strings from `violations contains msg if {...}`) or a `severity` that is not a string (e.g. `null`) exits 2 as well. Each used to print `✓ All policies passed.` and exit 0 (`--ci` included). When OPA did evaluate, every item is reported and the run passes when none is error-level — with only warning-level items `--ci` still exits 0 (by design); an empty set is a pass.

**Examples**

```bash
# Evaluate via OPA REST API
da-tools opa-evaluate --config-dir conf.d/ --opa-url http://localhost:8181

# Dry-run: show OPA input JSON only
da-tools opa-evaluate --config-dir conf.d/ --dry-run
```

**OPA input** ([#2115](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2115)): `input.tenants` keeps its shape (one "key → value as written" map per tenant); its content is as written plus inherited (via `da-guard effective`: `defaults:`, platform `tenants:`, profile, subdirectory `_defaults.yaml`; reserved keys and `_metadata` taken as `evaluate-policy` takes them), and tenants in subdirectories are included; keys stay as written (a retired spelling is not mapped). ⚠️ When one threshold is written in both its retired and its current spelling on different layers, `input.tenants` keeps only the winning layer's value and spelling (the per-threshold view since [#2720](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/2720)), so reading the current spelling can come up empty; judge thresholds from `input.served`. New `input.served`: per tenant, the threshold numbers `/metrics` serves now, keyed by the current spelling (aliases resolved; a critical row written as a `<key>_critical` key is under `<key>_critical`, a value written as `"95:critical"` stays under `<key>`). A rego rule judging a threshold's value should read `input.served`. ⚠️ Known limitation: `_routing` in `opa-evaluate`'s `input.tenants` is as written plus inherited, not the route generator's resolution — `_routing_defaults` is not applied and `{{tenant}}` is not substituted, so a tenant relying on `_routing_defaults` alone has no `_routing`; to read the resolved routing use `evaluate-policy` ([#2724](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2724)). `input.defaults` is the root defaults carrier's top-level keys (minus `_`-prefixed ones) as written, not unwrapped: the usual `_defaults.yaml` writes thresholds under `defaults:`, so a rego reads `input.defaults.defaults.<key>`. A file without `tenants:` is not a tenant (stderr `WARN`); a file the exporter drops or cannot read, a tree da-guard refuses, or no da-guard exits 2 (the `ERROR` line names it), as does a directory with no config file at all.

---

#### guard

Dangling Defaults Guard (v2.8.0). Python wrapper that shells out to the `da-guard` Go binary to validate a `conf.d/` tree across schema / cardinality. ⚠️ Since [#2904](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2904) ([ADR-036](adr/036-single-parser-effective-config.md) §9) `defaults-impact` **no longer judges routing or domain policy**; the route generator does: wherever you ran `da-tools guard defaults-impact`, add a `da-tools generate-routes --validate --strict` step. When the tree holds what only the generator can judge, the report lists it as "undetermined" and exits 4 (see Exit codes below).

**Usage**

```bash
da-tools guard <subcommand> [flags]
```

**Subcommands**

| Subcommand | Description |
|---|---|
| `defaults-impact` | Run deepMerge → guard checks against every tenant under conf.d/ (or the `--scope` subdirectory); emit Markdown / JSON report; judges no routing or domain policy ([#2904](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2904), exit 4) |
| `served-values` | Print, as JSON, the values the exporter's `/metrics` serves per tenant ([#2115](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2115)); Python readers call it through `scripts/tools/_lib_tenant_values.py`. Each tenant's `series` lists, for every threshold key of `severities`, the series it is on `/metrics` ([#2750](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2750)): `name` and the full `labels` taken from the `/metrics` Gather, `metric_key` the key the resolver parsed the `component` / `metric` labels from (`X` for both `X_critical` and `X{db="a"}`), `dimensions` / `dimensions_regex` the dimensional labels the key writes. Every key derived from a retired alias's target (the target itself, its `_critical`, its dimensional keys) has two: its own row first (`legacy_twin: false`), then the same value served under the retired metric identity (`legacy_twin: true`, the resolver's own mark). A key `/metrics` serves no row for (`unserved`, or only in `dropped`) and `_custom_alerts` have no entry. With `--schedules` each served segment also carries that segment's `series` (a window overridden to `N:critical` is another series); a segment whose value is `null` or that carries `error` has none |
| `effective` | Print, as JSON, every tenant's effective config as tenant-api's `/effective` resolves it, plus the bound profile and each key's source ([#2564](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2564)); Python readers call it through `load_effective()` in `scripts/tools/_lib_tenant_values.py` |
| `key-refs` | **Not forwarded by `da-tools guard`**; only `deprecate_rule` calls the da-guard binary with it directly (`da-guard key-refs --config-dir <dir> --metric <key> [--metric …]`, [#1822](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1822)): prints, as JSON, the keys the exporter assigns to each `--metric` (the classification ValidateTenantKeys uses: retired alias spellings, `_critical`, dimensional keys in any spelling), each `{file, section, owner, key, nested}` with `key` as written and `nested` marking a `_`-prefixed file below the root, beside `parse_failed` / `unreadable` read off the same load as `served-values`. Exit codes as for `served-values` (0 / 2 / 3) |
| `parse-tree` | **Not forwarded by `da-tools guard`**; the Python tools that read conf.d read it through `scripts/tools/_lib_parse_tree.py` and take whether each path is a config file from its `role`, a tool that already holds the whole-tree document reading that document's `entries` instead of listing again; the Python side keeps no name rule of its own — a name that is not in a tree (a ConfigMap key, a file name not yet written) is asked of names mode, and `tests/shared/test_confd_tools_no_python_judgement.py` keeps the name predicates out of `_lib_confd` ([ADR-036](adr/036-single-parser-effective-config.en.md) §2, [#2897](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2897), [#3206](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3206)). Four modes write one document shape, one line of JSON with `schema: "da-guard.parse-tree/v2"` (a reader refuses any other value, and an entry missing a field, as a da-guard older than the tool): `{schema, da_guard_version, mode, defaults_carriers, entries}`, plus `files` and `rendered_by` in tree and file mode. `da-guard parse-tree --config-dir <dir>` (`mode: "tree"`) prints the conf.d parse result — `defaults_carriers`: the one `_defaults` file each directory uses; `entries`: one per path the exporter's walk records, sorted by path, each carrying every field (`null` where it has no value): `path`; `depth` (directories between the root and the path); `class` (what the name makes it: `tenant`, `defaults`, `platform` = any other `_`-prefixed config name, `directory` = a directory the walk goes into, a symlink to a directory, or a directory that cannot be listed); `tenant_named` (class `tenant` only: the tenant id the file name carries, minus `.yaml` / `.yml`, case kept — the tenants the exporter serves come from the `tenants:` keys inside the file, not from this value); `defaults_document` (the name starts with `_defaults`, any case: a platform-defaults document held to the platform-defaults schema — wider than class `defaults`, e.g. `_defaults-multidb.yaml` is `platform` with this `true`; `false` whenever `role` is not `config`); `config_extension` (the name carries a `.yaml` / `.yml` extension, any case — by the name alone, so a hidden name and a directory with a config-file name answer too; `false` for the root `.`); `readers` (`exporter` / `generator`; a `_` file below the root other than its directory's selected `_defaults`, e.g. `team/_domain_policy.yaml`, is read by the generator only; `[]` when no reader does; a `role` `dir` or `unlistable_dir` entry is `[]` too, with `not_read` `null` — a directory is no file anyone reads, and nothing is passed over); `not_read` (why no reader does: `unselected_defaults_carrier`, a `_defaults` its directory does not select, at the root or below it); `role`, Go's verdict on what the path is — `config` (a config file, readable or not; its readers fail loudly on a `problem`), `dir` (a directory the walk goes into, each with an entry; a config-named one too, with a `not_regular_file` `problem`; the files below it are read as usual; file mode gives an existing directory `--path` the same entry), `not_a_file` (the exporter's walk cannot reach a regular file there and does not go into it: a symlink to a directory, a FIFO, socket or device or a link to one, a stat that finds nothing — a dangling link, a looping link, a chain of more than 40 links), `unlistable_dir` (a directory whose contents cannot be listed, `.` for the root: nothing under it was seen, so the document is incomplete); only those make a path `not_a_file`, and every other failure (permission denied at stat or open, EIO, a name too long) is `config`; `problem` (why a read fails, `{kind, message}`, `kind` from a closed list — anything else is read as yaml.v3 reads it, even where today's generator refuses the file, e.g. a TAB after a colon: `read_failure`, `not_regular_file` (a FIFO or another non-regular file is never read), `invalid_utf8`, `bom_not_at_start`, `syntax_error`, `multiple_documents`, `non_specific_tag` (a node tagged `!`, however yaml.v3 reads it), `tag_not_allowed`, `non_scalar_key`, `null_key`, `duplicate_key`, `decode_error`, `top_level_not_mapping`); `verdict`, what the path makes `da-guard --config-dir <dir>` (no `--scope`) do — always the whole tree's judgement, a superset of what a `--scope`d run lists under `parse_failed` / `unreadable`, so compare it with unscoped validation — computed in Go from validation's own lists — `unservable` (named under exit 3, in `parse_failed` or `unreadable`: the exporter cannot serve it; a file the exporter's decode refuses can have no `problem` at all), `tree_refused` (named under exit 4 as `unreadable_outside_run`: the route generator refuses the whole tree on it), or `null`; file mode gives the full verdict for `--path` alone, once its text is written (`null` for a path no walk records: hidden, `not_config_name`, `parent_not_walked`), and judges no other path — read tree mode for the rest. A broken `_defaults` carrier below the root that its directory does not select is both `not_read: unselected_defaults_carrier` and `unservable` (the exporter still probes and drops it). `verdict` answers per path only: `null` on every entry does not mean validation passes — a tenant declared twice (exit 2) or routing content (exit 4) falls on no path; a reader that needs the whole verdict runs validation. `files`: the content of every config file a reader reads that has no `problem` (the structure yaml.v3 decodes, aliases and unquoted `<<` merges expanded, keys always as written, scalars as `{text, tag, value}`: integers and floats have a decimal-string value, booleans and null a JSON value, timestamps and `.inf` / `.nan` no `value`); `rendered_by` ([#2962](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2962)): for each readable `_` file, the platform carriers it holds (`defaults`, `state_filters`, `optional_overrides`, `profiles`, `tenants`, `_routing_defaults`, `_routing_enforced`, `routing_profiles`) → the rendered results a change to that key, written there, can alter (`exporter` / `generator`; an empty list means nothing reads it there — `defaults`, for example, is read by the exporter only in the selected defaults file, and `tenants:` in a `_` file below the root by nothing); a key outside that list is left out, meaning "not answered", not "read by nothing". File mode, `--path <relative path> --input <file or ->` (`mode: "file"`), reads the input as that path's text and prints one entry for the path (`class` may also be `hidden` or `not_config_name`; with no reader, `readers` is `[]` and `not_read` says why; the path is also judged against the disk: a directory on it that exists but is not a real directory, such as a symlink or a file, is `parent_not_walked`; an existing path that is not a regular file gets exactly the entry tree mode gives the same path, and the text is not read). One file's content, aliases expanded, may encode to at most 8 MiB of JSON; past that the file is a `decode_error` (yaml.v3's own alias ratio limit still applies). A file nested close to yaml.v3's limit (10,000 levels) nests the JSON as deep, which a JSON decoder may refuse (measured: Python 3.13's json raises RecursionError at 10,000 nested arrays); a reader that cannot decode the document must treat the whole document as unusable. **List-only mode** ([ADR-036](adr/036-single-parser-effective-config.en.md) §7 step 9): `da-guard parse-tree --list-only --config-dir <dir>` (`mode: "list"`) makes the same walk and neither reads nor parses any file (each file is only opened and closed), and prints the same `defaults_carriers` and the same `entries` as tree mode — built by the same code — without `files` and `rendered_by`; its `problem` holds only the two kinds decided without reading, `read_failure` and `not_regular_file`, so content kinds (e.g. `syntax_error`) never appear and a file that opens but fails mid-read has none. Its `verdict` likewise knows only the walk's half of exit 3: a file whose content the exporter refuses is `null` here and `unservable` in tree mode, so a reader that must name every path validation names reads tree mode. Hidden entries and files without a config name are not read by the walk, so they are not listed; every directory the walk goes into is (`role: "dir"`). `--list-only --names <file|->` (`mode: "names"`) walks no directory and classifies a JSON array of strings (each a name with no separator) by name alone, in the order given (`depth` 0; `readers`, `role`, `problem` and `verdict` `null`; `class` may also be `hidden` or `not_config_name`; the names are taken as one directory and its defaults carrier is selected by the walk's rule: the chosen one is `defaults_carriers["."]`, every other carrier has `not_read` `unselected_defaults_carrier`, any other name `null`) — for names that are not on a disk, e.g. the keys of a ConfigMap built from a conf.d. Known limit: a lone UTF-16 surrogate escape in a string (e.g. `"\ud800"`) is read as U+FFFD, as Go's encoding/json reads it, so the entry answers for another name and the exit code is still 0 (no ConfigMap key or file name can hold such a character; the Python reader refuses it before asking). `--list-only` does not go with `--path` / `--input`, `--names` does not go with `--config-dir`, and `--names` requires `--list-only`. The full shape is in `da-guard parse-tree -h`. Exit codes: 0 (an unreadable file is data in the output, not a verdict), 2 (caller error, including a file name that is not valid UTF-8, or `--names` that is not a JSON array of strings, holds a non-name, or gives one name twice) |
| `compile-custom-alerts` | **Not forwarded by `da-tools guard`**; only `compile_custom_alerts.py` (`make custom-alerts-compile`) and recipe-preview call the da-guard binary with it (`da-guard compile-custom-alerts --config-dir <dir>`, [#3229](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3229)): prints, as JSON, the custom-alert rule pack (ADR-024 Capability B) — one vectorised rule set per shape (`recipe_id`) over every `_custom_alerts` entry the exporter serves or has switched off (the judgment `served-values` lists); entries it does not serve are in `not_compiled`, lists that do not decode in `block_errors`, tenants using a non-active recipe in `lifecycle`; plus `parse_failed` / `skipped` / `unreadable` from the same load as `served-values`. A tree whose `/metrics` cannot be gathered, or two shapes on one `recipe_id`, is exit 2; otherwise the exit codes are `served-values`' (0 / 2 / 3) |
| `write` | **Not forwarded by `da-tools guard`**; the one way Python tools change conf.d ([ADR-036](adr/036-single-parser-effective-config.en.md) §2: only Go writes conf.d, and tenant-api shares `pkg/confedit` with this command; [#3206](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3206)): `da-guard write --config-dir <dir> --input <file|-> [--dry-run] [--json]` takes a batch of files in one call, judged once and written only when all pass. The input is `{"files": [<entry>, …]}` (paths relative to `--config-dir`), each entry exactly one of: `{"path", "ops": [<op>, …]}` rewrites an existing file; `{"path", "create": <value>, "head_comment"?: <text>, "foot_comment"?: <text>}` writes a new file from the `map` value given (its top level, keys in the order given), `head_comment` at the top of the file and `foot_comment` after its last node (each set off by a blank line, a file that is `{}` alone too); `{"path", "from": <rel>}` moves an existing file, byte for byte, to a new path, the source gone after. Each path once, a move's source included (a path given twice is a caller error, `bad_input`); one file is a batch of one. A create's or a move's target must not exist: an existing regular file is a caller error (`path_exists`, exit 2); an existing path that is no regular file (a FIFO, a device, a directory, a symlink to one) is never read and is refused `not_read` (exit 1); missing directories are created, and removed again when the write does not complete. A move's source is judged by the same rule as an edit's original: a regular file the walk reads, with readers (a path through a symlinked directory — into the tree or out of it — under a hidden directory, with a hidden or non-config name is refused `not_read`; a symlink itself is refused `symlink`; a missing one is `source_missing`, exit 2) that `parse-tree` reads (else `original_unreadable`), and the target must read back as the source's own parse result; the moved file is the same file (its mode and owner too). Ops are applied in order: `{"op": "set", "path": [<key>, …], "value": <value>}` (sets the key; the mapping holding it must exist; with `path` `[]` the `value`, a `map`, replaces the whole top level and nothing of the old one is kept — for a file generated in full), `{"op": "unset", "path": […]}` (removes the key; absent is a no-op), `{"op": "ensure-map", "path": […]}` (the path is a mapping; each missing one is created). `<value>` names its type and the caller gives a mapping's order (a JSON object has none): `{"str": "80"}` (always double-quoted; under `_custom_alerts` a multi-line string that reads back the same is a literal block), `{"int": "80"}` / `{"float": "0.5"}` (canonical decimal text only; send other text as `str`), `{"bool": true}`, `{"null": null}`, `{"map": [["key", <value>], ["key", <value>, {"head": <text>, "line": <text>}], …]}` (keys unique, never `<<`; a new key is written plain only when it matches `[A-Za-z_][A-Za-z0-9_-]*` and is not a YAML 1.1 bool / null word (`y`, `n`, `yes`, `no`, `on`, `off`, `true`, `false`, `null`, any case), every other key double-quoted, e.g. `"010"`, `"on"`; the optional third element is the key's comments: `head` above the key, several lines, `line` after `key:`, one line), `{"seq": [<value>, [<value>, {"head": <text>, "line": <text>}], …]}` (an item given as the pair `[<value>, {comments}]` carries them: `head` above its `- `, several lines, `line` after it, one line; a value is always a JSON object, so an item that is an array can only be this pair). A comment (`head`, `line`, `head_comment`, `foot_comment`) is layout, not content: never read back, compared or judged; each of its lines is written `# <line>`, so whatever it holds it never becomes YAML; a carriage return, another control character but a tab, DEL, U+0085 / U+2028 / U+2029, U+FEFF, or a line break in `line` is a caller error (`bad_input`); a key whose value is an empty mapping or sequence (written `{}` / `[]`) takes no `line` (`bad_input`, naming the key; give it a `head` instead); nor does a sequence item that is a non-empty mapping or sequence (yaml.v3 would write it on another item's line or drop it; `bad_input`, give it a `head`); in such an item the `head` of the first key or item is written after the outer `- `, just above it (`- # …`). The input must be UTF-8 JSON: invalid UTF-8, an unpaired `\uD800`–`\uDFFF` escape, a member given twice in one object or a member name in another case is a caller error (`bad_input`). An edit is made on the yaml.v3 node tree; nodes and comments outside it are written back as they were, while a comment on an edited node itself may be dropped (a scalar replaced by a scalar keeps its trailing comment, one replaced by a mapping or sequence does not). The re-encode changes layout, never content: indentation becomes 2 spaces, a trailing comment sits one space after its value, an explicit key (`? a`) is written `a:`, blank lines may go; the line ending (CRLF or LF, from the first line) and a leading UTF-8 BOM are kept. When an edit's resulting node tree is the original's, the status is `unchanged` and nothing is written — but the text left in place after the call must read back clean whether or not it changed: on an original validation names, an edit that changes nothing is refused `read_back_mismatch` as well, never `unchanged`. A file holding only comments is edited as an empty one (as a blank file is): the ops apply to an empty mapping and the comments are written back, verbatim, first. Before writing, for every file, in order — and no file is written unless every check holds for every file; each refused file is named with its reason: a reader reads the target path (its `parse-tree` file-mode entry has `readers` and `role` `config`); `parse-tree` reads an edit's original (no `problem`; validation naming the original is no refusal: an edit may repair a file the exporter cannot serve, never worsen one — the text to be written is what is judged); every op applies on the node tree and on the parse result; the re-encode changes no node outside the edit (ADR-036 §4's invariant); the batch is judged once: the tree is walked once with every file's text in place at its path and every move's source gone (in memory, no copy of the tree) and judged by `parse-tree`'s tree mode, and each file's entry there has readers, no `problem` and no `verdict`, and its content equals the original's parse result with the ops applied (a create: the value given; a move: the source's own) (files of one batch bear on each other: two spellings of `_defaults` written into one directory together have one selected, and the other is refused `not_read`; a file the call leaves as it is is judged in that tree too); and every edited original and move's source still holds the bytes it was read from. A call may repair the tree, never worsen it: the tree before the call (the disk) and after it (the batch written) are compared, and the whole call is refused (the call's `reason` `worsens_tree`, `worsened` naming each path and what got worse) when the after-tree has anything the before-tree did not — a path outside the batch losing readers (e.g. creating `_defaults.yaml` beside the selected `_defaults.yml` deselects the latter), a new `verdict` or `problem` on any entry (a file or a directory, one the call creates included: `x.yaml/a.yaml` makes the directory `x.yaml` `tree_refused`; or moving the selected carrier away, so that the other spelling is selected and cannot be read), or a new error of da-guard's own validation (the same path as `da-guard --config-dir`: a tenant declared in two files, …). Validation errors are compared by kind, path and tenant, so an error already there elsewhere does not block an unrelated write, and a batch that moves a tenant from one file to another passes; a tenant declared in two files does not stop the validation — it is set aside and the rest is validated — and a tenant declared twice before the call has no baseline, so its errors after the call are not new (removing a copy is a repair); validation warnings and what only the route generator judges are not compared. The write has three steps: every file to edit or create goes to a hidden temporary file in its own directory; then every edited original and move's source is compared with the bytes it was read from and every target checked absent (one that fails is refused `changed_during_write`, and no file is written); then each goes in place, creates first, then moves, then edits: a create is hard-linked into place, a move hard-links its source at its target and then removes the source name (neither replaces a path created after the judgement; on a file system without hard links, a rename after checking once more that the path is absent), an edit is renamed into place (compared once more right before; a comparison, not a lock: what another writer writes between that check and the rename is overwritten). A failure in the last step removes the creates already in place and moves the moved files back (each `not_written`; only while the path is still the file this call put there, compared by inode — one replaced meanwhile is left as it is, and that or one that cannot be undone is `maybe_written`); an edit's rename cannot be undone, so those files stay written (the call's `reason` `partial_write`, their `status` `written`). A written file keeps the original's mode, and its owner and group where they can be set (otherwise a warning on stderr and in `warnings`, never silently); a symlink is not rewritten; a path that exists but is no regular file (a FIFO, a device, a directory) is never read: `parse-tree` judges it and it is refused `not_read`. `--dry-run` writes nothing and gives each file's text that would be written (without `--json` on stdout, each after a `==> <path> <==` line). `--json`: stdout is exactly one `{"schema": "da-guard.write/v3", da_guard_version, command, status, reason, message, files, worsened}`, `files` holding one `{path, kind, from, status, reason, message, entry, problem, difference, content, warnings}` per input file, in the input's order (`kind` is `edit`, `create` or `move`, `from` a move's source; once the input is read, one per input file even when the call then stops, e.g. on a `--config-dir` that is no directory; `[]` when the input itself is not usable), `worsened` a list of `{kind, path, tenant, finding, message}` (`kind`: `readers_lost`, `verdict`, `problem`, `duplicate_tenant`, `finding`; `finding` is the validation finding's kind), `null` where there is no value (`warnings` and `worsened` are `[]`). The call's `status` is `written`, `unchanged` (every file's text is its own; nothing written), `dry_run`, `refused` or `caller_error`; its `reason` is set only when the call itself fails: `bad_flags`, `bad_input`, `bad_path`, `write_failed` (a write failed and no file is left written), `partial_write` (the write stopped part-way and some files are written); on a refusal `worsens_tree` (the batch would worsen the tree, above); and is `null` when the status comes from the files. A file's `status` may also be `not_written` (fine, or not reached, but not written by this call — one written and undone again included) or `maybe_written` (whether it is in place is not known); `content` is the dry run's text; refusal `reason`s: `not_read`, `symlink`, `original_unreadable`, `edit_not_applicable`, `not_round_trippable`, `read_back_mismatch`, `changed_during_write`; `entry` is the `parse-tree` entry the refusal rests on (file mode for the original or the path, or the entry in the tree with the batch written), `problem` is `{kind, message}`, `difference` is `{path, want, got}` (`want` / `got` in parse-tree's form). Exit codes: 0 (every file written or unchanged, or a dry run), 1 (refused; nothing written; every refused file named), 2 (caller error: flags, input that is not the JSON above, a path given twice, absolute or outside `--config-dir`, the file to edit or the source to move missing, a create or move onto an existing regular file, a failed write; with `partial_write` some files are written). The Python tools use it through `scripts/tools/_lib_confedit.py` (`write_all`; `edit_all` / `emit_all` are its edit-only and create-only forms): `patch-config`, `threshold-govern`, `deprecate` for existing files (#3206 step 10b), and `patch-config` for the new `<tenant>.yaml` of a tenant no key declares; `scripts/tools/dx/generate_tenant_fixture.py` hands it every file of a synthetic fixture to create at once (one batch, the whole tree judged once, one refusal writes none; `write` exits 1 on the refusal and `generate_tenant_fixture.py` turns it into its own exit 2); the other tools that create new files are a later step |

**Binary resolution order**

1. `--da-guard-binary <path>` (explicit override)
2. `$DA_GUARD_BINARY` env var
3. `da-guard` on `$PATH`

If none resolves, prints install hints (download from `tools/v*` release / `cd components/threshold-exporter/app && CGO_ENABLED=0 go build -o /usr/local/bin/da-guard ./cmd/da-guard`).

**`defaults-impact` key flags**

| Flag | Default | Description |
|---|---|---|
| `--config-dir <path>` | (required) | conf.d/ root |
| `--scope <path>` | whole tree | Limit to a directory at or below `--config-dir` (CI passes the directory of the changed `_`-prefixed YAML file (e.g. `_defaults.yaml`, `_profiles.yaml`), relative to `--config-dir`; `.` when several directories under one root changed). A relative path is **relative to `--config-dir`**, whatever the working directory: `--config-dir conf.d/ --scope db/` (`.` = the whole tree); an absolute path is used as is. A scope that resolves outside `--config-dir` is exit 2 ([#2588](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2588)) |
| `--required-fields <a,b,c>` | empty | CSV of dotted-path fields every tenant must have, judged against the effective config. A path whose first segment is a routing-layer key (`_routing`, any `_routing`-prefixed key, `_severity_dedup`) is refused with exit 2 and the reason ([#2904](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2904); it used to be judged against the resolved routing) — never a check that silently passes |
| `--cardinality-limit <n>` | the root `_defaults.yaml`'s `max_metrics_per_tenant` (unset = 500; negative = no check) | Per-tenant predicted-metric ceiling; an explicit value overrides, `0` disables |
| `--cardinality-warn-ratio <r>` | 0.8 | Warn-tier ratio (0 < r < 1) |
| `--baseline-config-dir <path>` | empty | The same conf.d before the change (CI passes the PR's merge-base); when the root `_defaults.yaml`'s `max_metrics_per_tenant` is raised or disabled, the report opens with a notice. Never affects the exit code |
| `--format md\|json` | md | Output format |
| `--output <path>` | stdout | Write report to file (parent dir must exist) |
| `--warn-as-error` | false | Treat warnings as errors for exit code |

**Exit codes**

| Code | Meaning |
|---|---|
| 0 | clean — no error-tier findings (warnings don't block unless `--warn-as-error`), and nothing in the tree only the route generator can judge (see 4). A `--scope` with no tenant still runs every check: when those that need no tenant (the defaults files' shape, `/metrics` not being gatherable) yield findings, the report comes out as usual and the findings decide the exit code; only a run with no finding and nothing undetermined prints "vacuously safe" and exits 0 ([#2991](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2991)) |
| 1 | guard found errors, or warnings under `--warn-as-error` — block merge / commit |
| 2 | caller error (bad flags, a wrong path — a `--config-dir` or `--scope` that does not exist, a component on the way that is not a directory, or a symlink loop —, scope outside root (through a symlink included), binary missing), and a conf.d path name that is not valid UTF-8 (no output can carry it; `served-values`, `effective`, `key-refs`, `parse-tree` and `compile-custom-alerts` share the one check, and the message names the bytes escaped, e.g. `"bad-\xff.yaml"`; validation used to exit 0, [#3206](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3206)). Exception: a `--baseline-config-dir` that does not exist is not an error; the run is judged as usual |
| 3 | files the exporter drops whole when it loads the tree, plus files da-guard itself cannot decode, plus the files the exporter's load cannot stat or read and the sub-directories it cannot list (the same list as `served-values`' `unreadable`; the report lists them under "Files the exporter cannot read", the JSON report as `unreadable`; a symlink to a directory is not one, [#2588](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2588)), limited to those that bear on this run (files in `--scope`, and `_`-prefixed files in the directories above it; a directory that cannot be listed counts only when it is `--scope`, lies below it, or contains it), plus a `--scope` that cannot be read (its stat fails for a reason other than a wrong path, e.g. no permission, under a directory the running user may not enter; listed as `stat_error`, beside the `walk_error` of the directory that hides it) and a `--config-dir` that cannot be read (listed as `.`: `walk_error` when it cannot be listed, `stat_error` when it cannot be stat'ed; the reason on stderr; exit 2 before, [#2627](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2627)); independent of `--cardinality-limit`. The report and stderr list them (relative to `--config-dir`); a run may list only the first file that does not decode, so re-run after fixing. Takes precedence over 1 and replaces the "vacuously safe" 0. Since [ADR-036](adr/036-single-parser-effective-config.en.md) step 6 ([#3206](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3206)) the exporter asks `parse-tree` before it reads each file, and drops whole every file `parse-tree` names unreadable (several documents in one file or a trailing `---` alone, a tag outside its allowed set such as `!!binary` / `!!set`, the non-specific tag `!`, a byte order mark past the start, a null key, a repeated key, a non-scalar key, …; the closed list is in the `parse-tree` section), so those files are here too (`parse_failed`); the exporter's log and `da_config_parse_refused_total` carry the `parse-tree` `kind`. The contract test `TestExitThree_NamesExactlyTheFilesTheExporterDrops` is authoritative (#2123, #2179; its corpus holds the `parse-tree` refusals above, and `TestExitThree_ParseTreeRowsAreDropped` pins which file each of those rows drops) |
| 4 | **undetermined** ([#2904](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2904), [ADR-036](adr/036-single-parser-effective-config.md) §9): the tree holds what only the route generator can judge, and da-guard no longer judges routing or domain policy (it reports no routing finding at all). Judged over the **whole** `--config-dir`, whatever `--scope` says (the generator judges the whole tree), with the files and their content taken from the same result `parse-tree` prints (the files the generator reads). Each piece of evidence is `{file, field, reason}`, `reason` a closed list: `routing_key` (a mapping key at any depth that is a routing-layer key — `_routing`, any `_routing`-prefixed key misspellings included, `_severity_dedup` — or a key named `routing_profiles`; `field` is the dotted path, a list element written `[i]`), `domain_policy` (`_domain_policy.yaml` / `.yml`, any level, `field` empty; or a top-level `domain_policies` key in any other file the generator reads, `field` `domain_policies`, on which the generator refuses the whole tree in every mode, [#3126](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3126)), `routing_profiles_file` (`_routing_profiles.yaml` / `.yml`, any level; `field` empty), `unreadable_outside_run` (a path `parse-tree` names unreadable that this run's exit 3 does not name: such a file outside `--scope`, and a config-named directory or a symlink to a directory (`not_regular_file`; the exporter does not read it as a file); the generator judges the whole tree and still refuses it. `field` is the `parse-tree` kind; a tree the walk cannot scan at all is `.` with `read_failure`. Replaces `generator_unreadable` since ADR-036 step 6 ([#3206](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3206)): the exporter refuses every file `parse-tree` names unreadable, so such a file a run reads is exit 3 and no longer evidence too), `invalid_tenant_id` (an id under `tenants:` the tenant-id rule refuses: the exporter serves it, the generator refuses the whole tree; `field` is `tenants.<id>`), `duplicate_tenant` (one tenant id declared by two tenant files, the later file in name order named; a whole-tree run is already refused by the exporter with exit 2, a `--scope` run that does not see it reports it here; `field` is `tenants.<id>`). Fails toward 4: a misspelled key the generator would ignore counts too. Every JSON report carries `undetermined` beside `parse_failed` / `unreadable` (`[]` when there is none): the report, the report of a `--scope` with no tenant, and the report of a run that stopped while resolving (exit 3, still listing the evidence it can compute). The Markdown report adds a "Routing and domain policy: not judged" section (up to 20 items, the rest counted) with the remedy, and stderr carries one line saying the same. Remedy: run `da-tools generate-routes --validate --strict`. Precedence **3 > 1 > 4 > 0**: 3 and 1 win as before; `--warn-as-error` affects 1 only. No tenant in `--scope`, no finding, but such content in the tree is 4, not the "vacuously safe" 0; a tree without it exits 0 as before. |

**`unregistered_finding_kind` ([#2904](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2904))**: every finding's `kind` comes from a closed list (the tables below). If a finding carries no kind from that list (a da-guard bug), the report is still written whole (both `--format json` and `md`, and to `--output`): that finding's `kind` is written as `unregistered_finding_kind` and its severity forced to `error` (the summary is recomputed), stderr carries one line saying how many findings were affected, and the exit code is 1. The value is part of the JSON contract; please report it when you see it.

**Routing checks ([#2280](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2280))**

⚠️ Since [#2904](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2904) `defaults-impact` **does not run the checks of this section**, and since [#2905](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2905) (ADR-036 §7 step 3) da-guard has no code for them: none of the findings below appear in its report (a tree carrying such content exits 4). The route generator (`generate-routes --validate --strict`) reports findings of the same names; this section stays as each kind's definition, written as da-guard reported them until then.

The routing checks look at each tenant's **resolved** routing, merged from the same three layers the route generator (`generate-routes`) uses: the root `_routing_defaults` → the routing profile named by `_routing_profile` → the tenant's own `_routing`, a shallow merge per top-level key, then `{{tenant}}` replaced with the tenant id. `_routing_enforced` takes no part, except that its `group_by` is judged for `routing_group_by_invalid` and the shape of its `match` for `routing_enforced_match_invalid`. Platform files are read from the whole `--config-dir` tree (as the generator does, [#2326](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2326)): `_routing_defaults` merged shallowly level by level down the tenant's directory chain, a subdirectory's routing profiles and domain policies applying to that subtree only; never from `--scope`, so a tree-shape error outside the scope is reported by a scoped run too (the generator refuses the whole tree). The tenant layer is what the generator reads ([#2291](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2291)): the tenant file's own `_routing` / `_routing_profile` over the same keys of the root platform files' `tenants.<id>` entry (a key the tenant file writes replaces the platform's whole). The merged effective config is **not** read, so a `_routing` in a defaults block or a threshold profile is never judged as the tenant's routing; it is reported as `routing_in_unread_location`. A `--required-fields` entry that is `_routing` or starts with `_routing.` is judged against the resolved routing too; every other field still reads the effective config. The main receiver, every `overrides` entry and every ADR-007 `routes` entry get the same shape checks, and their receiver types are judged against `_domain_policy.yaml`: `forbidden_receiver_types` and `allowed_receiver_types` are separate tests, so one receiver can break both.

| Finding kind | Severity | Trigger |
|---|---|---|
| `invalid_route_entry` | error | `routes` is not a list, or an entry the generator skips (not a mapping, an unsupported key such as `continue` / `match_re`, a missing or empty `match`, an invalid label, a value that is not a non-empty string); Field is `routes` or `routes[i]` |
| `routing_value_not_string` | error | a `routes[i].match` value or `overrides[i].alertname` / `metric_group` that the route generator (through `da-guard parse-tree`, yaml.v3) reads as a non-string (`true` a boolean, `8` an integer, `2001-12-15` a timestamp, `~` null, `!!int 5`; `yes` / `on` / `1:30` are strings, #2906); Field is `routes[i].match.<label>` or `overrides[i].alertname` / `metric_group`. Same judgement as the `generate-routes --strict` ERROR (#2431). Fix: quote it, e.g. `team: "yes"` |
| `routing_group_by_invalid` | error | a `group_by` element (main route, `overrides[i]`, `routes[i]`) that, as the route generator reads it (yaml.v3), is not a string (an unquoted `8`, `true`), is empty, repeats a label listed earlier, or is `...` alongside other labels; Field is `group_by[i]`, `overrides[i].group_by[j]` or `routes[i].group_by[j]`. The root `_routing_enforced.group_by` is judged too, only when the generator renders that route; there the tenant is empty and Field is `<file>:_routing_enforced.group_by[i]`, or `<file>:_routing_enforced (<tenant>).group_by[i]` when it uses `{{tenant}}` — once per tenant, tenants without routing included, but not a tenant whose tenant-file body is null (or not a mapping) with no root platform `tenants.<id>` entry making it one — the generator does not load it (#2519). Alertmanager refuses the whole config for the last three, and groups by a label named after the text for a non-string. Same judgement as the `generate-routes --strict` ERROR (#2503). Fix: quote it (`"8"`) or remove it |
| `domain_policy_violation` | error | the main receiver / `overrides[i]` / `routes[i]` type breaks a domain policy; the message names the domain, the constraint and the layer the value came from |
| `critical_escalation_missing` | error | a domain policy sets `require_critical_escalation: true`, yet severity=critical alerts reach no pagerduty receiver: the main receiver is not pagerduty, and no rendered `routes` entry whose match has `severity: critical` sends to one ([#2325](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2325)); Field is `receiver.type`, one finding per requiring domain. A non-boolean value is reported as `domain_policy_unusable` instead (Field `<file>:domain_policies.<domain>.constraints.require_critical_escalation`) |
| `critical_escalation_leak` | warn | the tenant escalates, but this non-pagerduty destination (`overrides[i]` / `routes[i]`, the main receiver last) still receives some severity=critical alerts before any pagerduty receiver does; the message names the label set it catches. Same judgement as the generator's `--validate` WARN (not listed when an earlier sub-route's match is a subset of it, or when its match names another tenant or a non-critical severity); Field is `<ref>.receiver.type`. Never blocks |
| `unknown_routing_profile` | warn | `_routing_profile` names a profile nothing defines (whitespace-only counts) |
| `domain_policy_unusable` | error | a `_domain_policy.yaml` structure that cannot be used (e.g. `tenants` is not a list), or a `domain_policies:` block in a file other than `_domain_policy.yaml` / `.yml` (Field `domain_policies`, [#3126](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3126)); empty tenant. The table describes da-guard as it was (only the checks that depend on it were skipped); since [#3116](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3116) / [#3126](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3126) the route generator refuses the whole tree on every one of these in every mode |
| `routing_profiles_unusable` | warn | `routing_profiles:` is not a mapping; empty tenant. `generate-routes` applies the same test, as a WARN line that does not block (#2904) |
| `routing_defaults_routes_ignored` | error | `_routing_defaults` carries `routes` (they belong in a profile or the tenant); both readers drop them before the merge, and the generator's `--validate` fails on it too |
| `routing_key_ignored` | error | A key the route generator does not read in a tenant `_routing`, a routing profile, `_routing_defaults` or `_routing_enforced` (including their receivers, `overrides` entries and `routes` entries' receivers), such as `continue`, `mute_time_intervals` or a typo; the output is as if it were absent, and the generator's `--validate` fails on it too (#2960). For a tenant's `_routing` the tenant column is that tenant and Field the path relative to `_routing` (e.g. `overrides[0].matchers`); otherwise the tenant column is empty and Field is `<file>:<key path>` (e.g. `_routing_profiles.yaml:routing_profiles.p1.continue`). Keys are judged as written (`1:` counts); null keys and anything under `http_config` are not looked at. ⚠️ A boolean, number or date key is printed as da-guard decodes it (`True:` as `true`, `1.0:` as `1`); the verdict is the same. Also reported: a `_routing*` key the generator does not read (#2983) — in a tenant's config, anything but `_routing` / `_routing_profile` (the tenant column is that tenant, Field the key, e.g. `_routing_profle`; the tenant file and the root platform files' `tenants.<id>` entries are both read); at the top level of a `_` file, anything but `_routing_defaults` / `_routing_enforced` (Field is `<file>:<key>`, e.g. `_defaults.yaml:_routing_defualts`; a top-level `_routing` in a defaults file without `defaults:` is now reported here rather than as `routing_in_unread_location`) |
| `routing_not_mapping` | error | the tenant's `_routing` (the tenant file's over the root platform files' `tenants.<id>`), as the route generator reads it (yaml.v3), is neither a mapping nor a disabling string (`disable` / `disabled` / `off` / `false`, as a YAML string; an unquoted `off` is a string too, #2906): e.g. `"slack"`, `no`, a list, null, an unquoted `false` (a YAML boolean). The generator renders no route at all for the tenant (it does not fall back to `_routing_defaults`); `--validate` and `--strict` both fail on it (#2341). Field is `_routing`. To turn routing off, write `_routing: off` or `disable` |
| `severity_dedup_invalid` | error | a tenant's `_severity_dedup` is none of `enable` / `disable` (also `enabled` / `on` / `true`, `disabled` / `off` / `false`, any case), a boolean or null: for example a quoted `"no"` or `"yes"`, an empty string, a number; the generator refuses the whole tree in every mode (exit 1, [#2906](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2906)). Such a value used to be read as enable in silence. The tenant column is the tenant, Field is `_severity_dedup` |
| `routing_profile_invalid` | error | a tenant's `_routing_profile` is neither a profile name (a string) nor null: for example a mapping (a time-window schedule `{default: p1, overrides: [...]}` included), a number, a boolean, a list; the generator refuses the whole tree in every mode (exit 1, [#3269](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3269)). Such a value used to be skipped in silence, leaving the tenant on the default route. `check_routing_profiles.py` reports it as an ERROR with the same judgement and message (with or without `--strict`, exit 1). The tenant column is the tenant, Field is `_routing_profile` |
| `invalid_tenant_id` | error | a declared tenant id that is not a DNS-1123 label (1-63 lowercase letters, digits and `-`, starting and ending with a letter or digit; [ADR-035](adr/035-tenant-id-single-source.en.md)): empty, upper case, `_`, `.`, a space, or longer than 63 characters; the generator refuses the whole tree in every mode (exit 1; #2341, ADR-035). The message cites the schema's description of the rule. An empty id used to render a `tenant=""` matcher, which caught platform alerts that carry no tenant label. The tenant is the id (empty for an empty id) and Field is `<tenant file>:tenants.<id>` |
| `routing_defaults_not_mapping` | error | a `_routing_defaults` (a root `_` file, or a subdirectory's defaults carrier) that is neither a mapping nor null; the level contributes nothing, and the generator's `--validate` and `--strict` both fail on it (#2341). The tenant is empty and Field is `<file>:_routing_defaults` |
| `routing_enforced_match_invalid` | error | an enabled root `_routing_enforced` whose `match` is not a list of non-empty strings (the `routes[i].match` mapping `{severity: critical}`, a single string, `[1]`, `[~]`, ...); the generator renders no enforced route from that block and `--validate` fails on it (#2989). Before, the generator dropped such a `match` without a word and the NOC route received every alert. The tenant is empty and Field is `<file>:_routing_enforced.match` |
| `routing_in_unread_location` | error | a `_routing` or `_routing_*` key where the generator never reads it: inside the `defaults:` block of any `_defaults.yaml` (root or nested), or inside a threshold profile of a root platform file's `profiles:` (a `_routing*` key the generator does not read at the top level of a `_` file is reported as `routing_key_ignored` since #2983, not as this finding). The exporter merges one in a defaults block into the effective config, but no route is rendered from it. **Threshold profiles** ([#2914](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2914)): every routing-layer key is reported — `_severity_dedup` and any key starting with `_routing`, the same predicate as the keys the exporter does not fill into a tenant — whether or not a tenant elects the profile and whatever `--scope`. The generator reads these keys from the tenant's own entry only (its tenant file, or its entry in a root platform file's `tenants:`), and the exporter does not fill a profile's copies into the tenant either (it logs one ERROR per load and counts them on `da_config_profile_routing_keys`), so they have no effect on any tenant and the routes and inhibit rules rendered do not change. Migration: rewrite `_routing` as a routing profile in `_routing_profiles.yaml` and set `_routing_profile` in the tenant's entry (or write it into the tenant's own `_routing`); write `_severity_dedup` and `_routing_profile` into the tenant's own entry; then delete them from the profile. Empty tenant; Field is `<file>:<key path>` (e.g. `_profiles.yaml:profiles.p1._routing`); the message points to the root's `_routing_defaults`, `_routing_profiles.yaml`, or the tenant's own file. ⚠️ In the **root** `_defaults.yaml`, a `defaults:` block carrying `_routing*` fails the exporter's decode and the file is dropped whole, so that spelling shows up as exit 3 (`parse_failed`), not as this finding; only a nested `defaults:` block produces it. A top-level `_routing_defaults` in a nested `_defaults.yaml` is read since #2326 and not reported; one at the top level of any **other** `_` file below the root (e.g. `team/_routing.yaml`) is not read (the same file at the root would be), is reported as this finding, and fails the generator's `--validate` too. Since #2904 the generator reports this kind for every place above and fails `--validate` on it (the root `defaults:` case, exit 3 in da-guard, it reports as well). A non-string key in `profiles:` or in a profile no longer hides the `_routing` beside it (#2904). Separately, a tenant with `_routing: disable` is still reported `missing_required` under `--required-fields _routing*`, and the message says the routing is explicitly disabled |
| `routing_enforced_below_root` | error | a file below the root carries `_routing_enforced` (read at the root only; #2326). The generator refuses the whole tree (exit 2) |
| `routing_defaults_null_below_root` | error | `receiver` or `overrides` written as null in a subdirectory level's `_routing_defaults`; the generator refuses the tree (exit 2) |
| `routing_profile_duplicate` | error | one routing-profile name defined in two files (root `.yaml` beside `.yml` included); the first definition in name order (root first) is kept and Field names the later one; the generator refuses the tree (exit 2) |
| `domain_policy_out_of_scope` | error | a subdirectory's `_domain_policy.yaml` names a tenant outside that subtree; the entry is not enforced |

One tenant id declared by two tenant files is rejected by the exporter's own resolution (`duplicate tenant ID`): da-guard exits 2 before any check runs.

None of these puts a file on the exit-3 list; a platform file whose syntax the exporter cannot read is still named once, by exit 3.

**The `defaults:` wrapper of `_defaults.yaml` ([#2386](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2386))**

| Finding kind | Severity | Trigger |
|---|---|---|
| `root_defaults_unwrapped` | error | the conf.d root defaults file (`_defaults.yaml` / `.yml`) has a `defaults:` that is not a mapping (the key is absent, or present with no value), and its top level holds thresholds (keys with no `_` prefix) and reserved keys other than `_routing*` ones (e.g. `_severity_dedup`, `_silent_mode`, `_state_*`). Not listed: fields of the exporter's root config struct (e.g. `state_filters`, `max_metrics_per_tenant`); `_routing_defaults` / `_routing_enforced`, which other tools read at the top level; `_metadata`, which the defaults merge drops at every level; `_routing*` keys (the route generator does not read routing from a defaults file; a misplaced one is `routing_in_unread_location`); any other `_` key (e.g. `_x: &x`, a key that only carries a YAML anchor). The defaults merge then reads the whole document, so the merged config behind `/effective` and the other checks shows those keys; the exporter's root decode reads thresholds only from a `defaults:` mapping, so `/metrics` does not carry them from this file, and the load logs nothing. Measured: a top-level threshold was absent from `/metrics`; a top-level `_severity_dedup: disable` showed `disable` on `/effective` while `/metrics` served `enable`. Empty tenant; Field is the file name; the message names the keys and says the root `defaults:` holds numbers only; when `_custom_alerts` is among them it also says that no reader takes it from a `_defaults.yaml` and the list goes in a tenant's entry, a root platform file's `tenants:` or a profile ([#3042](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3042); `defaults_toplevel_ignored`'s root message says the same). Root only: below the root, the merged config of an unwrapped file is what is served |
| `defaults_toplevel_ignored` | error | a defaults file at any level whose `defaults:` is a mapping (`{}` included, and a YAML `!!set`) while its top level also holds the keys listed above. The defaults merge then reads only that mapping (the exporter's `ExtractDefaultsBlock`), and those keys appear in no tenant's `/effective`; with `defaults:` and no value the merge reads the whole document, and nothing is reported. Measured on a subtree: wrapped this way, a top-level threshold and `_severity_dedup` were no longer served on `/metrics`; `_silent_mode` and `_state_*` changed `/effective` only (they were not on `/metrics` in any shape). Empty tenant; Field is the file path; the message names the keys. Below the root it suggests moving them under `defaults:` or leaving `defaults:` with no value — except the keys subtree defaults do not support (those `subtree_default_reserved_key` reports, such as `_silent_mode`, `_severity_dedup`, `_state_*`): moving them would only trade this finding for that one, so they get that finding's per-key fix instead (today they have no effect at the top level — they are in neither `/effective` nor what the exporter serves — so deleting them from the file keeps things as they are; and "leave `defaults:` with no value" is then not suggested either) ([#2388](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2388)); at the root it says the root `defaults:` holds numbers only |
| `root_defaults_critical_key` | warn | the conf.d root defaults file's `defaults:` mapping holds a `<key>_critical` (either spelling; `_state_` / `_silent_` keys excluded). The exporter serves it as a threshold of its own (metric `<key>_critical`, severity=warning); it does **not** become `<key>`'s critical row, which comes only from the tenant side (the tenant's own key, or one a subtree `_defaults.yaml`, a root platform `tenants:` entry or a profile supplies). A tenant's key of the same name is therefore not reported as `redundant_override` against this root value. When the root writes a `_critical` key and a tenant also gets the same key, in either spelling, from any source (the tenant file, a subtree `_defaults.yaml`, a root platform `tenants:` entry or a profile) with a value that produces a critical row (it parses as a number, is not `disable`, and `<key>` has a default), `served-values` exits 2, because that key then owns a warning row and a critical row. Empty tenant; Field is `<file>:defaults.<key>` ([#2544](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2544)) |

All three skip a file the exporter drops (still named only by exit 3).

**Subtree defaults keys the exporter cannot deliver ([#1976](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1976))**

| Finding kind | Severity | Trigger |
|---|---|---|
| `subtree_default_undeliverable` | warn | a tenant inherits a threshold key from a subtree `_defaults.yaml` that neither the conf.d root `_defaults.yaml` nor `optional_overrides:` declares. The tenant's effective config (`/effective`) shows a value for it, but the exporter serves no series for it, so that alert can never fire; at load time the exporter logs an ERROR and counts the tenant on `da_config_subtree_undeliverable_tenants`. The verdict is taken from the exporter's own load of the tree; da-guard judges nothing itself, and reports tenants under `--scope` only. Fix: declare that threshold key in the root `_defaults.yaml` or in `optional_overrides:`. Tenant is the tenant; Field is the key. `served-values` lists the key in `unserved` too. Three kinds of key are not reported: reserved keys (such as `_metadata`, `_profile`, `_state_*`) and keys the exporter never serves as a threshold row (`_silent_*`, `_state_*`, `_severity_dedup`, `_routing*`) — declaring either kind at the root is not a fix (no `_` key makes a threshold row in any layer); they are reported as `subtree_default_reserved_key` below ([#2388](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2388)) — and keys the subtree switches off (a `disable` value and the like), which are simply not reported. The exporter's ERROR and gauge still count all of them. No `_` key is a threshold: the exporter makes no threshold row of one in any layer (one the registry does not list, such as `_myth`, included), so none is reported here but as `subtree_default_reserved_key` ([#3045](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3045); before it, a `_` key the registry does not list was served as a threshold and reported here). ⚠️ A warning for now (only `--warn-as-error` turns it into exit 1); planned to become an error in the next minor release |
| `root_default_non_finite` | error | a threshold under the root `_defaults.yaml`'s `defaults:` written as a non-finite number (`.nan`, `.inf`, `-.inf`) ([#3140](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3140)). The exporter serves no default row for it, and the key is read like an `optional_overrides:` declared key: a tenant's own value (its `<key>_critical` too) is served, a tenant that writes none has no series. The predicate is the exporter's (`pkg/config` `NonFiniteDefaultKeys`); a `_` key is not reported. Tenant is empty, Field is `<file>:defaults.<key>`; reported for a tree with no tenant too. ⚠️ **Behavior change**: such a tree exited 0 before and exits 1 now. Fix: write a finite number, or list the key under `optional_overrides:` instead |
| `root_default_null_undeclared` | warn | the tenant side (the tenant file, a root platform file's `tenants:` entry or a profile) sets a threshold key that the root `_defaults.yaml` writes as null (`k:`, `~`, `null`) under either spelling. A null is no write ([#2518](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2518)), so the root does not declare the key and the exporter serves no series for it: that alert can never fire. Before the upgrade the null decoded to a threshold of 0 and the tenant's own value was served, so this is where behavior changes on upgrade. The verdict is taken from the exporter's own load of the tree, for tenants under `--scope` only; `served-values` lists the key in `unserved` too. Fix: give it a number in the root `_defaults.yaml`, or list it under `optional_overrides:` (declared without a platform value; the null line may stay or go). No `_` key is a threshold or makes a threshold row in any layer, so a number is no fix and it is not reported ([#3045](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3045)). A tenant-side `<key>_critical` whose `<key>` the root writes as null (either spelling) is reported here too: the critical row is served only when the root `defaults:` holds `<key>`, so the only fix is a number for `<key>` at the root (which also serves `<key>` at warning severity to tenants that do not set it); listing it under `optional_overrides:` does not serve the critical row. A `<key>_critical` whose `<key>` the root never wrote is not caused by a null and is not reported here. A value a subtree `_defaults.yaml` hands down is reported by `subtree_default_undeliverable`, not here as well; a key the tenant switches off (`disable` and the like) is not reported |
| `schedule_null_value` | error | a threshold is written as a **schedule with override windows** and holds a null: a null window entry, a window's `window:` or `value:` written as null, or a null `default:` beside windows ([#2708](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2708)). The shape has no defined meaning and is refused at validation time. Checked in: the `tenants:` entries of tenants in `--scope` (tenant files and root platform files), each level's selected `_defaults.yaml` defaults block, and root platform files' `profiles:`; a file the exporter drops is skipped (still named only by exit 3). Field is `<file>:<section>.[<tenant or profile>.]<key>`; the tenant column is the tenant for a `tenants:` entry. Fix: give the default and every window a value; to leave that layer without a value, remove the key or write `null` |
| `value_not_served` | error | a threshold value the tenant's effective config shows is not served as written by `/metrics` ([#2065](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2065)). The verdict is the tenant's `not_served` (the same function as `da-guard effective`), five reasons only; the message starts with the reason, then names the file, the consequence and the fix: `value_unparsed` (not a finite number the exporter reads — `NaN`, `Inf`, `-Inf` included ([#3140](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3140)); so is `disable:critical` on a threshold row, whose message names `disable` and `<key>_critical: disable` ([#3141](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3141)) — or a severity suffix other than `warning` / `critical` in any letter case, e.g. `70:high`, `70:`, [#3071](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3071); the platform default is served instead), `value_unparsed_dropped` (a `_critical`, dimensional or `optional_overrides:`-declared key whose value does not parse and has no default to fall back to; that alert can never fire), `window_invalid` (a schedule override whose `window:` is not accepted — not a UTC `HH:MM-HH:MM`, missing, or starting where it ends — so that window never applies), `value_rejected` (a subtree `_defaults.yaml` value that is not threshold-shaped; the tenant keeps a shallower level's value), `spelling_duplicate` (the winning layer's mapping writes the threshold under another spelling too; [#2031](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2031)). One per (tenant, key), Field is the key; threshold keys only (not `_`-prefixed) — the other reasons and reserved keys in subtree defaults have findings of their own. ⚠️ **Behaviour change**: a tree with such a value that used to pass (exit 0) now exits 1; since [#3071](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3071) so does a tree writing a severity suffix such as `70:high` or `70:` (it exited 0 while the exporter served a severity no rule selects, silencing the alert); since [#3140](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3140) so does a tree whose tenant side or subtree `_defaults.yaml` writes a non-finite number such as `"NaN"` or `"-Inf"` (it exited 0 while the exporter served it, so the alert never or always fired); since [#3209](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3209) a `_custom_alerts` entry whose `threshold:` the exporter rejects (`abc`, `NaN`, `70:high`, …; the entry serves no series) is the same finding, Field `_custom_alerts[<index>]`, the message starting with `value_unparsed_dropped` and naming the entry, the value and the exporter's reason (it exited 0, only served-values marking it `rejected`). Fix: rewrite the value or the window as the message says |
| `custom_alert_duplicate_series` | error | an entry of the tenant's `_custom_alerts` has the shape and severity of an earlier entry of the list, whatever its name or mode (two `slo_burn_rate` alerts of one shape too: they share one `user_slo_objective{tenant, recipe_id}`). With the same name too, the whole `/metrics` answered 500 ([#2031](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2031)); with another name or mode, the shape's shared rule failed to evaluate and **no tenant** got an alert of that shape and severity ([#3112](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3112)). The exporter keeps the first entry, drops this one and counts it on `da_custom_alert_parse_errors`. The verdict is the exporter's own load of the tree: the tenant-api preflight's stricter "one name" rule is not reported here. Field is `_custom_alerts[i]`. ⚠️ **Behaviour change**: such a tree now exits 1 instead of 0. Fix: delete one of the two entries |
| `custom_alert_over_cap` | error | a tenant's own `_custom_alerts` (its tenant file, or its entry under a root platform file's `tenants:`) asks the exporter to serve more rows than the per-tenant cap of 20 ([#3042](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3042)). Counted as the exporter serves them, before the per-tenant metric limit truncates them: a switched-off recipe serves none and is not counted, an `slo_burn_rate` counts 2, an entry dropped as a duplicate is not counted (tenant-api's write check refuses a duplicate outright); a list a profile fills in is a shape the tenants using it share and is not counted. `validate-config`'s `custom_alert_cap` judges the same. Field is `_custom_alerts`. Fix: remove recipes, or move ones several tenants share into a profile |
| `metrics_not_gatherable` | error | the exporter's `/metrics` cannot be gathered for this tree: two keys give one series (e.g. `X: "70:critical"` beside `X_critical`, a regex label `q=~` beside an exact label `q_re=`), so the whole scrape answers 500 for every tenant ([#2031](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2031)). It Gathers the production collectors at every schedule segment of the day; the message names the tenant and keys where it can. It covers the whole tree, whatever `--scope` says; the tenant column is empty. ⚠️ **Behaviour change**: such a tree now exits 1 instead of 0. Fix: keep one of the keys |

**Reserved keys in subtree defaults ([#2388](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2388))**

| Finding kind | Severity | Trigger |
|---|---|---|
| `subtree_default_reserved_key` | warn | a subtree `_defaults.yaml` in a tenant's defaults chain (not the conf.d root's) writes, in its defaults, a reserved key (such as `_state_*`, `_silent_mode`, `_severity_dedup`, `_metadata`, `_profile`) or a key the exporter never serves as a threshold row (`_silent_*`, a `_` key the registry does not list — see [#3045](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3045) — and the like). Subtree defaults do not support these keys; it is a configuration error, reported whatever the value (except null: a null means the level does not write the key, and is not reported) and whether or not the exporter applies it today. Today the exporter applies only a `disable` or a number for these keys and drops any other value (`enable`, a severity name, a mapping that is not the schedule form); the schedule form (a time window or `expires:`) is judged by its `default` alone, exactly as that `default` written alone ([#3295](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3295)) — for `_state_*`, a subtree can switch a filter off but not on. The fix depends on the key, and the message gives the one that applies. A recognised reserved key (a `_state_<filter>` whose filter the root declares included) is split further by the exporter subtree overlay's own verdict (with several subtree levels writing the key, by the value the tenant ends up with): the exporter **uses** the value for this tenant today (e.g. a plain `disable`) — move it: the message names the value the tenant gets today and its file (e.g. `` `_silent_mode: disable` `` from `a/_defaults.yaml`); write that into this tenant's own `tenants:` entry and delete the key from every listed subtree file, or for `_state_<filter>` change `state_filters.<filter>.default_state` in the root `_defaults.yaml` (⚠️ that affects every tenant in the tree); deleting it without moving it can change what is served ("uses" means the exporter takes the subtree's value; whether that differs from what the tenant would get without it is not judged); the exporter **ignores** the value for this tenant today (e.g. `enable`, a severity name, a mapping, or the tenant sets the key itself) — when no other tenant in the whole tree (not only `--scope`) gets its value from one of the listed files, deleting it leaves what the exporter serves unchanged and `/effective` stops showing the ignored value; to have it take effect, write it in each tenant's entry, which changes what is served (`_custom_alerts` gets a message of its own: no reader takes it from a `_defaults.yaml`; write it in each tenant's entry, a root platform file's `tenants:`, or a profile its tenants select); when the tenant sets the key itself, the message says its own value is what is served and names where it is set: the file whose `tenants:` entry holds it (the tenant file, or a root platform file such as `_platform.yaml`), or the tenant's `_profile`; when other tenants do get their value from one of the listed files, the message names them (up to 5, plus a count) and says not to just delete it but to follow their move first; `_state_<filter>` whose filter is not declared — nothing reads it anywhere, a tenant's entry included: declare the filter under the root `state_filters:` (again tree-wide) or delete it from the file; a key that is not recognised (such as `_silent_x`: never served as a threshold row, and not a reserved key; or `_routingProfile` / `_routings`: they only start with a reserved prefix, and no reader looks them up by name) — the exporter does not read it, the message does not call it reserved, delete it from the file; An empty filter name is written `""` in the message (e.g. `state_filters."".default_state`). When `defaults:` has no value or is absent, the merge reads the whole document and such top-level keys are reported too, `_custom_alerts` included: at the top level or under `defaults:`, the exporter does not serve it and the custom-alert compiler builds no rule for it (see [ADR-024 §3](adr/024-version-aware-threshold-via-dimensional-label.en.md)). Routing keys (`_routing` and `_routing_*`, by the route generator's own test) are not reported here; the route generator judges them (`routing_in_unread_location` and the others; da-guard only lists them as undetermined evidence). A key that merely starts with `_routing`, such as `_routingProfile`, is not a routing key: it is reported, as a key that is not recognised (delete it). The verdict reads the defaults chain of the exporter's own load of the tree, for tenants under `--scope` only; the root `_defaults.yaml` is out of scope. Tenant is the tenant; Field is the key; the message names the subtree files that write it. A tenant entry setting the same value as such a subtree key is not reported as `redundant_override`: the subtree's value is to be moved out of the subtree file (and from the next minor release is not applied), so the tenant's own key is the one to keep. This finding does not change what the exporter does. ⚠️ A warning for now (only `--warn-as-error` turns it into exit 1); **from the next minor release the exporter stops applying these keys from subtree defaults (a `disable` that works today stops working too) and this finding becomes an error** — move them to the places above first |

**`served-values`**

| Flag | Default | Description |
|---|---|---|
| `--config-dir <path>` | (required) | conf.d/ root |
| `--at <RFC3339>` | now | Instant to resolve at (schedule windows, `expires`, silence / maintenance end times all follow it) |
| `--schedules` | off | Also print each tenant's `schedules` (per-segment thresholds, below); resolves the whole tree once more per part of the day in which some schedule changes, and is not computed without it |
| `--disable-effects` | off | Also give each key in `keys` that reads `disable` in some part of the day its `disable` (`noop` / `partial` / `effective`, below); takes as many rounds as one tenant has `disable`s at most, and is not computed without it |
| `--tenant <id>` | (every tenant) | Compute only this tenant; repeatable. The whole tree is still read: the platform files, `parse_failed` / `unreadable` / `skipped` and the refusal of a tenant declared twice are the whole tree's. Each requested tenant's entry is its entry in the whole-tree output; the document also carries `tenants_requested` (sorted, deduplicated) and `tenants_absent` (the ones the tree has none for, e.g. deleted or written where the exporter does not read; `[]` when none), not an exit 2. Whether `/metrics` can be gathered is judged over the requested tenants only: every series carries its tenant label, so another tenant's whole-scrape 500 and its error segments under `--schedules` are not seen here — a caller judging a write names every tenant the written file declares before and after it ([ADR-038](adr/038-unread-keys-judged-by-readers.md) §5) |

The values come from the exporter's own load and resolvers; the subcommand judges nothing itself. JSON output: `schema` (today `da-guard.served-values/v1`; the Python readers refuse any other value, and take a document without the field as coming from a da-guard older than the tool and ask for an upgrade — exit code 2 either way, [ADR-037](adr/037-config-diff-compares-rendered-output.en.md)), `parse_failed` (files the exporter's load skips whole; `[]` when none), `skipped` (files the load reads but serves no tenant from: a name not starting with `_` and no `tenants:`, or an empty one; each a `file` and a `reason`; `[]` when none), `unreadable` (files the load skips because their stat or read fails, e.g. no permission or a dangling symlink, and sub-directories it cannot list, everything under them skipped; each a `file` and a `reason`, which is only ever `stat_error`, `read_error`, `walk_error` (then `file` is the directory) or `special_file` (not a regular file: a FIFO, a socket, a device, or a symlink to one; never opened, #2939); a symlink to a directory is not listed; `[]` when none) and `tenants`, each with `values` (every threshold key `/metrics` emits a row for, canonical name → value, plus the reserved keys as the exporter's resolvers read them at `--at`), `severities` (each threshold key's severity label) , `unserved` (keys of the tenant's merged config absent from `values`, switched-off ones included; also the keys of the root `defaults:` the tenant does not write that serve it no row — a dimensional key and a `_` key the registry does not list ([#3045](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3045)) — with the root's value; also the threshold keys the tenant inherits from a subtree `_defaults.yaml` that the exporter cannot deliver — neither the root `_defaults.yaml` nor `optional_overrides:` declares them; reserved keys, keys the exporter never serves as a threshold row and switched-off keys are not included — the same set as da-guard's `subtree_default_undeliverable`. For those keys the value is that of the deepest level that writes the key in a threshold shape (a deeper level writing another shape, such as a YAML bool, is skipped), in the exporter's normalised form (`1e6` becomes `"1e+06"`), [#1976](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1976)), `dropped` (keys whose row `/metrics` drops because the exporter cannot build its series; the reason for each dropped row) and `state_filters` (every state filter on for the tenant at `--at` → `{"severity": …}`, the severity label of its `user_state_filter` row on `/metrics`; a filter that is off has no entry; `{}` when none. `_state_<filter>` in `values` stays true/false, [ADR-037](adr/037-config-diff-compares-rendered-output.en.md)) and `time_boxes` (the time-boxes the exporter honours on an operational mode: one entry per silenced severity for `_silent_mode`, one for a structured `_state_maintenance` (when the maintenance filter is declared; without it maintenance is not in force), each `{"target"` (`_silent_mode` only)`, "expires", "expired"}` — `expires` as the exporter parsed it (RFC3339, sub-second digits kept), `expired` its verdict at `--at`; a mode with no time-box, or one the exporter ignores, has no entry; `{}` when none. `values` only says whether a mode is in force at `--at`; extending it or making it open-ended shows only here), `custom_alert_rows` (the rows the tenant's `_custom_alerts` asks `/metrics` to serve, counted before the per-tenant metric limit `max_metrics_per_tenant` truncates them: a switched-off recipe is not counted, an `slo_burn_rate` counts 2, an entry dropped as a duplicate is not counted; the `_custom_alerts` of `values` is what survives the truncation. It is what the per-tenant cap on custom alerts is judged on, [#3042](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3042); `0` when none), `custom_alerts` (every element of the tenant's `_custom_alerts`, in declaration order, as the exporter's resolver judges it: `index`, `spec` (the entry as decoded), `status` (`served`; `disabled` — switched off, and served given a value, else it is `rejected`; `rejected`; `duplicate` — dropped because an earlier entry already serves its shape and severity), `reason` (why, for `rejected` and `duplicate`), `recipe_id` and `severities` (for `served` and `disabled` only: the rows it serves, or would serve switched back on). Not truncated by the per-tenant metric limit. It is what the custom-alert compiler compiles, [#3042](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3042); `[]` when none, and when the list does not decode; a `rejected` entry rejected for its `threshold:` — not a finite number the exporter reads, the threshold rows' own verdict — also carries `refused`, `non_finite` or `unparsed`, [#3209](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3209)), `custom_alerts_error` (why the tenant's `_custom_alerts` does not decode, the exporter then serving none of it; absent when it decodes), then `routing` and `keys`. `routing` is the tenant's `_routing` as the exporter module's routing resolver reads it (`receiver_type`, `receiver`, `group_by`, `group_wait`, `group_interval`, `repeat_interval`); the collector does not read `_routing` (the route generator does), so it is not in `values`; a `_routing` the resolver rejects stays in `unserved` as written, with no `routing`. `keys` ([ADR-038](adr/038-unread-keys-judged-by-readers.en.md) decision 5) lists every key of the tenant's merged config and of `unserved` (spelt as there), plus each threshold key the tenant does not write whose root `defaults:` value is non-finite (canonical spelling, `reason` `root_default_non_finite` below): `exporter` (whether the exporter reads it — for a threshold key and `_custom_alerts`, whether it has a `/metrics` row, kept or dropped; always false for a `_` key the registry does not list, which the exporter reads in no layer ([#3045](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3045)); any other registered `_` key as the registry declares, whether or not the resolver accepts its value, except that a `_state_<filter>` whose filter the root does not declare is false), `reason` (why not, only ever `disabled` (its value at `--at` switches it off; whether that changed what is served is judged only with `--disable-effects`), `no_row` (the resolver produced no row for this threshold key or `_custom_alerts` — a misspelt key and one the platform does not declare alike), `value_non_finite` / `value_unparsed` (the resolver met the key's value at `--at` and refused it — NaN or ±Inf, or a value it cannot read — so no row is served: not a switch-off, the alert the value was written for can never fire; [#3140](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3140)), `root_default_non_finite` (a key the tenant does not write whose root `_defaults.yaml` `defaults:` value is a non-finite number, so there is no default row; `refused` is `non_finite`), `undeliverable` (a subtree key the exporter cannot deliver, as above), `shadowed` (a retired spelling the same tenant also writes canonically), `not_exporter_key` (a registered `_` key the exporter does not read), `state_filter_undeclared`, `unregistered` (a `_` key the registry does not list: the exporter reads it in no layer, even when the root `defaults:` declares it, and a `disable` on it switches nothing off) or `retired` (a retired key; `hint` names what replaced it)), `registered` (for a `_` key only: whether the registry lists it or a prefix of it) and `declared` (the registry's other readers → the layers they read it in; join with `da-guard effective`'s `key_sources` for the actual layer). Facts, not a verdict: the route generator judges its own keys. `{}` when none. With `--disable-effects`, every key that reads `disable` in some part of the day (registered `_` keys aside: their readers judge them) also has `disable`: the tree is resolved again without that `disable` (only in the parts of the day it reads `disable`), through the same collectors, at each of the tenant's own schedule cuts — `noop` when its `user_threshold` rows do not change at all, `partial` when they do but a key of the same family (`<key>_critical`, `<key>{…}`) still has a row in some part of the day (`served_family` names them), `effective` otherwise; a part of the day in which `/metrics` cannot be gathered either way (HTTP 500) is no evidence, and the other parts decide. It takes as many rounds as one tenant has `disable`s at most, whatever the number of tenants (each round holds only that round's tenants); the `disable` is taken out of the merged config, so where a lower layer writes `disable` too, it is compared with the platform default. Which rows are kept is decided by `Gather` over a private registry holding the same collectors the exporter's `/metrics` serves. A threshold key also has `refused` ([#3140](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3140)): `non_finite` (NaN, ±Inf) or `unparsed` (any other value it cannot read) when the resolver refused its value at `--at`, whether it then serves the platform default (a base key, `exporter` true) or nothing (`reason` `value_non_finite` / `value_unparsed`); written only when the resolver itself recorded the refusal, so a key the platform does not declare stays `no_row` whatever it holds. With `--schedules`, a segment whose value is null also carries `reason`: `disabled` (a `disable` written for that part of the day), `value_non_finite` or `value_unparsed` (the value there was refused); absent when there is nothing to serve. A reader tells a switch-off from a refused value (an alert that can no longer fire) from these, without re-reading the value. Exit codes: 0 ok; 2 caller error, the exporter rejects the whole tree (e.g. a tenant declared in two files, a tree with no config file at all, a `--config-dir` that does not exist), or `Gather` fails (e.g. two keys produce one series; the exporter's `/metrics` then answers 500 as a whole), with the reason — and the keys where they can be named — on stderr; 3 a file was skipped whole or could not be read — the JSON is still written and names it in `parse_failed` / `unreadable`; when all config files are unreadable (`_defaults.yaml` included, if there is one), or `--config-dir` itself cannot be read (`unreadable` then lists `.`: `walk_error` when it cannot be listed, `stat_error` when it cannot be stat'ed), it is 3 too, with `tenants` empty, not exit 2 ([#2627](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2627)). Any string in the output that is not valid UTF-8 is exit 2 too (JSON cannot carry it), even where the exporter only drops that row and `/metrics` still answers 200. Exit 2 follows `Gather` over the same collectors production `/metrics` serves. Served means served to a UTF-8-negotiated scrape (the Prometheus 3 default); a scrape with legacy or underscores escaping may see labels such as `{a-b}` and `{a.b}` collide in the text output. `dropped` is keyed by the canonical spelling, `unserved` by the spelling as written. The `{…}` part of a dimensional key in `values`, `severities`, `unserved`, `dropped` and `schedules` is always the one the layer that supplied the value wrote, as in `da-guard effective`, so a reader can join the two documents by key ([#2031](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2031)). A top-level `unread_keys` ([#2296](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2296)) lists keys the exporter's load does not read, each with `file`, `key` and `reason`; today there is one `reason`, `root_defaults_unwrapped`: when the root `_defaults.yaml` has no `defaults:` mapping, its top-level keys da-guard's finding of the same name reports (the same predicate as that finding; `/effective` shows them, `/metrics` does not serve them, and `effective`'s `not_served` names them too). No effect on the exit code; `[]` when none.

Per-segment thresholds and the alias table ([#2115](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2115) (c)): with `--schedules`, each tenant also carries `schedules` (without it the field is absent and the rest of the output is unchanged), keyed by canonical name, covering every threshold key `/metrics` serves at some minute of the day (every threshold key of `values` included; `_custom_alerts` is not listed), plus the keys whose `expires:` the exporter honours (base keys only). Each key's `segments` cover UTC `00:00`–`24:00` in order with no gap; each segment has `from`, `to` (`"HH:MM"`, the last ending at `"24:00"`), `value` (written as in `values`; `null` when the key has no row in that segment, e.g. a `disable` window) and `severity` (always written when `value` is a number, an empty one included — `"70:"` gives `""`, as `severities` does; omitted when `value` is `null`); neighbouring segments with the same value and severity are merged. The segments are not inferred from the config as written: da-guard cuts the day at the minutes where any key's schedule may change, fixes the whole tree at each segment's time and resolves it at `--at` with the exporter's own resolver and `user_threshold` builder, then `Gather`s; the segment holding `--at` must match `values` exactly. `expires` (as written) and `expired` (the exporter's verdict at `--at`) appear only when the exporter honours the key's `expires:`; the segments already follow that verdict (when expired, the platform default all day) and do not follow an expiry after `--at`: with `expires` at 10:00 and `--at` at 03:00 the same day, every segment carries the value before expiry, and nothing turns into the platform default at 10:00. When `Gather` fails in a part of the day other than `--at`'s (e.g. two keys produce one series only inside a window; the exporter's `/metrics` then answers 500 as a whole and serves no key), the exit code is unaffected: every key of every tenant carries that part as `{"from","to","error"}`, `error` describing that `Gather` failure, with no `value` / `severity`; when `--at` falls inside that part, the run exits 2 as before. The document also carries `aliases` at the top level: the exporter's own alias table (retired base key → canonical base key), for readers to map a retired spelling onto the keys of `values` / `severities` / `schedules` / `dropped` without keeping a table of their own. There are exactly three conversions, all exact matches (the exporter's `CanonicalKeyFor`): a key in the table becomes its canonical key; a key that is in the table once its trailing `_critical` is removed becomes the canonical key plus `_critical`; a key whose part before `{` is in the table becomes the canonical key followed by the original `{…}` label part. A key that merely starts with a retired key (followed by other characters) is not converted.

**`effective`** ([#2564](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2564); `da-tools guard effective` forwards to `da-guard effective`)

| Flag | Default | Description |
|---|---|---|
| `--config-dir <path>` | (required) | conf.d/ root |
| `--overlay <rel-path>=<file>` | (none) | Resolve the tree as if `<rel-path>` (relative to `--config-dir`) held `<file>`'s bytes; the tree on disk is not touched ([#2906](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2906)). When `<rel-path>` is a file the scan lists, `<file>`'s bytes are read in its place; when nothing is there, a regular file holding them is taken as present, in its place in the scan's walk order — its parent must exist, be a real directory (not a symlink) and be listable by the scan. Each of these is exit 2 with nothing on stdout: a path that leaves the tree, is absolute or holds `..`; a name the scan does not read (hidden, or not `.yaml` / `.yml`); something at the path that is not a file the scan lists (a directory, a symlink to one, a special or unreadable file); a create whose parent does not qualify; a `<file>` that cannot be read; a value not of the form `<rel-path>=<file>`; a second `--overlay` (one per run). Bytes the exporter rejects are listed in `parse_failed` with exit 3, as a file on disk would be. `describe_tenant.py --what-if` simulates a draft with it |
| `--tenant <id>` | (every tenant) | Resolve only this tenant; repeatable. The tree is still walked whole and every platform file read; a tenant declared twice is still refused. The document also carries `tenants_requested` and `tenants_absent`, as `served-values --tenant` |

Prints, as JSON, the effective config of every tenant of the tree through the same code path as tenant-api's `/effective` (the same `pkg/config` resolver); the subcommand judges nothing itself. Python readers call it through `load_effective()` in `scripts/tools/_lib_tenant_values.py`. JSON output: `schema` (currently `da-guard.effective/v1`; a reader refuses any other value), `parse_failed` (files the exporter's load skips whole; `[]` when none), `unreadable` (files the exporter's load cannot stat or read and sub-directories it cannot list; the same list and shape as `served-values`' `unreadable`, `[]` when none, [#2588](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2588)), `skipped` (files whose name does not start with `_` that declare no tenant, e.g. a flat file with no `tenants:`; on exit 0 it matches `served-values`' `skipped` (tested); when a file does not decode (exit 3) it can be empty where `served-values` still lists files, so it is not a complete list there; no effect on the exit code, `[]` when none, [#2115](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2115)) and `tenants`. Each tenant carries the fields of the `/effective` response (`effective_config`, `merged_hash`, `source_file`, `source_hash`, `defaults_chain`, `platform_overlay`, `profile_overlay`) plus two: `profile` (the profile the tenant is bound to; `null` with no `_profile`, or a name the root platform files do not define) and `key_sources` (for every top-level key of `effective_config`, where it came from: `layer` is `defaults` / `platform` / `profile` / `tenant`, `file` the file that wrote the value, and the `defaults` layer adds `level`, the position in `defaults_chain`, 0 being the root). For a key whose mapping value is merged leaf by leaf across layers, the source is the highest layer that wrote any part of it; lower layers may have supplied other leaves. Every tenant also always carries `not_served` and `chain_parse_failed` ([#2296](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2296); the `/effective` response carries them only when non-empty). `effective_config` is the verbatim view of what was written and keeps every value as written; a value `/metrics` does not serve from these sources is named key by key in `not_served` — the defaults chain, a root `_defaults.yaml` without its `defaults:` wrapper, and a value in a platform `tenants:` entry or the tenant file that does not parse. ⚠️ A profile-layer value the exporter discards (e.g. `pg_connections: abc` in `_profiles.yaml`) and an expired (`expires:`) override are **not** named today, so an empty `not_served` does not mean `/metrics` serves what `effective_config` shows (keyed as `effective_config` spells the key; each value is `reason` and `file`, the file of the value `effective_config` shows). `reason` is a closed set, always taken from the exporter's own verdict while loading and resolving — never inferred by comparing the two outputs: `parse_failed` (the exporter drops the file whole), `root_defaults_unwrapped` (the root `_defaults.yaml` has no `defaults:` wrapper, so that top-level key is not read), `value_rejected` (a subtree `_defaults.yaml` value is not threshold-shaped; the exporter uses a shallower level's value or serves none), `value_unparsed` (the exporter cannot parse the tenant side's value and serves the platform default instead; not marked when the value shown is that root default), `value_unparsed_dropped` (the value of a `_critical`, dimensional or `optional_overrides`-declared key does not parse, so that row is not served), `window_invalid` (a schedule override's `window:` is not accepted: not a UTC `HH:MM-HH:MM`, an hour or minute out of range, missing, or starting where it ends, so that window never applies, [#2065](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2065); a value that also does not parse is named `value_unparsed` / `value_unparsed_dropped` instead), `undeliverable` (inherited from a subtree, not declared at the root, [#1976](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1976)), `root_null_undeclared` (the root writes it as null, [#2518](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2518)), `spelling_duplicate` (the winning layer's mapping also writes the threshold under another spelling; the key is the other spelling as written and is not in `effective_config`, [#2031](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2031)), `unregistered` (a `_` key the registry does not list, which `/metrics` serves in no layer, [#3045](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3045)). A dimensional key is one threshold whatever its label order, quoting or spacing: the exporter rewrites every layer's keys to the canonical spelling before the merge, while `effective_config`, `key_sources`, `not_served` and both overlays' keys spell each key as the layer that supplied its value wrote it ([#2031](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2031)). The unparsed verdicts do not depend on the time of day: a value that fails to parse in any part of the day is marked. A #1231 retired spelling is not `not_served` (`/metrics` serves it under the canonical name; map the two with `served-values`' `aliases`). `chain_parse_failed` lists the files on this tenant's `defaults_chain` whose syntax is broken, which the exporter does not read at all: the resolve reads such a file as empty (`merged_hash` is computed over the chain without it) and the tenant is still written — the file no longer stops the whole resolve; it is in `parse_failed` too (exit 3). A YAML `.inf` / `.nan` is written as the string `"Infinity"` / `"NaN"`, as `/effective` does. Exit codes: 0 ok; 2 caller error, the resolver rejects the whole tree (e.g. a tenant declared in two files, or no `.yaml` file in the tree), or a string in the output is not valid UTF-8, with the reason on stderr; 3 a file does not decode or cannot be read (a `--config-dir` that cannot be read too, listed as `.` with `walk_error` (cannot be listed) or `stat_error` (cannot be stat'ed), [#2627](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2627)) — the JSON is still written and names it in `parse_failed` / `unreadable` (a file the exporter reads but the resolve's merge cannot decode — a tenant file, or a root `_defaults.yaml` with a bad tag on a top-level key the exporter skips — still stops the resolve: `tenants` is empty, `parse_failed` lists that file only and `unreadable` is still filled). The tenants of an unreadable tenant file are absent from `tenants`; the values of an unreadable `_defaults.yaml` are absent from every tenant's `effective_config`. `parse_failed` is the same verdict as `served-values`' (the exporter's load): for example, when the exporter drops a root `_defaults.yaml` whole for a value of the wrong type, `/effective` still reads its values, while this subcommand lists it in `parse_failed` and exits 3, and the keys that file supplies are marked `parse_failed` in `not_served`.

**Examples**

```bash
# Validate the whole conf.d/ tree against a required-fields list
da-tools guard defaults-impact --config-dir conf.d/ --required-fields cpu,memory

# CI hook: limit to the changed _defaults.yaml's directory, relative to --config-dir (ceiling read from the root _defaults.yaml)
da-tools guard defaults-impact --config-dir conf.d/ \
    --scope db/

# JSON output for downstream PR comment poster
da-tools guard defaults-impact --config-dir conf.d/ \
    --format json --output guard-report.json

# What /metrics serves per tenant, at a given instant
da-tools guard served-values --config-dir conf.d/ --at 2026-07-01T03:00:00Z

# Each tenant's effective config on /effective, with profile binding and per-key sources
da-tools guard effective --config-dir conf.d/
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

If not found, prints install hints (download from a `tools/v*` release / `cd components/threshold-exporter/app && CGO_ENABLED=0 go build -o /usr/local/bin/da-batchpr ./cmd/da-batchpr`).

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

If none resolves, prints install hints (download from `tools/v*` release / `cd components/threshold-exporter/app && CGO_ENABLED=0 go build -o /usr/local/bin/da-parser ./cmd/da-parser`).

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

Print a tenant's effective config + `merged_hash` (v2.8.0). Supports the verification checklist in `docs/scenarios/incremental-migration-playbook.md` §Emergency Rollback Procedures item 6: after a rollback wave, every tenant's `merged_hash` must return to the pre-Base-PR snapshot. Reuses `describe_tenant.py`'s `ConfDScanner`, whose inheritance chain, `source_hash` and `merged_hash` are all read from `da-guard effective` (the values tenant-api `/effective` and the exporter have, never computed in Python, [#1549](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1549), [#2906](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2906)), so da-guard is required (`$DA_GUARD_BINARY` or `da-guard` on PATH; the da-tools image ships it). This tool is a thin CLI ergonomics layer (terse output + exit code). When a directory holds more than one defaults carrier, the one not selected, which no plane reads, is named on stderr in one `WARN: <file> is ignored on every plane (#1674)` line, the same line as `describe_tenant.py`'s; the exit code is unchanged ([#3230](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3230)).

When the conf.d holds a path da-guard validation names, this tool refuses with its input-error code `1` before it reads any value, each path named (the [shared refusal](#confd-refusal), [#3206](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3206); `2` still means only a verification mismatch).

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
| 1 | Missing tenant_id, conf-d not found, `--all` combined with `--expect-merged-hash`, or da-guard missing, failing or refusing the whole tree (reason on stderr), or no `merged_hash` to compare: the exporter rejecting the whole tree because another tenant is declared twice (a file in the tree the exporter cannot decode or read is the [shared refusal](#confd-refusal), also 1; entry `"error": "merged_hash_unavailable"`, reason in `detail`, [#1549](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1549), [#2906](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2906)). An uncaught exception (a `Traceback` plus one `ERROR: unexpected internal error` line, [#3090](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3090)) is also 1. Argument errors argparse rejects (unknown flag, extra argument) are 2. |
| 2 | Tenant not found, `--expect-merged-hash` mismatch, OR **duplicate declaration** (the same tenant in two or more files) (this is the incremental migration playbook checklist item 6 stop-signal). `--all`: any tenant is declared more than once |

**Duplicate declaration** ([#2093](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2093)): when one tenant is declared by several files, the tool computes no hash — the scanner keeps just one of them, chosen by filename order, so hashing it would let item 6 pass falsely whenever the stray file sorts first. Single-tenant mode (with or without `--expect-merged-hash`) exits 2 with JSON `{"tenant_id": ..., "error": "duplicate", "files": [...], "detail": ...}` (`files` sorted, conf.d-relative paths); the human output lists each file as `declared in: <file>`. `--all` reports that tenant as an error entry of the same shape (no `merged_hash`), still lists every other tenant, and exits 2; the human `# total:` line counts verified, duplicate-declared (not verified) and without-`merged_hash` (not verified) tenants separately. ⚠️ da-guard refuses a tree with a duplicate declaration as a whole, so the other tenants carry no `merged_hash` either (listed as `merged_hash_unavailable` entries, not counted as verified). Fix: delete the extra declaration so the tenant lives in exactly one file, then re-run (`validate-config`'s `tenant_uniqueness` reports the same state).

---

#### threshold-recommend

Threshold recommendation engine — recommends optimal thresholds based on historical Prometheus P50/P95/P99 percentiles.

When the conf.d holds a path da-guard validation names, this tool refuses with `2` before it reads or writes anything, each path named (the [shared refusal](#confd-refusal), [#3206](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3206)).

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
| `--min-samples` | Minimum sample count: both MEDIUM and HIGH need at least this many samples (see Confidence Levels below). May not be below `100`, else exit code `2` ([#2941](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2941)) | `100` |
| `--dry-run` | Show PromQL queries without executing | - |
| `--json` | JSON output | - |
| `--markdown` | Markdown table output | - |
| `--export-patch` | Output an applyable conf.d override fragment (#720 STAGE-1); only keys with \|delta\|≥5%, HIGH/MEDIUM confidence and a mapping | - |
| `--generate-observed-map` | Regenerate the observed-map from rule-packs (#719); does not need `--config-dir` | - |

> **#720 STAGE-1 (`--export-patch`)**: emits a `tenants:`-rooted conf.d override (only keys with an actionable recommendation; within-margin / skipped keys are listed as comments). The operator reviews it, merges it into the matching `conf.d/<tenant>.yaml`, and opens a PR → the existing `backtest.yaml` CI posts the old-vs-new firing-count risk report (the STAGE-1 value basis). The tool does **not** edit conf.d in place (the heavier in-place ruamel round-trip is deferred — #721).
>
> **#2116 where values come from**: the tenants, their current values and which file each key belongs to come from `da-guard effective` (the same resolution as tenant-api's `/effective`, covering `_defaults.yaml`, platform-file `tenants:`, profiles and tenants in sub-directories). Keys the tenant's own file writes get the usual recommendation; keys inherited from a defaults / platform / profile file are listed separately as "Inherited, reference only", with their current value and source file only — not queried, no recommended value. `--export-patch` names the source file of each current value (`# was 60 (from tenant-a.yaml)`); an inherited key appears only as a comment with its current value and the file that owns it — writing it into the tenant's file would pin the tenant to that value. A tenant name or key that is not a plain name (outside `[A-Za-z0-9][A-Za-z0-9._-]*`, or `null`, which the exporter reads as no key; ids such as `010`, `true` or `2024`, which the exporter reads as written, keep their value line) gets no value line: its recommended and current values appear as a `(not written)` comment, and the tenant's file has to be edited by hand. A tenant file using a retired spelling (e.g. `mysql_cpu`) is looked up in the observed-map through the exporter's alias table, and the output keeps the spelling the file uses. Needs da-guard (shipped in the da-tools image; or set `$DA_GUARD_BINARY`); on failure the exit code is 2 and da-guard's stderr is passed on. The alias table comes from `da-guard served-values`, so on a tree served-values refuses (e.g. the root `defaults:` and a tenant both writing `X_critical`) this tool and `threshold-govern` exit 2. A `--config-dir` with no config file at all (an empty directory) exits 2 as well (da-guard refuses it); a scheduled `threshold-govern` Job fails on it.
>
> **#719 data source**: recommendations come from the observed recording rule each threshold key is actually compared against in its rule-pack alert (via `scripts/tools/ops/metric_observed_map.yaml`), NOT the configured `user_threshold`. Keys that are unmapped / version-aware / pending manual resolution are fail-loud skipped with a reason. The map records facts — the series each key's alert compares and the direction it compares in (lower-bound keys too, since [#3017](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3017)); whether to recommend is decided by `recommendation_mode`. A key compared with `<` must be classified in `_observed_map_lib.py` (`NOT_APPLICABLE_KEYS` or `LOWER_BOUND_RESOLVED`), or the drift-guard fails. The observed-map is produced by `--generate-observed-map` and guarded by a CI drift-check; a key an alert compares but the map lacks (a coverage gap) fails that check in CI. When the map is missing, unreadable or has no `keys`, `threshold-recommend` and `threshold-govern` exit 2 naming the file ([#3017](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3017); a missing or key-less map used to skip every key silently at exit 0, a broken YAML one gave a traceback). `--generate-observed-map` rebuilds a missing or key-less map; it never overwrites a map whose YAML is broken — fix or delete it first. Regeneration is **merge-preserve** (#916): a still-valid human-resolved `observed_series` survives rule-pack edits (an invalidated pick falls back to needs_review; a removed key is dropped), and the summary reports preserved/demoted/dropped counts with details on stderr WARN.
>
> **How an upper-bound (`>`) recommendation is computed ([#2964](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2964))**: the direction is judged on the unrounded P95 (the P50/P95/P99 the report shows are rounded to 2 dp).
> - **P95 ≤ 0**: no recommendation (`recommended` is empty, nothing goes into `--export-patch`, `threshold-govern` opens no PR); the reason reads `P95 is non-positive (max in window X) — no recommendation`. A P95 of 0 is not necessarily idle — the current alert may have fired — so check the window max.
> - **P95 below current (tighten)**: the recommendation is `max(P95, half of current rounded up, the highest in-window point at or below current rounded up)`, capped at current. One round tightens at most 50%, and no point the current value did not fire on starts firing (one spike above current does not hold the tighten up), so backtest rates it LOW. When this floor holds the value, the reason starts with `tighten floored at …` and names which part held it. Rounding follows how current is written: a whole number for a whole-number current (including scientific notation such as `1e3`), otherwise current's own decimal places, at least 2 (`0.8` → 2, `0.001` and `1e-3` → 3); a current already at the smallest step of its precision (e.g. `0.001`) cannot be tightened and comes out as no change. A large tighten takes several rounds (a metric that is 94% zeros goes 30 → 15 → 8 → 4 → 2).
> - **P95 above current (relax)**: P99 instead, rounded **up** at that precision (relaxing to P95 still leaves ~5% of points over; rounding up keeps the recommendation from landing below current). The 5% change margin then measures the gap between P99 and current.
> - **Current value 0** ([#2997](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2997)): a 0 threshold means "alert on any positive value" and is often deliberate (e.g. `kafka_under_replicated_partitions`); a change relative to 0 has no defined percentage, so no value is recommended. With activity (P95 > 0) the key is `force_manual`, `recommended` and `delta_pct` are both empty, and the reason is `current threshold is 0 (fires on any positive value) — manual review; observed P99 X (rounded up to current's precision: Z), max Y` (Z is P99 rounded up to the precision above and can exceed the window max Y; the parenthesis is left out when it equals X); `--export-patch` lists it as a `(manual)` comment and `threshold-govern` lists it in the manual-review section without opening a PR. All-zero data falls under P95 ≤ 0 above.
> - A change under 5% is still no change.
>
> ⚠️ With `threshold-govern --min-delta-pct` at 50 or more, a -50% recommendation held by the "half" floor is filtered out by govern (no PR), while `--export-patch` still lists it.
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
| HIGH | ≥ 1000 and ≥ `--min-samples` |
| MEDIUM | ≥ `--min-samples` (default and floor: 100) |
| LOW | otherwise |

Only finite samples count; NaN / Inf are dropped ([#2956](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2956)). Upper-bound keys also have a coverage floor: when their samples fall in fewer than 24 distinct UTC hours the key is LOW whatever its count (the reason says `data in N distinct hour(s) < 24`), and `threshold-govern` opens no PR for it ([#2941](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2941)). It counts hours covered, not the first-to-last span, so one stray early sample or two short stretches far apart do not pass for a day. Lower-bound keys are gated by "at least 5 full UTC days" instead. A LOW recommendation is still listed in the report, but it is not counted in `recommended_changes` and is never a `--export-patch` value line: it is written as a whole-line comment `# [tenant] (low confidence) key: recommended X, was Y, -Z% — <reason>`, to be judged and copied by hand if wanted ([#2969](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2969)). The text report's heading reads `0/1 changes recommended, 1 low-confidence`, and `--json` gains a `low_confidence_changes` field in the summary and in each tenant. When a patch holds only LOW recommendations, the first line after its fixed header says how many low-confidence recommendations are listed below and that there is no value line to apply, instead of `no actionable recommendations`. This is the same confidence floor `threshold-govern` applies.

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

**A query not answered** ([#3133](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3133)): when a key's Prometheus query is not answered (unreachable, an HTTP error, an `error` status, an answer that is not a readable matrix), that key carries `query_error` (the reason in `reason`) and no recommendation, and the other keys are recommended as usual; the report is still printed, `--json` gains top-level `status: "partial"` and `reason: "prometheus_query_error"`, `summary.query_errors` counts the keys not measured, stderr carries one `ERROR: Prometheus query failed for N key(s)` line, and the exit code is `2` (it used to be only `reason: query error: …` at exit code `0`).

#### threshold-govern

Threshold governance loop (Renovate-for-thresholds, #656) — turns `threshold-recommend`'s output into an **active loop**: gate on rot magnitude, then open one approvable per-tenant proposed-PR via the tenant-api (single-writer, ADR-011/023) instead of a nudge. **Dry-run by default** — pass `--apply` to actually open PRs.

When the conf.d holds a path da-guard validation names, this tool refuses with `2` before it reads or writes anything, each path named (the [shared refusal](#confd-refusal), [#3206](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3206)).

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
| `--min-samples` | Minimum sample count, as for `threshold-recommend`. May not be below `100`, else exit code `2` | `100` |
| `--min-delta-pct` | Governance gate: only open a PR when \|delta%\| >= this | `25` |
| `--max-prs` | Max PRs opened per run (anti-flood / alert-fatigue budget). Must be at least 1; below that the exit code is `2` ([#2929](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2929)). When the cap is below the number of actionable tenants, the dry-run Summary says how many PRs at most would be opened ("at most": a dry run does not ask tenant-api, so it cannot foresee a PR that is already open) | `10` |
| `--apply` | Actually open PRs via tenant-api (default: dry-run, no writes) | - |
| `--tenant-api-url` | tenant-api base URL (required with `--apply`) | `$TENANT_API_URL` |
| `--identity-email` | X-Forwarded-Email (PR git author / governance identity) | `threshold-governance@platform.local` |
| `--identity-groups` | X-Forwarded-Groups (an RBAC group with write perm; required for direct `--apply`) | `$DA_GOVERN_GROUPS` |
| `--auth-token` | Bearer token (oauth2-proxy mode; or `$DA_GOVERN_TOKEN`) | - |
| `--auth-token-file` | Path to a Bearer token file (K8s audience-bound projected SA token, read at call time; or `$DA_GOVERN_TOKEN_FILE`) | - |
| `--throttle-seconds` | Seconds to pause between opened PRs (>= 0, fractions allowed; `0` = no pause; a negative value, `nan` or `inf` exits `2` before any PR is opened) | `2` |
| `--timeout` | HTTP timeout in seconds for the tenant-api calls (GET / PUT), used only with `--apply`; Prometheus queries are not affected. Must be above 0, fractions allowed; `0`, negatives, `nan`, `inf` and values above `9223372036` (`threading.TIMEOUT_MAX`) are refused with exit code `2` ([#2942](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2942)) | `30` |
| `--json` | JSON output | - |

> **Gate**: only recommendations with \|delta\| >= `--min-delta-pct` AND confidence ∈ {HIGH, MEDIUM} (never act on a thin sample — anti-broken-window), and **`force_manual` is excluded** (a floor recommendation that would relax / is out-of-domain / rests on a thin sample, an upper-bound key whose current value is 0, and a key whose recommendation raised never auto-open a PR). **Dedup**: a refused PUT is classified by its code's `remedy` in the tenant-api error contract (`tenant-api-errors.json`): `existing` (a PR for that tenant is already open) → treated as already-in-flight and skipped, so re-runs don't spam duplicates; `refresh_retry` (`CONFLICT`, the base moved) → GET, merge and verify again and PUT once more, a second refusal is an error; any other remedy, a code the contract does not list, or a remedy the tool does not handle is an error. With `--apply`, a missing contract file is exit 2 before any write. **Channel isolation**: the PUT carries `X-DA-Write-Source: threshold-governance` → the PR gets its own label / title / source instead of masquerading as a tenant-manager UI edit, and never touches the alert plane. Read-modify-write: the tenant YAML from the GET is laid out in a private copy of `--config-dir` (at the tenant file's path) and `da-guard write --dry-run` sets the recommended keys to the recommended values (written as double-quoted strings); the PUT carries the text it answers ([#3206](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3206)). Each key must already be a scalar direct child of the tenant block (no key is added); comments and other values stay as they were, but the layout is da-guard's (2-space indentation, one space before a trailing comment, blank lines may go), ⚠️ so the first PR for a file written this way also carries those layout changes (it used to be a surgical line replacement touching only the value line). A tenant da-guard refuses (an original it cannot read or validation names, a path no reader reads, ...) is an error, and no PUT is sent. Recommendation logic / data source are reused verbatim from `threshold-recommend` (Day-N observed recording rule, #719).
>
> **#2116 only the tenant's own keys are governed**: a PR changes only the keys the tenant's file itself writes, written back in the spelling the file uses (a retired spelling stays). Keys inherited from a defaults / platform / profile file never enter the plan and are not queried; they are listed as INFO (the `inherited` array and count in the JSON), so they can no longer fail because tenant-api's copy of the tenant file lacks them and trip the consecutive-error breaker. When a tenant's file is in a sub-directory, tenant-api reads tenant files at the top level of its config dir only, so this tool sends no GET and opens no PR; the outcome is `skipped_nested` (not an error, not counted against `--max-prs`) and the file needs a manual change.
>
> **#916 lower-bound integration**: a `percentile-lower` tighten recommendation (raising the floor) opens a PR through the gate as usual; but `force_manual` (relax / domain / sample / estimator divergence) is listed in a separate **manual-review section** (with its `guardrail_reason` + miss delta) and `not-applicable` is an **INFO** line (governance complete) — both counted in the summary, with `force_manual` / `not_applicable` arrays and counts in the JSON output. The governance table also tags lower-bound changes with `+X% miss`. An upper-bound key whose current value is 0 (#2997) is listed in the manual-review section too, with its reason and no delta (`delta_pct` is `null` in the JSON); `--apply` attempts no write for it, so compared with before #2997 (0 → P95, PR opened) the exit code differs in two cases: from `1` to `0` when such keys were the only writable ones and the writes fail (nothing is attempted); from `0` to `1` when its write used to be the run's only success (a 409 already-pending included) and every other tenant's write fails (no success masks the failure any more). It no longer takes a `--max-prs` slot either; that slot goes to another tenant.
>
> **`--min-delta-pct` space asymmetry**: this knob compares a **value-space** delta for upper-bound keys but a **miss-rate-space** delta for lower-bound keys — the same number is far more sensitive in one than the other (25% miss on a 0.95 floor ≈ 1.3% value). The `5.0` floor on `--min-delta-pct` is the upper-bound 5% value margin (the lower-bound no-change margin is 10% miss), so it is a necessary-not-sufficient bound for lower-bound keys. Pair lower-bound keys with `--lookback 14d` (and a weekly govern cadence for lower-bound coverage).
>
> **Exit code** (#656, [#3054](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3054)): with `--apply`, the exit code is `1` when failures (`error` plus circuit-breaker skips) make up ≥ 90% of the tenants whose write was sent or was going to be sent, so the scheduled Job is marked Failed; otherwise `0`. A dry run, or a run with no actionable tenant, is always `0` (a threshold not measured aside: see the next paragraph). An already-pending PR (409) and `no_changes` count as sent and not as failures; a `--max-prs` skip (`skipped`) and `skipped_nested` sent nothing and are not counted. `error` is not only a write-plane fault (PUT failed, HTTP 5xx, API not in PR write-mode): a failed read (`GET failed`, `GET returned no raw_yaml`) and the local checks that refuse to write (`keys not found in live config`, `refused to PUT (verify failed)` — the latter is [ADR-036](adr/036-single-parser-effective-config.en.md) §2's pre-write confirmation: the text before and after the edit are both read by `da-guard parse-tree`, and the edited one must be the original with exactly those keys set to the recommended strings, every other key, the structure and each scalar's source text / tag / value unchanged; an original parse-tree cannot read, or a tenant-file path no reader reads, is refused too) are also recorded as `error` and count as failures too; so does a da-guard that runs but fails on a tenant (a timeout, say: `refused to PUT (verify could not run)`), which records only that tenant as `error`, counted in the ratio above. No usable da-guard at all (`$DA_GUARD_BINARY`, then `$PATH`) belongs to no tenant: with `--apply` the run stops with exit `2`, outside the 90% ratio. After 5 consecutive errors the circuit breaker skips the remaining tenants, shown in `--json` as `outcomes[].status` `skipped_breaker` (previously `skipped`, the same as a `--max-prs` skip); `summary` counts `skipped` (cap reached) and `skipped_breaker` separately, and the text Summary lists `skipped (--max-prs)` and `skipped (circuit breaker)` apart. Since #3054 an opened PR no longer makes the run count as healthy by itself: a run that opened a PR but whose other writes failed (≥ 90%) now exits `1` instead of `0`. The exit code depends on how many tenants the run has (with few tenants one failure is a larger share), but for the same plan more failures never lower it; for a write-health signal that does not depend on the plan, use tenant-api's `tenant_api_writes_total{result}` and the `TenantApiWriteErrorsHigh` alert.

> **A threshold not measured** ([#3133](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3133)): when the Prometheus query of any threshold was not answered (unreachable, errored or answered in the wrong shape), the report is partial: the text report lists them in a "not measured" section and counts them in the Summary; `--json` adds a `not_measured` array and `summary.not_measured`, with a top-level `status` of `partial` and `reason` `prometheus_query_error`; stderr carries one `ERROR: Prometheus query failed for N threshold(s); the report is partial` line and the exit code is `2` — in a dry run too, and over the `1` above. The thresholds measured are governed as usual, and `--apply` still opens their PRs. These thresholds used to be skipped silently: with Prometheus unreachable the run printed "No thresholds need governance" at exit `0`.

#### test-notification

Multi-channel notification connectivity testing — verify reachability of all configured receivers and report status.

When the conf.d holds a path da-guard validation names, this tool refuses with `2` before it reads or writes anything, each path named (the [shared refusal](#confd-refusal), [#3206](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3206)).

The receivers tested are taken from each tenant's `_routing` as the route generator (`generate-routes`) resolves it — `_routing_defaults` (subdirectory levels included), routing profiles, the platform file's `tenants:` overlay and tenant files in subdirectories all count, matching the per-tenant receivers the generator renders; only per-tenant receivers are tested — the platform-enforced (`_routing_enforced`, NOC) receiver the generator also renders is not ([#2752](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2752)). A tree the route generator refuses while reading it (an unreadable file, a tenant declared in two files, a routing-tree error, an invalid tenant id) has no receiver tested and exits 2. ⚠️ What the generator refuses only when it renders the receivers (for example, two receivers rendered with the same name) is not checked here and is tested as usual. The list tested is the generator's own verdict on each slot (the main receiver, `overrides[i]`, `routes[i]`); this tool does not walk `_routing` itself ([#3076](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3076)). Only receivers the generator renders are sent; a slot it does not render is still listed, never sent, and counts as failed — `INVALID_URL` / `INVALID_CONFIG` when the generator (`build_receiver_config`) refuses the receiver's **own config**, `NOT_RENDERED` with the generator's reason when it skips the slot at the routing level (for example an override with no `alertname` / `metric_group`, `routes[i]` with an unsupported key, and every `overrides` / `routes` entry of a tenant whose main receiver is refused or missing). With `--policy` the verdict is the generator's under the same `--policy`: a slot its `allowed_domains` refuse is `POLICY_REFUSED` ([#3142](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3142)). To check whether the generator accepts the whole config, run `generate-routes --validate`.

**Usage**

```bash
da-tools test-notification --config-dir <PATH> [--tenant <NAME>] [--dry-run] [--json] [--ci] [--timeout <SEC>] [--rate-limit <SEC>] [--policy <FILE>]
```

**Parameters**

| Parameter | Description | Default |
|-----------|-------------|---------|
| `--config-dir` | Path to conf.d/ directory (required) | - |
| `--tenant` | Test only this tenant (omit to test all) | all |
| `--dry-run` | Check receiver config and URL format by the route generator's verdict and the port range 1-65535, do not send (hostname encoding is checked only when a request is sent) | - |
| `--json` | JSON output | - |
| `--ci` | CI mode: exit 1 if any receiver fails | - |
| `--timeout` | Timeout per receiver in seconds, applied to both connecting and waiting for the response; a timeout in either phase is reported as `TIMEOUT` ([#2965](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2965)). Must be above 0, fractions allowed; `0`, negatives, `nan`, `inf` and values above `9223372036` (`threading.TIMEOUT_MAX`) are refused with exit code `2` ([#2957](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2957)) | `10` |
| `--rate-limit` | Seconds to wait between each test (>= 0, fractions allowed; `0` = no wait; a negative value, `nan` or `inf` exits `2`) | `0.5` |
| `--policy <FILE>` | Deployment policy file (the one `generate-routes --policy` takes, read by the same resolver, [#3142](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3142)): a receiver its `allowed_domains` refuse is not sent, is marked `POLICY_REFUSED` and counts as failed; an unusable file or a `min_generator_version` newer than this tool exits `2` (`--json` `reason`: `policy_unusable`). Without it the allowlist is not applied and one `NOTICE` line goes to stderr; the exit code is unchanged, and `policy_applied` in `--json` says which | (not applied) |

**Supported Receiver Types**

`webhook`, `slack`, `teams`, `pagerduty`, `rocketchat`, `email` (SMTP connectivity check)

**Statuses**

Each receiver's result is one of the following (the status column of the text output, `status` in `--json`). Only `OK` and `DRY_RUN` count as passed, `SKIPPED` is counted apart, and everything else counts as failed. Connection-phase errors are classified by exception type, not by matching the error message ([#2995](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2995)).

| Status | Meaning |
|--------|---------|
| `OK` | The receiver returned a successful HTTP response |
| `DRY_RUN` | `--dry-run`: the route generator renders this receiver and the port is 1-65535; nothing was sent. Whether the hostname can be encoded (IDNA) is checked only when a request is sent, so a dry run does not cover it |
| `SKIPPED` | No testable URL (for example `email`) |
| `TIMEOUT` | Connecting or waiting for the response took longer than `--timeout` |
| `CONNECTION_REFUSED` | The peer refused the connection (nothing listening on the port, or a firewall rejecting with RST) |
| `CONNECTION_ERROR` | Any other connection error: DNS resolution failure, connection reset, connection closed after the request without a response, network unreachable. When an HTTPS proxy's CONNECT fails, the detail is prefixed with `via proxy: `: the proxy hop failed |
| `TLS_ERROR` | TLS error: certificate verification failed, https to a plain-http port, handshake failure |
| `AUTH_ERROR` | HTTP `401` / `403` |
| `HTTP_ERROR` | Any other HTTP error status, or a malformed or truncated response (detail starts with `malformed response:`) |
| `INVALID_URL` | The route generator (`build_receiver_config`) refuses the receiver's own config because its URL field (`smarthost` for email) is given but malformed or not a string, the port is not 1-65535, or at send time the hostname cannot be encoded or the URL is refused (no request was sent) |
| `INVALID_CONFIG` | The route generator (`build_receiver_config`) refuses the receiver's own config for another reason (for example a missing required field — a URL field that is missing, empty or null included, an unknown `type`, an optional field of the wrong type; an entry or tenant with no `receiver`, or a `receiver` that is not a mapping, too), matching the generator's verdict |
| `POLICY_REFUSED` | The `--policy` `allowed_domains` refuse this receiver (its host is not listed), so the generator does not render it; no request was sent. The detail is the generator's reason plus the fix (add the host to `allowed_domains` in the `--policy` file, or correct the tenant's receiver URL). When the main receiver is refused, the tenant's child slots are `NOT_RENDERED` ([#3142](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3142)) |
| `NOT_RENDERED` | The route generator skips this slot at the routing level, so Alertmanager never gets it (no request was sent): `overrides[i]` / `routes[i]` itself is unusable (for example no matcher, an unsupported key, not a mapping), or the tenant's main receiver is not rendered, so none of its children is. The detail is the generator's own reason; under a main receiver that is not rendered, a child with a defect of its own is also reported with that reason (`NOT_RENDERED`) until the main receiver is fixed; when `overrides` / `routes` is not a list, the row is `<tenant>-overrides` / `<tenant>-routes` ([#3076](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3076)) |

For `TIMEOUT`, `CONNECTION_REFUSED`, `CONNECTION_ERROR` and `TLS_ERROR` the detail is cut at 80 characters (the `via proxy: ` prefix included); longer error messages are truncated. With `https_proxy` / `http_proxy` set, these statuses may describe the hop to the proxy: a closed proxy port is `CONNECTION_REFUSED`, an unresolvable proxy host is `CONNECTION_ERROR`, and an `HTTP 502` an http receiver gets through the proxy is `HTTP_ERROR`. Only a failed HTTPS CONNECT carries the `via proxy: ` prefix (including a proxy answering `504 Connection Timed Out`, which is then not `TIMEOUT`).

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
| `1` | CI mode: one or more receivers failed (unreachable, an invalid receiver config or URL, a slot the generator does not render, `NOT_RENDERED`, or one the `--policy` refuses, `POLICY_REFUSED`; even under `--dry-run`, which connects to nothing). Without `--ci` the code is always `0`, but `status` in `--json` and `Overall` in the text report still read `fail` / `FAIL` |
| `2` | Caller error: a file under `--config-dir` cannot be read (content not UTF-8 or not valid YAML; the message names the file, #1654), or the route generator refuses the tree while reading it (stderr carries the generator's own refusal; `--json` prints `status: caller_error`, `reason: routing_tree_refused`, #2752); `--policy` unusable or its `min_generator_version` newer than this tool (`reason: policy_unusable`, #3142) |

#### explain-route

Routing merge pipeline debugger — shows the four-layer routing merge expansion per tenant (ADR-007): `_routing_defaults` → `routing_profiles` → tenant `_routing` → `_routing_enforced`.

When the conf.d holds a path da-guard validation names, this tool refuses with `2` before it reads or writes anything, each path named (the [shared refusal](#confd-refusal), [#3206](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3206)).

`overrides` and `routes` are not listed in the final merged result but under "Effective sub-routes" after it: every sub-route and receiver the generator actually renders, in match order (`overrides` → `routes`), plus the entries the generator skipped, each with its reason ([#2245](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2245)). Both lists come from the generator's verdict on each slot, the same source `test-notification` reads; when a tenant's main receiver is missing or refused, the generator renders no sub-route, and each `overrides` / `routes` entry is listed with a "main receiver not rendered" reason of its own ([#3076](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3076); before, nothing was listed when the main receiver was missing or refused). In `--json`, each tenant gains `sub_routes` and `skipped_sub_routes`; `final` is still the raw merged config. A duration Alertmanager does not accept is rendered as the platform default (and `--validate` fails); those lines are listed in a "Replaced by the generator" section (main route and sub-routes alike), under `replaced_values` in `--json`, apart from `skipped_sub_routes` — those entries are still rendered, with another value ([#2490](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2490)). `_routing_enforced` is listed as layer 4 only and is not merged into `final`: in the rendered config it is a **separate** `continue: true` route beside the tenant's, and does not replace the tenant's own receiver or timing ([#2293](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2293)).

**Usage**

```bash
da-tools explain-route --config-dir <PATH> [--tenant <NAME>...] [--show-profile-expansion] [--policy <FILE>] [--json]
da-tools explain-route --config-dir <PATH> --tenant <NAME> --trace [--alertname <NAME>] [--severity <LEVEL>] [--label <KEY=VALUE>...] [--base-config <PATH>] [--policy <FILE>] [--json]
```

**Parameters**

| Parameter | Description | Default |
|-----------|-------------|---------|
| `--config-dir` | Config directory path | (required) |
| `--tenant` | Show only specified tenant(s) (repeatable) | (all) |
| `--show-profile-expansion` | Show all routing profile expansions and references | `false` |
| `--trace` | Trace mode: one alert's routing path as Alertmanager (`amtool`) decides it (requires `--tenant`; needs `amtool` on PATH) | `false` |
| `--alertname` | Alert name to trace (read only with `--trace`) | `GenericAlert` |
| `--severity` | Alert severity to trace (read only with `--trace`) | `warning` |
| `--label` | Extra alert label for the trace, as `KEY=VALUE` (repeatable; read only with `--trace`) | (none) |
| `--base-config` | Base Alertmanager YAML for the trace: `route.routes` is replaced by the generated routes (read only with `--trace`) | built-in base (same as `generate_alertmanager_routes --validate`) |
| `--policy <FILE>` | Deployment policy file (the one `generate-routes --policy` takes, read by the same resolver, [#3142](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3142)): what is listed and traced is what the generator renders under its `allowed_domains`; a refused receiver is listed under "Refused by the --policy allowed_domains" (`policy_refused` in `--json`, each with `source` / `status: POLICY_REFUSED` / `detail` / `remedy`), and a refused main receiver takes the tenant's whole route out. An unusable file or a `min_generator_version` newer than this tool exits `2` (`reason: policy_unusable`). A refused platform-enforced (`_routing_enforced`) receiver is listed there too (`source: _routing_enforced.receiver`). Without it nothing is applied and one `NOTICE` line goes to stderr; the exit code is unchanged, and `policy_applied` on each `--json` entry says which. `--show-profile-expansion` lists profiles, not receivers: it does not apply the allowlist and prints no NOTICE (an unusable file is still `2`) | (not applied) |
| `--json` | Output in JSON format | `false` |

The `--trace` alert labels are built from `--alertname`, `--severity`, `--tenant` and `--label`; an `overrides` `metric_group`, or a `routes` `match` key other than `alertname` / `severity` / `tenant`, can only be supplied through `--label`, otherwise the trace does not reach that sub-route. `--label` splits on the first `=` (the value may contain `=` and may be empty); a missing `=`, a key that is not a valid label name, a key of `alertname` / `severity` / `tenant` (use the matching flag instead), a repeated key, or `--label` without `--trace` are all rejected with exit code `2` ([#2264](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2264)). `--alertname` / `--severity` without `--trace` are rejected with exit code `2` as well, even when the value given is the default ([#2926](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2926)).

Step 4 of `--trace` lists the effective inhibit rules (verbatim from the assembled config, including the `--base-config`'s own rules) and does not evaluate whether the alert would be inhibited — that depends on which alerts are firing at the same time.

`--trace` needs `amtool`; the da-tools image bundles it since [#2294](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2294) (the v2.9.0 image does not). <!-- image-caveat: v2.9.0 -->

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

# Use your own base Alertmanager config as the root (routes without timings inherit the root's)
da-tools explain-route --config-dir conf.d/ --tenant demo-tenant --trace --severity critical --base-config base-alertmanager.yaml
```

#### explain-keys

Explain a tenant's keys ([ADR-038](adr/038-unread-keys-judged-by-readers.en.md) decision 9). For each tenant it lists every key of its config: the layer and file it comes from (`da-guard effective`), which readers read it (the exporter as measured by `da-guard served-values`; the route generator, the custom-alert compiler, Policy-as-Code and annotations as the reserved-key registry declares for that layer; a key a `_policies` rule that applies to the tenant names as `target` (or `when.target`) counts as read by Policy-as-Code: either #1231 spelling matches, a wildcard names no key, and a `_` key must be one the registry lists), the verdict and its severity, what a `disable` does (judged by resolving again without it), the value as written and what `/metrics` serves. The verdict is the shared one every gate uses. The tools report what they can; whether a value is what you meant is yours to check with this output, and to pin in your own CI with `--assert`.

When the conf.d holds a path da-guard validation names, this tool refuses with `2` before it reads or writes anything, each path named (the [shared refusal](#confd-refusal), [#3206](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3206)).

```bash
da-tools explain-keys --config-dir <dir> [--tenant <name>...] [--at <RFC3339>] [--assert <file>] [--json]
```

**Main parameters**

| Parameter | Purpose |
|------|------|
| `--config-dir <dir>` | conf.d root (required) |
| `--tenant <name>` | Only this tenant (repeatable); default every tenant |
| `--at <RFC3339>` | Instant to read schedules and time-boxes at; default now. Fix it for an `--assert` in CI, or scheduled values follow the clock |
| `--assert <file>` | Expected document (same shape as `--json`); any difference is exit 1, each printed on stderr (and in `assert_differences` with `--json`) |
| `--json` | Print one JSON document (schema `da-tools.explain-keys/v1`) on stdout |
| `--da-guard-binary <path>` | Path to da-guard; without it `$DA_GUARD_BINARY`, then `da-guard` on `$PATH` (shipped in the da-tools image) |
| `--timeout <seconds>` | Limit for each da-guard run (default 600) |

**Fields of each key** (`--json`: `tenants.<tenant>[]`): `key`, `layer` / `file` (the source), `read_by`, `verdict` (`read` / `unread` / `undecidable`; a `_` key the registry does not list is read by no reader, so it is `unread` with `reason` `unregistered`), `severity` (the severity once the gates block on it: `error` / `warning` / `notice`), `reason`, `disable` (`effective` / `partial` / `noop`), `served_family`, `value` (as written), `served` (what `/metrics` serves). `findings` is the deduplicated list of problems: one entry for the same problem in a platform file or profile, with the tenants it reaches; a `disable_partial` finding's `served_family` lists only the siblings not written in the tenant's own file, and `served_from` gives, in order, the file each comes from (the same `disable` in a platform file or profile is still one finding: the siblings any tenant it reaches inherits are listed together, and the files are comma-joined when they inherit one from different files; a sibling written in an old spelling is recognised too) ([#3046](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3046)). A `_` key in a profile that no reader reads in the profile layer is judged directly from the registry, not through tenants: one finding on that profile file, its reason the same whether or not a tenant binds the profile; with no tenant binding it, `tenants` is `[]` and the table prints `no tenant`; with `--tenant`, only findings reaching a named tenant are listed, so an unbound one is not ([#3042](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3042)).

**The `--assert` file**: every tenant it lists under `tenants` lists all of its keys (one more or one fewer is a difference); each key is compared on the fields the file gives, so `key` and `verdict` alone pin only the verdict. Tenants it does not list are not compared. A field the document does not have (top level or in a key entry, e.g. a misspelt `verdcit`) or a key listed twice for one tenant is exit 2 — it would otherwise pass without comparing anything.

**Exit codes**

| Code | Meaning |
|------|------|
| 0 | Done; with `--assert`, the result matches |
| 1 | `--assert`: the result differs from the expected document |
| 2 | Caller error (`--config-dir` missing, no da-guard or da-guard failed, unreadable `--assert` file, unknown `--tenant`), or no root platform file was read so the threshold keys are `undecidable` (the result is still printed); with `--json`, stdout is still one JSON document, a caller error with `status: "caller_error"` and a `reason` |

```bash
da-tools explain-keys --config-dir conf.d/ --tenant db-a
da-tools explain-keys --config-dir conf.d/ --tenant db-a --at 2026-07-01T03:00:00Z --json > expected.json
da-tools explain-keys --config-dir conf.d/ --at 2026-07-01T03:00:00Z --assert expected.json
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
| `1` | No suitable partition labels found |
| `2` | Caller error: bad arguments, `--endpoint` unreachable or not a valid URL, a `--prometheus` series / label-values query unreachable, answered with an error, or malformed (it used to count as no labels, exit 1, [#3133](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3133)), or the output path given to `-o/--output` cannot be written (#1641) |

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
