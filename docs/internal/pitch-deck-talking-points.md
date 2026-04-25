---
title: "Pitch Deck Talking Points — v2.8.0 Phase 1 Baseline"
tags: [internal, pitch, sales, baseline]
audience: [maintainers, business]
version: v2.7.0
lang: zh
---

# Pitch Deck Talking Points — v2.8.0 Phase 1 Baseline

> **受眾**：Maintainers + business / sales（內部對外溝通協作）
> **版本**：v2.7.0（current canonical；Phase 1 baseline 量測於 v2.8.0 開發中）
>
> **相關文件**：[Benchmark Playbook §v2.8.0 1000-Tenant Hierarchical Baseline](./benchmark-playbook.md#v280-1000-tenant-hierarchical-baseline-phase-1-b-1) · [ADR-018: dual-hash hot-reload](../adr/018-defaults-yaml-inheritance-dual-hash.md) · [`config_debounce.go`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/components/threshold-exporter/app/config_debounce.go)

本文件整理 v2.8.0 Phase 1 baseline（PR #59 / merge commit `f1f14e7`, merged 2026-04-25）對應的客戶對話 talking points。每個 section 提供（a）一段技術錨點 + 客戶語言版本，（b）明確的「**不要這樣講**」清單以防 overclaim。**請先讀完本文件結尾的 Honest baseline disclaimer 再引用任何數字到對外材料**。

> **用詞釐清（避免混淆）**：本文件的 **Phase 1 / Phase 2** 一律指 `v2.8.0-planning §10` **B-1 Scale Gate program** 的兩個階段（Phase 1 = synthetic fixture baseline，已完成於 PR #59；Phase 2 = customer anonymized sample 校準 + full-stack e2e fire-through + hard SLO sign-off，gated on DEC-B，未啟動），**不是本 talking-points 文件自己的 phase**。本文件是 derivative artifact，會在 B-1 Phase 2 完成後**被同步更新**。

---

## 1. ADR-018 dual-hash 啟用的「Quiet defaults edit」noOp detection

### 技術錨點

PR #59 BlastRadius bench 初版用 `container_memory` 這個 key 變更 region-level `_defaults.yaml`，觀察到 `affected-tenants: 0`（每個 tenant 都 override 了該 key）。對應 `config_debounce.go` L313-318 的判斷：

```go
} else if defaultsChanged {
    if prev, ok := priorMergedHashes[tid]; ok && prev == mh {
        // 「Quiet defaults edit」— defaults 檔變了但合成後的 merged_hash 沒變
        noOp++
        IncDefaultsNoop()
```

`source_hash`（tenant YAML bytes）+ `merged_hash`（effective config canonical JSON）兩個 hash 是 ADR-018 的架構決策；上面 L313-318 的「prev == mh ⇒ noOp」是這套架構**啟用的**演算法。兩者 related but distinct。

### 客戶語言版本

> 客戶調整 platform-wide defaults（例如修改全域 CPU threshold）若被**所有受影響 tenant** 的 override 遮蔽，threshold-exporter 在 ~200 ms 內逐 tenant 判定「實際生效配置未變」，**對該批 tenant 不向下游 Alertmanager 發送 rule reload 信號**。已 customize 過的 tenant 不會被無謂打擾；未 override 的 tenant 仍會照常 reload。

### Talking bullets

- **ADR-018 引入 dual-hash**：`source_hash`（檔案內容）+ `merged_hash`（合成後 effective config）— 後者用 canonical JSON SHA-256 計算
- **Quiet edit 偵測（per-tenant）**：每個受影響 tenant 各自比對 prior vs recomputed merged_hash；不變者標 noOp、變者照常 reload。可能出現 mixed 結果（部分 tenant noOp、部分 reload）
- **計數器可觀測**：`da_config_defaults_change_noop_total` counter 記錄 quiet edit 次數，platform team 可審計
- **典型場景**：global defaults 註解修整、key 順序整理、被 tenant override 的 key 微調 — 客戶端服務不會被打擾
- **重要：noOp ≠ 零工作**：~200 ms 大部分花在 `config_debounce.go` L341 `diffAndReload` 尾段**無條件呼叫**的 `fullDirLoad`（1000-tenant 約 146-237 ms 主導 cost）；21 個 tenant merged_hash 重算只占 ~1 ms。節省的是**下游** Alertmanager rule re-evaluation cascade（Phase 2 優化候選：diff 階段全 noOp 時 skip 尾段 fullDirLoad，可再省 ~150 ms/tick）
- **Bench design 教訓**：blast-radius 想量真正影響面，要選 tenants 不 override 的 key（B-1 改用 `region_alert_schedule` → 21 affected）

### ❌ 不要這樣講

- ❌ **「microservices 不會 restart」** — threshold-exporter 是**單一 process**，不是 microservices；說錯會讓客戶以為架構是 service mesh 規模
- ❌ **「dual-hash 會自動跳過 reload」** — dual-hash 是 architecture（ADR-018）；noOp detection 是 algorithm（`config_debounce.go` L313-318）。**RELATED but DISTINCT**。dual-hash 啟用了 noOp 判定，但 noOp 是判定的**結果**，不是 dual-hash 本身的功能
- ❌ **「noOp 省下 reload 工作」** — 上游的 21 tenant merged_hash 重算（~200 ms）**還是花了**；省的是下游 Alertmanager rule cascade
- ❌ **「100% 不會誤觸發 reload」** — quiet edit 偵測只覆蓋「defaults 變更但 merged_hash 不變」的場景；source 變更（tenant YAML 自身改動）一律觸發 reload
- ❌ **「noOp 是 batch / 整批級別判定」** — noOp 是 per-tenant 標記。常見誤解：「只要有一個 tenant override 該 key 整個 reload 就跳過」— 錯。判定是逐 tenant 比對 merged_hash，可能出現 21 中 19 noOp + 2 reload 的 mixed 結果，下游 reload signal 仍會 fire（為那 2 個變動的 tenant）

---

## 2. Resource Footprint @ 1000 tenants

### 技術錨點

PR #59 baseline（[benchmark-playbook.md §Resource Baseline](./benchmark-playbook.md#v280-1000-tenant-hierarchical-baseline-phase-1-b-1)）：

| Metric | 1000-tenant 量測 | Notes |
|---|---|---|
| Heap after GC（steady state） | **0.46 – 0.50 MB** | 強制 `runtime.GC()` ×2 後 |
| Sys（total virtual memory） | **18 – 20 MB** | OS 視角 RSS-ish |
| Goroutines（benchmark steady） | **2** | main + test runner — 無 leak signal |
| Allocs / diff-reload op | ~1,000,000 | ~1K allocs/tenant，主要 YAML parse |

5000-tenant scaling 重跑：goroutines 仍 = 2，sys 42 MB（線性）— **無 leak signal at 5000-scale**。

### 客戶語言版本

> 1000 tenants 的 threshold-exporter 在 **~20 MB virtual memory**、**2 goroutines** 內穩定運行，5000-scale 也沒有 leak 訊號。Single-binary deployment 即可達 1000-tenant scale，**不需要 sidecar / sharding overhead**。

### Talking bullets

- **20 MB virtual memory @ 1000 tenants** — 比一個 Helm chart releaser sidecar 還小
- **0.46-0.50 MB heap-after-GC** — working set 極小，GC pressure 低
- **2 goroutines steady state** — 不是 goroutine-per-tenant 模型；併發在 in-process scan / debounce
- **5000-scale 仍 2 goroutines** — 量測 1000 / 2000 / 5000 三點都是 2，沒有隨 N 漂移 = 無 leak
- **線性記憶體成長**：1000=19 MB / 2000=29 MB / 5000=42 MB sys — predictable capacity planning
- **Single binary** — 沒有跨 process 通訊、沒有 sharding coordinator、沒有 leader election 開銷

### ❌ 不要這樣講

- ❌ **「永遠只用 20 MB」** — 這是 1000-tenant 的數字；5000-tenant 是 42 MB
- ❌ **「GC 完全沒有 pressure」** — 1000-tenant allocs/op ~1M（YAML parse 主導），4.17 M allocs/op for FullDirLoad；GC 有運作
- ❌ **「無 leak 已被證明」** — 我們量到 1000/2000/5000 三點 goroutine 數都是 2 → 沒有 leak signal。這是 **absence of evidence**，不是 proof of absence；長時間 soak test 仍待 B-1 Phase 2

---

## 3. Empirical Scaling Characterization

### 技術錨點

PR #59 三點實測（3-run median ms，[benchmark-playbook.md §Scaling Characterization](./benchmark-playbook.md#v280-1000-tenant-hierarchical-baseline-phase-1-b-1)）：

| Operation | 1000 | 2000 | 5000 | 1000→5000 ratio | Linearity |
|---|---|---|---|---|---|
| `scanDirHierarchical` | 51 | 105 | 273 | **5.35×** | 略 super-linear（+7% over linear）|
| `fullDirLoad` | 237 | 570 | 1097 | **4.63×** | 混合（variance 影響）|
| `BlastRadius` (defaults change) | 266 | 535 | 1308 | **4.92×** | near linear |
| `affected-tenants` (BlastRadius scope) | 21 | 42 | 105 | **5.0×** | 嚴格 linear |
| Sys RSS（resource） | 19 MB | 29 MB | 42 MB | **2.21×** | sub-linear |

### 客戶語言版本

> 從 1000 → 5000 tenants，scan / reload / blast-radius 時間 **near-linear** 增長（5× 規模 → 4.6-5.4× 時間），**無 super-linear 劣化**。Memory linear。10000-tenant 線性外推為 ~2 秒 reload — 但**這個外推未經實測**，不能寫進 SLA。

### Talking bullets

- **Scan path**：1000=51 ms → 5000=273 ms（5.35× / +7% over linear）— 沒有 O(N²) 級劣化
- **Cold full reload**：1000=237 ms → 5000=1097 ms（4.63×）— 適合 cold start，hot path 走 diffAndReload
- **Blast radius**：region defaults 變更，1000=266 ms（21 tenants）→ 5000=1308 ms（105 tenants）— 影響範圍與時間都嚴格 linear
- **Affected-tenants 嚴格 linear**：1 region × 3 envs × N/144 per leaf = 1000→21, 2000→42, 5000→105 — geometric expectation 完美對齊
- **Memory sub-linear**：1000=19 MB → 5000=42 MB（2.21×）— allocator amortization
- **Goroutine 數恆定 = 2**：在三個 scale 點都一樣 — 不是 N-thread fanout 模型

### ❌ 不要這樣講

- ❌ **「10000 tenants 約 2 秒 reload」 沒有附 caveat** — 這是線性外推，**未實測**。引用必須附「線性外推 / 不是實測」字樣
- ❌ **「scaling 完美 linear」** — `scanDirHierarchical` 5.35×（略 super-linear, +7%）；要嚴謹講「near-linear, no super-linear degradation observed up to 5000」
- ❌ **「Phase 1 數字可以寫進客戶 SLA」** — 不行。見下節 disclaimer
- ❌ **「Dev Container 量到的數字 = 客戶環境的數字」** — Dev Container CI runner 有 20-50% timing noise（observed），客戶 metal / cloud 環境會不同；引用實機數字前應重跑

---

## 4. ⛔ Honest Baseline Disclaimer ⛔

> 本 talking points 基於 v2.8.0 **B-1 Phase 1** synthetic fixture baseline（PR #59 / merge commit `f1f14e7`, merged 2026-04-25）。**不可作為客戶合約 SLA 承諾**。Definitive SLO sign-off 待 **B-1 Phase 2** customer anonymized sample 校準 + full-stack e2e fire-through 量測完成（DEC-B in `v2.8.0-planning §10`，內部 planning artifact）。
>
> 引用此文件的數字到 customer-facing materials（pitch deck / proposal / RFP response）時，**必須**附帶「B-1 Phase 1 synthetic baseline / pending B-1 Phase 2 customer-data validation」前綴。

### 為什麼 Phase 1 不夠？

- **Synthetic fixture 不等於客戶 workload**：`buildDirConfigHierarchical(b, N)` 用 8 domains × 6 regions × 3 envs = 144 leaf 的 geometric 分布。客戶真實 domain / region / env 比例**未知** — B-1 Phase 2 帶 customer anonymized sample 重跑才有代表性。
- **未含 alert fire-through e2e**：本 baseline 只量 internal SLO（scan / reload / blast-radius），**不含** Prometheus + Alertmanager + receiver 完整鏈路。客戶在意的「config change → alert visibly stops firing」這條 e2e latency **沒量過**（亦待 B-1 Phase 2）。
- **CI runner variance 20-50%**：Dev Container 量測有顯著 noise（observed: scan 32→51 ms, fullDirLoad 146→237 ms 跨兩次重跑）。SLA-grade 數字需 `count=10+` 且 isolated env。
- **10000-tenant 是線性外推**：5000 是上限實測點。10000 的 ~2 秒 reload 數字是 5000 × 2 的數學外推，**非量測**。

### 引用守則

| 場景 | 是否可引用本文件數字 | 必要前綴 |
|---|---|---|
| 內部 architecture review / contributor onboarding | ✅ 可 | 無 |
| Engineering blog / 公開技術文章 | ⚠️ 有條件 | 「Phase 1 synthetic baseline, lab fixture」 |
| Pitch deck / sales deck | ⚠️ 有條件 | 「B-1 Phase 1 synthetic baseline / pending B-1 Phase 2 customer-data validation」 |
| Customer proposal / RFP response | ⚠️ 有條件 | 同上 + 註明 B-1 Phase 2 校準時程 |
| 客戶合約 SLA 條款 | ❌ **禁止** | — |
| 公開 marketing 數字（網站 / press release） | ❌ **禁止** | — |

### B-1 Phase 2 完成後本文件如何更新

> 提醒：以下「Phase 2」= **B-1 Scale Gate Phase 2**（customer anonymized sample + full-stack e2e + hard SLO），不是 talking-points 文件自己的 phase。本文件不是 phased deliverable，而是會被同步更新的 derivative artifact。

1. B-1 Phase 2 完成 customer anonymized sample re-run + full-stack e2e fire-through 量測 + hard SLO sign-off（DEC-B 觸發）
2. 本文件 frontmatter `version` bump → 對應 release tag
3. 各 talking point 數字更新為 B-1 Phase 2 量測值；保留 Phase 1 數字於附錄供對照
4. 移除「pending B-1 Phase 2」前綴；引入正式 SLO 條款引用
5. **`docs/benchmarks.md` §12 同步升級**為 v2.8.0 calibrated baseline（雙語）— 公開 canonical perf doc 才算正式 promote。本文件「客戶語言版本」+「不要這樣講」永遠 stays internal（pedagogical artifact 不該進 canonical reference）

---

## 相關資源

| 資源 | 說明 |
|------|------|
| [Benchmark Playbook §v2.8.0 1000-Tenant Hierarchical Baseline](./benchmark-playbook.md#v280-1000-tenant-hierarchical-baseline-phase-1-b-1) | 完整方法論、fixture spec、量測指令 — talking points 的 Phase 1 數字 SOT |
| [docs/benchmarks.md §12 Incremental Hot-Reload + B-1 Scale Gate](../benchmarks.md#12-incremental-hot-reload-b-1-scale-gate) | 公開 perf doc（platform-engineer / sre 受眾，雙語）。**目前 §12 是 v2.7.0 B-1 baseline；v2.8.0 B-1 Phase 1 數字刻意不進 §12** — 待 B-1 Phase 2 customer sample calibrated 後再 promote 升級 §12（避免 Phase 1 disclaimer-laden 數字進 canonical doc 後產生 implicit anchoring） |
| [ADR-018: _defaults.yaml 繼承語意 + dual-hash hot-reload](../adr/018-defaults-yaml-inheritance-dual-hash.md) | dual-hash 架構決策原文（source_hash + merged_hash 定義、merge 語意） |
| [`config_debounce.go` L260-330](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/components/threshold-exporter/app/config_debounce.go#L260-L330) | quiet defaults edit noOp detection 實作（L313-318 prev == mh 判定） |
| [CHANGELOG `[Unreleased]`](../CHANGELOG.md) | Phase 1 baseline entry 與本文件交叉引用 |
| `v2.8.0-planning §10 DEC-B`（內部 planning artifact） | B-1 Phase 2 customer sample 校準時程 / definitive SLO sign-off 觸發條件 |
| [doc-map.md](./doc-map.md) | 內部文件導覽（自動產生） |
