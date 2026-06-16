---
title: "Synthetic-Probe Interop — verify end-to-end delivery with your own prober"
tags: [integration, alerting, synthetic-probe, alertmanager]
audience: [platform-engineer, sre]
version: v2.9.0
lang: en
---
# Synthetic-Probe Interop

> **Language / 語言：** [中文](./synthetic-probe-interop.md) | **English (Current)**

> **Audience**: Platform Engineers / SREs who already run blackbox_exporter / synthetic monitoring
> **Related**: [ADR-025 Alerting-Plane Self-Liveness](../adr/025-alerting-plane-self-liveness.en.md)

## What this is

The platform reserves a **synthetic-probe sinkhole route**: any alert carrying the label `component="synthetic-probe"` is **guaranteed** routed to a dedicated `synthetic-receiver` with `continue:false`.

This lets you use **your own existing prober** (blackbox_exporter, a home-grown synthetic monitor, a CI smoke check…) to fire a test alert that travels *through* the platform's Alertmanager, verifying the delivery chain end-to-end — and it is **zero-risk**: that test alert can **never** fall through to a human channel / page on-call.

> **Design boundary (stated honestly)**: the platform does **not** emit synthetic probes itself — this is the interop **surface** for *your* prober, not a built-in probe (that remains deferred; see ADR-025). `synthetic-receiver` is name-only (a no-op black hole) by default; point it at your own "probe-ack" endpoint, or leave it empty — the contract guarantees **isolation** (a test alert never pages), not delivery.

## The contract (guaranteed by the platform)

| Item | Value |
|---|---|
| Trigger label | `component="synthetic-probe"` |
| Route position | route index 2 (Watchdog → Custom → **synthetic-probe**, all ahead of the NOC match-all) |
| receiver | `synthetic-receiver` |
| `continue` | `false` (caught here, never overflows) |
| `group_by` | `[alertname]` (does not inherit the root `[alertname, tenant]`) |

This route is auto-injected and pinned at the front by `generate_alertmanager_routes.py` on every regen (it survives the `--apply` route-REPLACE); the hand-authored base `configmap-alertmanager.yaml` mirrors it and is guarded by the routing orchestration test.

## How to use it (your prober fires a test alert)

Hit the Alertmanager v2 API directly (replace `<alertmanager>`; in-cluster it is often `alertmanager.monitoring:9093`):

```bash
curl -sS -XPOST http://<alertmanager>:9093/api/v2/alerts \
  -H 'Content-Type: application/json' \
  -d '[{
        "labels": {
          "alertname": "SyntheticProbe",
          "component": "synthetic-probe",
          "severity": "none"
        },
        "annotations": {"summary": "synthetic probe — verifying end-to-end alert delivery"},
        "startsAt": "'"$(date -u +%Y-%m-%dT%H:%M:%SZ)"'"
      }]'
```

Or, in blackbox_exporter / your alerting rule, simply have the synthetic alert carry the `component: synthetic-probe` label — the platform handles the rest.

## Verify it lands safely (no overflow)

```bash
# It should appear under synthetic-receiver, NOT on any human channel:
amtool alert query --alertmanager.url=http://<alertmanager>:9093 alertname=SyntheticProbe
# or check the alert's receiver = synthetic-receiver in the AM UI
```

Seeing the alert hit **only** `synthetic-receiver` (`continue:false` stops it; it never reaches default / NOC) proves the platform's **blast-radius containment** — the test signal both exercises the delivery chain and can never trip a real human.
