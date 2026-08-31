# `IncrementalLoad_1000_OneFileChanged` 的 A/A 校準與迭代層級成本（2026-08-30）

> 主要消費者是 [#1497](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1497)（單一 benchmark 的比值雙峰）與 [#1545](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1545)（mtime guard 的 2 秒安全窗），
> 次要是 [#1439](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1439) / [ADR-032](../../../adr/032-paired-interleaved-bench-measurement.md) §待決 7 子題 4（UNSTABLE 涵蓋什麼）。

## 一句話

**A/A 是乾淨的。** 兩側跑同一份編譯出的執行檔（真值恆為 1.000）時，這支 benchmark 的逐輪比值在**三場、合計 30 輪、跨兩種 CPU 型號**下 rSD 落在 **0.53–1.28pp**、**0/30** 超過 5%、**絕對偏離最大 3.09%**（`benchtime=600x` 第 4 輪的 −3.09%；正向最大 +2.25%）—— #1497 報的 26 個百分點**沒有被重現**。

⛔ **這不等於回答了 #1497 的「benchmark 缺陷 vs 產品特性」。** 它只關掉一條岔路：「這支 benchmark 在成對 harness 下本來就很吵」不成立。那個擺盪需要**兩側是不同的樹**才會出現。

## 為什麼這份資料要進 repo

與隔壁 [`bench-paired-2026-08/`](../bench-paired-2026-08/README.md) 同一個理由：**來源是 job log，而 job log 有保留期限**；artifact 那條路在本專案的分析環境走不通（`*.blob.core.windows.net` 被 egress 政策擋掉，實測 `CONNECT tunnel failed, response 403`）。過了保留期，下面每一個數字就只剩散文。

## 重算

```bash
python3 docs/internal/audit-reports/bench-aa-2026-08/analyze_aa.py
```

不連網、不重跑 benchmark，只從本目錄兩個資料檔重算。它回答的是「README 的數字能不能從收進來的資料重算出來」，**不是**「今天重跑會得到什麼」。

## 量到什麼

### 一、A/A 逐輪比值（`aa-sessions.json`）

| 場次 | run | runner | 逐輪 `B/A−1` | rSD | 全距 | `\|ratio−1\|>5%` | `b.N` |
|---|---|---|---|---|---|---|---|
| `benchtime=3s`、12 輪 | 33341834761 | Intel Xeon Platinum 8370C | −1.73%…+2.25% | 1.17pp | 3.99pp | **0/12** | 320–441（散佈 28.1%） |
| `benchtime=600x`、6 輪 | 33341837679 | AMD EPYC 7763 | −3.09%…+0.92% | 1.28pp | 4.01pp | **0/6** | 600（散佈 **0.0%**） |
| `benchtime=3s`、12 輪（**獨立複製**） | 33342875971 | AMD EPYC 7763 | −0.95%…+0.73% | 0.53pp | 1.68pp | **0/12** | 279–303（散佈 8.1%） |

三場皆 `-test.count=5`、取每次 invocation 的中位數。**合計 30 輪、跨兩種 CPU 型號、兩種 `benchtime` 設定：`|ratio−1|>5%` 共 0/30，絕對偏離最大 3.09%。**

⛔ **一則更正**：本文初版三處都寫「最大偏離 **+2.25%**」，那是**正向**最大值，漏掉了同一張表上就列著的 −3.09% —— 把噪音上界低報 37%，而且錯在「讓儀器看起來比實測更準」的方向。`analyze_aa.py` 現在直接印出這個統計量（`[全部場次合計]` 那段），不再靠人去掃表格。

⚠️ 第三場的資料取自本 PR 新增的 `AAROW` 緊湊摘要，**故該場逐筆沒有 sha256**；前兩場的每筆記錄都有並已逐筆驗過。第三場的 base64 原件仍在該 job 的 log 裡，可依本文末節還原並驗證。

### 一之二、對照：預設四支在同一天的 A/A（⛔ 不同集合，不池化）

一個立刻會被問的問題：**1.17pp 是這支 bench 特有的，還是 CI 上 A/A 的一般水準？** 同一天用**預設 `bench_re`**（原本寫死的四支）跑了一場（[run 33343193382](https://github.com/vencil/Dynamic-Alerting-Integrations/actions/runs/33343193382)、`benchtime=3s`、`-test.count=5`）：

| benchmark | n | rSD | \|偏離\|最大 |
|---|---|---|---|
| `FullDirLoad_1000` | 4 | 1.46pp | 2.37% |
| `IncrementalLoad_1000_NoChange` | 4 | 0.65pp | 1.17% |
| `ResolveSilentModes_1000` | 4 | 1.38pp | 2.19% |
| `ScanDirFileHashes_1000` | 4 | 0.92pp | 2.02% |
| **四支合計** | **16** | **1.26pp** | **2.37%**（`>5%` 共 0/16） |

⇒ **`OneFileChanged` 的 1.17pp 不特別** —— 它就落在同一天其他四支的水準裡。這一格同時也是本 PR 的**行為保存檢查**：不傳 `bench_re` 時仍然選中原本那四支、產出 14 個檔。

⛔ **這組數字不可與上一節池化**：不同的 benchmark 集合。⚠️ 只有 rounds 3–6 四輪完整（r1、r2_A **未取回**，仍在該 job 的 log 裡），且資料經 `AAROW` 摘要取回**故無 sha256**。

### 二、迭代層級的工作量確實在變（`local-benchtime-sweep.tsv`）

用 `-benchtime=Nx` 固定迭代數掃描，`total(N) = N × ns/op` 的相鄰差分即第 k 段迭代的邊際成本。**零程式碼改動。**

| bench | 暖機期邊際 | 穩態邊際 | 倍率 | [#1545](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1545) 在 CI 量到 |
|---|---|---|---|---|
| `IncrementalLoad_1000_NoChange` | ~8.1 ms | ~2.65 ms | **3.05×** | 3.05× |
| `IncrementalLoad_1000_OneFileChanged` | ~11.4 ms | ~5.70 ms | **2.00×** | 2.26× |

⭐ **決定性檢驗：兩支的肘點都落在 ~2.0 秒牆鐘，但落在不同的迭代數**（≈175 vs ≈250）。若機制是「固定迭代數之後變快」，兩支的肘點該落在同一個 N；落在同一個**牆鐘**才與 `flat_scanner.go` 那道 `age > 2*time.Second` 的安全窗相符。

**正向對照**：暫時把 `backdateFiles(b, dir)` 加進 `OneFileChanged`（即 [#1545](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1545) 分支 `claude/fix-bench-mtime-warmup` 的修法），曲線**全平**、**穩態成本不變**（N=3200 兩者皆 ~5.96 ms）。量完即還原，還原後與 `HEAD` 位元組零差異。

局部彈性 `dln(ns/op)/dln(N)`（N=400→800 兩實測點）由 **−0.206 → −0.012**，即固定 `benchtime` 下「真實成本差異」的自洽放大倍率由 **1.26× → 1.01×**。

⇒ #1545 排隊等 reference re-pin 的那個修正，會同時清掉一個真實偏差**與**一個噪音放大器；但 **1.26× 的放大不足以產生 #1497 的雙峰**（要讀到 +30% 得先有真實 +23.8%），所以**不該預期它會把雙峰修掉**。

### 三、一個真實但不足以解釋的假象：job 冷啟動的首樣本

3s 那場有 **1/24** 個 invocation 的第一個 `-count` 樣本高出同檔其餘中位數 **+24.8%**（`r1_A`，`N=320` vs ~430）——就是整個 job 的第一次執行，量級與 #1497 的 +25.02% / +30.38% 相同。

⛔ **但它解釋不了那個擺盪**：`bench_interleave.sh:121` 每次 invocation 是 `-test.count=1`、而 `pair_bench_ratio.py:152` 取的是**跨 6 輪的中位數**；6 個樣本裡有 1 個離群值，中位數幾乎不動。

## ⛔ 這份資料的限制（讀之前先看）

1. **600x 那一場只取回 r3_B..r9_B（6 輪完整）。** 其餘 6 輪**仍在該 job 的 log 裡**——是**未取回**，不是不存在。連帶：該場**不含它自己的第一次 invocation**，所以「首樣本超額只出現在 3s 那場」**不能**讀成「釘死 `b.N` 防住了冷啟動」，那一格根本不在資料裡。
2. **兩場落在不同 CPU 型號**（Xeon 8370C vs EPYC 7763）。兩者 rSD 的差（1.17 vs 1.28pp）**混淆了機制與機器**，不得據以宣稱「釘死 `b.N` 改變了散佈」。要判那件事需要同一台機器上的兩種設定。
3. **A/A 兩側是同一份編譯出的執行檔**（workflow 只 `go test -c` 一次）。所以本資料**沒有**排除「兩份各自編譯的執行檔之間的差異」（程式碼佈局／對齊）。下一個對照是 A/A′：同一份原始碼、**分別編譯**兩側。⚠️ 但那類效應預期是**穩定的偏移**，不是同日 26 個百分點的擺盪。
4. **本機 sweep 跑在 4 vCPU Intel Xeon @2.10GHz 上**，絕對值不可與 CI 對比；該檔用來看形狀與比值。

## 檔案

| 檔 | 內容 |
|---|---|
| `aa-sessions.json` | 兩場 A/A 的逐輪逐側原始樣本（`b.N` 與 `ns/op`），附 run id / job id / CPU / 每筆的 sha256 |
| `local-benchtime-sweep.tsv` | 本機 `-benchtime=Nx` 三條曲線（兩支 baseline + `backdated` 對照），含受測物識別特徵 |
| `aa-multibench-control.tsv` | 預設四支的 A/A 對照（⛔ 不同 benchmark 集合，不與 `aa-sessions.json` 池化） |
| `analyze_aa.py` | 從上面三個資料檔重算本文所有數字 |

## 從 job log 還原原始記錄

`aa-sessions.json` 是**解析後**的結果。要拿逐位元的原件，`bench-aa-noise-experiment.yaml` 檔頭記著還原指令（錨在記錄形狀而非欄位位置）：

```bash
grep -oE 'RAW [A-Za-z0-9_.-]+ [0-9a-f]{64} [A-Za-z0-9+/]+={0,2}' job.log > records.txt
awk '{ print $4 | ("base64 -d > " $2 ".txt") }' records.txt
awk '{ print $3"  "$2".txt" }' records.txt | sha256sum -c -
```

⚠️ 該 workflow 現在**另外**印一行 `AAROW <檔名> <bench> <N>:<ns/op> …` 的緊湊摘要，取一個小 tail 就拿得到；但它不含 `cpu:` 那幾行與 digest，**不能**取代上面這條。
