---
title: "性能基準 (Performance Benchmarks)"
tags: [performance, benchmarks]
audience: [platform-engineer, sre]
version: v2.8.1
lang: zh
---
# 性能基準 (Performance Benchmarks)

> **Language / 語言：** | **中文（當前）** | [English](./benchmarks.en.md)

> **5 個你升級到 v2.8.0 需要知道的數字 + release-readiness 證據。** Internal tool benchmarks（route generation / pytest-benchmark micro / Policy-as-Code engine / Schema Validation 等）與量測踩坑見 [Benchmark Playbook](internal/benchmark-playbook.md)。
>
> 相關文件：[Architecture & Design](architecture-and-design.md) · [Test Coverage Matrix](internal/test-coverage-matrix.md)

---

## TL;DR — 5 個你需要的數字

| 你的問題 | 答案 | §出處 |
|---|---|:-:|
| **1000 個 tenants 能跑嗎？** | ✅ Cold load **112 ms**，steady-state reload **1.3 ms** | [§3](#3-v280-scale-gate-1000-tenant-實測) |
| **End-to-end alert 多久 fire？** | 1000-tenant P99 **4.98 s** / 5000-tenant P99 **4.98 s**（near-flat across 5×）| [§4](#4-v280-端到端-alert-fire-through-baseline) |
| **60 分鐘 sustained reload 會 leak 嗎？** | ✅ 無 goroutine / live-object leak（雙軌 GOGC=20 + default 平行驗證）| [§5](#5-v280-readiness-soak-60-min-1000-tenant) |
| **規則評估會隨 tenant 數變慢嗎？** | ⚡ **不會 — 維持 60 ms 不論 2 個還是 102 個 tenants**（O(M) by design）| [§2](#2-為什麼能-scale-架構保證-om-向量匹配) |
| **記憶體 sizing 怎麼估？** | 40 MiB exporter + 150 MiB Prometheus（典型 100-tenant）；per-tenant 邊際 ~4 series | [§6](#6-資源-sizing-customer-部署規劃) |

**v2.8.0 release confidence**：上述 5 個數字都已 verified；**bench-gate-pr Tier 1 CI gate** ([#433](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/433) W2) 在 v2.8.0 落地，每個 PR 自動驗 perf regression vs `merge-base`，statistically-significant 退化 block merge。

---

## 1. v2.2.0 → v2.8.0 性能優化沿革

| 版本 | 關鍵優化 | 量化影響 |
|---|---|---|
| **v2.2.0** | flat `conf.d/` + per-file SHA-256 + mtime guard | 100-tenant cold load **3.2 ms** baseline |
| **v2.5.0** | Multi-tenant Grouping + Saved Views (API layer，per-tenant perf 不變) | — |
| **v2.7.0** | **Hierarchical scan + dual-hash + populateHierarchyState** ([ADR-016](adr/016-conf-d-directory-hierarchy-mixed-mode.md) / [ADR-017](adr/017-defaults-yaml-inheritance-dual-hash.md)) | **1000-tenant cold 112 ms; steady reload 86× cheaper than cold** |
| **v2.8.0** | **5-anchor e2e fire-through harness + bench-gate-pr Tier 1 CI + 60-min readiness soak** | 1000/5000-tenant SLO baseline; per-PR statistical regression gate |

**遷移影響**：v2.2.0 → v2.8.0 的 conf.d/ schema 向後相容。Flat layout 直接 work，hierarchical layout 為 opt-in（放 `_defaults.yaml` 在子目錄即自動啟用）。客戶不需重寫 tenant YAML。

---

## 2. 為什麼能 scale — 架構保證 O(M) 向量匹配

**傳統方法 O(N×M)**：N 個 tenants × M 個 alert rules = N×M 個獨立 PromQL 評估。100 tenants × 35 rules = 3,500 evaluations。

**Dynamic Alerting O(M)**：向量匹配（`group_left`）→ 1 條 rule 涵蓋所有 tenants。100 tenants × 35 rules = **35 evaluations**。**與 tenant 數無關**。

**實測驗證**：

| 場景 | Tenants | Rules | Eval Time (median) |
|---|:-:|:-:|---:|
| Idle baseline | 2 | 237 | **59.1 ms** |
| Under-Load injection | 102 | 237 | **60.6 ms** |

51× 的 tenants 增量，eval time 從 59.1ms 變 60.6ms（+2.5%）— 完全驗證 O(M) 設計。

**空向量零成本**：15 個 Rule Pack 預載入（`optional: true`），沒部署 exporter 的 pack 評估 < 1 ms（empty vector 計算近似 O(1)）。客戶**不需要**做「Rule Pack 選擇」就直接全裝，**未部署的 pack ≈ 0 評估成本**，不增加 Prometheus 開銷。

| Rule Pack | 狀態 | Rules | Eval Time |
|---|:-:|---:|---:|
| MariaDB | ✓ active | 7 | 2.12 ms |
| MongoDB | ✗ no exporter | 7 | 0.64 ms (空向量) |
| Redis | ✗ no exporter | 7 | 0.41 ms (空向量) |
| Elasticsearch | ✗ no exporter | 7 | 1.75 ms (空向量，但 PromQL 較複雜) |

---

## 3. v2.8.0 Scale Gate — 1000-tenant 實測

**測試環境**：Dev Container (Intel Core 7 240H, Go 1.26.2 linux/amd64), `buildDirConfig` 合成 fixture，每 tenant 含 8 個 metric threshold (含 scheduled override + regex dimensional)。

**Baseline 量測於 v2.7.0** (2026-04-18, [`b808610`](https://github.com/vencil/Dynamic-Alerting-Integrations/commit/b808610), `-benchtime=3s -count=3`)；v2.8.0 scan path 未改動，**bench-gate-pr Tier 1 CI gate** 每個 PR 持續驗證該 baseline 無 regression。

| 路徑 | 100 tenants | **1000 tenants** | 含意 |
|---|---:|---:|---|
| **Cold load** (`FullDirLoad`) | 3.2 ms | **112 ms** (~112 µs/tenant, 線性) | Pod 啟動 / 全量重建一次 |
| **Steady-state reload** (`IncrementalLoad_NoChange` + mtime guard) | ~129 µs | **1.30 ms** | Reload ticker 每次成本 (預設 15s) |
| **Single-file change** (`IncrementalLoad_OneFileChanged`) | 628 µs | ~6.3 ms (線性外推) | Customer commit single tenant.yaml |
| **Raw scan** (`ScanDirFileHashes` + mtime guard) | 128 µs | 1.30 ms | mtime guard 4.6× speedup vs no-guard |

**Steady-state 是 cold load 的 86× 便宜** — v2.7.0 hierarchical + dual-hash + mtime guard 三層優化的 combined effect。

**Production 行為**：reload ticker 預設 15s，每次 tick 成本 ≈ interval 的 0.0087%。

**Observability — v2.8.0 reload telemetry metrics**：
- `da_config_scan_duration_seconds` (histogram)
- `da_config_reload_trigger_total{reason}` (counter)
- `da_config_defaults_change_noop_total` (counter, cosmetic edits)
- `da_config_defaults_shadowed_total` (counter, v2.8.0 新增, override-shadowed defaults)
- `da_config_blast_radius_tenants_affected{reason,scope,effect}` (histogram, v2.8.0 新增)

---

## 4. v2.8.0 端到端 Alert Fire-Through baseline

v2.8.0 落地 **5-anchor end-to-end alert fire-through harness** (`tests/e2e-bench/`)，覆蓋從 `conf.d/` 寫入 → exporter reload → Prometheus alert trigger → Alertmanager dispatch → webhook receiver 的完整鏈。

**Harness composition**：6-service docker-compose stack（threshold-exporter / Prometheus / pushgateway / Alertmanager / receiver / driver）+ statistical aggregator (n=30 + bootstrap 95% CI) + Tier 1 fail-fast smoke gate。

**1000-tenant baseline** (n=30, [GHA run](https://github.com/vencil/Dynamic-Alerting-Integrations/actions/runs/24951460457))：

| Anchor | P50 | P95 | P99 |
|---|---:|---:|---:|
| **Alert fire latency** | **4748.5 ms** | **4953.95 ms** | **4977.88 ms** |

**5000-tenant baseline** (n=30, [GHA run](https://github.com/vencil/Dynamic-Alerting-Integrations/actions/runs/24955478536))：

| Anchor | P50 | P95 | P99 |
|---|---:|---:|---:|
| **Alert fire latency** | **4763.5 ms** | **4971.55 ms** | **4984.07 ms** |

**Near-flat e2e at P95 across 1000 → 5000 tenants** (+0.4%) — 證實主導 latency 是 **Prometheus 5s scrape quantization**，不是 exporter scan time。從 1000-tenant 平台擴到 5000-tenant **不增加 customer-visible alert latency**。

**Run via**：`make bench-e2e` (local) + nightly `bench-record.yaml` workflow。

> **Methodology note**：以上數字為 synthetic-v2 fixture (Zipf+power-law tenant distribution) 的嚴謹量測 (n=30 + bootstrap 95% CI)，已通過 v2.8.0 release-readiness gate。客戶導入時若需 SLA contract anchor，可透過 [customer-anon corpus calibration](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/142) (±30% gate) 對客戶實際 fixture 形狀重新驗證後 promote 為合約數字。Calibration 流程 + ops 細節：[Benchmark Playbook §v2.8.0 e2e alert fire-through calibration](internal/benchmark-playbook.md#v280-phase-2-e2e-alert-fire-through-b-1-phase-2)。

---

## 5. v2.8.0 readiness soak — 60-min × 1000-tenant

驗證 sustained reload pressure 下的 leak / drift 行為。**雙軌平行設計**：Run A (`GOGC=20` — Go GC aggressive mode, 加速 leak 偵測；程式碼層使用) + Run B (`GOGC=100` Go runtime default, 對應 production 部署形態)。

**Setup**：1000-tenant fixture × 2 (independent dirs) × 60 min × 15s reload × 10s poll = **239 reloads + 360 polls per run**。

**Run B (production-config, GOGC=100) 結果**：

| Metric | Start | End | Drift | Verdict |
|---|---:|---:|---:|:-:|
| `go_goroutines` (goroutine leak detector) | 10 | 9 | -10% | ✅ no leak |
| `go_memstats_heap_objects` (reference-held leak) | 192,404 | 187,351 | -2.6% | ✅ no leak |
| `go_memstats_sys_bytes` (RSS proxy) | 35.0 MiB | 39.3 MiB | +12.4% | ✅ bounded (39 MiB ≪ 100-500 MiB pod limit) |
| `go_memstats_heap_idle_bytes` | 11.5 MiB | 17.4 MiB | +52% | ℹ️ Go runtime trait (see below) |

**判讀（confidence-first）**：

- ✅ **記憶體安全 (Memory Safe)**：`sys_bytes` 穩定停留在 39 MiB，**遠低於** typical k8s pod limit (100-500 MiB)
- ✅ **無記憶體洩漏 (No Memory Leak)**：`heap_objects` 數量持平（-2.6%），證實**沒有 reference-held leak**；goroutine 數也維持（10→9）
- ℹ️ **Go 運行時特徵 (Runtime Trait)**：`heap_idle +52%` 是 Go GC 在極端壓力（15s reload interval）下保留 OS pages 的 scavenger 預設策略，**不是 leak**。Run A 對照組（GOGC=20 aggressive GC）同 metric 僅 `+0.1%`，反證這是 GC pacing 行為

**真實 customer 場景**：production reload frequency 通常 hours-to-days（config 變更），不是 15s。實際 production growth rate 預估比 soak slow **10-100×**，此現象在客戶環境**不會發生**。Long-running characterization (4-hour soak) + `GOMEMLIMIT` tuning experiment 由 [#459](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/459) 跟進，作為 v2.9.0 perf hardening 一環。

---

## 6. 資源 sizing — customer 部署規劃

**Idle baseline (Kind 單節點, 2 tenants, 237 rules)**：

| 元件 | RSS | CPU (5m avg) | Storage |
|---|---:|---:|---:|
| Prometheus | **148.1 MiB** | 0.017 cores | 3.0 MiB TSDB |
| threshold-exporter ×2 HA | 6.2 MiB | < 0.005 cores | — |
| **Cluster total** | **~154 MiB** | < 0.05 cores | 3 MiB |

**對比傳統方案** (9,600 rules @ 100 tenants estimate, ~60 KB/rule)：~600 MiB+ → Dynamic Alerting saves **~75%** memory.

**Per-tenant 邊際成本**：`user_threshold` series ~4/tenant. 102-tenant under-load 實測 `user_threshold` series **正好 408 = 102 × 4**，per-tenant 線性模型成立。

**Cardinality 預估**：

| Tenants | Estimated series | Prometheus RSS budget |
|---:|---:|---:|
| 100 | ~1,500 | ~150 MiB |
| 1,000 | ~15,000 | ~180 MiB |
| 5,000 | ~75,000 | ~300 MiB |

> 實際 cardinality 取決於 label combinations (dimensional thresholds 約佔 5% configs)。Under-load 線性模型實測驗證。

**Under-load 行為 (100 synthetic tenants 注入)**：

| 指標 | Before | After | Delta |
|---|---:|---:|---:|
| Prometheus RSS | 168.2 MiB | 168.6 MiB | +0.4 MiB |
| Active series | 7,338 | 7,378 | +40 |
| Eval time | 65.3 ms | 29.8 ms / 86.6 ms (3 rounds) | within range |
| `user_threshold` series | 8 | 408 | × 51 (= 102 × 4) |

100 個 synthetic tenants 對 Prometheus 記憶體影響 median ~+0.4 MiB（GC 噪音範圍內）。

---

## 7. 量測方法論

**統計要求**：
- pytest-benchmark：min_rounds=20, warmup enabled, report median
- Go micro-bench：`-count=5 -benchtime=3s`, report median
- E2E fire-through (§4)：n=30, bootstrap 95% CI
- Soak (§5)：60 min, 30s warmup skip, drift = (last - first) / first × 100%

**Idle-state**：Kind 單節點 (Intel Core 7 240H), 2 tenants, 237 rules, 43 rule groups, 5 rounds @ 30s interval

**Under-load (§6)**：每輪獨立執行（避免 port-forward 連續不穩定 — 詳 Benchmark Playbook）

**完整方法論 + 踩坑記錄**：[Benchmark Playbook](internal/benchmark-playbook.md)

---

## 8. 進一步閱讀

| 內容 | 文件 |
|---|---|
| **完整 micro-bench numbers** (`Resolve_*` 系列 / synthetic fixture gen / schema validation / pytest-benchmark) | [Benchmark Playbook §Engineering Reference Benchmarks](internal/benchmark-playbook.md#engineering-reference-benchmarks) |
| **`IncrementalLoad_*` / `ScanDirFileHashes_*` 1000-tenant baseline** | [Benchmark Playbook §v2.8.0 1000-Tenant Hierarchical Baseline](internal/benchmark-playbook.md#v280-1000-tenant-hierarchical-baseline-phase-1-b-1) |
| **量測踩坑 & ops** (port-forward stability / `BENCH_OUT_DIR` isolation / `bench_wrapper.sh`) | [Benchmark Playbook §踩坑記錄 Lessons Learned](internal/benchmark-playbook.md#踩坑記錄-lessons-learned) |
| **Bench-gate-pr CI 機制** (Tier 1 / override label / sharding) | [Bench Gate Rollout](internal/bench-gate-rollout.md) · `.github/workflows/bench-gate-pr.yaml` |
| **Architecture & ADR 引用** | [Architecture & Design](architecture-and-design.md) · [ADR-016](adr/016-conf-d-directory-hierarchy-mixed-mode.md) · [ADR-017](adr/017-defaults-yaml-inheritance-dual-hash.md) |
