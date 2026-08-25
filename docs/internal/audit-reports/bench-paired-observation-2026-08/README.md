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
| **收** | ⑴ 逐夜的 `outcome`（`counted` / `not-counted` / `unreadable`）、`reason`、run id、head sha、兩顆對照測試的偏離、drift／digest 狀態（`nights.jsonl`）<br>⑵ **序列層級的 `fired` 集合**——哪些 benchmark 觸發、首次 fire 的日曆夜、由哪一次觀測 run 報出（見下節） |
| ⛔ **不收** | **任何比率**。比率取決於窗口，窗口取決於哪幾次 run 成功——那是會變的量；夜層級 outcome 與「哪一支 fire 了」不是 |

⚠️ **⑵ 是後來補的，原本沒有。** 初版只收夜層級事實，明文寫著「⛔ 只收夜層級事實」。盲審指出：ADR §待決 6 的**第三個主指標，分母就是這個 `fired` 集合**——若本檔不收，那個指標**在建構上就無法被任何讀者覆核**。⛔ 那正是本 ADR 全線在防的病。故擴充，並保留「不收比率」這條原則不變（`fired` 是判定結果，不是比率）。

⛔ **為什麼要收**：這些事實目前只活在 GitHub job log 裡。隔壁那份 README 記過同一個理由——job log 有保留期限、artifact 只留 90 天（`retention-days: 90`），過了就再也重建不回來。

## `fired` 集合（ADR §待決 6 第三個主指標的分母）

參數為生產環境實際使用的預設值：`--threshold-pct 5.0`、`--consecutive 2`（`paired_trend_watch.py:140-141`；`bench-record.yaml:572` 的呼叫未覆寫兩者）。

| benchmark | 首次 fire（live 判定） | 首次出現於哪次觀測 | `reference_sha` |
|---|---|---|---|
| `BenchmarkMergePartialConfigs_1000` | 2026-08-18 | run `32687665133`（08-24） | ⛔ **未記錄** |
| `BenchmarkResolveSilentModes_1000` | 2026-08-24 | run `32806126050`（08-25） | ⛔ **未記錄** |

⇒ 截至 2026-08-25，分母 = **2**。

⛔ **來源與其保存期限，必須跟數字一起讀。** 這兩筆來自兩次 `paired-trend-watch` job 的**摘要輸出**，而**那份摘要不在 repo 裡**——它活在 GitHub Actions 的 job log，會過期。本節存在的唯一理由就是把它固定下來；⚠️ 在此之前，「2」這個數字在 repo 內**查無依據**。

### ⛔ 這張表目前缺唯一鍵的一半

ADR §待決 6 定義第三個主指標的唯一鍵是 **`(benchmark, reference_sha)`**，理由是 re-pin 之後同一支再 fire 是**另一件事**，算成同一筆會把「已接受的成本」偷偷續期。**而本表沒有 `reference_sha`，`nights.jsonl` 也沒有。**

⇒ **讀者目前無法判斷這兩筆是否落在同一個 reference pin 期間內，也就是無法完整重建分母。** 這一節存在的理由是「讓分母可覆核」，而它目前只做到一半——被自己的契約點名的那一半。

已知的部分：ADR §待決 5 的封存資料集其 `reference` pin 為 `3fd96b51…`（`bench-paired-2026-08/nights.json` 頂層），且觀察期內唯一一次有紀錄的跨夜比對（08-23→08-24）reference pin 為 `same`。⛔ **但那不足以推斷整段觀察期未 re-pin**，故本表據實留白而不填。

**必修**：後續每次追加觀測時一併記 `reference_sha`（工具側有這個量——`paired_trend_watch.py:761-764` 的 `reference_pin_changed`），並在期末結算前補齊。**在補齊之前，第三個主指標不得結算。**

### ⚠️ 與錨點資料集的「1」為什麼不同 —— 兩個夜集合是巢狀的，差異可完全歸因

錨點 [`bench-paired-2026-08/README.md`](../bench-paired-2026-08/README.md) 在同一組參數下記 `1 fire(s): MergePartialConfigs_1000@08-17`。差異的原因是：

- 錨點涵蓋 **08-16..08-21**，live 窗口涵蓋 **08-16..08-24**，⇒ **後者包含前者**（巢狀，非不相交）
- `BenchmarkResolveSilentModes_1000` 滿足 k=2 的那一對相鄰夜是 **08-23 + 08-24**，落在錨點範圍之外

⇒ 差異**完全可歸因於多出來的三夜**，兩個數字**可以比較、而且對得起來**。

⛔ **本節前一版把理由寫成「該檔另記其 median +4.05%，在那六夜上本就不到 5% 門檻」——那是假的，已刪除。** 盲審實測錨點原始資料：

```shell
$ # BenchmarkResolveSilentModes_1000 在錨點六夜的逐夜值
2.24  4.74  5.31  3.37  2.36  5.34      ← 08-18 與 08-21 兩夜 > 5.0
$ python3 scripts/tools/dx/paired_trend_watch.py --dataset docs/internal/audit-reports/bench-paired-2026-08
### Over the threshold, but not sustained
| `BenchmarkResolveSilentModes_1000` | 2 (08-18, 08-21) | +5.34% |
```

⇒ 它在錨點六夜裡**兩夜超過門檻**，沒 fire 的真正原因是**那兩夜不相鄰**、k=2 從未滿足。⚠️ 原文用 median 去論證一條逐夜比較的規則——`fires()`（`:637-651`）從不計算 median，那個 +4.05% 出自該檔 §4 的 IQR 推導，**與 5%/2 夜規則不同軸**。把一個 near-miss 寫成 comfortable miss，反而弱化了本節的論證。

⚠️ **首次 fire 日期為 live 判定值。** 錨點把 `MergePartialConfigs_1000` 記為 **08-17**、live 記 **08-18**——正是本檔下面那節說的「同一個退化，歸因日期差一天」。

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

```shell
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
- ⚠️ 08-25 **那一夜**已產出（run `32806126050`）但**尚未被任何一次 `paired-trend-watch` 觀測到**——它會出現在 08-26 那次的窗口裡

### ⛔ 一個 run id 有兩個身分，讀本檔前必須先分清楚

`bench-record` 與 `paired-trend-watch` 是**同一個 workflow run 裡的兩個 job**（`.github/workflows/bench-record.yaml:36` 與 `:521`）。所以同一個 run id 會以兩種身分出現在本檔：

| 欄位 | 該 run 的身分 | 意思 |
|---|---|---|
| `run_id` | **被觀測者**（`bench-record` job） | 這一夜的量測是那個 run 產出的 |
| `observed_in` | **觀測者**（`paired-trend-watch` job） | 那個 run 的判斷引擎讀到了這一夜 |

⛔ **觀測者的窗口不含自己那一夜**——它讀的是先前的 run。所以 `32806126050` 同時是「08-25 這一夜的產出 run」（尚未被任何人讀到）**與**「讀了 08-16..08-24 的觀測者」，兩者都成立、不矛盾。

⚠️ 這一節是 CodeRabbit 在 [#1570](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/1570) 把上述兩處讀成互相矛盾之後補的。它的**事實判斷是錯的**（沒有矛盾），但它指出的問題是真的：一份存在目的就是「讓別人日後能複驗」的紀錄，讓認真的讀者讀成矛盾，那是本檔的責任。
