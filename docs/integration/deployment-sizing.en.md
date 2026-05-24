---
title: "Deployment Sizing Guide"
tags: [deployment, sizing, memory, performance]
audience: [platform-engineer, sre, devops]
version: v2.8.1
lang: en
---
# Deployment Sizing Guide

> **Language / 語言：** **English (Current)** | [中文](deployment-sizing.md)

> **Audience**: Platform Engineers, SREs, DevOps
> **Prerequisite**: [GitOps Deployment Guide](gitops-deployment.en.md)

---

## Overview

This guide helps you size the `threshold-exporter` memory limit and decide whether you need any of the memory-tuning levers. The one-line takeaway:

> **`threshold-exporter` memory exhibits a slow high-water creep proportional to the number of reloads — but it is not a leak.** The default config (128Mi limit) is more than enough for ≤1000 tenants; only abnormally high reload frequency warrants the GOMEMLIMIT or FreeOSMemory levers.

Read the [memory behavior](#memory-behavior-high-water-creep-under-reload-pressure) section before tuning, so you don't mistake normal GC pacing for a leak and over-provision.

## Memory behavior: high-water creep under reload pressure

Every config reload runs one scan + YAML parse + merge cycle, producing short-lived heap allocations. Under **sustained reload pressure** the Go runtime tends to hold the reclaimed idle pages (a high-water mark) rather than aggressively returning them to the OS:

- `go_memstats_sys_bytes` (total OS-held memory) and `go_memstats_heap_idle_bytes` (held but unused) creep upward.
- `go_memstats_heap_objects` (live object count) stays **flat** — meaning there is no reference-held leak.

This is **Go runtime GC pacing, not a code leak**. Stress testing confirms it: setting `GOGC` to 20 (more aggressive GC) makes the creep nearly vanish. Raw soak data and diagnosis live in the internal [Benchmark Playbook §Memory characteristics under reload pressure](../internal/benchmark-playbook.md#memory-characteristics-under-reload-pressure-459).

> ⚠️ This only shows up under a **production-shape (hundreds-to-thousands of tenants)** working set. A small test environment (single-digit tenants) has too small a working set to trigger high-water retention, so "I didn't see growth in staging" does **not** mean production won't grow.

## Capacity planning: reload-interval × uptime

Growth rate is proportional to **reload frequency**, so a rough proxy estimates it:

> **memory growth ∝ (1 / reload-interval) × uptime**

In practice, production customers change config far less often than a stress test:

| Scenario | Reload trigger frequency | Relative growth rate | Notes |
|---|---|---|---|
| Soak stress test | every 15s | **baseline (1×)** | artificial extreme, not real load |
| High-frequency GitOps | every few minutes | ~10–50× slower | busy multi-team platform |
| Typical customer | every few hours to days | ~100–1000× slower | reloads only on config change |

Worked example: the soak observed roughly 4 MiB/hour of `sys_bytes` growth; a typical customer (reloading 100× less often) sees about 1/100 of that, and the runtime's self-bounding usually converges rather than growing unbounded. **Bottom line**: the vast majority of deployments need no tuning at all.

## Sizing the container memory limit

Using 1000 tenants under aggressive soak as an upper bound, `sys_bytes` peaks around 40 MiB. Recommended:

| Tenant scale | `requests.memory` | `limits.memory` | Notes |
|---|---|---|---|
| ≤ 1000 | 64Mi | **128Mi** (chart default) | ~3× headroom absorbs creep + scrape spikes |
| 1000–5000 | 128Mi | 256Mi | scan/parse working set grows linearly with tenants |
| 5000–10000 | 256Mi | 512Mi | pair with a [sharding assessment](../internal/benchmark-playbook.md#sharding-決策建議empirical-not-extrapolated) |

Rule of thumb: set `limits.memory` to **at least 3×** the steady-state `heap_inuse` to absorb high-water creep and concurrent scrapes, avoiding OOMKilled.

## Memory-tuning levers

All three levers are **off by default** (they don't change existing Go runtime behavior). Order of use: GOMEMLIMIT first, then add FreeOSMemory if that isn't enough, and only finally consider reload-interval.

### GOMEMLIMIT (recommended soft ceiling)

The Go 1.19+ runtime natively reads the `GOMEMLIMIT` environment variable as a **soft heap ceiling**: as the heap approaches the limit, GC becomes more aggressive and returns idle pages to the OS sooner. This is the preferred way to bound the creep.

```yaml
# values.yaml
exporter:
  goMemLimit: "96MiB"   # good starting point ≈ limits.memory × 0.75
```

Sizing rule: keep it **above** steady-state `heap_inuse` and **below** the container `limits.memory` (leave GC room to react). The chart injects it as the container `GOMEMLIMIT` env, and the startup log prints the effective value for confirmation.

### `-free-os-mem-after-reload` (secondary explicit scavenge)

After each reload completes, explicitly call `runtime/debug.FreeOSMemory()`, forcing a GC plus an immediate scavenge that returns idle heap to the OS. The cost is one extra STW GC per reload.

```yaml
# values.yaml
exporter:
  freeOsMemAfterReload: true
```

Turn this on **only** when "GOMEMLIMIT still doesn't bound the creep" **and** "reload frequency is low enough that the per-reload GC cost is negligible". Not recommended for high-frequency reload environments (STW GC cost accumulates).

### reload-interval

Raising `reloadInterval` lowers reload frequency and gives GC more time to converge, but it delays config changes taking effect. The chart **default of 30s is unchanged**; if your config changes are already sparse, a customer can raise it (e.g. `5m`) without changing the chart default.

```yaml
# values.yaml
exporter:
  reloadInterval: "5m"   # customers with sparse config changes can raise this
```

## Monitoring signals

| Signal | Source | Purpose |
|---|---|---|
| `go_memstats_sys_bytes` | Go runtime | RSS proxy; watch the overall growth trend |
| `go_memstats_heap_released_bytes` | Go runtime | **direct return-to-OS signal**; rises when GOMEMLIMIT / FreeOSMemory take effect |
| `go_memstats_heap_objects` | Go runtime | flat = no leak; only rising is a genuine leak warning |
| `da_config_free_os_memory_total` | exporter | +1 per reload when the FreeOSMemory lever is on; 0 by default |

Suggested alert: fire when `sys_bytes` approaches 90% of `limits.memory` (an early warning before OOMKilled), rather than on absolute-value growth alone.

## Decision quick-reference

| Symptom | Action |
|---|---|
| `heap_objects` flat, `sys_bytes` creeping | ✅ normal GC pacing, **no action needed** |
| `heap_objects` continuously rising | ❌ suspected real leak — open an issue for profiling, out of scope here |
| High reload frequency + `sys_bytes` near limit | set `goMemLimit` ≈ `limits.memory` × 0.75 |
| GOMEMLIMIT set but still near limit + sparse reloads | also enable `freeOsMemAfterReload: true` |
| Config changes are already sparse | raise `reloadInterval` (e.g. `5m`) |
