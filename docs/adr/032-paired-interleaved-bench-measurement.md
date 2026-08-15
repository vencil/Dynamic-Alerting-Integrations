---
title: "ADR-032: 夜跑效能監測改用成對交錯量測，取代跨夜滑動錨點"
tags: [adr, benchmark, ci, perf-trend, measurement]
audience: [platform-engineers, contributors]
version: v2.9.0
lang: zh
id: ADR-032
tracking_kind: adr
status: accepted
domain: dx
created_at: 2026-08-15
updated_at: 2026-08-15
---

# ADR-032: 夜跑效能監測改用成對交錯量測，取代跨夜滑動錨點

## 狀態

✅ **Accepted**（2026-08-15，未實作）。本 ADR 把 `bench-record.yaml` 夜跑的量測拓撲，從「今夜 vs 窗內較舊的夜」改為「同機同時量兩個 build，比其比值」。

接受的依據是 §證據 4 的跨機 A/A 實測。判準在看到任何資料前寫死（放大倍率 ≥ 3× 且跨機比較超過 5% floor 的比例 ≥ 5%），實測 **3.8×** 與 **12.5%**，兩條都過。

⚠️ **餘裕比第一版報告的薄得多，原因是第一版報告的對照組挑錯了。** 初判用「全體跨機配對」得到 37×，但那描述的是 **#1396 之前**的設計；現行夜跑已經同類分層，而且參考點是同類多夜的**中位數**而非單一夜。兩項都修正後才是 3.8×。門檻是 3×，**只多 0.8**，且該估計只有 n=48。詳見 §證據 4。

⚠️ **配對不是零誤差。** 1 輪交錯下仍有 3.1% 的比較超過 5% floor（6 輪為 1.6%）。本提案是把機器造成的誤報從 12.5% 壓到個位數，不是歸零。

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

### 3. A/A 實跑：⛔ 沒有測到交錯的效益

在一台 4 核共用雲端 VM 上跑 8 輪 `A,B,A,B,…`，**兩側是同一份編譯出的 binary**，故真實效應恆為 0%。同一批資料事後以兩種拓撲分析（18 支 bench、16 次呼叫、636 秒）：

| | median | max |
|---|---|---|
| **PAIRED（相鄰配對＝交錯）殘餘噪音 rSD** | **7.99%** | 11.73% |
| PAIRED 偏差（真值 0%） | 2.18% | 6.02% |
| SEPARATED（時間分離＝v5 拓撲）偏差 | 1.94% | 8.23% |

**偏差放大倍率 SEPARATED / PAIRED = 0.9×——交錯沒有勝出，還略輸。**

機器在該次 session 內確實有漂（side A 第 7–8 輪相對第 1 輪 **-5.4%**），所以交錯要對付的現象存在；但它被更大的東西蓋過去了。

當時提出的解釋是：該次用 `-test.count=1`，每輪每支 bench 只量一次（最慢的 `FullDirLoad_1000` 在 `benchtime=1s` 下只跑 4 次迭代），而交錯消得掉時間相關漂移、消不掉單次抽樣噪音。

**修正版實驗（`count=5`，4 支代表性 bench × 8 輪）推翻了這個解釋：**

| | count=1（18 支 bench） | **count=5（4 支 bench）** |
|---|---|---|
| PAIRED 殘餘噪音 rSD | 7.99% | **7.39%** |
| PAIRED 偏差 | 2.18% | 2.10% |
| SEPARATED 偏差 | 1.94% | 2.29% |
| **SEPARATED / PAIRED** | **0.9×** | **1.1×** |

**取樣數提高 5 倍，殘餘噪音只從 7.99% 掉到 7.39%。** 若抽樣噪音是主因，取 5 個中位數應把它壓低約 √5 ≈ 2.2 倍。沒有發生 ⇒ **抽樣噪音不是主因，該假設作廢。**

資料指向的替代解釋（同樣未證實）：噪音存在於**數十秒尺度**上，且在 A 那次呼叫與 B 那次呼叫之間變化。本實驗的交錯是**呼叫層級**的（每次呼叫約 40 秒），所以同一支 bench 在兩側相隔約 40 秒。交錯要有效，前提是**漂移的時間尺度遠大於 A↔B 的間隔**——這台機器上不成立。

但還有第三種可能，而且我無法從這兩次實驗排除它：**這台機器根本不適合回答這個問題。** 殘餘噪音 7.4% 是 §證據 2 夜內抖動 0.52% 的約 14 倍；在噪音比訊號大一個數量級的環境裡，「測不到效果」既可能是效果不存在，也可能是儀器解析度不夠。

⛔ 當時的結論是：在 GitHub runner 上取得數字之前，不得宣稱交錯量測能降低噪音。刻意不再調整第三次實驗——連續調整到出現想要的答案，正是本 repo 反覆在抓的形狀。

**後續（`bench-aa-noise-experiment.yaml` run 31869902576，真 runner）證實了「儀器解析度不足」這條解釋：** 同樣參數、同樣 4 支 bench，PAIRED 殘餘噪音從本地 VM 的 7.39% 降到 **1.61%**（4.6× 改善）。本地那台 4 核共用 VM 的殘餘噪音是真實夜跑夜內抖動的約 14 倍，確實測不出這個量級的效果。

但那次實驗的另一條判準（SEPARATED/PAIRED 比值）**沒有過**，而且它從設計上就不可能過——見 §證據 4 開頭。

### 4. 跨機 A/A：機器項的直接量測（本 ADR 的決定性證據）

§證據 3 的實驗**兩側跑同一台機器**，所以本 ADR 要消掉的機器身分項在它裡面**依構造恆為 0**。它量得到「機器項被消掉之後還剩多少」，量不到「消掉賺了多少」。這個結構缺口由 `bench-xmachine-aa-experiment.yaml` 補上：16 個平行 job 各落在自己的 runner VM、各自編譯**同一份原始碼**，因此全矩陣任兩筆量測的真值都恰好是 1.000。

run **31872791407**（16/16 shard 成功、1 個 binary digest、208/208 記錄 sha 驗證通過）。實際取樣到 **5 種 CPU 型號**（8×EPYC 7763、4×EPYC 9V74、2×Xeon 8573C、1×Xeon 8370C、1×Xeon 6973P-C）。

| 估計量（誤差 vs 真值 1.000） | median | p90 | >5% floor | n |
|---|---|---|---|---|
| **PAIRED 同機（本提案）** | **0.37%** | 1.40% | **1.6%** | 64 |
| **CROSS 同類，對同類多夜中位數（現行設計）** | **1.38%** | **5.26%** | **12.5%** | 48 |
| CROSS 同類，單夜對單夜 | 2.32% | 6.93% | 21.1% | 280 |
| CROSS 跨類，單夜對單夜（#1396 之前） | 23.23% | 81.11% | 89.7% | 680 |

**對現行設計的放大倍率 = 3.8×。** 兩個估計量用同一種函數形式（ratio-of-medians）、每側同樣多的量測數，唯一差別是分母來自哪台機器。

⚠️ **必須用第二列當對照組，不是第三或第四列。** 現行夜跑經 #1396 之後只比較同類主機，且參考點是同類多夜的中位數（中位數會壓低參考端噪音）。用單夜對單夜會得到 6.3×，用全體會得到 37×——那兩個數字描述的是已經不存在的設計。

實務上更好讀的一條：**現行設計的 p90 誤差是 5.26%，正好壓在最低 floor 上**——約每 10 次比較就有 1 次光靠機器就擦到門檻。配對版的 p90 是 1.40%。

跨類那一列的極端值經查證是真實硬體差異而非解析錯誤：`IncrementalLoad_1000_NoChange` 在 EPYC 7763 上比 Xeon 6973P-C 慢 **3.05×**，正好對應 207% 的最大誤差。

#### 交錯輪數：1 輪就夠

| PAIRED 取樣深度 | median | p90 | >5% floor |
|---|---|---|---|
| 6 輪交錯 | 0.37% | 1.40% | 1.6% |
| 3 輪 | 0.45% | 2.36% | 1.6% |
| **1 輪** | **0.66%** | 2.92% | **3.1%** |

1 輪交錯仍比現行設計好 2.1×，floor 擦邊率 3.1% vs 12.5%。**這把夜跑成本從「翻 6 倍」降為「翻 2 倍」**（兩份 build，各一次 `count=6`），並直接結掉待決 3。

## 決策

**夜跑改為對固定 commit 做成對交錯量測，watchdog 觀測比值序列。**

每夜在**同一台 runner、同一個 job** 內交錯執行兩側：

```text
base(=固定參考 commit), head, base, head, …   一輪一輪交錯
```

watchdog 看的是 `head / base` 的比值隨時間的走勢，而不是 `今夜 / 某個舊夜`。

**這不是新技術，是把本 repo 已經驗證過的做法搬到夜跑。** `bench-gate-pr.yaml` 走過 v1→v5 五次拓撲演化後收斂到同一形狀，機制已存在於 [`scripts/tools/ops/bench_interleave.sh`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/scripts/tools/ops/bench_interleave.sh)，其檔頭記錄了理由與代價：

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

- **夜跑時間約翻倍**（要 build + 跑兩側）。§證據 4 顯示 **1 輪交錯就夠**，所以是 2× 而非 6×。參考點：PR gate 的交錯主迴圈在 `BENCHTIME=1s` 下約 **4 分鐘**（3s 時 12 分鐘），整個 gate ~8–10 分鐘。夜跑無等待壓力。⚠️ 夜跑側的 wall time 仍**未實測**（跨機實驗是 4 支 bench，夜跑是 20 支，不可線性外推）。
- **參考 commit 要選、要換。** 建議用「上一個 GA tag」，與六線 release 節奏對齊。換參考點會在比值序列上造成不連續，需要明確處理（見 §待決）。**這是本 ADR 接受後最大的未解問題。**
- **殘餘噪音已實測，但不是零。** 真 runner 上配對殘餘為 **0.37%（6 輪）/ 0.66%（1 輪）**，遠低於最低 floor 5%；但仍有 **1.6%–3.1% 的比較會超過該 floor**。本提案把機器造成的誤報壓從 12.5% 降到個位數，**不宣稱歸零**。
  - 附帶更正：§證據 3 那兩次本地量到的 7.99% / 7.39% 是**本地 VM 的儀器噪音**，不是這個設計的殘餘噪音。真 runner 上同參數為 1.61%（4 支 bench）與 0.37%（16 機矩陣）。
- **既有 release baseline asset 的角色會變。** `release-attach-bench-baseline.yaml` 每次 release 自動附上 `bench-baseline-<TAG>.txt`（已驗證存在，含 `cpu:` 表頭與 canary）。本提案下它不再是比對基準，而是稽核與冷啟動用。

## 否決的替代方案

| 方案 | 否決理由 |
|---|---|
| **凍結錨點**（#1396 曾實作，整組退回） | 五個獨立盲審找到 1 Critical + 5 High，且問題是四個結構性衝突而非漏 if；即使修好也只解 #1432，不解 #1430，更不解機型跑步機 |
| **per-class 參考點集合** | 正是本 ADR 要消滅的跑步機；且 v2.9.1 baseline 只在 EPYC 7763 上，30 夜窗中僅 20 夜同類 |
| **changepoint detection**（Chromium/MongoDB/Otava） | 需要 detector（我們沒有），且 catapult 自己的窗也只有 50 點——step 滑出窗一樣退化回 #1430。它真正有效的部分是「把偵測到的 step 凍結成物件」，而成對量測讓那件事變得不必要 |
| **control canary 正規化** | **已用跨機資料實測反證**（見下）。它比本提案便宜——不必跑第二份完整 build——但只在 canary 與目標 bench 屬於同一類 workload 時有效，而且**完全沒有動到滑動錨點** |

### canary 正規化的實測反證

若機器身分是**單一速度因子**，任何一支 bench 都能當 canary 正規化其他支。跨機資料否定了這個前提——各 bench 的相對快慢在同一台機器上並不同步：

| bench 對 | Pearson r | 斜率 |
|---|---|---|
| `IncrementalLoad` vs `ScanDirFileHashes` | 1.00 | 1.00 |
| `FullDirLoad` vs `ResolveSilentModes` | 0.92 | 0.62 |
| `IncrementalLoad` vs `ResolveSilentModes` | **0.58** | **0.19** |

用一支 bench 當 canary 正規化另一支，同類跨機的殘餘誤差：

| canary → target | 殘餘 median | >5% floor |
|---|---|---|
| `ScanDirFileHashes` → `IncrementalLoad` | **0.55%** | **0.0%** |
| `FullDirLoad` → `ScanDirFileHashes` | 1.11% | 1.4% |
| `ResolveSilentModes` → `ScanDirFileHashes` | 2.27% | **22.9%** |
| `IncrementalLoad` → `ResolveSilentModes` | 2.49% | **18.6%** |

**配得好時與配對量測同級（0.55% / 0%）；配得差時比什麼都不做還糟**（22.9% vs 現行 12.5%）。要用它就得先把每支 bench 依機器敏感度分類、每加一支新 bench 就重新分類——**那是換一個維度的機型窮舉跑步機**。

⛔ 但決定性的理由不是這個：**canary 正規化消掉機器項，卻完全沒有動到滑動錨點。** #1432 與 #1430 的成因是滑動錨點，不是機器身分。它最多是其中一半問題的廉價補丁，不是本提案的替代方案。

## 待決（Open Questions）

1. **參考 commit 的更換策略** —— 每個 GA tag 換一次？換的那夜比值序列不連續，watchdog 要怎麼處理才不會誤判為 step？**這是接受後最大的未解問題。**
2. ~~殘餘噪音實測值，以及交錯到底有沒有效~~ —— **已結（§證據 4）**：真 runner 上配對殘餘 0.37%（6 輪）/ 0.66%（1 輪），對現行設計放大 3.8×。
3. ~~A↔B 的間隔要多短才有效~~ —— **已結（§證據 4）**：呼叫層級的交錯（間隔約 30 秒）已足夠，且 1 輪即可。不需要縮到單支 bench 層級。
4. **夜跑 wall time 實測** —— 仍未實測。跨機實驗是 4 支 bench × 6 輪；夜跑是 20 支，不可線性外推。
5. **既有 14 夜窗與三態的去留** —— 比值序列上仍需要某種持續性判準（單夜比值跳動不該發射），但不再需要同類分層。
6. **遷移路徑** —— 新舊並行一段時間比對，或直接切換？並行成本是三倍夜跑時間。

## 已知限制（本 ADR 證據的）

- §證據 1 的 harness 用重建序列，**缺 canary**（`canary_rows_in_log` 在 30 夜全為 0）。canary 確實存在於偵測器實際讀的 artifact（已於 `bench-baseline-v2.9.1.txt` 驗證），是 job-log 重建過程遺失的。因此 floor 恆為最低值 5%。
- §證據 2 的「夜內抖動 0.52%」量自同批連續 5 個樣本；交錯輪次分散在整個 run 上，真實殘餘會**高於**此值。這是下限，不是預測。
- §證據 3 跑在一台 **4 核共用雲端 VM**，不是 GitHub runner；絕對數值不可外推。它的兩次負面結果已由 §證據 4 歸因為儀器解析度不足。
- §證據 3 與 §證據 4 兩側都用**同一份原始碼編譯的 binary**（§證據 4 逐 shard 驗證 SHA-256 一致），page cache 等因素完全相同，所以量到的是殘餘噪音的**下限**；真實夜跑用兩份不同 commit 的 binary 會更高。
- **§證據 4 的決定性數字 3.8× 只有 n=48**，因為「同類多夜中位數」需要至少 3 台同型號機器，只有 EPYC 7763（8 台）與 EPYC 9V74（4 台）兩群符合。門檻是 3×，餘裕僅 0.8。
- **§證據 4 是單次派發**，即對 runner pool 的單一時刻取樣；夜跑是跨 30 天取樣。這次拿到 5 種 CPU 型號（多樣性足夠，不觸發 INCONCLUSIVE 條件），但 pool 組成會隨時間變。跨日再派發並池化可以收緊這個估計。
- **§證據 4 量的是機器項，不是誤關率。** 它證明前提成立且量級大於預期，但「改成配對後誤關率降到多少」是另一條推論鏈，本 ADR 沒有證明。
- 四項證據都不構成「誤關率會歸零」的證明。那需要實跑成對夜跑。

## 相關

- [`benchmark-playbook.md`](../internal/benchmark-playbook.md) —— 夜跑 watchdog 現行行為與十三條已知限制
- [`audit-reports/bench-trend-2026-08/`](../internal/audit-reports/bench-trend-2026-08/README.md) —— 30 夜真實序列與 counterfactual harness
- [`scripts/tools/ops/bench_interleave.sh`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/scripts/tools/ops/bench_interleave.sh) —— 已存在的交錯量測機制
