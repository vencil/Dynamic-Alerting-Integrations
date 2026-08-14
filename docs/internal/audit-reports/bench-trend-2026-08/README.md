# perf-trend 夜跑序列 + counterfactual harness（2026-07-15 .. 2026-08-13）

> 產出於 [#1396](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1396) 的誤報根因調查（PR [#1431](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/1431)）。
> 主要消費者是 [#1432](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1432)（關票側滑動 anchor 重新設計）與 [#1430](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1430)（發射側缺絕對參考點）。

## 為什麼這份資料進 repo

`analyze_bench_history.py --trend-watch` 的行為只有放在**真實夜跑序列**上才驗得出來——
合成資料驗不出「換機器被讀成程式碼退化」這件事，因為那正是合成時會被抹掉的變異來源。

這份序列是從 **30 次 `bench-record` 的 job log** 逐筆解析重建的（3596 筆原始樣本聚合成
per-night median）。**GitHub 的 job log 有保留期限**，過了就再也重建不回來；
所以這裡收的是**輸出**，不是「重建方法」而已。

## 檔案

| 檔 | 內容 |
|---|---|
| `nights_meta.csv` | 每夜一列：run id / head SHA / **CPU 型號** / runner image / Go 版本 |
| `per_night_stats.csv` | 每夜 × 每支 benchmark 的 median ns/op（20 支 bench × 30 夜） |
| `counterfactual.py` | 驅動**真實** `analyze_bench_history` 的反事實 harness（唯一可直接跑的） |
| `parse_logs.py` | 重建鏈第 1 步的方法紀錄 |
| `aggregate_nights.py` | 重建鏈第 2 步的方法紀錄 |

⚠️ **重建鏈是不完整的，這裡照實說明**——`parse_logs.py` 不是「上面兩個 CSV 的產生方法」：

```text
job logs ──parse_logs.py──▶ raw_samples.json + nights.json   ← 中間產物，未收
                    │
                    ├──aggregate_nights.py──▶ per_night_stats.csv   ✅ 已收
                    └──（session 中一段未保留的展平步驟）──▶ nights_meta.csv   ✅ 已收
```

兩支 `.py` **都不可直接重跑**（檔頭各自寫明原因）：`parse_logs.py` 需要一個已抓取的 job log 目錄，
`aggregate_nights.py` 需要未收進 repo 的中間產物，而 `nights_meta.csv` 的展平步驟當時是 ad-hoc 的、
沒有留下腳本。它們的價值是**可稽核**（看得出這些數字怎麼來的），不是可重現。
真正要重現只能從 job log 重跑整條鏈——而那正是會過期的東西。

`cpu_model` 是關鍵欄位：30 夜落在 4 種 CPU，IO/CPU 比依 CPU 型號**完全分離**
（exact permutation p = 4.34e-06）。決定性證據是同 SHA 自然實驗——`f85bfc3ea8b8` 連跑兩夜、
程式碼零差異，13.077 → 15.984 ms（+22.2%），差別只在 EPYC 7763 → EPYC 9V74。

## 怎麼跑

```bash
python3 -B docs/internal/audit-reports/bench-trend-2026-08/counterfactual.py
# --tool PATH   改驗別份 analyze_bench_history.py（預設 scripts/tools/dx/ 那份）
# --skip-slow   跳過 check 3（injection sweep，最耗時的一項）
```

`-B` 不是裝飾：harness 會載入兩份同名模組（工作區版 + `git show HEAD` 版），
stale `.pyc` 會讓你比到錯的東西。同一個坑的完整說明見
[`testing-playbook.md` §Mutation harness](../../testing-playbook.md)。

三項檢查（現況全 PASS）：

1. **`#1396` 窗口不發射** —— `today=2026-08-12` 應為 `INCONCLUSIVE`、`findings=0`
2. **零誤報** —— 17 個窗口的 false-positive bench-nights 為 0
3. **偵測面未漂移** —— 工作區版與 `HEAD` 版在 17 個乾淨窗口 + 240 個 +20% 注入情境上輸出**逐位元相同**

⚠️ **第 3 項證明的是「沒變差」，不是「偵測力有多少」。**
改動偵測面（fire 算式）時它**應該**要 FAIL——那時要換的是重新論證，不是把這項刪掉。

## 這個 harness 的形狀為什麼重要

它**直接 `import` 受測模組**並呼叫真實的 `analyze_trend`。

這條不是風格偏好。同一次調查裡有兩支腳本因為沒做到而產出不可用的數字：一支**完全沒有 import
受測模組**、自行重寫了關票判定，量到的是一個模型而不是這份 code（那就是 CHANGELOG 一度寫過、
後來全數移除的 68%）；另一支把例外吞成「什麼都沒發生」，得到假的 0%。

**新 harness 上線前先讓它對一個已知會 fire 的輸入產出 fire**，再相信它的統計。

本檔的反面驗證（對**這份 committed 版本**現場重跑，非沿用舊結論）：把 `analyze_trend` 的
分層判定改成永遠走未分層退路——即扣掉「記錄主機類別」之前的行為——三項全部轉紅：

```text
[FAIL] 1  today=2026-08-12  status=FINDINGS  findings=5   (同類夜 14/14，因為所有夜都算同類)
[FAIL] 2  17 windows, 17 evaluable, false-positive bench-nights=15
[FAIL] 3  clean replay: DIFFERS;  +20% injection sweep: DIFFERS
0/3 PASS
```

所以「三項 PASS」是有訊息量的證據，不是恆真式。
（上面的 5 / 15 是**這組窗口**下的量測值；不要和 #1431 PR body 引用的數字互相代換，
那是另一組窗口組態下量的。）

## 不可引用的數字

- **假關票率**：無可信量測。曾出現的 68% 來源不成立（見上），**已移除且不以另一個數字取代**。
- **偵測力 83.1%**：曾以 260 情境的 attributable 偵測率量得，但那份 harness 已遺失、
  情境構造無法從數字反推。本檔 `counterfactual.py` 的 240 情境診斷數字**與它不可比**，
  也不宣稱重現它。
