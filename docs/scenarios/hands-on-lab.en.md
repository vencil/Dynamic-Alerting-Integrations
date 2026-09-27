---
title: "Hands-on Lab: From Zero to Production Alerting"
tags: [scenario, hands-on, lab, adoption, tutorial]
audience: [platform-engineer, tenant]
version: v2.9.0
lang: en
---

# Hands-on Lab: From Zero to Production Alerting

> **Language / 語言：** **English (Current)** | [中文](./hands-on-lab.md)

> **v2.9.0** | Estimated time: 30–45 minutes | Prerequisites: Docker installed
>
> Related: [GitOps CI/CD Guide](gitops-ci-integration.en.md) · [Tenant Lifecycle](tenant-lifecycle.en.md) · [CLI Reference](../cli-reference.md)

> 💡 **Want to see the product running in ~1 minute instead of typing CLI commands?** → [try-local](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/try-local/README.md) (recommended first stop: da-portal UI in the browser + a real firing alert, no K8s; `⏱️ <1 min · 🟢 Docker only`). **This lab** focuses on the hands-on **da-tools CLI workflow** (config / routing / blast radius; `⏱️ 30–45 min · 🟡 Medium (CLI)`) — complementary, different depth, not either/or.

## Lab Overview

This lab walks you through the complete Dynamic Alerting journey using 5 realistic tenants. By the end, you will have:

- Bootstrapped a complete monitoring config directory with `da-tools init`
- Configured thresholds for MariaDB, Redis, Kafka, JVM, PostgreSQL, Oracle, DB2, and Kubernetes rule packs
- Understood four-layer routing merge (ADR-007)
- Tested three-state operations (Normal / Silent / Maintenance)
- Generated Alertmanager routes with validation
- Analyzed blast radius of a config change

## Lab Environment

All exercises use `da-tools` via Docker — no Kubernetes cluster required for the config validation and route generation steps.

```bash
# Pull the da-tools image (one-time)
docker pull ghcr.io/vencil/da-tools:latest

# Create a working directory
mkdir -p ~/da-lab && cd ~/da-lab
```

## Exercise 1: Bootstrap with da-tools init

Use the CI/CD Setup Wizard or run `da-tools init` directly.

```bash
docker run --rm -it \
  --user $(id -u):$(id -g) \
  -v $(pwd):/workspace -w /workspace \
  ghcr.io/vencil/da-tools:latest \
  init \
  --ci github \
  --deploy kustomize \
  --tenants prod-mariadb,prod-redis,prod-kafka,staging-pg,prod-oracle \
  --rule-packs mariadb,redis,kafka,jvm,postgresql,oracle,db2,kubernetes \
  --non-interactive
```

Verify the generated structure:

```bash
find . -type f | sort
```

Expected output:

```
./.da-init.yaml
./.github/workflows/dynamic-alerting.yaml
./.pre-commit-config.da.yaml
./conf.d/_defaults.yaml
./conf.d/prod-kafka.yaml
./conf.d/prod-mariadb.yaml
./conf.d/prod-oracle.yaml
./conf.d/prod-redis.yaml
./conf.d/staging-pg.yaml
./kustomize/base/README.md
./kustomize/base/kustomization.yaml
./kustomize/overlays/dev/kustomization.yaml
./kustomize/overlays/prod/kustomization.yaml
```

Open any `conf.d/<tenant name>.yaml`: apart from the header comments, it is `tenants:` with one `<tenant name>:` level under it, and thresholds and `_routing` go under that second level. Exercise 2 keeps this frame.

## Exercise 2: Configure Tenant Thresholds

Replace the content of each tenant file with the following (keep or drop the header comments). Every snippet keeps the two outer levels init generated, `tenants:` → `<tenant name>:`. Without them validation finds no tenants and still reports a pass (see the note in Exercise 3).

**conf.d/prod-mariadb.yaml** — E-Commerce database:

```yaml
# E-Commerce MariaDB — tighter connection threshold for high-traffic
tenants:
  prod-mariadb:
    mysql_connections: "150"
    mysql_connections_critical: "200"
    mysql_threads_running: "40"    # threads_running saturation (concurrent threads, NOT host CPU%); platform default 30
    container_cpu: "75"
    container_memory: "80"

    _routing:
      receiver:
        type: slack
        api_url: https://hooks.slack.com/services/T00/B00/xxx
      group_by: [alertname, severity]
      group_wait: "30s"
      repeat_interval: "4h"

    _metadata:
      owner: ecommerce-team
      tier: production
      runbook_url: https://runbooks.example.com/ecommerce-mariadb
```

**conf.d/prod-redis.yaml** — Session cache using a routing profile:

```yaml
# Session Cache — Redis with shared routing profile
tenants:
  prod-redis:
    redis_memory_used_bytes: "3221225472"
    redis_memory_used_bytes_critical: "4294967296"
    redis_connected_clients: "3000"
    container_cpu: "70"
    container_memory: "80"

    _routing_profile: team-sre-apac

    _metadata:
      owner: sre-apac
      tier: production
```

**conf.d/prod-kafka.yaml** — Event pipeline with PagerDuty:

```yaml
tenants:
  prod-kafka:
    kafka_consumer_lag: "50000"
    kafka_consumer_lag_critical: "200000"
    kafka_broker_count: "3"
    kafka_active_controllers: "1"
    kafka_under_replicated_partitions: "0"
    jvm_gc_pause: "0.8"
    jvm_memory: "85"

    _routing:
      receiver:
        type: pagerduty
        service_key: "<your-pagerduty-service-key>"
      group_by: [alertname, topic]
      group_wait: "1m"
      repeat_interval: "12h"
```

**conf.d/staging-pg.yaml** — Staging with maintenance window:

```yaml
tenants:
  staging-pg:
    pg_connections: "100"
    pg_replication_lag: "60"
    container_cpu: "90"
    container_memory: "95"

    _state_maintenance:
      expires: "2099-03-20T06:00:00Z"

    _silent_mode:
      target: warning        # required — which severities to silence (warning | critical | all | disable)
      expires: "2099-03-18T12:00:00Z"

    _routing:
      receiver:
        type: email
        to: ["dba-oncall@example.com"]
        smarthost: "smtp.example.com:587"
        from: "alerting@example.com"
      group_wait: "5m"
      repeat_interval: "24h"
```

**conf.d/prod-oracle.yaml** — Finance DB (routing profile, constrained by the finance domain policy):

```yaml
tenants:
  prod-oracle:
    oracle_sessions_active: "100"
    oracle_sessions_active_critical: "150"
    oracle_tablespace_used_percent: "75"
    oracle_tablespace_used_percent_critical: "85"

    _routing_profile: domain-finance-tier1

    _metadata:
      owner: finance-dba-team
      domain: finance
      tags: [sox-compliant]
```

The two routing profiles that prod-redis and prod-oracle reference, and the domain policy that constrains prod-oracle, are platform-level settings with their own file names — they do not go in tenant files. Add these two files to `conf.d/`:

**conf.d/_routing_profiles.yaml** — named routing settings any tenant can reference:

```yaml
routing_profiles:
  team-sre-apac:
    receiver:
      type: slack
      api_url: https://hooks.slack.com/services/T00/B00/sre-apac
    group_by: [tenant, alertname, severity]
    group_wait: "30s"
    repeat_interval: "4h"

  domain-finance-tier1:
    receiver:
      type: pagerduty
      service_key: "<your-finance-pagerduty-key>"
    group_by: [tenant, alertname, severity]
    group_wait: "30s"
    repeat_interval: "1h"
```

**conf.d/_domain_policy.yaml** — compliance constraints for a business domain (used in Exercise 8):

```yaml
domain_policies:
  finance:
    description: "Notification compliance for the finance databases"
    tenants: [prod-oracle]
    constraints:
      forbidden_receiver_types: [slack, webhook]
      max_repeat_interval: 1h
```

A domain policy names the tenants it constrains in its `tenants:` list. The block is only read from a file named `_domain_policy.yaml`; writing `_domain_policy: finance` in a tenant file applies no policy, and validation only reports `unknown reserved key '_domain_policy'`.

## Exercise 3: Validate All Configs

```bash
docker run --rm \
  -v $(pwd)/conf.d:/data/conf.d:ro \
  ghcr.io/vencil/da-tools:latest \
  validate-config --config-dir /data/conf.d
```

Expected output (measured after doing Exercises 1 and 2 as written):

```
============================================================
  validate-config — Unified Validation Report
============================================================

[PASS] yaml_syntax
       8 files parsed successfully

[PASS] schema
       No schema warnings

[PASS] routes
       5 routes, 5 receivers, 5 inhibit_rules

[PASS] profiles
       5 tenants scanned, 0 profile refs, 0 profiles defined

[PASS] policy_dsl
       No _policies defined — skipped

[PASS] tenant_uniqueness
       5 tenant(s), each declared in exactly one file

------------------------------------------------------------
  Total: 6 checks | 6 pass | 0 warn | 0 fail
------------------------------------------------------------
  Result: PASS
```

The exit code is `0`: it is non-zero (`1`) only when a check is `fail`, and a WARN does not fail the run — so CI can call it as-is, no extra flag needed.

- The `schema` row also checks `_routing_profile` references and domain policies: without `_routing_profiles.yaml` it shows `_routing_profile references unknown profile`; without `_domain_policy.yaml` the finance constraints simply do not exist, and nothing says so.
- The `profiles` row is about threshold profiles (`_profiles.yaml` and a tenant's `_profile`), not routing profiles, so it still reads `0 profiles defined` after Exercise 2.

⚠️ If `tenant_uniqueness` says `0 tenant(s)` and `routes` says `0 routes`, you dropped the two outer levels (`tenants:` and `<tenant name>:`) when pasting the Exercise 2 snippets. Every check then reads PASS while nothing was validated.

**Checkpoint**: Can you explain why `group_wait: "2s"` produces a WARN under `routes`, and why the value that takes effect is 5s? (Hint: the guardrail range is 5s–5m; a value below the floor is clamped to 5s — a warning, not a failure)

## Exercise 4: Generate Alertmanager Routes

```bash
mkdir -p .output

# Write the file: do NOT add --validate here — it returns before -o is used, and the tool refuses the pair (exit 2)
docker run --rm \
  --user $(id -u):$(id -g) \
  -v $(pwd)/conf.d:/data/conf.d:ro \
  -v $(pwd)/.output:/data/output \
  ghcr.io/vencil/da-tools:latest \
  generate-routes --config-dir /data/conf.d \
  -o /data/output/alertmanager-routes.yaml

# Validate in a separate run (read-only, writes nothing)
docker run --rm \
  -v $(pwd)/conf.d:/data/conf.d:ro \
  ghcr.io/vencil/da-tools:latest \
  generate-routes --config-dir /data/conf.d --validate
```

Expected output summary:

```
Generated routes for 5 tenants:
  prod-mariadb  → slack     (group_wait: 30s, repeat: 4h)
  prod-redis    → slack     (profile: team-sre-apac)
  prod-kafka    → pagerduty (group_wait: 1m, repeat: 12h)
  staging-pg    → email     (group_wait: 5m, repeat: 24h)
  prod-oracle   → pagerduty (profile: domain-finance-tier1)
  + 5 inhibit rules (severity dedup)
Written: /data/output/alertmanager-routes.yaml
```

Each tenant gets its own route block, with receiver, group_by, timing parameters, and inhibit rules for severity dedup.

**Checkpoint**: Find the `inhibit_rules` section. How does it prevent duplicate warnings when a critical alert fires?

## Exercise 5: Trace a Routing Decision

```bash
docker run --rm \
  -v $(pwd)/conf.d:/data/conf.d:ro \
  ghcr.io/vencil/da-tools:latest \
  explain-route --tenant prod-redis --config-dir /data/conf.d
```

This shows the four-layer merge for prod-redis:
1. **Platform defaults** → webhook, 30s group_wait
2. **Routing profile** `team-sre-apac` → overrides to slack, 30s wait, 4h repeat
3. **Tenant _routing** → (none, uses profile)
4. **Platform enforced** → NOC copy

**Checkpoint**: What receiver_type does prod-redis resolve to? Which layer set it?

## Exercise 6: Blast Radius Analysis

Simulate lowering the ecommerce MySQL threshold:

```bash
# Create a modified copy
cp -r conf.d conf.d.new
# In conf.d.new/prod-mariadb.yaml, change mysql_connections: "150" to "120"
sed -i 's/mysql_connections: "150"/mysql_connections: "120"/' conf.d.new/prod-mariadb.yaml

docker run --rm \
  -v $(pwd)/conf.d:/data/conf.d:ro \
  -v $(pwd)/conf.d.new:/data/conf.d.new:ro \
  ghcr.io/vencil/da-tools:latest \
  config-diff --old-dir /data/conf.d --new-dir /data/conf.d.new
```

The diff shows exactly which tenant and metrics are affected — this is what gets posted as a PR comment in CI.

## Exercise 7: Three-State Operations

Examine `staging-pg.yaml`:

- **`_state_maintenance`**: Alerts still evaluate but route to maintenance-specific handling. The `expires` timestamp means the state auto-reverts to normal after that time.
- **`_silent_mode`**: Alerts are fully suppressed — no notifications sent. Also has `expires` for safety.

Try removing `_state_maintenance` and re-running validate — you'll see the tenant return to normal routing.

## Exercise 8: Domain Policy Test

Try changing prod-oracle's routing to use Slack: in `conf.d/prod-oracle.yaml`, add this under `prod-oracle:` (at the same level as `_metadata`):

```yaml
_routing:
  receiver:
    type: slack
    api_url: https://hooks.slack.com/services/xxx
```

Run validation again — you should see a domain policy warning: `finance` domain forbids `slack`. Add `--strict` and the warning escalates to an ERROR with a non-zero exit code (CI runs `--validate --strict`, blocking such violations before merge).

This is the Policy-as-Code enforcement at work.

## Cleanup

```bash
cd ~ && rm -rf ~/da-lab
```

## What's Next?

- **Deploy to a real cluster**: Follow the [GitOps CI/CD Guide](gitops-ci-integration.en.md) to set up the full pipeline
- **Explore interactive tools**: Open the [Self-Service Portal](../assets/jsx-loader.html?component=self-service-portal) in your browser for visual validation
- **Run the showcase**: `make demo-showcase` runs all these exercises as an automated script
- **Deep dive**: Read the [Architecture & Design](../architecture-and-design.md) doc for full platform concepts

---

**Document version:** v2.9.0
**Maintainer:** Platform Engineering Team
