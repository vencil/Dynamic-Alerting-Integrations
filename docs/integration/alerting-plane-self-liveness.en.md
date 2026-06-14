---
title: "Alerting-Plane Self-Liveness — Watchdog + External Dead-Man's-Switch (Operator Guide)"
tags: [operator, alerting, observability, prometheus, alertmanager, watchdog]
audience: [platform-engineer]
version: v2.9.0
lang: en
---

# Alerting-Plane Self-Liveness — Watchdog + External Dead-Man's-Switch (Operator Guide)

> **Language / 語言：** [中文](./alerting-plane-self-liveness.md) | **English (Current)**

> **Audience**: the **Operator / SRE who deploys and runs this platform**. This is the setup-and-operations contract that lets "Prometheus / Alertmanager dying" be noticed from the outside — you need an out-of-cluster monitoring point, the configuration below, and discipline about the silence/inhibit no-go zones.
>
> Design rationale: [ADR-025 Alerting-Plane Self-Liveness](../adr/025-alerting-plane-self-liveness.en.md) (D1 heartbeat / D2 air-gapped passive probe / D3 HA boundary). For tenant-alert receiver / Secret / inhibit configuration, see the [Operator Alertmanager Integration Guide](operator-alertmanager-integration.en.md).

## What it solves

The platform ships a single Prometheus and a single Alertmanager, and **every platform alert is evaluated by that one Prometheus** — so when it dies, alerts **stop silently** and nobody is notified.

The fix: a **permanently-firing** `Watchdog` alert (`expr: vector(1)`) routed through a **top-priority, zero-aggregation, fixed-cadence** Alertmanager route to a dead-man's-switch (DMS) **outside** the platform. **The trick is to invert it**: the external service does not "alarm on receipt" — it "**expects a ping every few minutes; if it stops arriving, page a human**." That way, whether Prometheus dies, Alertmanager dies, or the signal can't get out (firewall / certificate), the heartbeat stops and the outside notices.

| Component | Location |
|---|---|
| `Watchdog` rule + `AlertmanagerWebhookNotificationsFailing` internal companion alert | `k8s/03-monitoring/configmap-rules-platform.yaml` |
| Top-priority route (`routes[0]`) + `watchdog-heartbeat` receiver (`url_file`) | `k8s/03-monitoring/configmap-alertmanager.yaml` (the route is re-injected at index 0 by the platform's `generate_alertmanager_routes.py` on every regeneration, surviving the route-REPLACE) |
| External DMS URL (embeds a token, **secret**) | `k8s/03-monitoring/secret-watchdog-heartbeat.yaml` → mounted at `/etc/alertmanager/secrets/watchdog-heartbeat-url` |

## ① Configure the external heartbeat (required)

1. Outside the platform (somewhere that won't die with this cluster), set up a DMS / heartbeat monitor (e.g. Healthchecks.io, Better Stack, a PagerDuty heartbeat, or your own on an independent VM / cluster). Get its ingest URL (it usually embeds a token / UUID).
2. **Never inline the URL in the ConfigMap** (the embedded token would trip secret-scan and leak). Overwrite the mounted Secret instead:

   ```bash
   kubectl create secret generic watchdog-heartbeat \
     --from-literal=watchdog-heartbeat-url="https://<dms-host>/api/heartbeat/<token>" \
     -n monitoring --dry-run=client -o yaml | kubectl apply -f -
   ```

   The receiver uses `webhook_configs[].url_file` pointing at this Secret file; the file is **read at send time**, so rotating the URL later **needs no Alertmanager reload**.
   > Note: after `kubectl apply` updates the Secret, Kubernetes takes roughly **1–2 minutes** to propagate the new value into the Pod's mounted volume file (kubelet sync period + kubelet cache). During that window the heartbeat still goes to the **old** URL — this is normal; **wait for it to take effect, don't mistake it** for a misconfiguration.
3. **Empty = documented blind spot**: if the Secret stays at the placeholder, the heartbeat can't egress and `AlertmanagerWebhookNotificationsFailing` fires as a "configure me" reminder (the demo / lab default).

## ② Sizing the external TTL (the tolerance contract)

The external monitor's timeout (TTL) **must be longer than the route's `repeat_interval` (3m)**, and must absorb all of:

- **network jitter**;
- **rule-evaluation lag under extreme load** (Prometheus pod is alive but its evaluation loop falls badly behind → the heartbeat slips late);
- the **~60s cold-start blind window after a Prometheus restart** (the first post-restart ping is delayed).

Recommendation: **external TTL = 5m** (= `repeat_interval` 3m + 2m margin), and enable a **first-heartbeat grace period** on the external DMS so a platform restart doesn't false-alarm. If you change `repeat_interval`, scale the external TTL proportionally.

## ③ Silence / inhibit no-go zones (⛔ strict)

Alertmanager has **no "inhibition immunity" primitive**; `severity: none` only keeps Watchdog out of existing severity-targeted inhibits — it is **not** universal immunity. The real guarantee comes from two gates:

- **Inhibit side (mechanically enforced, built into the platform)**: no `inhibit_rules` `target_matchers` may match `alertname="Watchdog"` (including negative matching — e.g. `severity!="critical"` also matches Watchdog's `severity: none`). The platform's `generate_alertmanager_routes.py` validates this **fail-closed** over the **full base + generated merged set** on both output paths (GitOps assembly / `--apply` merge); a violation rejects generation.
  > ⚠️ **Do not** add a "`source = Watchdog` → suppress other alerts" rule: Watchdog fires permanently and has no `equal:`, so it would permanently suppress **all** non-Watchdog alerts (explicitly rejected in ADR-025).
- **Silence side (cannot be machine-enforced → discipline)**:
  - ⛔ **Never** put a Silence on `alertname="Watchdog"`.
  - ⛔ When a major incident makes you reach for a **global wildcard silence** (`.*` / `alertname=~".*"` to stem an alert storm), you **must explicitly exclude** Watchdog (add a matcher `alertname!="Watchdog"`). Otherwise the external DMS false-alarms "platform dead" exactly when you need it most.

## ④ Air-gapped environments (passive health checks)

Fully air-gapped environments (financial intranets, factory edge) can't send the heartbeat out. The fallback is the **reverse**: have an **out-of-cluster** upper-layer monitoring system **actively poll** Prometheus's and Alertmanager's health endpoints on a schedule (both ship `/-/ready` and `/-/healthy`).

⚠️ This is **not** a Kubernetes readiness / liveness probe — a failed built-in probe only restarts the Pod or removes it from the Service, which is **in-cluster** behavior with **no external alerting**, useless when the network or a whole node is gone. This means an **out-of-cluster** monitor actively pulling.

## ⑤ Troubleshooting

| Symptom | Likely cause / action |
|---|---|
| `AlertmanagerWebhookNotificationsFailing` firing | webhook egress is broken: Secret unset (still the placeholder) / invalid URL / expired token / egress firewall. Check the Secret contents and the network path to the DMS. In a demo this is expected (configure the Secret). |
| External DMS reports "no heartbeat" but the platform looks fine | Check `AlertmanagerWebhookNotificationsFailing` first: **firing** ⇒ the **heartbeat pipe is broken** (egress), not the platform; **not firing** ⇒ Prometheus / Alertmanager may actually be down (pull `/-/ready` from outside the cluster to confirm), or the external TTL is set too short (see ②). |
| After regenerating the ConfigMap, Watchdog isn't at `routes[0]` | Shouldn't happen — `generate_alertmanager_routes.py` force-injects it at index 0. If someone hand-edited the base ConfigMap's route order, re-run the generator to correct it. |

> **Known limitation (Day-2 radar)**: `AlertmanagerWebhookNotificationsFailing` watches the **global** failure count for `integration="webhook"`, and Alertmanager's metric has **no `receiver` label** by default — so **any** tenant's custom webhook receiver breaking (URL / certificate) also trips this platform alert. This is accepted for the MVP (fail-safe beats a missed alert), hence `warning` + `for: 15m` to ride out transients. **Trigger**: when the number of tenant webhooks grows enough that the false attribution gets noisy, switch to parsing the Alertmanager log via Vector to extract `receiver="watchdog-heartbeat"`-scoped failures, replacing this global metric.

## ⑥ Verification (manual, staging)

1. Confirm `Watchdog` is permanently Firing on Prometheus `/alerts`, and the external DMS is receiving the heartbeat.
2. Stop Prometheus: `kubectl scale deploy/prometheus --replicas=0 -n monitoring`.
3. Wait past the external TTL (~5m) → the external DMS should raise a "heartbeat stopped" alert.
4. Restore (`--replicas=1`) and confirm the heartbeat recovers and the DMS alert resolves.

> The end-to-end synthetic probe (automated E2E: inject a test alert from outside and verify the full Prometheus → Alertmanager → external path) is an ADR-025 **defer-with-trigger** item; the manual verification here is the interim.

## Related

- [ADR-025 Alerting-Plane Self-Liveness](../adr/025-alerting-plane-self-liveness.en.md) (design decision)
- [Operator Alertmanager Integration Guide](operator-alertmanager-integration.en.md) (tenant-alert receivers / Secrets / inhibit rules)
- [High-Availability Design](../design/high-availability.en.md) (data-plane HA — complementary, a different plane)
