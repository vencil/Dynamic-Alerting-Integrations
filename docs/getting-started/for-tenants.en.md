---
title: "Tenant Quick Start Guide"
tags: [getting-started, tenant-onboard]
audience: [tenant]
version: v2.9.0
lang: en
---
# Tenant Quick Start Guide

> **Language / 語言：** **English (Current)** | [中文](./for-tenants.md)

> **v2.9.0** | Audience: Tenant administrators, DBAs, SREs
>
> Related docs: [Migration Guide](../migration-guide.en.md) · [Architecture](../architecture-and-design.en.md) §2 · [Rule Packs](../rule-packs/README.md) · [Alert Design Fundamentals](../alerting-design-fundamentals.en.md)

> 💡 **Want to play first?** [`try-local/`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/try-local/README.md) spins up a self-service sandbox in one command (Tenant Manager UI + 2 demo tenants) — try threshold edits and Saved Views without waiting for your Platform Team to deploy.

## Your Onboarding Path

1. **Run the whole stack first** (big picture) → [try-local](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/try-local/README.md): `docker compose up da-portal tenant-api`, open the Tenant Manager in your browser.
2. **Go deeper on your main interface** → [da-portal QUICKSTART](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/components/da-portal/QUICKSTART.md) (browser self-service threshold edits / Saved Views).
3. **Configure your tenant for real** → the "30-Second Quick Setup" and common operations below.

## Three Things You Need to Know

**1. Your monitoring is already active.** The platform ships with 16 Rule Packs covering MariaDB, PostgreSQL, Redis, MongoDB, Elasticsearch, Kafka, and more. As long as your exporter is running, alert rules are in effect.

**2. You only manage one YAML file (or use the Web UI).** All customization lives in `conf.d/<tenant>.yaml` — thresholds, notification routing, maintenance windows, everything. If your Platform Team has deployed the Self-Service Portal (tenant-manager UI), you can manage everything directly in the browser without editing YAML.

**3. Defaults are reasonable. You may not need to change anything.** Unless your business scenario requires stricter or more relaxed thresholds, the defaults in `_defaults.yaml` are sufficient.

## 30-Second Quick Setup

The minimal viable tenant config is just two lines:

```yaml
# conf.d/my-tenant.yaml
tenants:
  my-tenant: {}
```

This gives your tenant **whatever default thresholds the platform currently supplies**, with no custom routing (alerts go to Alertmanager's default receiver).

⚠️ **"Default thresholds" is not "all thresholds" — and "you can set it" is not "the alert is live."** The shipped helm chart carries platform defaults for only a handful of keys (`thresholdConfig.defaults`). Every other key falls into one of **three** groups, and they are handled very differently:

- **Keys the platform DECLARES but assigns no value to** (9 today) — listed under `optional_overrides:` in `_defaults.yaml` / helm values, shipped with the chart and with the onboarding artifacts. **These need no further operator action**: put a value in your own `conf.d/<tenant>.yaml` and it takes effect. But the platform deliberately asserts no default for them, so **leaving one unset is silent, with no error message either** — that is the design, not a missing default: thresholds like Oracle process count or DB2 deadlock rate can only be calibrated against your own baseline, and a platform-chosen number would just produce false alarms.
- **`<base>_critical` keys that are off the list but whose base already HAS a platform default** (5 today: `mysql_connections_critical`, `mysql_threads_running_critical`, `mysql_replication_lag_critical`, `pg_connections_critical`, `pg_replication_lag_critical`) — **you can set these today**: a write through the Portal / Tenant API is not rejected, a direct GitOps push works too, and either way you get a real critical alert row. `_critical` keys never go through the declared list (listing them would be decoration); their entry condition is "the same-named base has a value in `defaults:`", and the bases of these five (`mysql_connections`, `pg_connections`, …) are shipped platform defaults already.
- **Keys whose base has no platform default either** (11 today, e.g. `kafka_*_critical` / `jvm_*_critical` / `nginx_*_critical`) — this is the group that really does need the **platform operator** to supply the base first (via the `_defaults.yaml` that `scaffold-tenant` generates, or by hand in helm values) before you can set them. Until then, writing one through the Portal / Tenant API is rejected as an unknown key, and pushing it straight to GitOps is accepted but has no effect.

The first group (when you leave it unset) and the third group end the same way: no threshold series ⇒ the rule matches nothing ⇒ empty result — that is the config-driven join itself.

**Unsure which group a key is in? Do not eyeball it** — the command under *Self-Service Verification › View Inheritance Chain* below prints the answer already split into sections.

`_defaults.yaml` normally sits in the **same `conf.d/` directory** you edit `<tenant>.yaml` in (CODEOWNERS restricts who may **change** it, not who may **read** it), so in most setups you can read it and run that command yourself. Only when your organisation keeps the platform file in a source you cannot reach (a multi-team sharded layout, see the [GitOps CI/CD Integration Guide](../scenarios/gitops-ci-integration.en.md)) do you need to ask your platform operator for it — **both the `defaults:` and the `optional_overrides:` sections**.

## Common Operations

### Adjusting Thresholds

```yaml
tenants:
  my-tenant:
    mysql_connections: "70"       # Connection warning threshold (default: 80)
    mysql_connections_critical: "95"  # Connection critical threshold
    container_cpu: "60"           # Container CPU warning threshold (default: 70)
```

Tri-state design (**for keys that have a platform default**): each metric can be **custom value**, **omitted** (use default), or `"disable"` (suppress alert).

⚠️ Group 1 above (the declared tier, `optional_overrides:`) has no platform default to fall back to, so **omitting one means no value and therefore no series** — not "use the default". That tier really has only two states: set it, or leave it silent.

> 💡 **Interactive Tools** — Want to validate your YAML in real-time? Try [YAML Playground](https://vencil.github.io/Dynamic-Alerting-Integrations/assets/jsx-loader.html?component=../interactive/tools/playground.jsx). Unsure how to set thresholds? Use [Threshold Calculator](https://vencil.github.io/Dynamic-Alerting-Integrations/assets/jsx-loader.html?component=../interactive/tools/threshold-calculator.jsx) to derive values from p50/p90/p99.

```yaml
tenants:
  my-tenant:
    mysql_connections: "70"       # Custom
    # mysql_threads_running omitted → uses _defaults.yaml value
    container_memory: "disable"   # Suppress this alert
```

### Adding Custom Alerts (no PromQL)

Beyond the platform's built-in alerts, you can add your own. **The easiest way: use the Self-Service Portal's Recipe Builder** — pick a recipe (threshold / rate / ratio / forecast …), fill in the parameters, and it generates the config for you, with no PromQL to write.

Advanced users can also declare them directly under `_custom_alerts` in `conf.d/<tenant>.yaml`; see [threshold-exporter §Custom Alerts](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/components/threshold-exporter/README.md#45-自訂告警-_custom_alerts).

> **Three footguns to know**:
> 1. Your metric **must carry a `tenant` label** (platform rules join `on(tenant)`), or the rule **silently never fires** — multi-tenant scrapes usually inject it via relabel.
> 2. The `absence` recipe **fires immediately** for a metric that **never appeared** (the safety guard isn't shipped yet); on first onboarding, make the metric appear at least once.
> 3. `threshold` / `rate` aggregate with `max`/`sum by(tenant)`, which **drops sub-labels** like `device` / `instance` (the alert says "the worst one", not which one).

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

### Using Routing Profiles (v2.1.0)

If your Platform Team has defined shared routing profiles (`_routing_profiles.yaml`), you can reference them via `_routing_profile` instead of writing full `_routing` config:

```yaml
tenants:
  my-tenant:
    _routing_profile: "team-sre-apac"   # Use shared routing profile
    _routing:
      repeat_interval: "2h"             # Override individual fields from profile
```

Four-layer merge order: `_routing_defaults` → profile → tenant `_routing` → `_routing_enforced`. Your `_routing` fields always take precedence over profile values. Debug tool: `da-tools explain-route --config-dir conf.d/ --tenant my-tenant`.

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

The output has two sections, matching the three groups described under *30-Second Quick Setup*:

- **`resolved`** — keys this tenant **already has a value for** (whether it came from a platform default, a profile, or your own file).
- **`declared`** — keys the platform **recognises but assigns no value to** (group 1). These **take effect the moment you set one, and stay silent with no error message if you do not** — so this section is effectively the list of *protections you are currently going without*.

⚠️ Group 2 (`<base>_critical`) appears in **neither** section: `_critical` keys never enter the declared list, so they are absent from `declared`, and until you set one there is no value, so they are absent from `resolved` too. Spot that group by hand — the key is spelled `X_critical` and `defaults:` carries `X`. If a key is **none of the three** (not in `resolved`, not in `declared`, and not an `X_critical` whose base has a default), it is group 3: the base has not been supplied yet, so your platform operator has to add it before you can set anything.

### Preview Change Impact

```bash
# Compare before/after blast radius
python3 scripts/tools/ops/config_diff.py \
  --old-dir conf.d.baseline --new-dir conf.d/
```

### Check Alert Quality (v2.1.0)

```bash
# Check your tenant's alert quality for noise (flapping) / stale (idle) issues
da-tools alert-quality --prometheus http://localhost:9090 --tenant <your-tenant-id>
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
A: Run the command under *Self-Service Verification › View Inheritance Chain* above — its `resolved` / `declared` sections are the answer, and that section also explains why a `<base>_critical` key shows up in neither of them. Per-key units and suggested starting points live in the header comments of each Rule Pack YAML.

> 💡 **First time going live?** Use [Onboarding Checklist](https://vencil.github.io/Dynamic-Alerting-Integrations/assets/jsx-loader.html?component=../interactive/tools/onboarding-checklist.jsx) for a complete step-by-step guide, or start with the [interactive setup wizard](https://vencil.github.io/Dynamic-Alerting-Integrations/assets/jsx-loader.html?component=../getting-started/wizard.jsx). Want to see the complete platform in action? [Platform Demo](https://vencil.github.io/Dynamic-Alerting-Integrations/assets/jsx-loader.html?component=../interactive/tools/platform-demo.jsx) demonstrates real scenarios. See all tools at [Interactive Tools Hub](https://vencil.github.io/Dynamic-Alerting-Integrations/). For enterprise intranet deployment, use the `da-portal` Docker image: `docker run -p 8080:80 ghcr.io/vencil/da-portal` ([deployment guide](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/components/da-portal/README.md)).

## Related Resources

| Resource | Relevance |
|----------|-----------|
| ["Tenant Quick Start Guide"](for-tenants.en.md) | ⭐⭐⭐ |
| ["Migration Guide — From Traditional Monitoring to Dynamic Alerting Platform"](../migration-guide.en.md) | ⭐⭐ |
| ["Domain Expert (DBA) Quick Start Guide"](for-domain-experts.en.md) | ⭐⭐ |
| ["Platform Engineer Quick Start Guide"](for-platform-engineers.en.md) | ⭐⭐ |
