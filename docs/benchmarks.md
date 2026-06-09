---
title: "性能基準 (Performance Benchmarks)"
tags: [performance, benchmarks]
audience: [platform-engineer, sre, decision-maker]
version: v2.9.0
lang: zh
---
# 性能基準 (Performance Benchmarks)

> **Language / 語言：** | **中文（當前）** | [English](./benchmarks.en.md)

> **受眾**：Platform Engineer / SRE（主）、企業決策者、Domain Expert（DBA/Infra）、租戶技術窗口
>
> **相關文件**：[架構與設計](architecture-and-design.md) · [測試覆蓋矩陣](internal/test-coverage-matrix.md) · 量測細節與踩坑 [Benchmark Playbook](internal/benchmark-playbook.md)

**如何讀**（讀者隨時可切換，且常多人同看一份）：

| 你是… | 先看 | 想深入再看 |
|---|---|---|
| 企業決策者 | TL;DR、[§3 容量](#3-容量怎麼-sizing) | [§5 可信度](#5-量測方法與可信度) |
| 租戶技術窗口 | TL;DR、[§1.2 自訂告警成本](#12-租戶自訂告警的成本另一條曲線) | [§2 速度](#2-速度告警多久-fire) |
| Platform / SRE | 全部 | — |
| Domain Expert（DBA/Infra） | [§1 規模](#1-規模能撐多少租戶)、[§5 方法](#5-量測方法與可信度) | [§4 穩定](#4-穩定長時間會-leak-嗎) |

> **多人同看**：先共享 **TL;DR + §1 規模 + §2 速度**（人人都懂的結論），再各自下鑽到自己關心的段落。本文件由淺入深排序——讀到語氣變「平台深入」處，非平台讀者即可停。

---

## TL;DR — 你需要的數字

| 你的問題 | 答案 | 出處 |
|---|---|:-:|
| **1000 個租戶能跑嗎？** | ✅ 冷啟動 **112 ms**、穩態 reload **1.3 ms** | [§1](#1-規模能撐多少租戶) |
| **規則評估會隨租戶數變慢嗎？** | ⚡ **不會** — 2 個或 102 個租戶都維持 ~60 ms（O(M) 設計） | [§1](#1-規模能撐多少租戶) |
| **告警多久 fire？** | 1000-租戶 P99 **4.98 s**、5000-租戶 P99 **4.98 s**（5× 規模幾乎持平） | [§2](#2-速度告警多久-fire) |
| **記憶體怎麼估？** | 40 MiB exporter + 150 MiB Prometheus（典型 100-租戶）；每租戶邊際 ~4 series | [§3](#3-容量怎麼-sizing) |
| **長時間 reload 會 leak 嗎？** | ✅ 無 goroutine / 物件洩漏（60 分鐘 sustained 壓力驗證） | [§4](#4-穩定長時間會-leak-嗎) |

這些是**現行（v2.9.0）有效基線**：核心 scale 與 soak 已在本版同機重驗無回歸；端到端與資源基線於前版建立，本版為 behavior-preserving 重構、由每個 PR 的自動回歸閘門持續守護（機制與版本溯源見 [§5](#5-量測方法與可信度)）。

---

## 1. 規模：能撐多少租戶？

### 1.1 平台規則：為什麼與租戶數無關（O(M)）

**傳統做法 O(N×M)**：N 個租戶 × M 條規則 = N×M 次獨立 PromQL 評估。100 租戶 × 35 規則 = 3,500 次。

**本平台 O(M)**：用向量匹配（`group_left`）讓 **1 條規則涵蓋所有租戶**。100 租戶 × 35 規則 = **35 次**評估，**與租戶數無關**。

實測印證——租戶數放大 51×，評估時間幾乎不動：

| 場景 | 租戶數 | 規則數 | 評估時間（中位數） |
|---|:-:|:-:|---:|
| 閒置基線 | 2 | 237 | **59.1 ms** |
| 加壓注入 | 102 | 237 | **60.6 ms** |

51× 的租戶增量只讓評估時間從 59.1 ms 變 60.6 ms（**+2.5%**）——完全符合 O(M) 設計。

**沒部署的指標 ≈ 零成本**：15 個規則包全部預載入，未部署對應 exporter 的包評估 < 1 ms（空向量計算近似 O(1)）。客戶**不需挑選規則包**、直接全裝，未用到的包幾乎不增加 Prometheus 開銷。

| 規則包 | 狀態 | 規則數 | 評估時間 |
|---|:-:|---:|---:|
| MariaDB | ✓ 啟用 | 7 | 2.12 ms |
| MongoDB | ✗ 無 exporter | 7 | 0.64 ms（空向量） |
| Redis | ✗ 無 exporter | 7 | 0.41 ms（空向量） |
| Elasticsearch | ✗ 無 exporter | 7 | 1.75 ms（空向量，PromQL 較複雜） |

**冷啟動與穩態 reload**（1000-租戶實測）：

| 路徑 | 100 租戶 | **1000 租戶** | 含意 |
|---|---:|---:|---|
| **冷啟動全量載入** | 3.2 ms | **112 ms**（~112 µs/租戶，線性） | Pod 啟動 / 全量重建一次 |
| **穩態 reload（無變更）** | ~129 µs | **1.30 ms** | reload ticker 每次成本（預設 15 s） |
| **單檔變更 reload** | 628 µs | ~6.3 ms（線性外推） | 客戶 commit 單一 tenant.yaml |
| **目錄掃描雜湊** | 128 µs | 1.30 ms | mtime guard 帶來 4.6× 加速 |

**穩態 reload 比冷啟動便宜 86×**——階層掃描 + 雙雜湊 + mtime guard 三層優化的合併效果。production 預設 reload 間隔 15 s，每次成本 ≈ 間隔的 0.0087%。

> 上表絕對值是在固定參考環境量測的基準；不同量測機的絕對時間會浮動 ~1.5×。本平台用**同機相對比對**驗證版本間無回歸（細節與最近一次比對見 [§5](#5-量測方法與可信度)），而非跨環境硬比絕對值。

### 1.2 租戶自訂告警的成本（另一條曲線）

平台規則的 O(M) 保證是對「平台 authored 的規則」成立。**租戶自訂告警**（v2.9.0 起）走另一條成本路徑，同樣刻意守住規模：

- **新增一種自訂告警 = 1 條跨租戶共用規則**，不是每租戶一條。編譯器把「同指標、同 recipe、同參數形狀」的所有租戶宣告，編成**單一**規則（規則數 = 不同的「告警形狀」數，不乘租戶數）。
- **成本誠實**：這條保證**只對「同指標共用」成立**——不同指標必然產生不同規則，所以規則總數隨**自訂告警的種類**增長、不隨租戶數。由每租戶上限（預設 20 種）封頂，防單一租戶灌爆。
- **給租戶**：你寫 YAML recipe、不碰 PromQL，告警秒級生效；而且不會因為你新增告警就拖慢整個平台。
- **給 Platform Engineer**：規則總數 = O(告警種類)、非 O(租戶數 × 告警種類)；容量規劃對「自訂告警種類數」估算，每租戶上限 + 全域規則數預算（規劃中）為護欄。

詳 [ADR-024 §向量化編譯](adr/024-version-aware-threshold-via-dimensional-label.md)。

---

## 2. 速度：告警多久 fire？

端到端量測涵蓋完整鏈路：`conf.d/` 寫入 → exporter reload → Prometheus 觸發 → Alertmanager 派送 → webhook 收件。量測在一個多服務容器堆疊上跑、用統計嚴謹的方式聚合（n=30 + bootstrap 95% 信賴區間）。

**1000-租戶基線**（[GHA run](https://github.com/vencil/Dynamic-Alerting-Integrations/actions/runs/24951460457)）：

| 量測 | P50 | P95 | P99 |
|---|---:|---:|---:|
| **告警 fire 延遲** | **4748.5 ms** | **4953.95 ms** | **4977.88 ms** |

**5000-租戶基線**（[GHA run](https://github.com/vencil/Dynamic-Alerting-Integrations/actions/runs/24955478536)）：

| 量測 | P50 | P95 | P99 |
|---|---:|---:|---:|
| **告警 fire 延遲** | **4763.5 ms** | **4971.55 ms** | **4984.07 ms** |

**1000 → 5000 租戶，P95 幾乎持平（+0.4%）**——證實主導延遲是 **Prometheus 5 s scrape 量化間隔**，不是 exporter 掃描時間。從 1000 擴到 5000 租戶，**客戶看到的告警延遲不增加**。

> 客戶導入若需 SLA 合約用的延遲數字，可對客戶實際 fixture 形狀重新校準（±30% gate）後再 promote 為合約數字。校準流程見 [Benchmark Playbook](internal/benchmark-playbook.md#v280-phase-2-e2e-alert-fire-through-b-1-phase-2)。

---

## 3. 容量：怎麼 sizing？

**閒置基線**（單節點，2 租戶，237 規則）：

| 元件 | RSS | CPU（5 分均） | 儲存 |
|---|---:|---:|---:|
| Prometheus | **148.1 MiB** | 0.017 cores | 3.0 MiB TSDB |
| threshold-exporter ×2（HA） | 6.2 MiB | < 0.005 cores | — |
| **叢集合計** | **~154 MiB** | < 0.05 cores | 3 MiB |

**對比傳統方案**（100 租戶估 9,600 條規則、約 60 KB/規則）：~600 MiB+ → 本平台省下 **~75%** 記憶體。

**每租戶邊際成本**：`user_threshold` series 約 4 條/租戶。102-租戶加壓實測 `user_threshold` series **正好 408 = 102 × 4**，線性模型成立。

**容量預估**：

| 租戶數 | 估計 series | Prometheus RSS 預算 |
|---:|---:|---:|
| 100 | ~1,500 | ~150 MiB |
| 1,000 | ~15,000 | ~180 MiB |
| 5,000 | ~75,000 | ~300 MiB |

> 實際 cardinality 取決於 label 組合（dimensional threshold 約佔 5% 設定）。

**加壓行為**（注入 100 個合成租戶）：

| 指標 | 前 | 後 | 變化 |
|---|---:|---:|---:|
| Prometheus RSS | 168.2 MiB | 168.6 MiB | +0.4 MiB |
| Active series | 7,338 | 7,378 | +40 |
| `user_threshold` series | 8 | 408 | × 51（= 102 × 4） |

100 個合成租戶對 Prometheus 記憶體的影響中位數 ~+0.4 MiB（GC 噪音範圍內）。

---

## 4. 穩定：長時間會 leak 嗎？

**結論先講**：60 分鐘 sustained reload 壓力下**記憶體安全、無洩漏**——goroutine 持平、reference-held 物件持平，RSS 有界且遠低於典型 pod limit。

最近一次（v2.9.0，乾淨量測機、60 分鐘 / 15 s reload、239 次 reload）：RSS 完全持平（**+0.0%**）、`heap_objects` −1.0%、goroutine 持平。

下表是一次更深入的四指標基線（production GC 預設模式）。其 `sys_bytes +12.4%` 與上面 +0.0% 的差異，純粹來自量測機與 GC 壓力的浮動——兩次都判定「有界、無洩漏」；保留這張表是因為它的 `heap_idle` 漲幅正好示範「為什麼 heap_idle 上升不是 leak」：

| 指標 | 起 | 迄 | 漂移 | 判定 |
|---|---:|---:|---:|:-:|
| `go_goroutines`（goroutine 洩漏偵測） | 10 | 9 | −10% | ✅ 無洩漏 |
| `go_memstats_heap_objects`（reference-held 洩漏） | 192,404 | 187,351 | −2.6% | ✅ 無洩漏 |
| `go_memstats_sys_bytes`（RSS proxy） | 35.0 MiB | 39.3 MiB | +12.4% | ✅ 有界（39 MiB ≪ 100–500 MiB pod limit） |
| `go_memstats_heap_idle_bytes` | 11.5 MiB | 17.4 MiB | +52% | ℹ️ Go 運行時特徵（見下） |

**判讀（confidence-first）**：

- ✅ **記憶體安全**：`sys_bytes` 穩定停在 39 MiB，**遠低於** 典型 k8s pod limit（100–500 MiB）。
- ✅ **無洩漏**：`heap_objects` 持平（−2.6%）證實沒有 reference-held 洩漏；goroutine 也維持（10→9）。
- ℹ️ **`heap_idle +52%` 不是 leak**：那是 Go GC 在極端壓力（15 s reload 間隔）下保留 OS pages 的 scavenger 預設策略。平行對照組（積極 GC 模式）同指標僅 **+0.1%**，反證這是 GC pacing 行為而非洩漏。

> **真實場景**：production 的 reload 頻率通常是 hours-to-days（config 變更），不是 15 s。實際成長率預估比此壓力測試慢 **10–100×**，上述 heap_idle 現象在客戶環境**不會發生**。針對 reload 頻率極高的部署，v2.9.0 已提供記憶體釋放 lever（`--free-os-mem-after-reload`，預設關閉，見 [#459](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/459)）。

---

## 5. 量測方法與可信度

**自動化回歸閘門**：每個 PR 都會對 merge-base 自動量測效能，統計上顯著的退化會**擋下 merge**（Tier 1 必過檢查）。這讓上述每個數字持續受守護，不是一次性快照。

**絕對值的版本溯源**：headline 的絕對值（如冷啟動 112 ms）是在固定**參考環境**量測的歷史基準；同一份 code 在不同量測機上的絕對時間會浮動 ~1.5×。因此版本間「有沒有回歸」**用同機相對比對**判定，而非跨環境硬比絕對值。最近一次（v2.9.0 exporter/loader 重構後）同機比對冷啟動：上一版 code **172 ms** vs 本版 **169 ms**，效能持平、無回歸。

**統計要求**：

| 量測類型 | 設定 |
|---|---|
| Go micro-bench（§1） | `-count=5 -benchtime=3s`，取中位數 |
| 端到端 fire-through（§2） | n=30，bootstrap 95% 信賴區間 |
| 資源 / cardinality（§3） | 每輪獨立執行，避免連續量測互擾 |
| Soak（§4） | 60 分鐘，跳過 30 s warmup，漂移 =（迄 − 起）/ 起 × 100% |

**量測環境**：單節點容器（Intel Core 7 240H、Go 1.26.2 linux/amd64），合成 fixture（每租戶含多個 metric threshold，涵蓋 scheduled override 與 regex dimensional）。端到端用貼近真實的長尾租戶分佈 fixture。

**production 可觀測性 metrics**（觀察 reload 行為）：

- `da_config_scan_duration_seconds`（histogram）
- `da_config_reload_trigger_total{reason}`（counter）
- `da_config_defaults_change_noop_total`（counter，cosmetic edits）
- `da_config_defaults_shadowed_total`（counter，override-shadowed defaults）
- `da_config_blast_radius_tenants_affected{reason,scope,effect}`（histogram）

完整方法論與量測踩坑：[Benchmark Playbook](internal/benchmark-playbook.md)。

---

## 6. 性能沿革（歷史，可選讀）

效能隨版本演進的關鍵節點——評估「平台成熟度」時可參考，日常使用不必細讀：

| 版本 | 關鍵優化 | 量化影響 |
|---|---|---|
| **v2.2.0** | 扁平 `conf.d/` + 逐檔 SHA-256 + mtime guard | 100-租戶冷啟動 **3.2 ms** 基線 |
| **v2.5.0** | 多租戶分群 + Saved Views（API 層，per-tenant 效能不變） | — |
| **v2.7.0** | 階層掃描 + 雙雜湊 + 階層狀態載入（[ADR-016](adr/016-conf-d-directory-hierarchy-mixed-mode.md) / [ADR-017](adr/017-defaults-yaml-inheritance-dual-hash.md)） | **1000-租戶冷啟動 112 ms；穩態 reload 比冷啟動便宜 86×** |
| **v2.8.0** | 端到端告警觸發量測 + 每-PR 效能回歸閘門 + 60 分鐘 readiness soak | 1000/5000-租戶 SLO 基線；每-PR 統計回歸守門 |
| **v2.9.0** | 自訂告警向量化編譯 + exporter/loader 重構（behavior-preserving） | 核心 scale 無回歸（同機控制確認）；租戶自訂告警 = O(告警種類)、非 O(租戶數) |

**遷移影響**：`conf.d/` schema 向後相容。扁平 layout 直接可用，階層 layout 為 opt-in（在子目錄放 `_defaults.yaml` 即自動啟用）。客戶不需重寫 tenant YAML。

---

## 7. 進一步閱讀

| 內容 | 文件 |
|---|---|
| **完整 micro-bench 數字 / 合成 fixture 產生 / schema validation / pytest-benchmark** | [Benchmark Playbook §Engineering Reference Benchmarks](internal/benchmark-playbook.md#engineering-reference-benchmarks) |
| **1000-租戶各路徑完整 baseline**（incremental / 掃描雜湊系列） | [Benchmark Playbook §v2.8.0 1000-Tenant Hierarchical Baseline](internal/benchmark-playbook.md#v280-1000-tenant-hierarchical-baseline-phase-1-b-1) |
| **量測踩坑 & ops**（port-forward 穩定性 / 輸出隔離 / wrapper script） | [Benchmark Playbook §踩坑記錄 Lessons Learned](internal/benchmark-playbook.md#踩坑記錄-lessons-learned) |
| **回歸閘門 CI 機制**（Tier 1 / override label / sharding） | `.github/workflows/bench-gate-pr.yaml` |
| **架構與 ADR 引用** | [架構與設計](architecture-and-design.md) · [ADR-016](adr/016-conf-d-directory-hierarchy-mixed-mode.md) · [ADR-017](adr/017-defaults-yaml-inheritance-dual-hash.md) |

### 本文件數字 ↔ 對應 benchmark（要重跑時用）

| 本文件用語 | 對應 benchmark / 量測來源 |
|---|---|
| 冷啟動全量載入（§1） | `FullDirLoad`（Go micro-bench） |
| 穩態 reload（無變更）（§1） | `IncrementalLoad_NoChange` + mtime guard |
| 單檔變更 reload（§1） | `IncrementalLoad_OneFileChanged` |
| 目錄掃描雜湊（§1） | `ScanDirFileHashes` + mtime guard |
| 端到端告警 fire（§2） | `make bench-e2e`（5-anchor harness，nightly `bench-record.yaml`） |
| Soak 漂移（§4） | `run_chaos_soak.py`（雙軌 GOGC=20 / GOGC=100） |
| 回歸閘門 | `bench-gate-pr.yaml`（Tier 1） |
