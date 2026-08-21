# 成對交錯量測的夜跑比值序列（2026-08-16 .. 2026-08-21）

> [ADR-032](../../../adr/032-paired-interleaved-bench-measurement.md) 第一段（量測管線）上線後的頭六夜。
> 主要消費者是 [#1439](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1439)（TRK-359）**第二段**的門檻決策與驗收 harness，
> 以及 [#1497](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1497)（單一 benchmark 雙峰）。

## 為什麼這份資料要進 repo

它正在腐爛，而且是兩層腐爛：

1. **本序列的來源是 job log**，六夜逐夜解析而得。GitHub 的 job log 有保留期限，過了就再也重建不回來——與隔壁 [`bench-trend-2026-08/`](../bench-trend-2026-08/README.md) 收絕對序列的理由完全相同。
2. **artifact 只留 90 天**（`bench-record.yaml` 的 `retention-days: 90`），所以 `bench-paired.json` 原件同樣會消失。

⛔ **這裡收的是輸出，不是「重建方法」。**

## ⛔ 單位不同，不可與隔壁那份混用

| | 這份 | [`bench-trend-2026-08/`](../bench-trend-2026-08/README.md) |
|---|---|---|
| 內容 | `(main ÷ 釘死的參考版本 − 1) × 100`，**比值** | 每夜 median **ns/op**，絕對值 |
| 機器項 | 在比值裡相消（同一台 runner、同一個 job 內交錯） | **不相消**，是該序列最大的噪音來源 |
| 用途 | 第二段門檻決策、配對版驗收 harness | 現行監測器的反事實驗證 |

把兩者池化就是把 ADR-032 整個論證抹掉——它存在的理由正是「機器項在比值裡消失、在絕對序列裡不會」。

## 檔案

| 檔 | 內容 |
|---|---|
| `nights.json` | 六夜 × 22 支（20 支 benchmark + 2 支對照）的比值，每夜附 run id / job id / head SHA / CPU 型號 / 當夜 `workload_drift` 狀態 |
| `analyze_paired.py` | **可直接重跑**，重新導出本文引用的每一個數字：`python3 -B analyze_paired.py` |

⚠️ 與隔壁不同的一點：那份的兩支 `.py` 不可重跑（需要未收進 repo 的中間產物）。這一份**可以**——它唯一的輸入就是旁邊的 `nights.json`。

## Provenance

參考版本固定為 `exporter/v2.9.0` / `3fd96b51f52e61566bb12c4c3fa23fed7e34dfa0`（[`.github/bench-reference.yaml`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/.github/bench-reference.yaml)）。

| 夜 | run | CPU | `workload_drift` |
|---|---|---|---|
| 08-16 | [31924848480](https://github.com/vencil/Dynamic-Alerting-Integrations/actions/runs/31924848480) | AMD EPYC 7763 | **欄位不存在**（#1455 當天稍晚才 merge） |
| 08-17 | [31992030018](https://github.com/vencil/Dynamic-Alerting-Integrations/actions/runs/31992030018) | Intel Xeon 8573C | 4 檔 |
| 08-18 | [32096162832](https://github.com/vencil/Dynamic-Alerting-Integrations/actions/runs/32096162832) | AMD EPYC 9V74 | 4 檔 |
| 08-19 | [32212925057](https://github.com/vencil/Dynamic-Alerting-Integrations/actions/runs/32212925057) | AMD EPYC 7763 | 4 檔 |
| 08-20 | [32329013063](https://github.com/vencil/Dynamic-Alerting-Integrations/actions/runs/32329013063) | **未記錄** | 4 檔 |
| 08-21 | [32444409563](https://github.com/vencil/Dynamic-Alerting-Integrations/actions/runs/32444409563) | AMD EPYC 9V74 | 4 檔 |

⚠️ **08-20 的 CPU 型號是空的，因為我沒讀到那一行**，不是因為它不存在。`null` 在這裡的意思是「本次未取得」，不是「無此資訊」——兩者混淆正是這條線一路在修的病。要補的話那一行還在該 run 的 log 裡。

## 這六夜說了什麼

以下每個數字都由 `analyze_paired.py` 現場導出。

### 1. 已拍板的判定規則（門檻 5%、連續 2 夜）在真實序列上只發射一次

```text
threshold 5% / 2 nights -> 1 fire(s): MergePartialConfigs_1000@08-17
threshold 3% / 2 nights -> 3 fire(s)
threshold 1% / 2 nights -> 9 fire(s)
```

⇒ 第二段監測器切換後開的**第一張票**，會是 [#1474](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1474) 已經量測過、歸因過、並決定**接受**的那一支。這不是預測，是把規則重播在資料上算出來的。門檻下調的代價同樣是量出來的，不是形容詞。

### 2. 離群集中在一支 benchmark，不是全域噪音

| 樣本集 | n | 中位 | p90 | max | >5% |
|---|---|---|---|---|---|
| 全部 20 支 × 6 夜 | 120 | 0.66% | 3.57% | 25.02% | 10 |
| 扣掉 #1474 已歸因的 2 支 | 108 | 0.60% | 1.65% | **25.02%** | 2 |
| 再扣掉 #1497 那一支 | 102 | 0.57% | 1.43% | **3.25%** | **0** |

其餘 17 支在 102 個 bench-night 裡**從沒超過 3.25%**。

### 3. 對照測試乾淨 ≠ 當夜每一支都可信

```text
2026-08-17  canary -0.12/-0.09   worst 19.34%  IncrementalLoad_1000_OneFileChanged
2026-08-21  canary +0.02/+0.00   worst 25.02%  IncrementalLoad_1000_OneFileChanged
```

六夜中有兩夜是「對照測試乾淨，而某一支擺了 20 個百分點以上」。已拍板的**對照測試閘門**（ADR-032 §待決 5）回答的是「成對量測這一夜有沒有壞掉」，**不是**「這支 bench 今晚穩不穩」——這兩件事在資料上是分開的。

### 4. per-bench 門檻（rustc-perf 形）現在不能用，而且理由有兩層

判準取自 `rust-lang/rustc-perf` 的 `docs/comparison-analysis.md`（實抓原文）：`result > Q3 + IQR × 3`。

**第一層：樣本數不足，而且不足到門檻本身會亂跑。**

```text
IncrementalLoad_1000_OneFileChanged   n=6  thr=+7.46%   leave-one-out +6.33% .. +8.49%
```

⛔ 而同一支在**只有前四夜**（08-18..21）時算出來的門檻是 **+27.94%**。多兩夜、門檻移動 **20 個百分點**——這比 leave-one-out 更能說明「歷史不足時這個門檻不能用」。

⚠️ 這一段同時是一次更正：本序列進 repo 之前，[#1497](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1497) 與相關討論裡引用過「門檻 +27.94%，所以 +30.38% 照樣穿過去」——那個數字出自四夜窗，六夜下**不成立**（+25.02% 會被 +7.46% 的門檻抓到）。結論的方向不變（per-bench 門檻解決不了那支 benchmark），但當時的**論據**是錯的。

**第二層是結構性的，與樣本數無關。**

```text
MergePartialConfigs_1000   median +7.42%  -> threshold +9.57%
ResolveSilentModes_1000    median +4.05%  -> threshold +12.83%
```

rustc-perf 的歷史是 **commit 對 commit 的差值**（分布中心在 0），而這裡的歷史是**對釘死參考版本的水位**（中心在 +8%）。把水位餵進「歷史分布」，門檻就長在水位之上 ⇒ **這個形式的 per-bench 門檻是一台吸收機**，正是 ADR-032 存在要消滅的東西。

⇒ 若日後採用，必須是「**離散度**決定門檻寬度、**水位**另行判定（走 ACCEPTED 出口）」那一形，不是把水位混進歷史那一形。
**Defer-with-trigger**：配對序列 ≥30 夜（1 筆/夜，自 08-16 起算 ≈ 2026-09-14）且屆時重跑本檔、確認門檻在增加樣本後收斂。

## 沒有做的事（別讀成已做）

- **沒有收 `bench-paired.json` 原件**，只收了從 log 解析出的比值。原件在各 run 的 artifact 裡，90 天後消失。
- **沒有收每支的樣本離散度**：夜跑每側每支有 6 個樣本，本序列只留了 median 導出的比值。要做「當夜重複樣本」的分析得回去讀 artifact。
- **08-20 的 CPU 未取得**（見上）。
- 六夜**不足以**支撐任何關於誤報率的宣稱；本檔只陳述這六夜發生了什麼。
