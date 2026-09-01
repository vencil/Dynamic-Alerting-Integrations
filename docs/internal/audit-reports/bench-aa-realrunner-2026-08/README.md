---
title: "真 runner 單機 A/A 稽核資料集（2026-08）—— 補上 1.61% 那個數字缺的依據"
tags: [audit-report, benchmark, perf-trend, measurement, adr]
audience: [platform-engineers, contributors]
version: v2.9.0
lang: zh
---

# 真 runner 單機 A/A 稽核資料集（2026-08）

[ADR-032](../../../adr/032-paired-interleaved-bench-measurement.md) 的成對量測，**在機器被固定住之後還剩多少噪音**，由這個目錄回答。**這裡跑不出來的數字，就是沒有依據的數字。**

## 一行重現

```bash
python3 docs/internal/audit-reports/bench-aa-realrunner-2026-08/reproduce.py
python3 docs/internal/audit-reports/bench-aa-realrunner-2026-08/selftest.py
```

無參數、無網路、不重跑 benchmark、不讀 repo 其他狀態。

## 這份存檔為什麼要存在

`.github/workflows/bench-xmachine-aa-experiment.yaml` 的 §WHY THIS EXISTS 檔頭原本寫著姊妹 workflow「measured, **1.61% residual rSD on a real runner**」。實查（2026-09-01）：**這個數字在全 repo 只出現那一次**，沒有資料集、沒有重現腳本、沒有寫是哪一個 run，而它唯一的來源是 job log，**2026-11-13 到期**。相鄰的 [`bench-xmachine-2026-08/`](../bench-xmachine-2026-08/README.md) 有 `reproduce.py` + `selftest.py` + 逐 shard digest，這一支沒有。

本目錄把那筆資料在到期前取回、驗過、存下，並附上可重跑的算式。

## 證據索引

| | |
|---|---|
| **產生資料的指令** | `.github/workflows/bench-aa-noise-experiment.yaml`，`workflow_dispatch` 於 `main` @ `05d61e12`。生效參數 `rounds=12 count=5 benchtime=1s`、預設四支 bench（皆為該 workflow 的 `default`）。⚠️ `rounds` / `count` / bench 集合是**從資料直接數出來的**；`benchtime` 是**佐證**不是直接觀察——每次呼叫的 `b.N × ns/op` 落在 1.00–1.33 秒，與 `1s` 相符 |
| **執行定位** | run [`31869902576`](https://github.com/vencil/Dynamic-Alerting-Integrations/actions/runs/31869902576)（run_number 2，success；job `94976812028`） |
| **實際跑的** | `aa.test -test.bench=<預設四支> -test.benchtime=1s -test.run='^$' -test.count=5`，12 輪交錯，**順序固定 `A,B`**（該 workflow 的迴圈是 `for side in A B`；輪次奇偶交替是姊妹 workflow 才有的設計） |
| **兩側的執行檔** | **同一個**。workflow 的 `Build test binary once` 步驟只做一次 `go test -c`，量測迴圈兩側呼叫同一個 `aa.test` ⇒ 真值恆為 1.000，任何偏離都是量測誤差 |
| **原始資料取件** | 從 job log 的 base64 `RAW` 記錄還原，**24 筆逐筆 SHA-256 驗證通過**；digest 取自 runner 自己印出的值，不是對複本重算 |
| **runner** | AMD EPYC 9V74 80-Core、`GOMAXPROCS=4`、linux/amd64、Go 1.26.5（24 個檔的 `cpu:` 行完全一致） |

## 檔案

| 檔案 | 內容 |
|---|---|
| `raw/` | 24 個位元組精確的 `go test -bench` 輸出（12 輪 × 2 側），即 SSOT |
| `raw_digests.csv` | runner 印出的 24 個 SHA-256 |
| `reproduce.py` | 驗 digest → 印受測物識別特徵 → 算出下方每一個數字；digest 不符 `exit 2` |
| `selftest.py` | negative validation：植入已知效應 / 植入**無**效應 / 竄改與缺檔必須被擋 |

⛔ **刻意沒有 `measurements.csv`。** 派生表格與原始位元組並存等於同一批事實有兩個住址，會漂。要表格就 `reproduce.py --csv`，即時從 `raw/` 生出來。

## 量到的數字

估計量：每 (bench, 輪) 取兩側各自 `count=5` 的**中位數**，再取比值；pooled `n = 4 × 12 = 48`。

| benchmark | n | rSD | 最大\|偏離\| | 中位數 |
|---|---|---|---|---|
| `FullDirLoad_1000` | 12 | 0.78pp | 1.39% | +0.08% |
| `IncrementalLoad_1000_NoChange` | 12 | **1.61pp** | 3.79% | −0.45% |
| `ScanDirFileHashes_1000` | 12 | 1.45pp | 4.35% | −0.21% |
| `ResolveSilentModes_1000` | 12 | **2.40pp** | 6.32% | +0.71% |
| **四支池化** | **48** | **1.67pp** | **6.32%** | −0.04% |

`|偏離| > 5%` 共 **1/48**。

## 與 ADR-032 §證據 3 的獨立對照

這批資料的單位（每側各執行一次、每次 `count=5`）**正好等於** ADR-032 §「交錯幾輪就夠」表裡的 **1 輪**，而該節明寫夜跑實際上線後適用的就是那一列。所以可以拿來做一次獨立複製：

| | 中位 \|誤差\| | 第 90 百分位 | 超過 5% 門檻 | n |
|---|---|---|---|---|
| ADR-032「**1 輪**」（跨機實驗，16 機 × 6 輪） | **0.66%** | —（該表未列） | **3.1%** | 64 |
| **本目錄**（單機，run 31869902576，12 輪） | **0.87%** | 2.63% | **2.1%** | 48 |

⇒ 同一個量測單位、不同機器、不同日期、不同實驗拓撲，**中位誤差 0.66% vs 0.87%、超標率 3.1% vs 2.1%**——同一個量級，本次略高於 ADR 記的那個數。對 5% 門檻仍有約 6 倍餘裕。

⛔ **不可拿來對 ADR §證據 3 表格裡那列 0.34% / 1.57% / 2.1%（「6 輪」）。** 兩者的 `n` 都是 48，但**數的不是同一件事**：那一列是 12 台機器 × 4 支，每格已先對 6 輪取中位數（雜訊被平均掉）；本目錄是 4 支 × 12 輪，每格是單次呼叫。`n` 相同純屬巧合，池化會把「平均過的」與「沒平均的」混成一鍋。⚠️ 本節初稿正是先對到那一列去，`>5%` 兩邊都是 2.1% 看起來像漂亮的複製——**那個吻合是巧合**。

## ⚠️ 1.61% 對不上，而對不上的方式是有意義的

**四支池化是 1.67pp，不是 1.61pp。** 為了確認差異不是我這條管線算錯，先做一件事：同一個檔案在量測迴圈的 `Alternate which side goes first` 註解裡，還對**這個 run** 具名下了幾個宣稱（順序偏差）。用 log 形式估計量重算，三個宣稱**逐位對上**：

| 檔頭宣稱 | 本目錄重算 |
|---|---|
| B 比 A 平均快 **0.395%** | 0.395% ✅ |
| 48 個成對觀測 | 48 ✅ |
| \|t\| = **1.59** | 1.59 ✅ |
| sign split **23/48** | 23/48 ✅ |
| 四支都往同一邊倒 | 是 ✅ |

⇒ **這份存檔確實就是該檔頭所引用的那個 run 的資料**，管線沒有跑在錯的樹上。

那 1.61 是什麼？列舉 108 種聚合方式（2 種估計量形式 × 2 種 SD 定義 × 池化／單支／去一支／去一輪／兩兩配對），**恰好落在 1.61 的只有一種**：`IncrementalLoad_1000_NoChange` 這**一支**的 rSD（n=12）。

⇒ **最可能的解讀是：那句話把四支裡的一支寫成了儀器的殘餘水準。** 差距本身不大（1.61 vs 1.67），但方向是**低報**，而且它掩蓋掉兩件在同一批資料裡的事：最差的一支是 **2.40pp**，而且 48 個比較裡**有一個超過 5% floor**（6.32%）。「一個真值為 1.000 的比較超過了發射門檻」正是這個實驗該讓人看見的東西。

⛔ **這不是「證明」那句話錯了。** 同一支 workflow 在同一天還有更早的 run [`31864272536`](https://github.com/vencil/Dynamic-Alerting-Integrations/actions/runs/31864272536)（run_number 1），1.61 也可能出自它。**它的逐輪資料本環境取不到**——那次執行時「把原始資料印進 job log」這一步**還不存在**（正是由 run 1 的結果促成、寫在 run 2 的 head commit 裡），所以資料只在 artifact 裡，而 artifact 走被擋的 host：

```text
GET /repos/.../actions/artifacts/9241669656/zip   → 302
Location: https://productionresultssa6.blob.core.windows.net/...
curl 該 host                                       → CONNECT tunnel failed, response 403
```

⇒ 這是**量不到**，不是量了沒事，也不是沒去量（2026-09-01 實測）。run 1 的 artifact 保留期為 30 天，早已過期，**該筆資料已永久不可取回**。

## ⛔ 這批資料不能拿來說的話

- **不能說「機器項被消掉了」。** 兩側同一台機器，機器身分項在建構上恆為零 ⇒ 這支儀器**看不到**它要驗證的那個東西。跨機的量在 [`bench-xmachine-2026-08/`](../bench-xmachine-2026-08/README.md)。
- **不能與 [`bench-aa-2026-08/`](../bench-aa-2026-08/README.md) 池化。** 那批是**單一支** `IncrementalLoad_1000_OneFileChanged`、`benchtime=3s`／`600x`、不同機器。benchmark 集合、benchtime、機器三個變數同時不同。
- **不是「殘餘噪音的預測值」。** 兩側同一份執行檔，所以這是殘餘的**下限**；真實夜跑兩側是不同版本、且 `bench_interleave.sh` 兩側**各自編譯**，會更高。
- **不是儀器的通用規格。** 單一 CPU 型號（EPYC 9V74）、單次 dispatch、n=48。
