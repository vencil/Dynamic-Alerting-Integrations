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
2. **Go deeper on your main interface** → [da-portal QUICKSTART](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/components/da-portal/QUICKSTART.md) (in-browser threshold calculator, custom-alert management / Saved Views).
3. **Configure your tenant for real** → the "30-Second Quick Setup" and common operations below.

## Three Things You Need to Know

**1. Your monitoring is already active.** The platform ships with Rule Packs covering MariaDB, PostgreSQL, Redis, MongoDB, Elasticsearch, Kafka, and more. As long as your exporter is running, alert rules are in effect.

**2. You only manage one YAML file (or use the Web UI).** All of your own customization lives in `conf.d/<tenant>.yaml` — thresholds, notification routing, maintenance windows, everything. If your Platform Team has deployed the Self-Service Portal (tenant-manager UI), you can edit custom alerts directly in the browser; for maintenance / silent mode the UI generates YAML fragments you paste into your tenant file; thresholds and routing are still edited in YAML.

**3. Keys that have a default may not need changing.** Unless your business scenario requires stricter or more relaxed thresholds, the defaults in `_defaults.yaml` are sufficient; but some keys deliberately have no platform default and stay silent until you set them — see the ⚠️ below.

## 30-Second Quick Setup

The minimal viable tenant config is just two lines:

```yaml
# conf.d/my-tenant.yaml
tenants:
  my-tenant: {}
```

This gives your tenant **whatever default thresholds the platform currently supplies**, with no custom routing (alerts go to Alertmanager's default receiver).

⚠️ **"Default thresholds" is not "all thresholds" — and "you can set it" is not "the alert is live."** The platform gives a value for only a handful of keys, built into the readers (the Helm chart's `thresholdConfig.defaults` is empty by default and does not write them again). Every other key falls into one of **two** groups, and neither needs the platform operator to act first:

- **Keys the platform ships no value for** (35 today) — the readers' built-in layer declares them, valued off: put a value in your own `conf.d/<tenant>.yaml` and it takes effect, with no entry under `defaults:` or `optional_overrides:` in the root `_defaults.yaml` ([#4163](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4163)). But the platform deliberately asserts no default for them, so **leaving one unset is silent, and by default there is no error message either** (you can subscribe to a notice: see `<Pack>ThresholdNotConfigured` below) — that is the design, not a missing default: thresholds like Oracle's PGA share of `PGA_AGGREGATE_LIMIT`, DB2 deadlocks per 5 minutes or MariaDB's `mysql_threads_running` (a healthy running-thread count depends on the host's CPU core count, #3778) can only be calibrated against your own baseline, and a platform-chosen number can be wrong in **either direction** — it may fire on your normal operation, or sit so loose that a real fault never trips it. ⚠️ Do not read this as "the platform's numbers run low": every counter-example measured so far is a false alarm, but no measured miss is not the same as no miss. Keys the platform's own reference library has actually measured a counter-example for are annotated per key in that rule pack's header (`rule-packs/rule-pack-<pack>.yaml`) (`⚠ 參考庫實測反例`); no annotation means nothing was measured.
- **`<base>_critical` keys** (34 today) — **you can set these today**, without writing the base first: the built-in layer gives the base the platform's value or off, and a `<base>_critical` written alone is served as a critical alert row. A write through the Portal / Tenant API is not rejected, and a direct GitOps push works too.

When you leave a key of the first group unset: no threshold series ⇒ the rule matches nothing ⇒ empty result — that is the config-driven join itself.

**To learn which alerts are silent for this reason, subscribe to the notice** ([#1175](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1175); off by default). It needs `unset_thresholds` under `state_filters:` in the root `_defaults.yaml`: the current chart, `scaffold-tenant` and `da-tools init` ship it, but an existing deployment must have the platform add it first, or a tenant's subscription has no effect. Then write `_state_unset_thresholds: "enable"` in your tenant file (not needed if the platform set `unset_thresholds` to `default_state: "enable"` under `state_filters:`; write `"disable"` to opt out). Then, for a key the platform ships no value for and that serves you no threshold, while that pack's exporter is answering for you, an info alert `<Pack>ThresholdNotConfigured` (for example `RedisThresholdNotConfigured`) fires after 30 minutes, with the key in its `key` label; write a threshold to arm it, with no declaration by the platform first. Write `"disable"` for a key whose alert you do not want, which ends that notice. A value that is written but never served (a typo, a value that does not read) does not count as set; a `<key>_critical` alone does. It does not fire in maintenance mode.

**Unsure which group a key is in? Do not eyeball it** — the command under *Self-Service Verification › View Inheritance Chain* below prints the answer already split into sections.

`_defaults.yaml` normally sits in the **same `conf.d/` directory** you edit `<tenant>.yaml` in (CODEOWNERS restricts who may **change** it, not who may **read** it), so in most setups you can read it and run that command yourself. Only when your organisation keeps the platform file in a source you cannot reach (a multi-team sharded layout, see the [GitOps CI/CD Integration Guide](../scenarios/gitops-ci-integration.en.md)) do you need to ask your platform operator for it — **the whole root `_defaults.yaml`** (its `defaults:` holds only the platform side's overrides and opt-outs; the rule-pack thresholds are built into the readers, not written in the file).

## Common Operations

### Adjusting Thresholds

```yaml
tenants:
  my-tenant:
    mysql_connection_usage_percent: "70"           # Connection usage warning threshold (% of max_connections, default: 80)
    mysql_connection_usage_percent_critical: "95"  # Connection usage critical threshold (%)
    container_cpu: "60"           # Container CPU warning threshold (default: 70)
```

Tri-state design (**for keys that have a platform default**): each metric can be **custom value**, **omitted** (use default), or `"disable"` (suppress alert).

⚠️ Group 1 above (keys the platform ships no value for) has no platform default to fall back to, so **omitting one means no value and therefore no series** — not "use the default". That tier really has only two states: set it, or leave it silent.

> 💡 **Interactive Tools** — Want to check your YAML syntax and structure in real-time (syntax only — the exporter is the authority on what values mean)? Try [YAML Playground](https://vencil.github.io/Dynamic-Alerting-Integrations/assets/jsx-loader.html?component=../interactive/tools/playground.jsx). Unsure how to set thresholds? Use [Threshold Calculator](https://vencil.github.io/Dynamic-Alerting-Integrations/assets/jsx-loader.html?component=../interactive/tools/threshold-calculator.jsx) to derive values from p50/p90/p99.

```yaml
tenants:
  my-tenant:
    mysql_connection_usage_percent: "70"       # Custom
    # mysql_replication_lag omitted → uses _defaults.yaml value
    container_memory: "disable"   # Suppress this alert
```

### Adding Custom Alerts (no PromQL)

Beyond the platform's built-in alerts, you can add your own. **The easiest way: use the Self-Service Portal's Recipe Builder** — pick a recipe (threshold / rate / ratio / forecast …), fill in the parameters, and it generates the config for you, with no PromQL to write.

Advanced users can also declare them directly under `_custom_alerts` in `conf.d/<tenant>.yaml`; see [threshold-exporter §Custom Alerts](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/components/threshold-exporter/README.md#45-自訂告警-_custom_alerts).

> **Three footguns to know**:
> 1. Your metric **must carry a `tenant` label** (platform rules join `on(tenant)`), or the rule **silently never fires** — multi-tenant scrapes usually inject it via relabel.
> 2. The `absence` recipe **fires immediately** for a metric that **never appeared** (the safety guard isn't shipped yet); on first onboarding, make the metric appear at least once.
> 3. `threshold` / `rate` aggregate with `max`/`sum` into one value per tenant (one per cluster when the tenant spans several clusters), which **drops sub-labels** like `device` / `instance` (the alert says "the worst one", not which one).

> **When your tenant spans several clusters**: each cluster is computed and compared against the threshold on its own, and the alert carries `da_cluster` to say which cluster it is. Recipes that sum or divide — `rate`, `ratio`, `slo_burn_rate` — therefore **no longer combine the clusters** (behaviour change: a case where only the clusters added together exceed the threshold no longer alerts). `absence` is the one exception: the metric counts as present when any cluster has it. Thresholds are still one set for the whole tenant. See [ADR-006 "One tenant in several clusters"](../adr/006-tenant-mapping-topologies.en.md).

### Setting Up Alert Notification Routing

```yaml
tenants:
  my-tenant:
    _routing:
      receiver:
        type: "slack"
        api_url: {secret: {key: slack-api-url}}
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
    mysql_connection_usage_percent: "50"       # This overrides the profile value
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
      expires: "2099-03-15T06:00:00Z"   # Auto-recover
      reason: "Planned DB migration"
```

In maintenance mode, alerts labelled `maintenance: mute` don't fire (PromQL-level suppression; threshold and custom alerts are all `mute`); cluster-survival, exporter-absent and single-instance critical down alerts (such as `PostgreSQLDown`) are `page` and keep firing — per alert see the *In maintenance* column of the [alert reference](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/rule-packs/ALERT-REFERENCE.en.md); for the scope see [config-driven §2.7](../design/config-driven.en.md#27-three-state-operational-modes). Automatically resumes after expiry.

The switch is `enabled: true` (a YAML boolean) or `target: enable`; to turn it off write `enabled: false` / `target: disable`, or `disable` as the whole value. A scalar is read only as `enable` or `disable` (and its aliases) — values such as `"yes"`, `"true"` or `"1"` do **not** turn maintenance mode on: the exporter logs a WARN and tenant-api refuses them. An object with `recurring:` and no switch turns maintenance mode on inside each window only (the threshold-exporter evaluates the windows; `mute` alerts do not fire inside them, [#3639](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3639)). With no switch but an `expires`, maintenance mode is on from now until `expires`, and inside the windows too (`expires` does not limit them). A window is written `- weekdays: [sun]` plus `window: "02:00-06:00"`; the old `cron` + `duration` form is refused: the whole value is not read and maintenance is **not on**. The full reading is in [config-driven §2.7](../design/config-driven.en.md#27-three-state-operational-modes) ([#3620](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3620)).

> **Timezone:** The `expires` field uses RFC 3339 format (with timezone). Scheduled thresholds' `window` and recurring maintenance's `weekdays` / `window` both use **UTC timezone**; a recurring window that crosses midnight belongs to the day it starts on.

### Silencing Specific Severities

```yaml
tenants:
  my-tenant:
    _silent_mode:
      target: "warning"                  # Silence only warnings
      expires: "2099-03-13T12:00:00Z"
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

Every tenant alert (thresholds, down, exporter-absent, sentinels, custom alerts) carries these three values in its annotations, so they appear in notifications; without `_metadata` the three values are empty strings and the alert still fires.

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

> Running `validate_config.py` directly needs da-guard: run `make da-guard-build` and set `DA_GUARD_BINARY=.build/da-guard` first (the da-tools image ships it); see [validate-config's prerequisite](../cli-reference.en.md#validate-config).

### View Inheritance Chain

```bash
# See the final resolved thresholds for your tenant
python3 scripts/tools/ops/diagnose.py my-tenant \
  --config-dir conf.d/ --show-inheritance
```

> This needs da-guard too (`DA_GUARD_BINARY`, as above): the values are what the exporter serves (`/metrics`), not a Python reading; see [diagnose](../cli-reference.en.md#diagnose).

The output has two sections, matching the two groups described under *30-Second Quick Setup*:

- **`resolved`** — keys this tenant **already has a value for** (whether it came from a platform default, a profile, or your own file).
- **`declared`** — keys the platform **recognises but assigns no value to** and that you have no value for yet (group 1: those the root lists under `optional_overrides:`, plus those the built-in layer declares, valued off). These **take effect the moment you set one, and stay silent if you do not** (with no error message by default; subscribe to `_state_unset_thresholds`, above, for a notice) — so this section is effectively the list of *protections you are currently going without*.

⚠️ Group 2 (`<base>_critical`) appears in **neither** section until you set one: `_critical` keys do not enter the declared list, so they are absent from `declared`, and with no value they are absent from `resolved`. There is nothing to work out for that group: every `X_critical` can be set today (`X` is in `resolved` or in `declared`).

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
