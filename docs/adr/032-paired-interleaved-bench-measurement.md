---
title: "ADR-032: 夜跑效能監測改用成對交錯量測，取代跨夜滑動錨點"
tags: [adr, benchmark, ci, perf-trend, measurement]
audience: [platform-engineers, contributors]
version: v2.10.0
lang: zh
id: ADR-032
tracking_kind: adr
status: proposed
domain: dx
created_at: 2026-08-15
updated_at: 2026-08-15
---

# ADR-032: 夜跑效能監測改用成對交錯量測，取代跨夜滑動錨點

## 狀態

🟡 **Proposed（草案，未實作）**。本 ADR 提出把 `bench-record.yaml` 夜跑的量測拓撲，從「今夜 vs 窗內較舊的夜」改為「同機同時量兩個 build，比其比值」。

⚠️ **本文的核心宣稱尚未端到端驗證。** 已量到的是這個做法的**前提**（見 §證據），不是它的效果。要證明效果只能實跑一次成對夜跑——這是本 ADR 提議的第一個里程碑，不是它的結論。

Tracker：[#1432](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1432)（關票側誤關）、[#1430](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1430)（aged-in 不發射）。前情：[#1396](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1396) / PR [#1431](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/1431)。

## 背景

`analyze_bench_history.py --trend-watch` 每夜比對最近 K 夜與**錨定 baseline**——後者是同一個 14 夜窗內較舊、已 settled 的夜的中位數。錨點因此是**滑動**的：它由資料自己算出來，資料一變它就跟著變。

這個形狀已經生出三個各自獨立的問題：

- **#1396（已修，代價未消）** —— GitHub hosted runner 是異質的，混機器的錨點比的是 CPU 不是 commit。修法是同類主機分層 + 三態，代價是**約 1/3 的夜變成 `INCONCLUSIVE`**（同類夜不足，無法評估）。
- **#1432（未修）** —— 永久退化只要在窗內待夠久就會變成新錨點，於是「不再超標」被讀成「已復原」而自動關票。
- **#1430（未修）** —— 同一個滑動錨點的另一面：aged-in 的退化變成新常態，所以**根本不會發射**。

三者共用一個根因：**參考點是「某一夜」，而夜會老、會換機器。**

## 證據

### 1. 誤關率：給夠夜數就是 100%

以驅動真實 `run_trend_watch` 的生命週期 harness 量測（20 支 bench × 11 個注入夜，每個注入 **+30% 永久 step**）：

| | |
|---|---|
| 開票 / 歸屬正確 | 220 / 220 |
| **誤關（runway ≥ 10 夜）** | **120 / 120 = 100%** |
| 注入 → 關票 | median **7 夜**（6–10） |
| 關票當下仍高出 | median **+31.8%**（max +114.9%） |

⚠️ 全體 220 個情境只有 120 個關票（54.5%），但那是**右截斷**——runway 不足的 100 個情境是序列先結束，不是安全。**不可引用 54.5%。**

⚠️ 此量測的 floor 恆為 5.0%（`canary_cv = 0.0`），因為重建序列缺 canary（見 §已知限制）。生產環境 floor 可能高到 10%，方向上會讓關票**更早**發生。

附帶發現：自動關票留言寫著 *"A new issue is filed if it regresses again."* —— 在 100 個關票後仍有夜可跑的情境中，**重新開票 0 次（0.0%）**。這句話對「單一永久退化」永遠是假的：錨點吸收 step 之後 findings 恆空，除非**第二個**退化疊上來。

### 2. 變異分解：機器身分是主導噪音項

30 夜 × 20 支 bench，相對標準差中位數：

| 層級 | 大小 | 交錯量測能消掉嗎 |
|---|---|---|
| **夜內抖動**（同批 5 樣本，已丟 warm-up） | **0.52%** | ❌ 消不掉，殘餘下限 |
| **同類跨夜** | **3.15%** | 大部分（時間相關漂移在同輪內相消） |
| **跨 CPU 類** | **10.82%** | ✅ 完全消掉 |

**跨類 / 夜內 = 20.8×。**

兩個推論直接掉出來：

- **現行 floor 結構上擋不住機器差異**：最低 floor 5%，跨類變異中位數 10.82%——噪音比門檻大一倍。這從另一個角度獨立重現了 #1396 的根因。
- **IO 重的 bench 受害最深**（`ScanDirFileHashes_1000` 跨類 **32.15%**、`IncrementalLoad_1000_NoChange` 27.01%），與「IO/CPU 比依 CPU 型號完全分離」一致。

## 決策

**夜跑改為對固定 commit 做成對交錯量測，watchdog 觀測比值序列。**

每夜在**同一台 runner、同一個 job** 內交錯執行兩側：

```text
base(=固定參考 commit), head, base, head, …   一輪一輪交錯
```

watchdog 看的是 `head / base` 的比值隨時間的走勢，而不是 `今夜 / 某個舊夜`。

**這不是新技術，是把本 repo 已經驗證過的做法搬到夜跑。** `bench-gate-pr.yaml` 走過 v1→v5 五次拓撲演化後收斂到同一形狀，機制已存在於 [`scripts/tools/ops/bench_interleave.sh`](../../scripts/tools/ops/bench_interleave.sh)，其檔頭記錄了理由與代價：

> On shared GitHub-hosted runners the machine drifts mid-run (thermal throttle, noisy neighbour, CPU frequency scaling)... **INTERLEAVE.** Run base and pr alternately, one round at a time (base, pr, base, pr, ...). Any time-correlated drift now hits **BOTH sides within the same round and cancels**

這條路是用四次假紅換來的：`#502 / #608 / #611 / #695`，其中 **#695 沒有任何 Go 變更卻仍轉紅**。

Go 官方的 `benchseries` 是同一結構（`Denominator: "baseline"` / `DenominatorHash: "baseline-commit"`），分子分母在同一次 trial 內量。

### 為什麼這同時解掉三個問題

| | 現行（滑動錨點） | 本提案（成對交錯） |
|---|---|---|
| 參考點 | 窗內較舊的**夜** —— 會滑動、會換機器 | 固定 **commit** —— 不滑動 |
| 機型變動 | 新 CPU 出現 → 同類 settled 夜歸零 → `INCONCLUSIVE`；**永遠在追** | 無感，機器身分在比值裡相消 |
| 需維護 | class 對照表、per-class 最小夜數、`unknown` 出口 | 無 |
| #1432 誤關 | 需凍結錨點補丁 | **結構上不存在**：沒有錨點可漂 |
| #1430 aged-in | 不解 | **結構上不存在**：參考點不隨時間移動 |
| 1/3 夜不可評估 | 是 | 不會發生 |

⛔ **關鍵差別不是「更準」，是「不需要窮舉機型」。** 分層是一條跑步機：hosted runner 的機型集合無上界，每出現一種新 CPU，同類夜就從零開始累積。成對量測不需要知道機器是什麼。

## 代價與風險

- **夜跑時間約翻倍**（要 build + 跑兩側）。參考點：PR gate 的交錯主迴圈在 `BENCHTIME=1s` 下約 **4 分鐘**（3s 時 12 分鐘），整個 gate ~8–10 分鐘。夜跑無等待壓力，此成本應可負擔——但**尚未實測夜跑側的數字**。
- **參考 commit 要選、要換。** 建議用「上一個 GA tag」，與六線 release 節奏對齊。換參考點會在比值序列上造成不連續，需要明確處理（見 §待決）。
- **殘餘噪音未知。** 落在 0.52%（同批樣本下限）與 3.15%（同類跨夜）之間；真值只能由實跑取得。floor 能降到多少**因此還不能宣稱**。
- **既有 release baseline asset 的角色會變。** `release-attach-bench-baseline.yaml` 每次 release 自動附上 `bench-baseline-<TAG>.txt`（已驗證存在，含 `cpu:` 表頭與 canary）。本提案下它不再是比對基準，而是稽核與冷啟動用。

## 否決的替代方案

| 方案 | 否決理由 |
|---|---|
| **凍結錨點**（#1396 曾實作，整組退回） | 五個獨立盲審找到 1 Critical + 5 High，且問題是四個結構性衝突而非漏 if；即使修好也只解 #1432，不解 #1430，更不解機型跑步機 |
| **per-class 參考點集合** | 正是本 ADR 要消滅的跑步機；且 v2.9.1 baseline 只在 EPYC 7763 上，30 夜窗中僅 20 夜同類 |
| **changepoint detection**（Chromium/MongoDB/Otava） | 需要 detector（我們沒有），且 catapult 自己的窗也只有 50 點——step 滑出窗一樣退化回 #1430。它真正有效的部分是「把偵測到的 step 凍結成物件」，而成對量測讓那件事變得不必要 |
| **control canary 正規化** | 查不到可信的公開實作；且 CPU canary 無法正規化 IO-bound bench——跨類變異在 IO 重的 bench 高達 32%，正是 IO/CPU 比分離所致 |

## 待決（Open Questions）

1. **參考 commit 的更換策略** —— 每個 GA tag 換一次？換的那夜比值序列不連續，watchdog 要怎麼處理才不會誤判為 step？
2. **殘餘噪音實測值** —— 決定 floor 能降到多少；未量到之前不得宣稱偵測力提升。
3. **夜跑 wall time 實測** —— 翻倍是估計，非實測。
4. **既有 14 夜窗與三態的去留** —— 比值序列上仍需要某種持續性判準（單夜比值跳動不該發射），但不再需要同類分層。
5. **遷移路徑** —— 新舊並行一段時間比對，或直接切換？並行成本是三倍夜跑時間。

## 已知限制（本 ADR 證據的）

- §證據 1 的 harness 用重建序列，**缺 canary**（`canary_rows_in_log` 在 30 夜全為 0）。canary 確實存在於偵測器實際讀的 artifact（已於 `bench-baseline-v2.9.1.txt` 驗證），是 job-log 重建過程遺失的。因此 floor 恆為最低值 5%。
- §證據 2 的「夜內抖動 0.52%」量自同批連續 5 個樣本；交錯輪次分散在整個 run 上，真實殘餘會**高於**此值。這是下限，不是預測。
- 兩項證據都不構成「誤關率會歸零」的證明。那需要實跑成對夜跑。

## 相關

- [`benchmark-playbook.md`](../internal/benchmark-playbook.md) —— 夜跑 watchdog 現行行為與十三條已知限制
- [`audit-reports/bench-trend-2026-08/`](../internal/audit-reports/bench-trend-2026-08/README.md) —— 30 夜真實序列與 counterfactual harness
- [`scripts/tools/ops/bench_interleave.sh`](../../scripts/tools/ops/bench_interleave.sh) —— 已存在的交錯量測機制
