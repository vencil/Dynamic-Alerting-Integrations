---
title: "Performance Analysis & Benchmarks"
tags: [performance, benchmarks]
audience: [platform-engineer, sre]
version: v2.6.0
lang: en
---
# Performance Analysis & Benchmarks

> **Language / 語言：** **English (Current)** | [中文](benchmarks.md)

> Related docs: [Architecture](architecture-and-design.en.md) · [Benchmark Playbook](internal/benchmark-playbook.md) (Methodology, lessons learned) · [Test Map § Benchmark Baseline](internal/test-map.md#benchmark-基線)

**Test Environment:** Kind single-node cluster (Intel Core 7 240H), 2 tenants, 237 rules (15 Rule Packs), 43 rule groups. All data collected uniformly at v2.5.0.

---

## 1. Vector Matching Complexity Analysis

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

## 2. Prometheus Rule Evaluation (Idle-State, 5 Rounds)

**Setup:** 2 tenants, 237 rules (15 Rule Packs), 43 rule groups.

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

## 3. Empty Vector Zero-Cost

All rule packs are pre-loaded (`optional: true`). Packs without deployed exporters are evaluated against empty vectors.

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

## 4. Resource Usage Baseline (Idle-State, 5 Rounds Median)

| Metric | Component | Median | StdDev |
|--------|-----------|--------|--------|
| CPU (5m avg) | Prometheus | 0.017 cores | ±0.001 |
| RSS Memory | Prometheus | 148.1MB | ±1.3MB |
| Heap Memory | threshold-exporter (×2 HA) | 3.1MB | ±0.5MB |
| Scrape Duration | Prometheus → exporter | 6.1ms | ±3.9ms |
| Active Series | Prometheus | 6,338 | ±10 |
| TSDB Storage | Prometheus | 3.0MB | ±0.1MB |

**Memory efficiency:**

```
threshold-exporter ×2 HA: ~6.2MB
+ Prometheus RSS: 148.1MB
= Cluster overhead: ~154MB

vs. Traditional approach (9,600 rules @ 100 tenants): ~600MB+ (estimated at ~60KB per-rule memory)
```

**Automated collection:**

```bash
make benchmark              # Full report (human-readable)
make benchmark ARGS=--json  # JSON output (CI/CD consumption)
```

## 5. Storage and Cardinality Analysis

Prometheus performance bottleneck is **Active Series count**, not disk space. Each series consumes ~2KB of memory.

| Metric | Value (5 rounds median) | Description |
|--------|-------|-------------|
| TSDB Disk Usage | 3.0MB | All rules and metrics included |
| Active Series Total | 6,338 | Includes all exporters + recording rules |
| `user_threshold` Series | 8 | Threshold metrics from threshold-exporter |
| Series Per Tenant (marginal) | ~4 | Marginal cost of adding 1 tenant |

**Scaling estimation:**

```
100 tenants:
  user_threshold series = 100 × 4 = 400
  Memory delta ≈ (400 - 8) × 2KB ≈ 0.8MB
  Total series ≈ 6,338 - 8 + 400 = 6,730
```

Dynamic architecture's series growth is minimal per tenant (~4 series). 100 tenants add only ~0.8MB of memory.

## 6. Go Micro-Benchmark (threshold-exporter)

`config_bench_test.go` measures threshold-exporter config parsing performance (`go test -bench=. -benchmem -count=5`, Intel Core 7 240H):

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

**| Benchmark | ns/op (median) | B/op | allocs/op |
|-----------|------:|-----:|----------:|
| Resolve_10Tenants_Scalar | 19,590 | 26,488 | 61 |
| Resolve_100Tenants_Scalar | 163,839 | 202,777 | 520 |
| Resolve_1000Tenants_Scalar | 4,076,536 | 3,848,575 | 5,039 |
| ResolveAt_10Tenants_Mixed | 71,536 | 40,032 | 271 |
| ResolveAt_100Tenants_Mixed | 927,426 | 461,872 | 2,621 |
| ResolveAt_1000Tenants_Mixed | 10,274,749 | 5,244,817 | 26,054 |
| ResolveAt_NightWindow_1000 | 8,438,156 | 5,220,583 | 25,055 |
| ResolveSilentModes_1000 | 156,172 | 187,218 | 10 |

10→100→1000 tenants scale linearly. 1000 tenants with full ResolveAt (including scheduled thresholds) stays under ~10ms. `ResolveSilentModes_1000` is only 156µs—flag metric queries are near-zero cost.

> **Relationship to § 2 (Prometheus Rule Evaluation):** § 2 measures Prometheus rule evaluation (O(M), independent of tenant count), this section measures threshold-exporter config resolution (O(N), linear growth). Two are complementary: the platform's most critical bottleneck (rule evaluation) stays constant, while secondary cost (config resolution) at 1000 tenants is only ~10ms — well below 15-second scrape interval.

## 7. Route Generation Scaling (Alertmanager Route Output Performance)

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

**Conclusion:** From 3→9→15 Rule Packs, eval time grows from 7.7→22.7→23.2ms. 9→15 packs (+96 rules) adds only 0.5ms to eval time, because the new JVM/Nginx Rule Packs trigger [Empty Vector Zero-Cost](#3-empty-vector-zero-cost) with no matching exporter data. Average eval time per group (23.2ms / 43 groups = 0.54ms) remains stable. Projected Volume horizontal scalability confirmed.

## 8. Alertmanager Notification Performance

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

**Key insight:** The inhibited/received ratio reflects severity dedup effectiveness. During normal operations, when both warning + critical fire simultaneously for a dedup-enabled tenant-metric_group pair, the warning should be inhibited. The 3 inhibit rules have negligible impact on Alertmanager route matching performance.

## 9. Config Reload E2E Latency

Measures end-to-end latency from "tenant changes routing settings" to "new routes are active".

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
- Route generation (2 tenants): ~94ms (from [Route Generation Scaling](#7-route-generation-scaling-alertmanager-route-output-performance) data)
- `kubectl patch` ConfigMap + API server response: ~500–700ms
- `/-/reload` API: ~0.3ms
- Sum consistent with measured total (~763ms)

> **configmap-reload sidecar note:** The sidecar watches Projected Volume **file content changes**, not ConfigMap annotations. `--apply` mode directly updates ConfigMap `data` section + triggers `/-/reload`, so it does not depend on the sidecar's polling interval. If only annotations are modified without changing data, the sidecar will not detect the change.

**Conclusion:** Full path completes in ~760ms on Kind. Bottleneck is kubectl API server (~600ms), not route generation (~94ms). Production (dedicated etcd) expected < 500ms.

## 10. Toolchain Performance Baseline

Core toolchain computational performance (excluding Prometheus query I/O, 20 rounds in-process median):

### Policy-as-Code Engine

`evaluate_policies()` evaluates policy rules on all tenants. 3 × PolicyRule per N tenant:

| Tenants | Median | Description |
|---------|--------|------|
| 10      | 0.032ms | Real-time response |
| 50      | 0.148ms | Linear growth |
| 100     | 0.262ms | Sub-millisecond |
| 500     | 1.295ms | Linear scaling |
| 1000    | 2.605ms | 1000 tenants still < 3ms |

100 tenants × 3 rules completes in < 0.3ms, safe for CI pipeline or pre-commit hooks.

### Alert Quality Scoring

`compute_noise_score()` + `compute_stale_score()` are pure computation:

| Operation | Median (20 rounds) | Description |
|------|---------------|------|
| noise+stale × 1,000 calls | 1.06ms | ~1.1µs/call |
| noise+stale × 10,000 calls | 4.73ms | ~0.5µs/call (amortized) |

Bottleneck is Prometheus range query (~1-3s), not computation itself.

### Cardinality Forecasting

`linear_regression()` (pure Python, no NumPy dependency):

| Operation | Median (20 rounds) | Description |
|------|---------------|------|
| 100 data points × 100 calls | 2.9ms | ~29µs/call |
| 100 data points × 1,000 calls | 28.2ms | ~28µs/call |
| 100 data points × 10,000 calls | 286.7ms | ~29µs/call |

Linear scaling stable. 100 tenants full forecast (including Prometheus query) estimated 3-5s, bottleneck at network I/O.

### validate_config E2E

`da-tools validate-config` all-in-one validation (schema + routing + policy + drift), CLI E2E with all checks (10 rounds median ± stddev):

| Tenants | Wall Time | Description |
|---------|-----------|------|
| 2       | 225ms ±5ms | Existing config (fast path) |
| 10      | 305ms ±54ms | Synthetic config |
| 50      | 606ms ±289ms | Including routing + policy |

Python startup overhead ~200ms. Pure validation logic < 100ms/50 tenants.

### Schema Validation (validate_tenant_keys)

`validate_tenant_keys()` validates key legality per-tenant (20 rounds median ± stddev):

| Tenants | Median | Description |
|---------|--------|------|
| 10      | 0.010ms | Near-zero cost |
| 100     | 0.128ms | Linear growth |
| 500     | 0.498ms | Sub-millisecond |
| 1000    | 0.978ms | 1000 tenants < 1ms |

Pure dict operations, safe to embed in hot-reload path.

## 11. Under-Load Benchmark Mode (100 Synthetic Tenants)

Inject 100 synthetic tenants into ConfigMap, wait for exporter hot-reload + Prometheus scrape, measure system behavior under load.

```bash
make benchmark ARGS="--under-load --tenants 100"
```

**Kind single-node cluster measurement (3 independent runs, 102 tenants = 2 existing + 100 synthetic):**

| Metric | Round 1 | Round 2 | Round 3 | Description |
|--------|---------|---------|---------|------|
| Prometheus RSS (before) | 148.6MB | 168.2MB | 171.0MB | Baseline before injection |
| Prometheus RSS (after) | 150.8MB | 168.6MB | 168.7MB | Steady state after injection |
| **Memory Delta** | **+2.2MB** | **+0.4MB** | **-2.3MB** | GC noise |
| Scrape Duration (after) | 104.2ms | 6.3ms | 19.9ms | Scrape time |
| Eval Time (after) | 65.3ms | 29.8ms | 86.6ms | Rule evaluation time |
| Active Series | 7,338 | 7,378 | 7,378 | Stable |
| user_threshold Series | 8→408 | 408→408 | 408→408 | = 102 tenants × 4 |

**Alertmanager baseline (Idle-State vs Under-Load):**

| Metric | Idle-State (2 tenants) | Under-Load (102 tenants) |
|--------|----------------------|------------------------|
| Active Inhibit Rules | 3 | 3 (fixed cost, no tenant growth) |
| Active Alerts | 1 (sentinel) | 1 (sentinel) |

Alertmanager inhibit rule count unchanged between idle-state and under-load (2 severity dedup + 1 default), confirming per-tenant routing adds no inhibit overhead.

**Conclusion:**

100 synthetic tenants have minimal memory impact on Prometheus (median delta ~+0.4MB, within GC noise). Active series stable at ~7,360, user_threshold series matches `102 × 4 = 408`, confirming linear per-tenant model. Eval time and scrape duration vary with Prometheus cache state but remain acceptable (< 105ms).

## 12. Incremental Hot-Reload Performance (v2.1.0)

v2.1.0 introduced per-file SHA-256 index + parsed config cache for incremental reload. Go micro-benchmarks measure incremental vs full reload performance (`config_bench_test.go`, `-count=3` median).

**Environment:** Dev Container (Intel Core 7 240H), each tenant with 8 metric thresholds (including scheduled overrides).

**v2.1.0 optimizations:** (1) Removed `Resolve()` calls from reload path; (2) **mtime guard** — `scanDirFileHashes` uses `DirEntry.Info()` mtime+size as first-level cache, skipping `os.ReadFile` + SHA-256 when unchanged; (3) **incremental merge** — patches config directly instead of O(N) `mergePartialConfigs`; (4) **byte cache** — scan phase caches `[]byte` data, reused to eliminate double disk reads.

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

## pytest-benchmark Micro-Benchmarks

`pytest -m benchmark` (min_rounds=20, warmup=on). For version-to-version trend detection. Route generation data see § 7.

| Test | Median | Rounds | Description |
|------|--------|--------|------|
| `test_parse_integer` | ~102ns | 100,161 | parse_duration_seconds fastest path |
| `test_parse_seconds` | ~634ns | 164,555 | includes string parsing |
| `test_parse_minutes` | ~624ns | 168,039 | includes string parsing |
| `test_parse_hours` | ~619ns | 168,663 | includes string parsing |
| `test_format_seconds` | ~128ns | 80,167 | format_duration |
| `test_format_minutes` | ~160ns | 59,443 | format_duration (minutes) |
| `test_format_hours` | ~147ns | 70,872 | format_duration (hours) |
| `test_within_bounds` | ~796ns | 131,303 | validate_and_clamp (no clamp) |
| `test_clamped` | ~1.2µs | 85,129 | validate_and_clamp (with clamp) |

---

## Methodology

Complete methodology and lessons learned detailed in [Benchmark Playbook](internal/benchmark-playbook.md).

**Statistical requirements:**
- pytest-benchmark: min_rounds=20, warmup enabled, report median
- benchmark.sh (K8s idle-state): 5 rounds, 30s interval, report median ± stddev
- benchmark.sh (K8s under-load): each round independent execution (avoid port-forward instability)
- Go micro-bench: `-count=5`, report median
- Toolchain performance baseline: 20 rounds in-process, report median
- CLI E2E: 10 rounds subprocess, report median ± stddev

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
| ["Advanced Scenarios & Test Coverage"](internal/test-coverage-matrix.md) | ⭐⭐ |
| ["Shadow Monitoring SRE SOP"] | ⭐⭐ |
