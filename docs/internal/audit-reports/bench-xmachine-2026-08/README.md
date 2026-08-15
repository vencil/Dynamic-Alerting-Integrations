---
title: "跨機 A/A 稽核資料集（2026-08）—— ADR-032 的接受依據"
tags: [audit-report, benchmark, perf-trend, measurement, adr]
audience: [platform-engineers, contributors]
version: v2.9.0
lang: zh
---

# 跨機 A/A 稽核資料集（2026-08）

[ADR-032](../../../adr/032-paired-interleaved-bench-measurement.md) §證據 4 引用的每一個數字，都由這個目錄重現。**ADR 裡出現、這裡跑不出來的數字，就是沒有依據的數字。**

## 一行重現

```bash
python3 docs/internal/audit-reports/bench-xmachine-2026-08/reproduce.py
```

無參數、無網路、不讀 repo 其他狀態。

## 證據索引

| | |
|---|---|
| **產生資料的指令** | `.github/workflows/bench-xmachine-aa-experiment.yaml`，`workflow_dispatch` 於 `main` @ `931f952c`，inputs `shards=16 rounds=6 count=5 benchtime=1s` |
| **執行定位** | run [`31872791407`](https://github.com/vencil/Dynamic-Alerting-Integrations/actions/runs/31872791407)（16/16 measure job 成功；collect job `94984699506`） |
| **每個 shard 實際跑的** | `aa.test -test.bench=<4 benches> -test.benchtime=1s -test.run='^$' -test.count=5`，6 輪交錯、順序依輪次奇偶交替 `A,B` / `B,A` |
| **輸入（fixtures）** | 無外部 fixture。四支 bench 皆來自 `components/threshold-exporter/app`，各 shard **自行編譯同一份原始碼**；`shard_meta.csv` 的 `binary_sha256` 是各機實測值 |
| **原始資料取件** | 從 collect job 的 log 還原（見下「從原始 log 重建」）；208 筆記錄逐筆 SHA-256 驗證通過 |
| **negative validation** | `python3 .../selftest.py` —— 植入已知效應與已知**無**效應各一，驗估計量兩者都認得；外加 digest 分歧必須被拒 |
| **逐項輸出與解讀** | ADR-032 §證據 4；本檔以下各節 |

## 檔案

| 檔案 | 內容 |
|---|---|
| `shard_meta.csv` | 16 列：shard、CPU 型號、nproc、Go 版本、**binary SHA-256** |
| `measurements.csv` | 768 列 = 16 shard × 6 輪 × 2 側 × 4 bench；`ns_per_op_median` 是該次呼叫 `count=5` 的中位數 |
| `reproduce.py` | 重現 ADR §證據 4 的每個數字 |
| `selftest.py` | negative validation；匯入 `reproduce.py` 的 `estimators()`，不重寫算式 |

## 這批資料為什麼能當證據

全部 16 台跑**同一份原始碼**，所以矩陣裡任兩筆量測之間的**真值恰好是 1.000**，任何偏離都是量測誤差。這件事不是假設——`binary_sha256` 逐 shard 記錄，`reproduce.py` 在 digest 不唯一時直接 `exit 2` 拒絕輸出（`selftest.py` 第 3 案驗過它真的會拒）。

## 兩個估計量與「同母體」這件事

```text
PAIRED   B_j / A_j              同機          —— ADR-032 提議的做法
CROSS    B_j / median(A_k)      k≠j、同 CPU 型號 —— 夜跑現在的做法（#1396 後）
```

⚠️ **CROSS 只能對「有 ≥2 台同型號同伴」的 shard 成立**，16 台裡有 4 台不符合。若拿 16 台的 PAIRED 去比 12 台的 CROSS，**變動的就不只是分母的機器身分，還有樣本母體**。外部 review 正是點名這一點；`reproduce.py` 因此兩種母體都印，ADR 引用的是**同母體**那一個。

修正後倍率從 3.8× 變成 **4.0×**——**更正沒有救援結論，而是讓它更銳利**。這一點值得記住：對抗性檢查的價值不在於它總是推翻你。

## 被排除的 4 台不是雜訊，是另一個發現

那 4 台正是現行設計**連評估都做不到**的機器（無同類同伴 ⇒ 真實夜跑回 `INCONCLUSIVE`）。所以覆蓋率本身就是比較項的一部分：

```text
現行設計可評估   12/16 = 75%
配對設計可評估   16/16 = 100%
```

這與 #1396 修法付出的「約 1/3 的夜不可評估」代價直接對應。

## 從原始 log 重建（若要從頭驗）

CSV 是衍生物。原始的 208 筆記錄可從 collect job 的 log 還原：

```bash
# job log 每行都有 RFC3339 時間戳，且 ##[group] 會回顯該 step 自己的
# shell 原始碼（其中含一行 printf 'RAW %s %s %s\n' 會變成誘餌記錄），
# 所以錨在記錄「形狀」而不是欄位位置：
grep -oE 'RAW [A-Za-z0-9_.-]+ [0-9a-f]{64} [A-Za-z0-9+/]+={0,2}' job.log > records.txt
awk '{ print $4 | ("base64 -d > " $2 ".txt") }' records.txt
awk '{ print $3"  "$2".txt" }' records.txt | sha256sum -c -
```

## 已知限制

- **決定性數字 4.0× 只有 n=48。** 門檻是 3×，餘裕僅 1.0。
- **單次派發**＝對 runner pool 的單一時刻取樣；夜跑是跨 30 天取樣。這次拿到 5 種 CPU 型號（多樣性足夠），但 pool 組成會隨時間變。跨日再派發並池化可收緊此估計。
- **兩側用同一份原始碼編譯的 binary**，page cache 等因素完全相同，所以量到的是殘餘噪音的**下限**；真實夜跑用兩份不同 commit 的 binary 會更高。
- **量的是機器項，不是誤關率。** 「改成配對後誤關率降到多少」是另一條推論鏈，本資料集沒有證明。

## 相關

- [ADR-032](../../../adr/032-paired-interleaved-bench-measurement.md) —— 決策本體
- [`bench-trend-2026-08/`](../bench-trend-2026-08/README.md) —— 前情：30 夜真實序列與誤關率 counterfactual
- [`benchmark-playbook.md`](../../benchmark-playbook.md) —— 夜跑 watchdog 現行行為
