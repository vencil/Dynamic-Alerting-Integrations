# perf-trend 夜跑序列 + counterfactual harness（2026-07-15 .. 2026-08-13）

> 產出於 [#1396](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1396) 的誤報根因調查（PR [#1431](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/1431)）。
> 主要消費者是 [#1432](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1432)（關票側滑動 anchor 重新設計）與 [#1430](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1430)（發射側缺絕對參考點）。

⛔ **姊妹資料集：[`bench-paired-2026-08/`](../bench-paired-2026-08/README.md)**（ADR-032 成對量測上線後的比值序列）。
兩者**單位不同、不可池化**——這份是每夜 median **ns/op**（機器項不相消，且是本序列最大的噪音來源），
那份是 `main ÷ 釘死參考版本` 的**比值**（機器項在同一台 runner 上相消）。混用等於抹掉 ADR-032 的整個論證。

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
# --skip-slow   跳過 check 3（HEAD 比對用的 injection sweep）；check 4 仍會跑
```

⚠️ **「最耗時的一項」這個舊敘述已經不對了。** 實測（同一次執行內逐項計時）：
`check1 0.00s / check2 0.00s / check3 0.53s / check4 2.13s` —— check 4 才是最貴的。
它仍留在快速模式裡是刻意的：整套跑完不到 3 秒，而 check 4 是**唯一**會碰關票路徑的檢查，
用它換 2 秒不划算。（PR #1496 的 review 建議 `--skip-slow` 一併跳過 check 4；前提「check 4
比 check 3 貴」實測**成立**，但結論不採——理由是絕對成本，見上。）

`-B` 不是裝飾：harness 會載入兩份同名模組（工作區版 + `git show HEAD` 版），
stale `.pyc` 會讓你比到錯的東西。同一個坑的完整說明見
[`testing-playbook.md` §Mutation harness](../../testing-playbook.md)。

四項檢查（現況全 PASS）：

1. **`#1396` 窗口不發射** —— `today=2026-08-12` 應為 `INCONCLUSIVE`、`findings=0`
2. **零誤報** —— 17 個窗口的 false-positive bench-nights 為 0
3. **偵測面未漂移** —— 工作區版與 `HEAD` 版在 17 個乾淨窗口 + 240 個 +20% 注入情境上輸出**逐位元相同**
4. **關票路徑** —— 逐夜驅動 `run_trend_watch`，把每夜 render 出的 body 當下一夜的 issue body 餵回，量「永久退化的票會不會被自動關掉」

⛔ **檢查 1–3 完全沒有碰關票路徑**，而在檢查 4 之前，本檔的 docstring 卻寫著它「calls
`analyze_trend` / `run_trend_watch` directly」——`run_trend_watch` 在整份檔案裡只出現在那句
docstring。一個沒有實作的檢查被描述成有，比沒有這個檢查更糟；更正留在檔頭而不是靜默改掉。

檢查 4 的現況數字是 **120/120**（開票後仍有 ≥10 夜可觀察的情境全部被誤關，開票→關票中位 6 夜、
最長 9 夜），與 ADR-032 引用的數字一致——但那個數字原本出自一支已遺失的 scratchpad harness，
這裡是**從程式碼重新量出來的**。⚠️ 基準線是 100% 時這個棘輪**沒有牙齒**：沒有「更糟」可抓，
今天它只會在 harness 自檢失敗、關票落在 runway 之外、或情境母體改變時 FAIL。

⚠️ **第 3 項證明的是「沒變差」，不是「偵測力有多少」。**
改動偵測面（fire 算式）時它**應該**要 FAIL——那時要換的是重新論證，不是把這項刪掉。

## 這個 harness 的形狀為什麼重要

它**直接 `import` 受測模組**並呼叫真實的 `analyze_trend`。

這條不是風格偏好。同一次調查裡有兩支腳本因為沒做到而產出不可用的數字：一支**完全沒有 import
受測模組**、自行重寫了關票判定，量到的是一個模型而不是這份 code（那就是 CHANGELOG 一度寫過、
後來全數移除的 68%）；另一支把例外吞成「什麼都沒發生」，得到假的 0%。

**新 harness 上線前先讓它對一個已知會 fire 的輸入產出 fire**，再相信它的統計。

本檔的反面驗證（對**這份 committed 版本**現場重跑，非沿用舊結論）：把 `analyze_trend` 的
分層判定改成永遠走未分層退路——即扣掉「記錄主機類別」之前的行為——四項全部轉紅：

```text
[FAIL] 1  today=2026-08-12  status=FINDINGS  findings=5   (同類夜 14/14，因為所有夜都算同類)
[FAIL] 2  17 windows, 17 evaluable, false-positive bench-nights=15
[FAIL] 3  clean replay: DIFFERS;  +20% injection sweep: DIFFERS
[FAIL] 4  MIS-CLOSED 81/120（baseline 120/120）；self-check: clean series opens nothing = False
0/4 PASS
```

所以「四項 PASS」是有訊息量的證據，不是恆真式。

⭐ **第 4 項紅的方式值得單獨記**：它的誤關率在這個突變下**下降**到 81/120——只看棘輪
（`81 ≤ 120`）會判成通過，甚至看起來像「關票路徑變好了」。真正把它擋下來的是**自檢**：
未分層之後乾淨序列開始開假票，`clean series opens nothing` 變成 `False`。一個只有比率、
沒有自檢的版本，會在偵測器變糟的同一刻回報「改善」。
（上面的 5 / 15 是**這組窗口**下的量測值；不要和 #1431 PR body 引用的數字互相代換，
那是另一組窗口組態下量的。）

## 不可引用的數字

- **假關票率**：無可信量測。曾出現的 68% 來源不成立（見上），**已移除且不以另一個數字取代**。
- **偵測力 83.1%**：曾以 260 情境的 attributable 偵測率量得，但那份 harness 已遺失、
  情境構造無法從數字反推。本檔 `counterfactual.py` 的 240 情境診斷數字**與它不可比**，
  也不宣稱重現它。
