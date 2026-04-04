---
title: "Performance Analysis & Benchmarks"
tags: [performance, benchmarks]
audience: [platform-engineer, sre]
version: v2.3.0
lang: en
---
# Performance Analysis & Benchmarks

> **Language / 語言：** **English (Current)** | [中文](benchmarks.md)

> Related docs: [Architecture](architecture-and-design.en.md) · [Testing Playbook](internal/testing-playbook.md)

---

## Vector Matching Complexity Analysis

**Traditional approach (multi-tenant hardcoded):**
```
N tenants × M alert rules = N×M independent PromQL evaluations
Complexity: O(N×M)

Example: 100 tenants, 35 alert rules
= 3,500 independent rule evaluations
```

**Dynamic approach (vector matching with `group_left`):**
```
M alert rules × 1 vector matching = M evaluations
Complexity: O(M), independent of tenant count

Example: 100 tenants, 35 alert rules
= 35 rule evaluations (regardless of tenant count)
```

## Actual Benchmark Data (Kind Cluster Measurement)

**v1.12.0 setup: 2 tenants, 237 rules (15 Rule Packs), 43 rule groups**

> v1.11.0 (13 packs) vs v1.12.0 (15 packs) comparison from Kind single-node cluster.

```
v1.12.0 (15 Rule Packs):
  Total evaluation time (per cycle): 23.2ms
  p50 per-group: 0.39ms
  p99 per-group: 4.89ms

v1.11.0 (13 Rule Packs, 5-round mean ± stddev):
  Total evaluation time (per cycle): 20.3 ± 1.9ms  (range: 17.7–22.8ms, n=5)
  p50: 1.23 ± 0.28ms per group
  p99: 6.89 ± 0.44ms per group
```

**Scalability comparison:**

| Metric | Current (2 tenants) | Traditional (100 tenants) | Dynamic (100 tenants) |
|--------|-------|-------------------|------------------|
| Alert rule count | 96 (fixed) | 9,600 (96×100) | 96 (fixed) |
| Recording rule count | 141 (normalization) | 0 (embedded in alerts) | 141 (fixed) |
| **Total rule count** | **237** | **9,600** | **237** |
| Evaluation complexity | O(M) | O(N×M) | O(M) |
| **Estimated evaluation time** | **~23ms** | **~1,100ms+** | **~23ms** |

**Conclusion:**
- Traditional approach increases evaluation time by **~48×** at 100 tenants
- Dynamic approach maintains **constant** evaluation time, linear scalability

## Empty Vector Zero-Cost

All rule packs are pre-loaded (9 at benchmark time, now expanded to 15 as of v1.8.0). Packs without deployed exporters are evaluated against empty vectors.

**Kind cluster actual measurement:**

| Rule Pack | Status | Rule Count | Evaluation Time | Notes |
|-----------|--------|-----------|-----------------|-------|
| MariaDB | ✓ Active | 7 | **2.12ms** | Has exporter |
| MongoDB | ✗ No exporter | 7 | **0.64ms** | Empty vector |
| Redis | ✗ No exporter | 7 | **0.41ms** | Empty vector |
| Elasticsearch | ✗ No exporter | 7 | **1.75ms** | Complex PromQL, still low-cost |

**Conclusion:**
- Empty vector operations are approximately O(1)
- Pre-loading unused rule packs has **negligible** overhead (< 1ms)
- When new tenants come online, all rules automatically apply, **no redeployment needed**

## Memory Efficiency

> Data below from **5 independent rounds** mean ± stddev.

```
Single threshold-exporter pod (measured):
- Heap memory: 2.4 ± 0.4MB (YAML parsing + metric generation)
- Output metrics: ~8 user_threshold series (2 tenants)
- Scrape Duration: 4.1 ± 1.2ms

× 2 HA Replicas: ~4.8MB total
+ Prometheus RSS: 142.7 ± 1.4MB (9 Rule Packs, 141 rules)
= Cluster overhead: ~148MB

vs. Traditional approach (100 tenants, 5,600 rules):
- Prometheus rules cache: ~500MB+
- Total overhead: ~600MB+ (single hub)
```

## Resource Usage Baseline

Kind single-node cluster measurements (2 tenants):

| Metric | Component | v1.11.0 (13 packs, n=5) | v1.12.0 (15 packs) | Purpose |
|--------|-----------|-------|-------|---------|
| CPU (5m avg) | Prometheus | ~0.014 ± 0.003 cores | 0.004 cores | Capacity planning |
| RSS Memory | Prometheus | 142.7 ± 1.4MB | 112.6MB | Memory budgeting |
| Heap Memory | threshold-exporter (per pod) | 2.4 ± 0.4MB | 2.2MB | Pod resource limits |
| Scrape Duration | Prometheus → exporter | 4.1 ± 1.2ms | 2.7ms | Scrape performance baseline |

**Automated collection:**

```bash
make benchmark              # Full report (human-readable)
make benchmark ARGS=--json  # JSON output (CI/CD consumption)
```

## Storage and Cardinality Analysis

**Why Cardinality Matters More Than Disk**

The performance bottleneck in Prometheus is **Active Series count**, not disk space. Each series consumes approximately 2KB of memory, and the series count directly determines: query latency, memory usage, and compaction frequency.

**Kind cluster measurements:**

| Metric | Value | Description |
|--------|-------|-------------|
| TSDB Disk Usage | 8.9 ± 0.2MB (v1.11.0) / 0.5MB (v1.12.0) | All rules and metrics included |
| Active Series Total | ~6,037 (v1.11.0) / 6,239 (v1.12.0) | Includes all exporters + recording rules |
| `user_threshold` Series | ~8 | Threshold metrics from threshold-exporter |
| Series Per Tenant (marginal) | ~4 | Marginal cost of adding 1 tenant |

**Scaling estimation formula:**

```
Marginal cost of adding N tenants:
  Series delta = N × (series per tenant)
  Memory delta ≈ Series delta × 2KB

Example (100 tenants):
  user_threshold series = 100 × 4 = 400
  Memory delta ≈ (400 - 8) × 2KB ≈ 0.8MB
  Total series ≈ 6,239 - 8 + 400 = 6,631
```

**Conclusion:** The dynamic architecture has minimal series growth per tenant (~4 series each). 100 tenants add only ~0.8MB of memory. v1.12.0 added JVM + Nginx Rule Packs (+96 rules), active series increased by only ~200 (from 6,037 to 6,239), confirming that Rule Pack expansion has controllable series overhead.

## Under-Load Benchmark Mode

v0.13.0 added the `--under-load` mode, which validates platform scalability under synthetic tenant load. Idle-state benchmarks only measure performance at rest; under-load mode simulates real multi-tenant environments.

**Test methodology:**
```bash
make benchmark ARGS="--under-load --tenants 1000"
```

1. **Synthetic tenant generation**: Dynamically creates N synthetic tenant configurations (scalar + mixed + night-window combinations)
2. **ConfigMap patch**: Injects synthetic configurations into the `threshold-config` ConfigMap
3. **Measurement dimensions**:
   - **Reload Latency**: Time from ConfigMap change to exporter reload completion
   - **Memory Delta**: RSS memory change after adding N tenants
   - **Scrape Duration**: Prometheus scrape time for threshold-exporter
   - **Evaluation Time**: Recording rules + Alert rules evaluation time
4. **Cleanup**: Automatically removes synthetic tenants, restoring original state

**Go Micro-Benchmark:**

`config_bench_test.go` provides precise Go-level performance measurement (Intel Core 7 240H, `-count=5` median):

**v1.12.0 (with Tenant Profiles support):**

| Benchmark | ns/op (median) | B/op | allocs/op |
|-----------|------:|-----:|----------:|
| Resolve_10Tenants_Scalar | 12,209 | 26,488 | 61 |
| Resolve_100Tenants_Scalar | 100,400 | 202,777 | 520 |
| Resolve_1000Tenants_Scalar | 1,951,206 | 3,848,574 | 5,039 |
| ResolveAt_10Tenants_Mixed | 34,048 | 40,052 | 271 |
| ResolveAt_100Tenants_Mixed | 405,797 | 462,636 | 2,622 |
| ResolveAt_1000Tenants_Mixed | 5,337,575 | 5,258,548 | 26,056 |
| ResolveAt_NightWindow_1000 | 5,404,213 | 5,223,925 | 25,056 |
| ResolveSilentModes_1000 | 86,700 | 186,086 | 10 |

<details>
<summary>v1.11.0 comparison data</summary>

| Benchmark | ns/op (median) | ns/op (stddev) | B/op | allocs/op |
|-----------|------:|------:|-----:|----------:|
| Resolve_10Tenants_Scalar | 11,570 | 237 | 26,032 | 58 |
| Resolve_100Tenants_Scalar | 107,346 | 4,315 | 196,080 | 511 |
| Resolve_1000Tenants_Scalar | 2,215,080 | 113,589 | 3,739,792 | 5,019 |
| ResolveAt_10Tenants_Mixed | 39,487 | 1,720 | 39,491 | 268 |
| ResolveAt_100Tenants_Mixed | 419,960 | 18,120 | 454,366 | 2,612 |
| ResolveAt_1000Tenants_Mixed | 4,882,962 | 105,810 | 5,160,416 | 26,038 |
| ResolveAt_NightWindow_1000 | 4,887,959 | 123,943 | 5,123,590 | 25,037 |

</details>

**Conclusion:** v1.12.0 with `applyProfiles()` maintains comparable performance (Scalar 1000 tenants: 1.95ms vs 2.22ms), Mixed slightly increased (5.34ms vs 4.88ms). New `ResolveSilentModes_1000` benchmark (86µs/1000 tenants). 10→100→1000 tenants scale linearly, full resolve for 1000 tenants stays under 5.5ms.

> **Relationship to [Actual Benchmark Data](#actual-benchmark-data-kind-cluster-measurement):** [Actual Benchmark Data](#actual-benchmark-data-kind-cluster-measurement) measures **Prometheus rule evaluation** — since rule count is fixed at O(M), evaluation time does not grow with tenant count (2 tenants ~20ms ≈ 100 tenants ~20ms). This section measures **threshold-exporter config resolution** — each additional tenant adds one more config to resolve, so the cost is O(N) linear growth. The two are complementary: the platform's most critical bottleneck (rule evaluation) remains constant, while the secondary cost (config resolution) grows linearly but stays at ~5ms even at 1000 tenants — well below Prometheus's 15-second scrape interval and negligible in end-to-end performance.

## Rule Evaluation Scaling Curve

Measures the marginal impact of Rule Pack count on Prometheus rule evaluation time. By progressively removing Rule Packs (9→6→3) and measuring `prometheus_rule_group_last_duration_seconds`, we can observe whether evaluation cost grows linearly.

**Methodology:**
```bash
make benchmark ARGS="--scaling-curve"
```

1. **Tier 3 (9 packs)**: Full state (mariadb, kubernetes, redis, mongodb, elasticsearch, oracle, db2, clickhouse, platform)
2. **Tier 2 (6 packs)**: Remove oracle, db2, clickhouse
3. **Tier 1 (3 packs)**: Keep only mariadb, kubernetes, platform

Each tier waits for at least 2 Prometheus evaluation cycles before sampling. All Rule Packs are automatically restored after the test.

**Kind cluster measurement:**

| Rule Packs | Rule Groups | Total Rules | Eval Time (median) | Range | Version |
|------------|-------------|-------------|-----------|-------|---------|
| 3          | 9           | 34          | 7.7ms     | 3.3–15.3ms | v1.11.0 |
| 6          | 18          | 85          | 17.3ms    | 14.3–18.6ms | v1.11.0 |
| 9          | 27          | 141         | 22.7ms    | 8.7–26.0ms | v1.11.0 |
| **15**     | **43**      | **237**     | **23.2ms** | — | **v1.12.0** |

> **Measurement note:** v1.11.0 data from 3-round median (each round: remove Rule Packs → restart Prometheus → stabilize → sample). v1.12.0 data from idle-state measurement (all 15 packs mounted).

**Conclusion:** From 3→9→15 Rule Packs, eval time grows from 7.7→22.7→23.2ms. 9→15 packs (+96 rules) adds only 0.5ms to eval time, because the new JVM/Nginx Rule Packs trigger [Empty Vector Zero-Cost](#empty-vector-zero-cost) with no matching exporter data. Average eval time per group (23.2ms / 43 groups = 0.54ms) remains stable. Projected Volume horizontal scalability confirmed.

## Route Generation Scaling (Alertmanager Route Output Performance)

`generate_alertmanager_routes.py` converts all tenant YAML into Alertmanager route + receiver + inhibit_rules fragments. As tenant count grows, the output route tree grows linearly. This benchmark measures route generation wall time, confirming that CI pipeline and `--apply` responsiveness are not bottlenecked by tenant scale.

**Test method:**
```bash
make benchmark ARGS="--routing-bench --tenants 100"
```

1. Generate N synthetic tenant configs (cycling through 6 receiver types, with severity_dedup and routing overrides every 5th tenant)
2. Run `generate_alertmanager_routes.py --dry-run` 5 times per N (2, 10, 50, 100), report median wall time
3. Record output YAML line count, route count, and inhibit rule count

**Measured data:**

| Tenants | Wall Time (v1.11.0) | Wall Time (v1.12.0) | Output Lines | Routes | Inhibit Rules |
|---------|-------------------|-------------------|--------------|--------|---------------|
| 2       | 94ms              | 181ms             | 72           | 3      | 2             |
| 10      | 118ms             | 196ms             | 209          | 8      | 10            |
| 50      | 245ms             | 248ms             | 994          | 41     | 50            |
| 100     | 298ms             | 327ms             | 1,943        | 80     | 100           |
| 200     | 397ms             | —                 | 3,884        | 161    | 200           |

> **Synthetic tenant spec:** Cycling through 6 receiver types (webhook/email/slack/teams/rocketchat/pagerduty), all tenants with `_severity_dedup` enabled, every 5th tenant with 1 routing override. Wall time includes Python startup + YAML loading + route generation. v1.11.0 is 10-round median, v1.12.0 is single measurement.

**Conclusion:** Base overhead ~80–180ms (Python startup + import), then ~+150–200ms per additional 100 tenants. 100 tenants under 330ms, well within CI pipeline tolerance (seconds). Output lines scale strictly linearly (~19 lines/tenant), inhibit rules = tenant count (1 severity dedup rule per dedup-enabled tenant).

## Alertmanager Notification Performance

Measures Alertmanager runtime performance under dynamic routing configuration, focusing on inhibit rule evaluation and notification latency.

**Test method:**
```bash
make benchmark ARGS="--alertmanager-bench"
```

Collects metrics from Prometheus and Alertmanager API:

| Metric | Source | Description |
|--------|--------|-------------|
| Notification Latency p99 | `alertmanager_notification_latency_seconds` | 99th percentile from alert receipt to notification dispatch |
| Alerts Received (5m) | `alertmanager_alerts_received_total` | Alerts received in last 5 minutes |
| Notifications Sent (5m) | `alertmanager_notifications_total` | Successful notifications in last 5 minutes |
| Notifications Failed (5m) | `alertmanager_notifications_failed_total` | Failed notifications |
| Inhibited Alerts | `/api/v2/alerts` | Currently inhibited alerts (severity dedup + enforced routing) |
| Active Inhibit Rules | `/api/v2/status` | Total inhibit rules in configuration |

**Kind cluster idle-state measurements (2 tenants, 3 inhibit rules):**

| Metric | Value | Notes |
|--------|-------|-------|
| Active Inhibit Rules | 3 | 2 severity dedup (per-tenant) + 1 default |
| Active Alerts | 1 | Steady-state sentinel alert |
| Inhibited Alerts | 0 | No simultaneous warning+critical in idle state |
| Notification Latency p99 | N/A | No notification activity in idle state (requires `--under-load` to trigger alerts) |

> **Note:** In idle state, Alertmanager has no notification activity, so the notification latency histogram is empty. Full notification latency measurement requires `make demo-full` (composite load → trigger alerts → observe latency) or `--under-load` mode.

**Key insight:** The inhibited/received ratio reflects severity dedup effectiveness. During normal operations, when both warning + critical fire simultaneously for a dedup-enabled tenant-metric_group pair, the warning should be inhibited. The 3 inhibit rules (2 tenants × 1 severity dedup + 1 default) have negligible impact on Alertmanager route matching performance.

## Config Reload E2E Latency

Measures end-to-end latency for Alertmanager configuration changes to take effect — the time from "tenant changes routing settings" to "new routes are active".

**Test method:**
```bash
make benchmark ARGS="--reload-bench"
```

**Measured path:**
```
Tenant YAML change
  → generate_alertmanager_routes.py --apply
    → kubectl patch ConfigMap
      → configmap-reload sidecar detects file change
        → POST /-/reload
          → New routes active
```

**Kind cluster measured results (5 rounds, median):**

| Metric | Value (median) | Description |
|--------|---------------|-------------|
| `/-/reload` API | **0.3ms** | Alertmanager's own config reload (sub-millisecond) |
| `--apply` E2E | **763ms** | Full path: route generation + `kubectl patch` + `/-/reload` |

**`--apply` E2E 5-round breakdown:** 676ms, 707ms, **763ms**, 858ms, 956ms

**Component analysis:**
- Route generation (2 tenants): ~94ms (from [Route Generation Scaling](#route-generation-scaling-alertmanager-route-output-performance) data)
- `kubectl patch` ConfigMap + API server response: ~500–700ms
- `/-/reload` API: ~0.3ms
- Sum consistent with measured total (~763ms)

> **configmap-reload sidecar note:** The sidecar watches Projected Volume **file content changes**, not ConfigMap annotations. `--apply` mode directly updates ConfigMap `data` section + triggers `/-/reload`, so it does not depend on the sidecar's polling interval. If only annotations are modified without changing data, the sidecar will not detect the change.

**Conclusion:** The full "tenant changes routing → Alertmanager active" path completes in ~760ms (sub-second) on Kind. The bottleneck is kubectl API server interaction (~600ms), not route generation (~94ms) or Alertmanager reload (<1ms). In production environments with dedicated etcd, expect E2E < 500ms.

## Incremental Hot-Reload Performance (v2.1.0)

v2.1.0 introduced a per-file SHA-256 index + parsed config cache for incremental reload. The following Go micro-benchmarks measure incremental vs full reload performance (`config_bench_test.go`, `-count=3` median).

**Environment:** Dev Container (Intel Core 7 240H), each tenant with 8 metric thresholds (including scheduled overrides).

> **v2.1.0 optimizations:** (1) Removed `Resolve()` calls from reload path (replaced with `logConfigStats` direct counting); (2) **mtime guard** — `scanDirFileHashes` uses `DirEntry.Info()` mtime+size as first-level cache (eliminates per-file `os.Stat` calls), skipping `os.ReadFile` + SHA-256 when unchanged; (3) **incremental merge** — when only tenant files change, patches merged config directly instead of O(N) `mergePartialConfigs`; (4) **byte cache** — scan phase caches read `[]byte` data, reused by `fullDirLoad`/`IncrementalLoad` Phase 3 to eliminate double disk reads.

**100 Tenants (cold mtime†):**

| Benchmark | ns/op (median) | B/op | allocs/op | Description |
|-----------|---------------:|-----:|----------:|-------------|
| `FullDirLoad_100` | 3,244,752 | 1,888,152 | 21,527 | Baseline: full YAML parsing of 100 files |
| `IncrementalLoad_100_NoChange` | 546,230 | 175,565 | 1,443 | All hashes hit, zero parsing (cold mtime†) |
| `IncrementalLoad_100_OneFileChanged` | 628,194 | 199,572 | 1,565 | Typical case: re-parse only 1 changed file |
| `ScanDirFileHashes_100` | 530,564 | 175,543 | 1,443 | Hash scan (cold mtime†) |
| `ScanDirFileHashes_100_MtimeGuard` | 128,801 | 71,009 | 635 | **Mtime guard hit: stat-only (4.1×)** |
| `MergePartialConfigs_100` | 52,907 | 58,488 | 209 | Cache merge into global config |

**1000 Tenants:**

| Benchmark | ns/op (median) | B/op | allocs/op | Description |
|-----------|---------------:|-----:|----------:|-------------|
| `FullDirLoad_1000` | 34,857,623 | 18,791,153 | 213,258 | Baseline: full YAML parsing of 1000 files |
| `IncrementalLoad_1000_NoChange` | 6,913,622 | 1,786,217 | 14,059 | All hashes hit (cold mtime†) |
| `IncrementalLoad_1000_NoChange_MtimeGuard` | 1,470,214 | 782,112 | 6,047 | **Mtime guard hit (4.7×)** |
| `IncrementalLoad_1000_OneFileChanged` | 6,862,339 | 2,017,520 | 14,187 | Re-parse 1 file + incremental merge |
| `ScanDirFileHashes_1000` | 6,199,982 | 1,785,278 | 14,058 | Hash scan (cold mtime†) |
| `ScanDirFileHashes_1000_MtimeGuard` | 1,440,053 | 749,421 | 6,047 | **Mtime guard hit (4.3×)** |
| `MergePartialConfigs_1000` | 702,822 | 599,403 | 2,011 | Cache merge (1000 partial configs) |

† "Cold mtime" = files just created within 2 seconds; mtime guard does not activate (safety window). **In production where polling interval ≥ 10s, the mtime guard always hits.**

**Key Observations:**

- **Mtime guard effect (production scenario)**: `NoChange_1000` drops from 6.9ms to **1.5ms** (**4.7×**); scan cost reduced from O(N×ReadFile+SHA256) to O(N×Stat)
- **Incremental merge effect**: `OneFileChanged_1000` drops from 7.4ms to **6.9ms**, saving ~700µs `mergePartialConfigs` by patching tenant entries directly
- **100 tenants**: `NoChange` (546µs) is **5.9× faster** than `FullDirLoad` (3,245µs); `OneFileChanged` (628µs) is **5.2× faster**
- **1000 tenants**: `NoChange` (1.5ms mtime) is **23.7× faster** than `FullDirLoad` (34.9ms); `OneFileChanged` (6.9ms) is **5.1× faster**
- **Scaling**: 100→1000 (×10), `FullDirLoad` from 3.2ms→34.9ms (×10.9); mtime NoChange from ~129µs→1.5ms (×11.6)
- **Cost breakdown (1000T OneFileChanged = 6.9ms)**: scan 6.2ms (1 changed + 999 stat) + 1 file re-parse ~0.2ms + incremental merge ~0.5ms
- **v2.1.0 → v2.1.0 total speedup**: `OneFileChanged_1000` 10.5ms→6.9ms (**-34%**), `NoChange_1000` 5.4ms→1.5ms (**-72%**, mtime guard)

---

> This document was extracted from [`architecture-and-design.en.md`](architecture-and-design.en.md).

## Related Resources

| Resource | Relevance |
|----------|-----------|
| ["性能分析與基準測試 (Performance Analysis & Benchmarks)"](./benchmarks.md) | ⭐⭐⭐ |
| ["Threshold Exporter API Reference"](api/README.en.md) | ⭐⭐ |
| ["BYO Alertmanager Integration Guide"] | ⭐⭐ |
| ["Bring Your Own Prometheus (BYOP) — Existing Monitoring Infrastructure Integration Guide"] | ⭐⭐ |
| ["da-tools CLI Reference"] | ⭐⭐ |
| ["Grafana Dashboard Guide"] | ⭐⭐ |
| ["Advanced Scenarios & Test Coverage"](scenarios/advanced-scenarios.en.md) | ⭐⭐ |
| ["Shadow Monitoring SRE SOP"] | ⭐⭐ |
