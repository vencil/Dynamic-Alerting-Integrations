---
title: "Tenant Quick Start Guide"
tags: [getting-started, tenant-onboard]
audience: [tenant]
version: v2.0.0
lang: en
---
# Tenant Quick Start Guide

> **v2.0.0** | Audience: Tenant administrators, DBAs, SREs
>
> Related docs: [Migration Guide](../migration-guide.md) · [Architecture](../architecture-and-design.md) §2 · [Rule Packs](../rule-packs/README.md)

## Three Things You Need to Know

**1. Your monitoring is already active.** The platform ships with 15 Rule Packs covering MariaDB, PostgreSQL, Redis, MongoDB, Elasticsearch, Kafka, and more. As long as your exporter is running, alert rules are in effect.

**2. You only manage one YAML file.** All customization lives in `conf.d/<tenant>.yaml` — thresholds, notification routing, maintenance windows, everything.

**3. Defaults are reasonable. You may not need to change anything.** Unless your business scenario requires stricter or more relaxed thresholds, the defaults in `_defaults.yaml` are sufficient.

## 30-Second Quick Setup

The minimal viable tenant config is just two lines:

```yaml
# conf.d/my-tenant.yaml
tenants:
  my-tenant: {}
```

This gives your tenant all default thresholds with no custom routing (alerts go to Alertmanager's default receiver).

## Common Operations

### Adjusting Thresholds

```yaml
tenants:
  my-tenant:
    mysql_connections: "70"       # Connection warning threshold (default: 80)
    mysql_connections_critical: "95"  # Connection critical threshold
    container_cpu: "60"           # Container CPU warning threshold (default: 70)
```

Tri-state design: each metric can be **custom value**, **omitted** (use default), or `"disable"` (suppress alert).

> 💡 **Interactive Tools** — Want to validate your YAML in real-time? Try [YAML Playground](https://vencil.github.io/Dynamic-Alerting-Integrations/assets/jsx-loader.html?component=../interactive/tools/playground.jsx). Unsure how to set thresholds? Use [Threshold Calculator](https://vencil.github.io/Dynamic-Alerting-Integrations/assets/jsx-loader.html?component=../interactive/tools/threshold-calculator.jsx) to derive values from p50/p90/p99.

```yaml
tenants:
  my-tenant:
    mysql_connections: "70"       # Custom
    # mysql_cpu omitted           → uses _defaults.yaml value
    container_memory: "disable"   # Suppress this alert
```

### Setting Up Alert Notification Routing

```yaml
tenants:
  my-tenant:
    _routing:
      receiver:
        type: "slack"
        api_url: "https://hooks.slack.com/services/T/B/xxx"
        channel: "#my-team-alerts"
      group_wait: "30s"
      repeat_interval: "4h"
```

Supported receiver types: `webhook`, `email`, `slack`, `teams`, `rocketchat`, `pagerduty`.

### Using Profile Inheritance

If multiple tenants share similar configs, use Profiles to avoid repetition:

```yaml
# conf.d/my-tenant.yaml
tenants:
  my-tenant:
    _profile: "standard-db"      # Inherit from _profiles.yaml
    mysql_connections: "50"       # This overrides the profile value
```

Inheritance order: `_defaults.yaml` → `_profiles.yaml` → tenant overrides. Tenant values always win.

### Entering Maintenance Mode

```yaml
tenants:
  my-tenant:
    _state_maintenance:
      enabled: true
      expires: "2026-03-15T06:00:00Z"   # Auto-recover
      reason: "Planned DB migration"
```

In maintenance mode, alerts don't fire (PromQL-level suppression). Automatically resumes after expiry.

> **Timezone:** The `expires` field uses RFC 3339 format (with timezone). Scheduled thresholds' `window` and recurring maintenance's `cron` both use **UTC timezone**.

### Silencing Specific Severities

```yaml
tenants:
  my-tenant:
    _silent_mode:
      target: "warning"                  # Silence only warnings
      expires: "2026-03-13T12:00:00Z"
      reason: "Known noisy alert during migration"
```

In silent mode, alerts still fire (TSDB records them), but Alertmanager won't send notifications.

### Injecting Runbook / Owner / Tier

```yaml
tenants:
  my-tenant:
    _metadata:
      runbook_url: "https://wiki.example.com/my-tenant"
      owner: "dba-team"
      tier: "tier-1"
```

These metadata fields are automatically injected into all alert annotations and appear in notifications.

## What Your Notifications Look Like

Alert notifications use `summary` and `description` written for you (the Tenant), telling you:

- **What's wrong** (e.g., "High connections on my-tenant")
- **Specific values** (e.g., "150 threads connected")
- **What you can do** (in description or runbook)

> If your Platform team enabled `_routing_enforced`, they receive a parallel platform-perspective summary (`platform_summary`) focused on capacity planning and escalation decisions. You don't need to worry about this — your notifications are unaffected.

> 💡 **Interactive Tools** — Want to see which alerts you'll receive? Use [Alert Simulator](https://vencil.github.io/Dynamic-Alerting-Integrations/assets/jsx-loader.html?component=../interactive/tools/alert-simulator.jsx). Not sure which Rule Pack to use? Try [Rule Pack Selector](https://vencil.github.io/Dynamic-Alerting-Integrations/assets/jsx-loader.html?component=../interactive/tools/rule-pack-selector.jsx).

## Self-Service Verification

### Validate Configuration

```bash
# One-stop validation (YAML syntax + schema + routes + profiles)
python3 scripts/tools/ops/validate_config.py --config-dir conf.d/
```

### View Inheritance Chain

```bash
# See the final resolved thresholds for your tenant
python3 scripts/tools/ops/diagnose.py my-tenant \
  --config-dir conf.d/ --show-inheritance
```

### Preview Change Impact

```bash
# Compare before/after blast radius
python3 scripts/tools/ops/config_diff.py \
  --old-dir conf.d.baseline --new-dir conf.d/
```

### Check Alert Quality (v2.0.0)

```bash
# Check your tenant's alert quality for noise (flapping) / stale (idle) issues
da-tools alert-quality --prometheus http://localhost:9090 --config-dir conf.d/
```

Output: per-tenant quality score (0–100) and specific issue list.

## Generate Config (Interactive)

First time onboarding? Use the scaffold tool:

```bash
python3 scripts/tools/ops/scaffold_tenant.py
```

It asks a few questions (DB type, notification method), then generates a complete YAML file.

## FAQ

**Q: How long until my YAML changes take effect?**
A: threshold-exporter checks ConfigMap SHA-256 hash every 15 seconds. Changes are hot-reloaded, no restart needed.

**Q: Can I use only some Rule Packs?**
A: Rule Packs without matching exporter metrics simply don't fire alerts (no data = no trigger). To fully remove one, the Projected Volume `optional: true` mechanism allows safe uninstallation.

**Q: What's the difference between _profile and direct settings?**
A: Profiles are fill-in only — they apply only when the tenant hasn't set that key. Your direct settings always take precedence.

**Q: How do I find available metric keys?**
A: Check `_defaults.yaml` and the header comments in each Rule Pack YAML. You can also run `diagnose.py --show-inheritance` to see all resolved keys.

> 💡 **First time going live?** Use [Onboarding Checklist](https://vencil.github.io/Dynamic-Alerting-Integrations/assets/jsx-loader.html?component=../interactive/tools/onboarding-checklist.jsx) for a complete step-by-step guide, or start with the [interactive setup wizard](https://vencil.github.io/Dynamic-Alerting-Integrations/assets/jsx-loader.html?component=../getting-started/wizard.jsx). Want to see the complete platform in action? [Platform Demo](https://vencil.github.io/Dynamic-Alerting-Integrations/assets/jsx-loader.html?component=../interactive/tools/platform-demo.jsx) demonstrates real scenarios. See all tools at [Interactive Tools Hub](https://vencil.github.io/Dynamic-Alerting-Integrations/).

## Related Resources

| Resource | Relevance |
|----------|-----------|
| ["Tenant Quick Start Guide"](for-tenants.en.md) | ⭐⭐⭐ |
| ["Migration Guide — From Traditional Monitoring to Dynamic Alerting Platform"](../migration-guide.en.md) | ⭐⭐ |
| ["Domain Expert (DBA) Quick Start Guide"](for-domain-experts.en.md) | ⭐⭐ |
| ["Platform Engineer Quick Start Guide"](for-platform-engineers.en.md) | ⭐⭐ |
