---
title: "Performance Benchmarks"
tags: [performance, benchmarks]
audience: [platform-engineer, sre, decision-maker]
version: v2.9.0
lang: en
---
# Performance Benchmarks

> **Language / 語言：** | [中文](./benchmarks.md) | **English (current)**

> **Audience**: Platform Engineers / SREs (primary), decision-makers, Domain Experts (DBA/Infra), tenant tech leads
>
> **Related**: [Architecture & Design](architecture-and-design.en.md) · [Verified Scenarios](scenarios/verified-scenarios.en.md) · measurement detail & gotchas in the [Benchmark Playbook](internal/benchmark-playbook.md)

**How to read this** (readers switch at any time, and often several read it together):

| You are… | Start with | Drill into |
|---|---|---|
| Decision-maker | TL;DR, [§3 Capacity](#3-capacity-how-to-size-it) | [§5 Trust](#5-measurement-method-and-trust) |
| Tenant tech lead | TL;DR, [§1.2 Custom-alert cost](#12-the-cost-of-tenant-custom-alerts-a-different-curve) | [§2 Speed](#2-speed-how-fast-do-alerts-fire) |
| Platform / SRE | everything | — |
| Domain Expert (DBA/Infra) | [§1 Scale](#1-scale-how-many-tenants), [§5 Method](#5-measurement-method-and-trust) | [§4 Stability](#4-stability-does-it-leak-over-time) |

> **Reading together**: everyone shares **TL;DR + §1 Scale + §2 Speed** (the conclusions anyone can grasp), then drills into the section they care about. This doc is ordered shallow-to-deep — once the tone shifts to "platform deep-dive", non-platform readers can stop.

---

## TL;DR — The numbers you need

| Your question | Answer | Source |
|---|---|:-:|
| **Can it run 1000 tenants?** | ✅ Cold start **112 ms**, steady-state reload **1.3 ms** | [§1](#1-scale-how-many-tenants) |
| **Does rule eval slow down with tenant count?** | ⚡ **No** — ~60 ms whether 2 or 102 tenants (O(M) by design) | [§1](#1-scale-how-many-tenants) |
| **How fast do alerts fire?** | 1000-tenant P99 **4.98 s**, 5000-tenant P99 **4.98 s** (near-flat across 5×) | [§2](#2-speed-how-fast-do-alerts-fire) |
| **How do I size memory?** | 40 MiB exporter + 150 MiB Prometheus (typical 100-tenant); ~4 series marginal per tenant | [§3](#3-capacity-how-to-size-it) |
| **Will sustained reload leak?** | ✅ No goroutine / object leak (verified under 60-min sustained pressure) | [§4](#4-stability-does-it-leak-over-time) |

These are the **current (v2.9.0) valid baselines**: core scale and soak were re-verified no-regression on this version (same-machine control); the end-to-end and resource baselines were established on the prior version, and this version is a behavior-preserving refactor guarded continuously by the per-PR regression gate (mechanism & version provenance in [§5](#5-measurement-method-and-trust)).

---

## 1. Scale: how many tenants?

### 1.1 Platform rules: why it's independent of tenant count (O(M))

**Traditional approach, O(N×M)**: N tenants × M rules = N×M independent PromQL evaluations. 100 tenants × 35 rules = 3,500 evaluations.

**This platform, O(M)**: vector matching (`group_left`) lets **one rule cover all tenants**. 100 tenants × 35 rules = **35 evaluations**, **independent of tenant count**.

Measured proof — scale tenants by 51×, eval time barely moves:

| Scenario | Tenants | Rules | Eval time (median) |
|---|:-:|:-:|---:|
| Idle baseline | 2 | 237 | **59.1 ms** |
| Under-load injection | 102 | 237 | **60.6 ms** |

A 51× tenant increase moves eval time from 59.1 ms to 60.6 ms (**+2.5%**) — exactly as O(M) predicts.

**Undeployed metrics ≈ zero cost**: all 15 rule packs are preloaded; a pack with no deployed exporter evaluates in < 1 ms (empty-vector compute ≈ O(1)). Customers **don't need to select rule packs** — install them all; unused packs add almost nothing to Prometheus overhead.

| Rule pack | State | Rules | Eval time |
|---|:-:|---:|---:|
| MariaDB | ✓ active | 7 | 2.12 ms |
| MongoDB | ✗ no exporter | 7 | 0.64 ms (empty vector) |
| Redis | ✗ no exporter | 7 | 0.41 ms (empty vector) |
| Elasticsearch | ✗ no exporter | 7 | 1.75 ms (empty vector, heavier PromQL) |

**Cold start vs steady-state reload** (1000-tenant measured):

| Path | 100 tenants | **1000 tenants** | Meaning |
|---|---:|---:|---|
| **Cold full load** | 3.2 ms | **112 ms** (~112 µs/tenant, linear) | Pod start / full rebuild once |
| **Steady-state reload (no change)** | ~129 µs | **1.30 ms** | reload-ticker per-tick cost (default 15 s) |
| **Single-file change reload** | 628 µs | ~6.3 ms (linear extrapolation) | Customer commits one tenant.yaml |
| **Directory scan hashing** | 128 µs | 1.30 ms | mtime guard gives 4.6× speedup |

**Steady-state reload is 86× cheaper than cold start** — the combined effect of hierarchical scan + dual-hash + mtime guard. Default production reload interval is 15 s, so each tick costs ≈ 0.0087% of the interval.

> The absolute numbers above are a baseline measured on a fixed reference machine; absolute times drift ~1.5× across measurement machines. The platform verifies no regression between versions via **same-machine relative comparison** (details and the latest comparison in [§5](#5-measurement-method-and-trust)), not by comparing absolute numbers across environments.

### 1.2 The cost of tenant custom alerts (a different curve)

The O(M) guarantee for platform rules covers **platform-authored** rules. **Tenant custom alerts** (from v2.9.0) take a different cost path that is deliberately kept scalable too:

- **One new custom-alert kind = one shared cross-tenant rule**, not one per tenant. The compiler groups all tenant declarations of "same metric, same recipe, same parameter shape" into a **single** rule (rule count = number of distinct "alert shapes", not × tenants).
- **Honest about cost**: this guarantee holds **only for "same-metric sharing"** — different metrics necessarily produce different rules, so total rule count grows with the **number of custom-alert kinds**, not tenant count. A per-tenant cap (default 20 kinds) prevents any single tenant from flooding.
- **For tenants**: you write a YAML recipe, never touch PromQL, alerts take effect in seconds — and adding your alert won't slow down the whole platform.
- **For platform engineers**: total rule count = O(alert kinds), not O(tenants × alert kinds); capacity-plan against "number of custom-alert kinds", with the per-tenant cap + a global rule-count budget (planned) as guardrails.

See [ADR-024 §Vectorized compilation](adr/024-version-aware-threshold-via-dimensional-label.md).

---

## 2. Speed: how fast do alerts fire?

The end-to-end measurement covers the full chain: write to `conf.d/` → exporter reload → Prometheus trigger → Alertmanager dispatch → webhook receipt. It runs on a multi-service container stack and is aggregated rigorously (n=30 + bootstrap 95% CI).

**1000-tenant baseline** ([GHA run](https://github.com/vencil/Dynamic-Alerting-Integrations/actions/runs/24951460457)):

| Measurement | P50 | P95 | P99 |
|---|---:|---:|---:|
| **Alert fire latency** | **4748.5 ms** | **4953.95 ms** | **4977.88 ms** |

**5000-tenant baseline** ([GHA run](https://github.com/vencil/Dynamic-Alerting-Integrations/actions/runs/24955478536)):

| Measurement | P50 | P95 | P99 |
|---|---:|---:|---:|
| **Alert fire latency** | **4763.5 ms** | **4971.55 ms** | **4984.07 ms** |

**1000 → 5000 tenants, P95 near-flat (+0.4%)** — confirming the dominant latency is **Prometheus's 5 s scrape quantization**, not exporter scan time. Scaling the platform from 1000 to 5000 tenants **does not increase customer-visible alert latency**.

> For an SLA-contract latency number on customer onboarding, recalibrate against the customer's actual fixture shape (±30% gate), then promote to a contract number. Calibration flow: [Benchmark Playbook](internal/benchmark-playbook.md#v280-phase-2-e2e-alert-fire-through-b-1-phase-2).

---

## 3. Capacity: how to size it?

**Idle baseline** (single node, 2 tenants, 237 rules):

| Component | RSS | CPU (5-min avg) | Storage |
|---|---:|---:|---:|
| Prometheus | **148.1 MiB** | 0.017 cores | 3.0 MiB TSDB |
| threshold-exporter ×2 (HA) | 6.2 MiB | < 0.005 cores | — |
| **Cluster total** | **~154 MiB** | < 0.05 cores | 3 MiB |

**Versus a traditional approach** (9,600 rules estimated at 100 tenants, ~60 KB/rule): ~600 MiB+ → this platform saves **~75%** memory.

**Marginal cost per tenant**: ~4 `user_threshold` series per tenant. Under 102-tenant load the `user_threshold` series count is **exactly 408 = 102 × 4** — the linear model holds.

**Capacity estimate**:

| Tenants | Estimated series | Prometheus RSS budget |
|---:|---:|---:|
| 100 | ~1,500 | ~150 MiB |
| 1,000 | ~15,000 | ~180 MiB |
| 5,000 | ~75,000 | ~300 MiB |

> Actual cardinality depends on label combinations (dimensional thresholds are ~5% of configs).

**Under-load behavior** (100 synthetic tenants injected):

| Metric | Before | After | Delta |
|---|---:|---:|---:|
| Prometheus RSS | 168.2 MiB | 168.6 MiB | +0.4 MiB |
| Active series | 7,338 | 7,378 | +40 |
| `user_threshold` series | 8 | 408 | × 51 (= 102 × 4) |

100 synthetic tenants move Prometheus memory by a median of ~+0.4 MiB (within GC noise).

---

## 4. Stability: does it leak over time?

**Conclusion first**: under 60 minutes of sustained reload pressure the service is **memory-safe with no leak** — goroutines flat, reference-held objects flat, RSS bounded and far below a typical pod limit.

Latest run (v2.9.0, clean measurement machine, 60 min / 15 s reload, 239 reloads): RSS perfectly flat (**+0.0%**), `heap_objects` −1.0%, goroutines flat.

The table below is a deeper four-metric baseline (production default GC mode). Its `sys_bytes +12.4%` vs the +0.0% above is purely measurement-machine and GC-pressure variance — both runs verdict "bounded, no leak". It is kept because its `heap_idle` rise is exactly what illustrates "why a rising heap_idle is not a leak":

| Metric | Start | End | Drift | Verdict |
|---|---:|---:|---:|:-:|
| `go_goroutines` (goroutine-leak detector) | 10 | 9 | −10% | ✅ no leak |
| `go_memstats_heap_objects` (reference-held leak) | 192,404 | 187,351 | −2.6% | ✅ no leak |
| `go_memstats_sys_bytes` (RSS proxy) | 35.0 MiB | 39.3 MiB | +12.4% | ✅ bounded (39 MiB ≪ 100–500 MiB pod limit) |
| `go_memstats_heap_idle_bytes` | 11.5 MiB | 17.4 MiB | +52% | ℹ️ Go runtime trait (see below) |

**Reading it (confidence-first)**:

- ✅ **Memory-safe**: `sys_bytes` settles at 39 MiB, **far below** a typical k8s pod limit (100–500 MiB).
- ✅ **No leak**: `heap_objects` flat (−2.6%) proves no reference-held leak; goroutines hold too (10→9).
- ℹ️ **`heap_idle +52%` is not a leak**: it's the Go GC scavenger's default policy of retaining OS pages under extreme pressure (15 s reload interval). The parallel control (aggressive GC mode) shows only **+0.1%** on the same metric — proving this is GC pacing, not a leak.

> **Real-world**: production reload frequency is usually hours-to-days (config changes), not 15 s. The real growth rate is estimated **10–100× slower** than this stress test, so the heap_idle behavior above **won't occur** in customer environments. For deployments with very high reload cadence, v2.9.0 ships a memory-release lever (`--free-os-mem-after-reload`, off by default, see [#459](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/459)).

---

## 5. Measurement method and trust

**Automated regression gate**: every PR measures performance against its merge-base; a statistically significant regression **blocks merge** (a Tier 1 required check). This keeps every number above continuously guarded, not a one-off snapshot.

**Version provenance of absolute numbers**: the headline absolutes (e.g. cold start 112 ms) are a historical baseline measured on a fixed **reference environment**; the same code drifts ~1.5× in absolute time across measurement machines. So "is there a regression" between versions is decided by **same-machine relative comparison**, not by comparing absolutes across environments. The latest comparison (after the v2.9.0 exporter/loader refactor), same-machine cold start: previous code **172 ms** vs this version **169 ms** — flat, no regression.

**Statistical requirements**:

| Measurement type | Setup |
|---|---|
| Go micro-bench (§1) | `-count=5 -benchtime=3s`, report median |
| End-to-end fire-through (§2) | n=30, bootstrap 95% CI |
| Resource / cardinality (§3) | each round run independently to avoid cross-interference |
| Soak (§4) | 60 min, skip 30 s warmup, drift = (end − start) / start × 100% |

**Measurement environment**: single-node container (Intel Core 7 240H, Go 1.26.2 linux/amd64), synthetic fixtures (each tenant carries several metric thresholds, covering scheduled overrides and regex dimensional). End-to-end uses a realistic long-tail tenant-distribution fixture.

**Production observability metrics** (to watch reload behavior):

- `da_config_scan_duration_seconds` (histogram)
- `da_config_reload_trigger_total{reason}` (counter)
- `da_config_defaults_change_noop_total` (counter, cosmetic edits)
- `da_config_defaults_shadowed_total` (counter, override-shadowed defaults)
- `da_config_blast_radius_tenants_affected{reason,scope,effect}` (histogram)

Full methodology and measurement gotchas: [Benchmark Playbook](internal/benchmark-playbook.md).

---

## 6. Performance history (optional)

Key milestones in how performance evolved across versions — useful when assessing platform maturity, not needed for day-to-day use:

| Version | Key optimization | Quantified impact |
|---|---|---|
| **v2.2.0** | Flat `conf.d/` + per-file SHA-256 + mtime guard | 100-tenant cold start **3.2 ms** baseline |
| **v2.5.0** | Multi-tenant grouping + Saved Views (API layer, per-tenant perf unchanged) | — |
| **v2.7.0** | Hierarchical scan + dual-hash + hierarchy-state load ([ADR-016](adr/016-conf-d-directory-hierarchy-mixed-mode.md) / [ADR-017](adr/017-defaults-yaml-inheritance-dual-hash.md)) | **1000-tenant cold start 112 ms; steady reload 86× cheaper than cold** |
| **v2.8.0** | End-to-end alert fire-through measurement + per-PR regression gate + 60-min readiness soak | 1000/5000-tenant SLO baseline; per-PR statistical regression gate |
| **v2.9.0** | Custom-alert vectorized compilation + exporter/loader refactor (behavior-preserving) | Core scale no-regression (same-machine control); tenant custom alerts = O(alert kinds), not O(tenants) |

**Migration impact**: the `conf.d/` schema is backward-compatible. Flat layout works directly; hierarchical layout is opt-in (drop a `_defaults.yaml` in a subdirectory to enable it). Customers don't rewrite tenant YAML.

---

## 7. Further reading

| Topic | Document |
|---|---|
| **Full micro-bench numbers / synthetic fixture gen / schema validation / pytest-benchmark** | [Benchmark Playbook §Engineering Reference Benchmarks](internal/benchmark-playbook.md#engineering-reference-benchmarks) |
| **Full 1000-tenant baseline per path** (incremental / scan-hash series) | [Benchmark Playbook §v2.8.0 1000-Tenant Hierarchical Baseline](internal/benchmark-playbook.md#v280-1000-tenant-hierarchical-baseline-phase-1-b-1) |
| **Measurement gotchas & ops** (port-forward stability / output isolation / wrapper script) | [Benchmark Playbook §Lessons Learned](internal/benchmark-playbook.md#踩坑記錄-lessons-learned) |
| **Regression-gate CI mechanism** (Tier 1 / override label / sharding) | `.github/workflows/bench-gate-pr.yaml` |
| **Architecture & ADR references** | [Architecture & Design](architecture-and-design.en.md) · [ADR-016](adr/016-conf-d-directory-hierarchy-mixed-mode.md) · [ADR-017](adr/017-defaults-yaml-inheritance-dual-hash.md) |

### The numbers ↔ benchmark mapping (for re-runs)

| Term in this doc | Corresponding benchmark / source |
|---|---|
| Cold full load (§1) | `FullDirLoad` (Go micro-bench) |
| Steady-state reload, no change (§1) | `IncrementalLoad_NoChange` + mtime guard |
| Single-file change reload (§1) | `IncrementalLoad_OneFileChanged` |
| Directory scan hashing (§1) | `ScanDirFileHashes` + mtime guard |
| End-to-end alert fire (§2) | `make bench-e2e` (5-anchor harness, nightly `bench-record.yaml`) |
| Soak drift (§4) | `run_chaos_soak.py` (dual-track GOGC=20 / GOGC=100) |
| Regression gate | `bench-gate-pr.yaml` (Tier 1) |
