---
title: "ADR-032 §待決 6 驗收指標：並行期逐夜觀察紀錄"
tags: [adr, benchmark, measurement, observation]
audience: [platform-engineers, contributors]
version: v2.9.0
lang: zh
---

# 並行期逐夜觀察紀錄（ADR-032 §待決 6）

> 本檔收的是 [ADR-032](../../../adr/032-paired-interleaved-bench-measurement.md) §待決 6 驗收指標的**原始觀測**。
> ⛔ 它**不是**隔壁 [`bench-paired-2026-08/`](../bench-paired-2026-08/README.md) 那份錨點資料集，兩者不可混用（見下）。

## 這裡收什麼、不收什麼

| | |
|---|---|
| **收** | 逐夜的 `outcome`（`counted` / `not-counted` / `unreadable`）、`reason`、run id、head sha、兩顆對照測試的偏離、drift／digest 狀態 |
| ⛔ **不收** | **任何比率**。比率取決於窗口，窗口取決於哪幾次 run 成功——那是會變的量；夜層級 outcome 不是 |

⛔ **為什麼要收**：這些事實目前只活在 GitHub job log 裡。隔壁那份 README 記過同一個理由——job log 有保留期限、artifact 只留 90 天（`retention-days: 90`），過了就再也重建不回來。

## ⚠️ 觀察期是回溯認定的（明文揭露）

觀察期定為 **2026-08-17 起**，而本檔建立於 **2026-08-25**——也就是說，**其中 8 夜是在看到結果之後才被劃進觀察期的**。

這件事必須揭露，因為事後劃定窗口是一種選擇偏誤的入口。判斷它可不可接受的依據：

- 本檔記錄的是**夜層級事實**（那一夜讀不讀得到），不是任何調過的門檻
- 記錄當下 **δ₁ 的數值一個都還沒選**——沒有「調到好看」的對象
- 起點 08-17 由下面那條效度準則決定，**不是**由「哪一天的數字好看」決定

⛔ 若日後有人用本檔支持某個 δ₁ 值，這一節必須一併被讀到。

## 效度準則（決定哪些夜進分母）

> **分母只含「產出端當時已能寫出消費端必需欄位」的夜。**

這是**效度準則**不是排除規則：它問的是「當時這個功能上線了沒」，**完全不看該夜的判定結果**，所以不循環。求值方式（依優先序）：

1. 該 run 的 artifact 內有沒有 `bench-paired.json`（`paired_trend_watch.py` 已在算，reason 為 `run predates the paired pipeline`）
2. 該 run 的 `head_sha` 是否為 `60f4523`（加入 `status` 欄位的 commit）的後代

⚠️ **這條準則排除了什麼，必須看得見**：目前它只排除 **2026-08-16 一夜**。⛔ 那一夜的 `outcome` 與 `reason` **仍然記在本檔**（`in_denominator: false`），不是刪掉——讓讀者能自己判斷這是效度準則還是遮羞布。

⚠️ **未被它排除的 unreadable 一律留在分母**：artifact 下載失敗、夜跑自報 INCONCLUSIVE、payload 型別錯、缺 `ratios_pct` 等等，全部算數。

## 為什麼 08-16 被排除（可複驗）

```
$ git show 9599bdd:scripts/tools/dx/pair_bench_ratio.py | grep '"schema"'
        "schema": "bench-paired/v1",          ← payload 內無 status
$ git show 60f4523:scripts/tools/dx/pair_bench_ratio.py | grep -E '"schema"|"status"'
        "schema": "bench-paired/v1",          ← 仍是 v1
        "status": "OK",                       ← status 在此加入，schema 未 bump
$ git merge-base --is-ancestor 90391b5f 60f4523 && echo YES
YES                                            ← 08-16 的 head 早於 60f4523
```

⇒ 08-16 的 artifact 是一份**合法的舊 v1**，消費端要求 `status` 存在故判 unreadable。

⛔ **這不是化妝品問題。** 隔壁錨點資料集（能讀到 08-16）記 `BenchmarkMergePartialConfigs_1000` 於 **2026-08-17** 首次 fire；live 讀不到 08-16，同一支記 **2026-08-18**。**同一個退化，歸因日期差一天。**

## ⛔ 與隔壁那份錨點資料集的關係

| | [`bench-paired-2026-08/`](../bench-paired-2026-08/README.md) | 本檔 |
|---|---|---|
| 用途 | **錨點**：測試逐字重播、驗判斷引擎沒漂 | **觀測**：驗收指標的原始資料 |
| 來源 | job log 逐夜解析（`bench-paired-series/v1`） | `paired-trend-watch` job 的 Nights 表 |
| 含 08-16 | ✅（該格式補上了 raw artifact 沒有的欄位） | ✅ 但 `in_denominator: false` |
| 可否 append | ⛔ **不可**——`tests/dx/test_paired_trend_watch.py` 斷言 `counted == 6` 與逐一 fire 日期 | ✅ 逐夜追加 |

⛔ **本檔不得用於調任何門檻。** ADR §待決 6 明訂觀察期與驗收期分離：用來訂門檻的資料不得同時當驗收資料。

## 目前狀態（截至 2026-08-25）

- 分母內 **8 夜**（08-17 .. 08-24），`unreadable` **0**、`not-counted` **0**
- 分母外 **1 夜**（08-16，效度準則排除）
- ⚠️ 08-25 的 run 已存在（`32806126050`）但**尚未被任何一次 `paired-trend-watch` 觀測到**——它會出現在 08-26 那次的窗口裡
