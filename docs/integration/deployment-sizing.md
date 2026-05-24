---
title: "部署容量規劃指南"
tags: [deployment, sizing, memory, performance]
audience: [platform-engineer, sre, devops]
version: v2.8.1
lang: zh
---
# 部署容量規劃指南

> **Language / 語言：** **中文 (Current)** | [English](./deployment-sizing.en.md)

> **受眾**：Platform Engineers、SREs、DevOps
> **前置文件**：[GitOps 部署指南](gitops-deployment.md)

---

## 概述

本指南協助你為 `threshold-exporter` 設定記憶體 limit，並判斷是否需要動用記憶體調節 lever。核心觀念一句話：

> **`threshold-exporter` 的記憶體會隨「reload 次數」緩慢 high-water creep，但不是 leak。** 預設配置（128Mi limit）對 ≤1000 租戶綽綽有餘；只有在 reload 頻率異常高時，才需要 GOMEMLIMIT 或 FreeOSMemory lever。

調校前請先讀完[記憶體行為](#記憶體行為reload-pressure-下的-high-water-creep)一節，避免把正常的 GC pacing 誤判為記憶體洩漏而過度配置。

## 記憶體行為：reload pressure 下的 high-water creep

每次 config reload 都會做一輪 scan + YAML parse + merge，產生短命的 heap 配置。在 **sustained reload pressure** 下，Go runtime 傾向把回收後的 idle pages 留在手上（高水位），而不積極 return-to-OS：

- `go_memstats_sys_bytes`（OS 視角總記憶體）與 `go_memstats_heap_idle_bytes`（持有但未使用）會緩慢上漲。
- `go_memstats_heap_objects`（live object 數）**持平** — 代表沒有 reference-held leak。

這是 **Go runtime 的 GC pacing 行為，不是程式碼洩漏**。壓力測試證實：把 `GOGC` 調到 20（更積極 GC）時 creep 幾乎消失。原始 soak 數據與診斷見內部 [Benchmark Playbook §Memory characteristics under reload pressure](../internal/benchmark-playbook.md#memory-characteristics-under-reload-pressure-459)。

> ⚠️ 這個現象只在 **production-shape（數百～千租戶）** 工作集下才看得到。小型測試環境（個位數租戶）working set 太小，不會觸發高水位保留，因此「測試環境沒看到漲」**不代表** production 不會漲。

## 容量規劃公式：reload-interval × uptime

記憶體成長率與 **reload 頻率** 成正比，因此可用一個粗略的 proxy 估算：

> **記憶體成長 ∝ (1 / reload-interval) × uptime**

實務上 production 客戶的 config 變更頻率遠低於壓力測試：

| 情境 | reload 觸發頻率 | 相對成長率 | 說明 |
|---|---|---|---|
| Soak 壓測 | 每 15s | **基準 (1×)** | 人為極端壓力，非真實負載 |
| 高頻 GitOps | 每數分鐘 | ~10–50× 慢 | 活躍 multi-team 平台 |
| 一般客戶 | 每數小時～數天 | ~100–1000× 慢 | config change 才 reload |

換算：壓測觀察到約 4 MiB/hour 的 `sys_bytes` 成長；一般客戶（reload 頻率慢 100×）實際成長率約為其 1/100，且 Go runtime 的自我 bound 機制通常會在某點收斂，不會線性無上限。**結論**：絕大多數部署不需任何調校。

## 設定容器記憶體 limit

以 1000 租戶、aggressive soak 為上界參考，`sys_bytes` 峰值約 40 MiB。建議：

| 租戶規模 | `requests.memory` | `limits.memory` | 備註 |
|---|---|---|---|
| ≤ 1000 | 64Mi | **128Mi**（chart 預設）| 留 ~3× headroom 吸收 creep + scrape 突波 |
| 1000–5000 | 128Mi | 256Mi | scan/parse 工作集隨租戶線性成長 |
| 5000–10000 | 256Mi | 512Mi | 搭配 [sharding 評估](../internal/benchmark-playbook.md#sharding-決策建議empirical-not-extrapolated) |

原則：`limits.memory` 設為 steady-state `heap_inuse` 的 **3× 以上**，吸收 high-water creep 與並發 scrape，避免 OOMKilled。

## 記憶體調節 lever

三個 lever 皆 **預設關閉**（不改 Go runtime 既有行為）。動用順序：先 GOMEMLIMIT，不夠再加 FreeOSMemory，最後才考慮 reload-interval。

### GOMEMLIMIT（首選 soft ceiling）

Go 1.19+ runtime 原生讀取 `GOMEMLIMIT` 環境變數作為 **soft heap 上限**：逼近上限時 GC 變積極、並提早把 idle pages 還給 OS。這是壓制 creep 的首選。

```yaml
# values.yaml
exporter:
  goMemLimit: "96MiB"   # 建議起點 ≈ limits.memory × 0.75
```

設定原則：**高於** steady-state `heap_inuse`、**低於** 容器 `limits.memory`（留 GC 反應空間）。chart 會自動把它注入為容器 `GOMEMLIMIT` env，啟動 log 也會印出 effective 值供確認。

### `-free-os-mem-after-reload`（次選 explicit scavenge）

每次 reload 完成後顯式呼叫 `runtime/debug.FreeOSMemory()`，強制一次 GC + 立即 scavenge，把 idle heap 還給 OS。成本是每次 reload 多一次 STW GC。

```yaml
# values.yaml
exporter:
  freeOsMemAfterReload: true
```

**僅在**「GOMEMLIMIT 仍壓不住 creep」**且**「reload 頻率低到 per-reload GC 成本可忽略」時開啟。高頻 reload 環境不建議（STW GC 成本累積）。

### reload-interval

調高 `reloadInterval` 會降低 reload 頻率、給 GC 更多收斂時間，但會延遲 config 變更生效。chart **預設 30s 不變**；若你的 config 變更本就稀疏，可由客戶自行調高（例如 `5m`），不需改 chart 預設。

```yaml
# values.yaml
exporter:
  reloadInterval: "5m"   # config 變更稀疏的客戶可調高
```

## 監控訊號

| 訊號 | 來源 | 用途 |
|---|---|---|
| `go_memstats_sys_bytes` | Go runtime | RSS proxy；觀察整體成長趨勢 |
| `go_memstats_heap_released_bytes` | Go runtime | **return-to-OS 直接訊號**；GOMEMLIMIT / FreeOSMemory 生效時上升 |
| `go_memstats_heap_objects` | Go runtime | 持平 = 無 leak；上漲才是真正的洩漏警訊 |
| `da_config_free_os_memory_total` | exporter | FreeOSMemory lever 啟用時每次 reload +1；預設 0 |

建議告警：`sys_bytes` 逼近 `limits.memory` 的 90%（OOMKilled 前的早期警訊），而非單看絕對值成長。

## 決策速查表

| 症狀 | 動作 |
|---|---|
| `heap_objects` 持平、`sys_bytes` 緩漲 | ✅ 正常 GC pacing，**不需處理** |
| `heap_objects` 持續上漲 | ❌ 疑似真 leak，開 issue 走 profiling，不是本指南範圍 |
| reload 頻率高 + `sys_bytes` 逼近 limit | 設 `goMemLimit` ≈ `limits.memory` × 0.75 |
| 設了 GOMEMLIMIT 仍逼近 limit + reload 稀疏 | 加開 `freeOsMemAfterReload: true` |
| config 變更本就稀疏 | 調高 `reloadInterval`（例如 `5m`）|
