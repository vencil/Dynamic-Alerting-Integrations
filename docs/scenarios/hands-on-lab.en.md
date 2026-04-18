---
title: "Hands-on Lab: From Zero to Production Alerting"
tags: [scenario, hands-on, lab, adoption, tutorial]
audience: [platform-engineer, tenant]
version: v2.7.0
lang: en
---

# Hands-on Lab: From Zero to Production Alerting

> **v2.7.0** | Estimated time: 30–45 minutes | Prerequisites: Docker installed
>
> Related: [GitOps CI/CD Guide](gitops-ci-integration.en.md) · [Tenant Lifecycle](tenant-lifecycle.en.md) · [CLI Reference](../cli-reference.md)

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

Expected output (indentation omitted):

```
.da-init.yaml
.github/workflows/dynamic-alerting.yaml
.pre-commit-config.da.yaml
conf.d/_defaults.yaml
conf.d/prod-mariadb.yaml
conf.d/prod-redis.yaml
conf.d/prod-kafka.yaml
conf.d/staging-pg.yaml
conf.d/prod-oracle.yaml
kustomize/base/kustomization.yaml
```

## Exercise 2: Configure Tenant Thresholds

Edit each tenant file to set realistic thresholds.

**prod-mariadb.yaml** — E-Commerce database:

```yaml
# E-Commerce MariaDB — tighter connection threshold for high-traffic
mysql_connections: "150"
mysql_connections_critical: "200"
mysql_cpu: "75"
container_cpu: "75"
container_memory: "80"

_routing:
  receiver_type: slack
  webhook_url: https://hooks.slack.com/services/T00/B00/xxx
  group_by: [alertname, severity]
  group_wait: "30s"
  repeat_interval: "4h"

_metadata:
  owner: ecommerce-team
  tier: production
  runbook_url: https://runbooks.example.com/ecommerce-mariadb
```

**prod-redis.yaml** — Session cache using a routing profile:

```yaml
# Session Cache — Redis with shared routing profile
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

**prod-kafka.yaml** — Event pipeline with PagerDuty:

```yaml
kafka_consumer_lag: "50000"
kafka_consumer_lag_critical: "200000"
kafka_broker_count: "3"
kafka_active_controllers: "1"
kafka_under_replicated_partitions: "0"
jvm_gc_pause: "0.8"
jvm_memory: "85"

_routing:
  receiver_type: pagerduty
  group_by: [alertname, topic]
  group_wait: "1m"
  repeat_interval: "12h"
```

**staging-pg.yaml** — Staging with maintenance window:

```yaml
pg_connections: "100"
pg_replication_lag: "60"
container_cpu: "90"
container_memory: "95"

_state_maintenance:
  expires: "2026-03-20T06:00:00Z"

_silent_mode:
  expires: "2026-03-18T12:00:00Z"

_routing:
  receiver_type: email
  group_wait: "5m"
  repeat_interval: "24h"
```

**prod-oracle.yaml** — Finance DB with domain policy:

```yaml
oracle_sessions_active: "100"
oracle_sessions_active_critical: "150"
oracle_tablespace_used_percent: "75"
oracle_tablespace_used_percent_critical: "85"

_routing_profile: domain-finance-tier1
_domain_policy: finance

_metadata:
  owner: finance-dba-team
  domain: finance
  compliance: SOX
```

## Exercise 3: Validate All Configs

```bash
docker run --rm \
  -v $(pwd)/conf.d:/data/conf.d:ro \
  ghcr.io/vencil/da-tools:latest \
  validate-config --config-dir /data/conf.d --ci
```

Expected output:

```
[PASS] prod-mariadb: 5 keys, routing OK
[PASS] prod-redis:   5 keys, routing OK (profile: team-sre-apac)
[PASS] prod-kafka:   7 keys, routing OK
[PASS] staging-pg:   2 keys, routing OK, maintenance window active
[PASS] prod-oracle:  4 keys, routing OK (profile: domain-finance-tier1)

✅ All 5 tenants passed validation.
```

If any warnings appear, review the key names and timing guardrails.

**Checkpoint**: Can you explain why `group_wait: "2s"` would fail validation? (Hint: guardrail minimum is 5s)

## Exercise 4: Generate Alertmanager Routes

```bash
mkdir -p .output

docker run --rm \
  -v $(pwd)/conf.d:/data/conf.d:ro \
  -v $(pwd)/.output:/data/output \
  ghcr.io/vencil/da-tools:latest \
  generate-routes --config-dir /data/conf.d \
  -o /data/output/alertmanager-routes.yaml --validate
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

Try changing prod-oracle's routing to use Slack:

```yaml
# In prod-oracle.yaml, replace _routing_profile line with:
_routing:
  receiver_type: slack
  webhook_url: https://hooks.slack.com/services/xxx
```

Run validation again — you should see a domain policy warning: `finance` domain forbids `slack`.

This is the Policy-as-Code enforcement at work.

## Cleanup

```bash
cd ~ && rm -rf ~/da-lab
```

## What's Next?

- **Deploy to a real cluster**: Follow the [GitOps CI/CD Guide](gitops-ci-integration.en.md) to set up the full pipeline
- **Explore interactive tools**: Open the [Self-Service Portal](../interactive/tools/self-service-portal.jsx) in your browser for visual validation
- **Run the showcase**: `make demo-showcase` runs all these exercises as an automated script
- **Deep dive**: Read the [Architecture & Design](../architecture-and-design.md) doc for full platform concepts

---

**Document version:** v2.7.0 — 2026-04-18
**Maintainer:** Platform Engineering Team
