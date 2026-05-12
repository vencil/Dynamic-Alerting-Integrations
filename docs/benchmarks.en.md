---
title: "Performance Benchmarks"
tags: [performance, benchmarks]
audience: [platform-engineer, sre]
version: v2.8.0
lang: en
---
# Performance Benchmarks

> **Language / 語言：** [中文](./benchmarks.md) | **English (Current)**

> **The 5 numbers you need to know for v2.8.0 + release-readiness evidence.** Internal tool benchmarks (route generation / pytest-benchmark micros / Policy-as-Code engine / Schema Validation / etc.) and measurement caveats live in the [Benchmark Playbook](internal/benchmark-playbook.md).
>
> Related: [Architecture & Design](architecture-and-design.en.md) · [Test Coverage Matrix](internal/test-coverage-matrix.md)

---

## TL;DR — 5 numbers you need

| Your question | Answer | §Source |
|---|---|:-:|
| **Can it run 1000 tenants?** | ✅ Cold load **112 ms**, steady-state reload **1.3 ms** | [§3](#3-v280-scale-gate-1000-tenant-measured) |
| **How long does an alert take end-to-end?** | 1000-tenant P99 **4.98 s** / 5000-tenant P99 **4.98 s** (near-flat across 5×) | [§4](#4-v280-end-to-end-alert-fire-through-baseline) |
| **Does 60-min sustained reload leak?** | ✅ No goroutine / live-object leak (dual GOGC=20 + default parallel verification) | [§5](#5-v280-readiness-soak-60-min-1000-tenant) |
| **Does rule eval scale with tenant count?** | ⚡ **No — stays 60 ms whether 2 or 102 tenants** (O(M) by design) | [§2](#2-why-it-scales-om-vector-matching) |
| **How to size memory?** | 40 MiB exporter + 150 MiB Prometheus (typical 100-tenant); ~4 series per tenant marginal | [§6](#6-resource-sizing-customer-deployment-planning) |

**v2.8.0 release confidence**: All 5 numbers verified. **bench-gate-pr Tier 1 CI gate** ([#433](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/433) W2) shipped in v2.8.0 — every PR auto-runs `benchstat -confidence=0.99` against `merge-base`, statistically significant regression blocks merge.

---

## 1. v2.2.0 → v2.8.0 performance evolution

| Version | Key optimizations | Quantified impact |
|---|---|---|
| **v2.2.0** | flat `conf.d/` + per-file SHA-256 + mtime guard | 100-tenant cold load **3.2 ms** baseline |
| **v2.5.0** | Multi-tenant Grouping + Saved Views (API-layer, per-tenant perf unchanged) | — |
| **v2.7.0** | **Hierarchical scan + dual-hash + populateHierarchyState** ([ADR-017](adr/017-conf-d-directory-hierarchy-mixed-mode.en.md) / [ADR-018](adr/018-defaults-yaml-inheritance-dual-hash.en.md)) | **1000-tenant cold 112 ms; steady reload 86× cheaper than cold** |
| **v2.8.0** | **5-anchor e2e fire-through harness + bench-gate-pr Tier 1 CI + 60-min readiness soak** | 1000/5000-tenant SLO baseline; per-PR statistical regression gate |

**Migration impact**: `conf.d/` schema is backward-compatible from v2.2.0 → v2.8.0. Flat layout still works as-is; hierarchical layout is opt-in (drop a `_defaults.yaml` in a subdir to auto-enable). No tenant YAML rewrites required.

---

## 2. Why it scales — O(M) vector matching

**Traditional approach O(N×M)**: N tenants × M alert rules = N×M independent PromQL evaluations. 100 tenants × 35 rules = 3,500 evaluations.

**Dynamic Alerting O(M)**: Vector matching (`group_left`) → 1 rule covers all tenants. 100 tenants × 35 rules = **35 evaluations**. **Independent of tenant count**.

**Empirical verification**:

| Scenario | Tenants | Rules | Eval Time (median) |
|---|:-:|:-:|---:|
| Idle baseline | 2 | 237 | **59.1 ms** |
| Under-Load injection | 102 | 237 | **60.6 ms** |

51× tenant increase, eval time goes 59.1 → 60.6 ms (+2.5%) — empirically validates O(M) design.

**Empty vector zero-cost**: 15 Rule Packs pre-loaded (`optional: true`); packs without an exporter evaluate < 1 ms (empty-vector compute is near-O(1)). Customers **don't need to** curate "which Rule Pack to install" — load them all, **unused packs ≈ 0 evaluation cost**, no Prometheus overhead added.

| Rule Pack | State | Rules | Eval Time |
|---|:-:|---:|---:|
| MariaDB | ✓ active | 7 | 2.12 ms |
| MongoDB | ✗ no exporter | 7 | 0.64 ms (empty vec) |
| Redis | ✗ no exporter | 7 | 0.41 ms (empty vec) |
| Elasticsearch | ✗ no exporter | 7 | 1.75 ms (empty vec, complex PromQL) |

---

## 3. v2.8.0 Scale Gate — 1000-tenant measured

**Environment**: Dev Container (Intel Core 7 240H, Go 1.26.2 linux/amd64), `buildDirConfig` synthetic fixture, each tenant with 8 metric thresholds (including scheduled overrides + regex dimensional).

**Baseline measured at v2.7.0-final** (2026-04-18, [`b808610`](https://github.com/vencil/Dynamic-Alerting-Integrations/commit/b808610), `-benchtime=3s -count=3`); the scan path is unchanged in v2.8.0, and **bench-gate-pr Tier 1 CI gate** validates this baseline against every PR for regression.

| Path | 100 tenants | **1000 tenants** | Meaning |
|---|---:|---:|---|
| **Cold load** (`FullDirLoad`) | 3.2 ms | **112 ms** (~112 µs/tenant, linear) | Pod startup / full rebuild |
| **Steady-state reload** (`IncrementalLoad_NoChange` + mtime guard) | ~129 µs | **1.30 ms** | Reload ticker per-tick cost (15s default) |
| **Single-file change** (`IncrementalLoad_OneFileChanged`) | 628 µs | ~6.3 ms (linear extrapolation) | Customer commits a single tenant.yaml |
| **Raw scan** (`ScanDirFileHashes` + mtime guard) | 128 µs | 1.30 ms | mtime guard 4.6× speedup vs no-guard |

**Steady-state is 86× cheaper than cold load** — combined effect of v2.7.0 hierarchical scan + dual-hash + mtime guard.

**Production behavior**: Reload ticker defaults 15s; each tick costs ≈ 0.0087% of interval.

**Observability — v2.8.0 reload telemetry metrics**:
- `da_config_scan_duration_seconds` (histogram)
- `da_config_reload_trigger_total{reason}` (counter)
- `da_config_defaults_change_noop_total` (counter, cosmetic edits)
- `da_config_defaults_shadowed_total` (counter, NEW in v2.8.0, override-shadowed defaults)
- `da_config_blast_radius_tenants_affected{reason,scope,effect}` (histogram, NEW in v2.8.0)

---

## 4. v2.8.0 end-to-end Alert Fire-Through baseline

v2.8.0 B-1 Phase 2 shipped the **5-anchor end-to-end alert fire-through harness** (`tests/e2e-bench/`), covering the full chain from `conf.d/` write → exporter reload → Prometheus alert trigger → Alertmanager dispatch → webhook receiver.

**Harness composition**: 6-service docker-compose stack (threshold-exporter / Prometheus / pushgateway / Alertmanager / receiver / driver) + statistical aggregator (n=30 + bootstrap 95% CI) + Tier 1 fail-fast smoke gate.

**1000-tenant baseline** (n=30, [GHA run](https://github.com/vencil/Dynamic-Alerting-Integrations/actions/runs/24951460457)):

| Anchor | P50 | P95 | P99 |
|---|---:|---:|---:|
| **Alert fire latency** | **4748.5 ms** | **4953.95 ms** | **4977.88 ms** |

**5000-tenant baseline** (n=30, [GHA run](https://github.com/vencil/Dynamic-Alerting-Integrations/actions/runs/24955478536)):

| Anchor | P50 | P95 | P99 |
|---|---:|---:|---:|
| **Alert fire latency** | **4763.5 ms** | **4971.55 ms** | **4984.07 ms** |

**Near-flat e2e at P95 across 1000 → 5000 tenants** (+0.4%) — proves the dominant latency is **Prometheus 5s scrape quantization**, not exporter scan time. Scaling from 1000 → 5000 tenants **does not add customer-visible alert latency**.

**Run via**: `make bench-e2e` (local) + nightly `bench-record.yaml` workflow.

> **Methodology note**: These figures are rigorously measured on the synthetic-v2 fixture (Zipf + power-law tenant distribution; n=30 + bootstrap 95% CI), passing the v2.8.0 release-readiness gate. Customers seeking an SLA contract anchor can re-validate against their actual workload shape via the [DEC-B customer-anon corpus calibration](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/142) (±30% gate) — those revalidated numbers then become the contract anchor. Calibration flow + ops details: [Benchmark Playbook §Phase 2 calibration](internal/benchmark-playbook.md#v280-phase-2-e2e-alert-fire-through-b-1-phase-2).

---

## 5. v2.8.0 readiness soak — 60-min × 1000-tenant

Validates leak / drift behavior under sustained reload pressure. **Dual-track parallel design**: Run A (`GOGC=20` — Go GC aggressive mode, accelerates leak detection; for code-level use) + Run B (`GOGC=100` Go runtime default, mirrors production deployment shape).

**Setup**: 1000-tenant fixture × 2 (independent dirs) × 60 min × 15s reload × 10s poll = **239 reloads + 360 polls per run**.

**Run B (production-config, GOGC=100) results**:

| Metric | Start | End | Drift | Verdict |
|---|---:|---:|---:|:-:|
| `go_goroutines` (goroutine leak detector) | 10 | 9 | -10% | ✅ no leak |
| `go_memstats_heap_objects` (reference-held leak) | 192,404 | 187,351 | -2.6% | ✅ no leak |
| `go_memstats_sys_bytes` (RSS proxy) | 35.0 MiB | 39.3 MiB | +12.4% | ✅ bounded (39 MiB ≪ 100-500 MiB pod limit) |
| `go_memstats_heap_idle_bytes` | 11.5 MiB | 17.4 MiB | +52% | ℹ️ Go runtime trait (see below) |

**Interpretation (confidence-first)**:

- ✅ **Memory Safe**: `sys_bytes` settles at 39 MiB, **far below** typical k8s pod limit (100-500 MiB)
- ✅ **No Memory Leak**: `heap_objects` stays flat (-2.6%), confirming **no reference-held leak**; goroutines also stay flat (10→9)
- ℹ️ **Go Runtime Trait**: `heap_idle +52%` is Go GC's scavenger default strategy of retaining OS pages under extreme pressure (15s reload interval) — **not a leak**. The Run A control (GOGC=20 aggressive GC) showed only `+0.1%` on the same metric, confirming this is GC pacing behavior

**Real customer scenarios**: Production reload frequency is typically hours-to-days (config changes), not 15s. Actual production growth rate is estimated **10-100× slower** than this soak — **this behavior will not surface in customer environments**. Long-running characterization (4-hour soak) + `GOMEMLIMIT` tuning experiment tracked in [#459](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/459) as part of v2.9.0 perf hardening.

---

## 6. Resource sizing — customer deployment planning

**Idle baseline (Kind single-node, 2 tenants, 237 rules)**:

| Component | RSS | CPU (5m avg) | Storage |
|---|---:|---:|---:|
| Prometheus | **148.1 MiB** | 0.017 cores | 3.0 MiB TSDB |
| threshold-exporter ×2 HA | 6.2 MiB | < 0.005 cores | — |
| **Cluster total** | **~154 MiB** | < 0.05 cores | 3 MiB |

**vs traditional approach** (9,600 rules @ 100 tenants estimate, ~60 KB/rule): ~600 MiB+ → Dynamic Alerting saves **~75%** memory.

**Per-tenant marginal cost**: `user_threshold` series ~4/tenant. 102-tenant under-load measurement: `user_threshold` series exactly **408 = 102 × 4**, linear model holds.

**Cardinality estimates**:

| Tenants | Estimated series | Prometheus RSS budget |
|---:|---:|---:|
| 100 | ~1,500 | ~150 MiB |
| 1,000 | ~15,000 | ~180 MiB |
| 5,000 | ~75,000 | ~300 MiB |

> Actual cardinality depends on label combinations (dimensional thresholds account for ~5% of configs). Linear model validated by under-load measurement.

**Under-load behavior (100 synthetic tenants injected)**:

| Metric | Before | After | Delta |
|---|---:|---:|---:|
| Prometheus RSS | 168.2 MiB | 168.6 MiB | +0.4 MiB |
| Active series | 7,338 | 7,378 | +40 |
| Eval time | 65.3 ms | 29.8 / 86.6 ms (3 rounds) | within range |
| `user_threshold` series | 8 | 408 | × 51 (= 102 × 4) |

100 synthetic tenants impact Prometheus memory median ~+0.4 MiB (GC noise range).

---

## 7. Measurement methodology

**Statistical requirements**:
- pytest-benchmark: min_rounds=20, warmup enabled, report median
- Go micro-bench: `-count=5 -benchtime=3s`, report median
- E2E fire-through (§4): n=30, bootstrap 95% CI
- Soak (§5): 60 min, 30s warmup skip, drift = (last - first) / first × 100%

**Idle-state**: Kind single-node (Intel Core 7 240H), 2 tenants, 237 rules, 43 rule groups, 5 rounds @ 30s interval

**Under-load (§6)**: Each round runs independently (avoid port-forward continuity instability — see Benchmark Playbook)

**Full methodology + caveats**: [Benchmark Playbook](internal/benchmark-playbook.md)

---

## 8. Further reading

| Topic | Document |
|---|---|
| **Full micro-bench numbers** (Resolve_*, IncrementalLoad_*, ScanDirFileHashes_*) | [Benchmark Playbook §Go Micro-Bench](internal/benchmark-playbook.md) |
| **Internal tool benchmarks** (route generation / pytest micros / Policy-as-Code / Cardinality forecast / Schema validation / Synthetic fixture gen) | [Benchmark Playbook §Toolchain](internal/benchmark-playbook.md) |
| **Measurement caveats & ops** (port-forward stability / `BENCH_OUT_DIR` isolation / `bench_wrapper.sh`) | [Benchmark Playbook §Ops](internal/benchmark-playbook.md) |
| **bench-gate-pr CI mechanism** (Tier 1 / override label / sharding) | [Bench Gate Rollout](internal/bench-gate-rollout.md) · `.github/workflows/bench-gate-pr.yaml` |
| **Architecture & ADR references** | [Architecture & Design](architecture-and-design.en.md) · [ADR-017](adr/017-conf-d-directory-hierarchy-mixed-mode.en.md) · [ADR-018](adr/018-defaults-yaml-inheritance-dual-hash.en.md) |
