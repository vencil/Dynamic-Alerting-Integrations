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

⚠️ **⑵ 是後來補的，原本沒有。** 初版只收夜層級事實，明文寫著「⛔ 只收夜層級事實」。盲審指出：ADR §待決 6 的第三個通過條件**要對每一支 fire 過的 benchmark 做出書面判斷，而清單就是這個 `fired` 集合**——若本檔不收，**沒有人知道該裁決哪幾支**，那個條件在建構上就無法被任何讀者覆核。⛔ 那正是本 ADR 全線在防的病。故擴充，並保留「不收比率」這條原則不變（`fired` 是判定結果，不是比率）。

⛔ **為什麼要收**：這些事實目前只活在 GitHub job log 裡。隔壁那份 README 記過同一個理由——job log 有保留期限、artifact 只留 90 天（`retention-days: 90`），過了就再也重建不回來。

## `fired` 集合（ADR §待決 6 第三個通過條件的**工作清單**）

參數為生產環境實際使用的預設值：`--threshold-pct 5.0`、`--consecutive 2`（`paired_trend_watch.py:140-141`；`bench-record.yaml:572` 的呼叫未覆寫兩者）。

| benchmark | 首次 fire（live 判定） | 首次出現於哪次觀測 | `reference_sha` |
|---|---|---|---|
| `BenchmarkMergePartialConfigs_1000` | 2026-08-18 | run `32687665133`（08-24） | `3fd96b51…` |
| `BenchmarkResolveSilentModes_1000` | 2026-08-24 | run `32806126050`（08-25） | `3fd96b51…` |

⇒ 截至 2026-08-25，待判斷 **2** 支，**兩支皆尚無書面判斷**。⛔ **這是一份工作清單，不是分母**——ADR §待決 6 的第三個通過條件是「每一支都必須有一則有依據、有日期的書面判斷」，不算比例。

**判斷記在這裡**（目前為空，故該條件現在的狀態是**不通過**——這是實情，不是待辦事項的委婉說法）：

| benchmark | 判斷 | 依據 | 判斷日期 |
|---|---|---|---|
| `BenchmarkMergePartialConfigs_1000` | — | — | — |
| `BenchmarkResolveSilentModes_1000` | — | — | — |

⛔ **這張表就是條件的範圍本身**——ADR 的通過條件錨在「本表列了誰」，不錨在「所有曾經 fire 過的支」。⚠️ 前一版在此寫了兩條規則（聯集不移除／re-pin 需新判斷），**已拆掉**：查證後**工具兩條都不執行**（`paired_trend_watch.py` 無跨 run 累積；`fires()` 只回首次 fire 夜、無 re-pin 概念），它們只是讀起來像規則。⇒ 保留的是**資料**：`reference_sha` 欄讓讀者自己看得出某則判斷是在哪個 pin 下做的。

⛔ **因此本表的完整性是一條「限制」而不是「保證」**：它只反映**有人追加進來的**觀測。⇒ ADR §待決 6 據此加了一道**結算前的完整性檢查**：觀察期內每一個日曆夜在 `nights.jsonl` 都必須有一列，**有缺夜就不得結算**。⭐ 那不是新機械，是對本檔做**日期連續性的算術**——一夜一列，缺不缺看檔案就知道。⚠️ 它擋得住「整夜漏追加」，⛔ **擋不住**「該夜有列、但那次觀測的 `fired` 沒被抄進上面那張表」——後者仍是純人工，據實寫明。

⛔ **來源與其保存期限，必須跟數字一起讀。** 這兩筆來自兩次 `paired-trend-watch` job 的**摘要輸出**，而**那份摘要不在 repo 裡**——它活在 GitHub Actions 的 job log，會過期。本節存在的唯一理由就是把它固定下來；⚠️ 在此之前，「2」這個數字在 repo 內**查無依據**。

### `reference_sha` 全期唯一，可從 repo 判定（可複驗）

記這一欄的理由是 **§待決 7 的裁決在 re-pin 之後是否失效，是那一節四個未裁決子題之一**（⚠️ 前一版的理由是「第三個主指標唯一鍵的一半」，那個比率形式已撤回，見 ADR）。本觀察期的答案是**同一個 pin，零 re-pin**：

```shell
$ git log --follow --format='%h %cs %s' -- .github/bench-reference.yaml
c7d0586 2026-08-22 feat(dx): …補齊範圍，並加一個會變的量 (#1502)
9599bdd 2026-08-15 feat(dx): 夜跑成對交錯量測管線 —— 參考版本釘死在檔案裡… (#1441)
$ git show c7d0586 -- .github/bench-reference.yaml | grep -E '^[+-]\s*(tag|sha):'
（無輸出 —— 那次只動 workload_closure，未觸 tag/sha）
$ grep -E '^\s*(tag|sha):' .github/bench-reference.yaml
tag: exporter/v2.9.0
sha: 3fd96b51f52e61566bb12c4c3fa23fed7e34dfa0
```

再加兩件事把路徑封死：⑴ 夜跑取 reference 的**唯一**來源就是該檔（`bench-record.yaml:121-142` 以 `sed` 抽 `tag:`/`sha:`、shape-check 後 checkout **sha**——tag 是可變的所以刻意不用）；⑵ `workflow_dispatch:` **沒有任何 `inputs:`**，單次 run 無從覆寫。⇒ 觀察期（含錨點六夜，其 `nights.json` 頂層 `reference` 與此**逐字相同**）全程 `3fd96b51…`。

⛔ **本節前一版把這一欄填「未記錄」，還據此訂了一道「補齊前不得結算」的閘門——兩者都撤回。**

⚠️ 那是把「**我沒去查**」寫成「量不到」。這是本 ADR 核心紀律的**鏡像違反**：全線在防「量不到渲染成沒問題」，而這裡是「**量得到渲染成量不到**」。兩者都是對「我們知道什麼」作出的假陳述——而後者的具體代價是憑空造出一道不存在的阻塞。⇒ 判準補一句：**宣告「量不到」之前，先證明它量不到。**

**仍然要做的**：後續追加觀測時一併記 `reference_sha`（工具側有 `paired_trend_watch.py:761-764` 的 `reference_pin_changed`），因為**日後 re-pin 是遲早的事**——那時 §待決 7 才需要回答「已接受的裁決要不要失效」。

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

⇒ 它在錨點六夜裡**兩夜超過門檻**，沒 fire 的真正原因是**那兩夜不相鄰**、k=2 從未滿足。⚠️ 原文用 median 去論證一條逐夜比較的規則——`fires()`（`paired_trend_watch.py:624-652`）從不計算 median，那個 +4.05% 出自該檔 §4 的 IQR 推導，**與 5%/2 夜規則不同軸**。把一個 near-miss 寫成 comfortable miss，反而弱化了本節的論證。

⚠️ **首次 fire 日期為 live 判定值，而且這是刻意的裁決、不是預設。** 錨點把 `MergePartialConfigs_1000` 記為 **08-17**、live 記 **08-18**——同一個退化、同一個 `reference_sha`（見上）。⚠️ **這個張力隨比率形式一起消失了**：現在的通過條件是「這一支有沒有人裁決」，不需要選定一個「正確的首次 fire 夜」。本檔兩個日期都記著——live 的 **08-18**（本清單採用，因為清單來自 live 判斷路徑）與錨點的 **08-17**（同一退化的另一種歸因，其依賴的 08-16 已被效度準則排除）。⇒ 差異是**已知且已解釋**的，不需要裁決哪個才算數。

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

⛔ **本檔就是「調參」資料，因此不得日後被當成「驗收」資料。** 順序是：本期量出分布 → 訂 δ₁ → **另起**驗收期。⚠️ 前一版把這句寫成「本檔不得用於調任何門檻」——**寫反了**，而且與 ADR「δ₁ 門檻要靠這段觀察量出分布」直接矛盾（CodeRabbit 在 [#1570](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/1570) 指出）。已更正。

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
