---
title: "Platform Engineer Quick Start Guide"
tags: [getting-started, platform-setup]
audience: [platform-engineer]
version: v2.0.0-preview.3
lang: en
---
# Platform Engineer Quick Start Guide

> **v2.0.0-preview** | Audience: Platform Engineers, SREs, Infrastructure Managers
>
> Related docs: [Architecture](../architecture-and-design.md) · [Benchmarks](../architecture-and-design.md) · [GitOps Deployment](../gitops-deployment.md) · [Rule Packs](../rule-packs/README.md)

## Three Things You Need to Know

**1. threshold-exporter is the core.** It reads YAML config, emits Prometheus metrics, and supports SHA-256 hot-reload. Two replicas run HA on port 8080.

**2. Rule Packs are self-contained units.** 15 Rule Packs mount via Projected Volume into Prometheus, each covering a database or service type (MariaDB, PostgreSQL, Redis, etc.). Use the `optional: true` mechanism to safely uninstall unwanted Rule Packs.

**3. Everything is config-driven.** `_defaults.yaml` controls global platform behavior, tenant YAML overrides defaults, and `_profiles.yaml` provides inheritance chains. No hardcoding, no secrets.

## 30-Second Quick Deploy

Minimal viable platform config:

```yaml
# conf.d/_defaults.yaml
defaults:
  mysql_connections: "80"
  mysql_cpu: "75"
  mysql_memory: "85"
  # Other default thresholds...
```

### Deploy threshold-exporter ×2 HA

```bash
kubectl apply -f k8s/02-threshold-exporter/
# Verify replicas are running
kubectl get pod -n monitoring | grep threshold-exporter
```

### Mount Rule Packs

```bash
# Prometheus StatefulSet uses Projected Volume
# Confirm volume section in k8s/03-monitoring/prometheus-statefulset.yaml
kubectl get configmap -n monitoring | grep rule-pack
```

## Common Operations

### Managing Global Defaults

```yaml
# conf.d/_defaults.yaml
defaults:
  mysql_connections: "80"
  mysql_connections_critical: "95"
  container_cpu: "70"
  container_memory: "80"
  # Dimension threshold omitted (will use default)
  redis_memory: "disable"      # Suppress entirely
  _routing_defaults:
    group_wait: "30s"
    group_interval: "5m"
    repeat_interval: "12h"
```

Validate defaults syntax:

```bash
python3 scripts/tools/ops/validate_config.py --config-dir conf.d/ --schema
```

### Managing Rule Packs

List mounted Rule Packs:

```bash
kubectl get configmap -n monitoring | grep rule-pack
# Possible output: rule-pack-mariadb, rule-pack-postgresql, rule-pack-redis...
```

Remove unwanted Rule Pack (edit Prometheus StatefulSet):

```bash
kubectl edit statefulset prometheus -n monitoring
# Remove corresponding configMapRef from volumes.projected.sources
# Or set Projected Volume optional: true for safe uninstallation
```

### Setting Up Platform Enforced Routing (_routing_enforced)

Enable dual-channel notifications (NOC + Tenant):

```yaml
# conf.d/_defaults.yaml
defaults:
  _routing_enforced:
    receiver:
      type: "slack"
      api_url: "https://hooks.slack.com/services/T/B/xxx"
      channel: "#noc-alerts"
    group_wait: "10s"
    repeat_interval: "2h"
```

NOC receives notifications using `platform_summary` annotation, focused on capacity planning and escalation decisions. Tenants still receive their own `summary` unaffected.

### Setting Up Routing Defaults (_routing_defaults)

```yaml
# conf.d/_defaults.yaml
defaults:
  _routing_defaults:
    receiver:
      type: "slack"
      api_url: "https://hooks.slack.com/services/T/{{tenant}}-alerts"
      channel: "#{{tenant}}-team"
    group_wait: "30s"
    repeat_interval: "4h"
```

The `{{tenant}}` placeholder expands to each tenant's name. Tenant YAML's `_routing` can override this default.

### Configuring Tenant Profiles

```yaml
# conf.d/_profiles.yaml
profiles:
  standard-db:
    mysql_connections: "80"
    mysql_cpu: "75"
    container_memory: "85"
  high-load-db:
    mysql_connections: "60"     # Stricter
    mysql_cpu: "60"
    container_memory: "80"
```

Tenants inherit via `_profile`:

```yaml
# conf.d/my-tenant.yaml
tenants:
  my-tenant:
    _profile: "standard-db"
    mysql_connections: "70"     # Overrides profile value
```

### Setting Up Webhook Domain Allowlist

Restrict webhook receiver target domains:

```bash
python3 scripts/tools/ops/generate_alertmanager_routes.py \
  --config-dir conf.d/ \
  --policy "*.example.com" \
  --policy "hooks.slack.com" \
  --validate
```

Empty list means no restriction; fnmatch patterns support wildcards.

## Validation Tools

### One-Stop Configuration Validation

```bash
python3 scripts/tools/ops/validate_config.py \
  --config-dir conf.d/ \
  --schema
```

Checked items:
- YAML syntax correctness
- Parameter schema conformance
- Route generation success
- Policy checks pass
- Version consistency

### Configuration Difference Analysis

```bash
python3 scripts/tools/ops/config_diff.py \
  --old-dir conf.d.baseline \
  --new-dir conf.d/ \
  --format json
```

Output: added tenants, removed tenants, changed defaults, changed profiles. Use for GitOps PR review.

### Version Consistency Check

```bash
make version-check
python3 scripts/tools/dx/bump_docs.py --check
```

Ensure versions in CLAUDE.md, README, and CHANGELOG are synchronized.

## Performance Monitoring

### Run Benchmarks

```bash
make benchmark ARGS="--under-load --scaling-curve --routing-bench --alertmanager-bench --reload-bench --json"
```

Output metrics:
- Idle memory footprint
- Scaling curve (QPS vs memory/latency)
- Routing throughput
- Alertmanager response time
- ConfigMap reload latency

Results saved as JSON for CI comparison.

### Platform Rule Pack Self-Monitoring

The platform itself provides Rule Pack alerts (e.g., exporter offline, Alertmanager delay > 1m):

```bash
kubectl get alerts -n monitoring | grep platform
```

## FAQ

**Q: How do I add a new Rule Pack?**
A: Create a new YAML file in `rule-packs/` directory and mount the corresponding ConfigMap in Prometheus's Projected Volume config. See the Rule Pack README for templates.

**Q: How do I force NOC to receive all notifications?**
A: Set `_routing_enforced` in `_defaults.yaml`. Notifications go to the NOC channel and each tenant's receiver independently.

**Q: Why does the webhook allowlist reject my domain?**
A: Check if your webhook URL matches the fnmatch pattern using `--policy`. For example, `*.example.com` won't match `webhook.internal.example.com` (multi-level subdomain).

**Q: How do I validate that a new tenant's config won't cause alert noise?**
A: First use `validate_config.py` to check syntax and schema, then `config_diff.py` to see blast radius, finally test in a shadow monitoring environment (see shadow-monitoring-sop.md).

**Q: What is Rule Pack optional: true?**
A: A Kubernetes Projected Volume feature. With `optional: true`, if the ConfigMap doesn't exist, Prometheus still starts (volume mount is empty). Use for safe Rule Pack uninstallation.

**Q: Do I need to customize rules within a Rule Pack?**
A: Don't modify Rule Packs directly. Use `_routing.overrides[]` in tenant YAML to override routing for single rules, or add custom rules via custom rule governance (lint_custom_rules.py).

## Related Resources

| Resource | Relevance |
|----------|-----------|
| ["Platform Engineer Quick Start Guide"](for-platform-engineers.en.md) | ★★★ |
| ["Domain Expert (DBA) Quick Start Guide"](for-domain-experts.en.md) | ★★ |
| ["Tenant Quick Start Guide"](for-tenants.en.md) | ★★ |
| ["Migration Guide — From Traditional Monitoring to Dynamic Alerting Platform"] | ★★ |
