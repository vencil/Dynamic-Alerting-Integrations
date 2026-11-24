---
title: "ADR-039: 刻意付出的效能成本寫進 repo 裡的接受帳本，由 bench 閘門核對實測"
tags: [adr, benchmark, ci, perf-regression]
audience: [platform-engineers, contributors]
version: v2.9.0
lang: zh
id: ADR-039
tracking_kind: adr
status: accepted
domain: dx
created_at: 2026-10-31
updated_at: 2026-11-24
---

# ADR-039: 刻意付出的效能成本寫進 repo 裡的接受帳本，由 bench 閘門核對實測

> **Language / 語言：** **中文 (Current)** | [English](./039-bench-acceptance-ledger.en.md)

## 狀態

✅ **Accepted**（2026-10-31）。帳本與兩道 bench 閘門（PR 上的比對、合併到 main 後的逐 commit 歸因）的判定已上線。累積預算、PR 上改為擋 merge、換參考版本時帳本如何處理，見 §尚未決定。

2026-11-01 修訂：舊的絕對 ns/op 夜跑監測器退役，確定性指標的累積預算成為唯一規劃中的逐夜趨勢偵測器（見 §尚未決定）。

2026-11-13 修訂：「雙峰、量不穩的 benchmark 要不要獨立一類」改以 bench 閘門的判定為觸發，不再另做專門量測。

2026-11-24 修訂：改寫為現行行為。決策不變。

## 決策

**刻意付出的效能成本，在引入它的同一個 PR 裡，寫進 `.github/bench-accepted.toml`。兩道閘門只採用「這次變更新增的紀錄」，並拿實測值核對紀錄宣告的上限。**

一筆紀錄寫出四件事：哪幾支 benchmark（完整名稱，不接受正則）、哪個指標（`sec/op`、`B/op` 或 `allocs/op`）、允許的上限（百分比），以及一句理由。另有選填的 `issue` 欄位，事後補宣告時填那張退化票的號碼。現行帳本裡的一筆：

```toml {.quote src=".github/bench-accepted.toml"}
[[accepted]]
benches = ["BenchmarkMergeParsedPlatform_1000_Cached"]
metric = "allocs/op"
max_delta_pct = 8
reason = "寫入檢查的合併也墊內建層（#3832），多 11 個鍵；根層讀不讀得了改用已快取的解碼，不再每次重解"
```

閘門比對時被標出的每一列（統計上顯著、且至少 +5% 的退化）照下表判定：

| 這次新增的紀錄 | 判定 | 閘門怎麼做 |
|---|---|---|
| 沒有吻合的紀錄 | 退化 | 照舊：PR 上留言並貼 `perf-regression` label；合併後開票 |
| 名稱與指標吻合，實測 ≤ 上限 | 已接受 | 不算退化。不貼 label、不開票，只在執行摘要列出 |
| 名稱與指標吻合，實測 > 上限 | 退化 | 票與留言同時寫出宣告的上限與實測值 |

「吻合」要求名稱與指標兩者都對上：宣告了 `allocs/op` 的紀錄，蓋不住同一支 benchmark 的 `B/op` 退化。同一列吻合多筆紀錄時，採其中最寬的上限。

### 範例

輸入：上面那筆紀錄是這次變更新增的；benchstat 比對標出兩列。第一列只有 `sec/op` 被標，閘門因此再跑一次獨立的交錯量測，第二輪沒有再標到它。

```text
IncrementalLoad_1000_NoChange-4      6.912m ± 1%    7.403m ± 1%   +7.10% (p=0.002 n=6)     ← sec/op
MergeParsedPlatform_1000_Cached-4    8.000k ± 0%    8.520k ± 0%   +6.50% (p=0.002 n=6)     ← allocs/op
```

輸出（`scripts/tools/dx/bench_verdict.py verdict` 寫給 workflow 的值，略去最前面 `inconclusive`、`inconclusive_reason`、`benchstat_txt` 那五行）：

```text
regression_basis=timing-not-replicated
regression=false
unreplicated<<__UNREPEOF__
IncrementalLoad_1000_NoChange-4      6.912m ± 1%    7.403m ± 1%   +7.10% (p=0.002 n=6)
__UNREPEOF__
accepted<<__ACCEOF__
MergeParsedPlatform_1000_Cached-4    8.000k ± 0%    8.520k ± 0%   +6.50% (p=0.002 n=6)  [declared ≤ +8%: 寫入檢查的合併也墊內建層（#3832），多 11 個鍵；根層讀不讀得了改用已快取的解碼，不再每次重解]
__ACCEOF__
```

同一筆紀錄下，若 `allocs/op` 實測是 +9.20%，就超出上限。這時仍有確定性指標的退化，閘門不跑第二輪，`sec/op` 那列也一起算進退化（同樣略去那五行）：

```text
regression_basis=deterministic
regression=true
regressions<<__EOF__
IncrementalLoad_1000_NoChange-4      6.912m ± 1%    7.403m ± 1%   +7.10% (p=0.002 n=6)
MergeParsedPlatform_1000_Cached-4    8.000k ± 0%    8.736k ± 0%   +9.20% (p=0.002 n=6)
__EOF__
over_bound<<__OVEREOF__
MergeParsedPlatform_1000_Cached-4    8.000k ± 0%    8.736k ± 0%   +9.20% (p=0.002 n=6)  [declared ≤ +8% in .github/bench-accepted.toml]
__OVEREOF__
```

### 「這次新增的紀錄」怎麼算

取比較範圍兩端 commit 上的帳本，各自解析成紀錄的集合，取「頭端有、基端沒有」的那幾筆。比的是解析後的紀錄，不是文字行。一筆紀錄的身分是「benchmark 集合、指標、上限」三者：調整格式、重排欄位或 `benches` 內的順序、改寫理由、補上 `issue`，都還是同一筆，不會讓舊紀錄重新生效；放寬上限、改指標或增減 benchmark 則是一筆新的接受，要在那個 PR 裡重新 review。

| 閘門 | 基端 | 頭端 |
|---|---|---|
| PR 上 | merge-base | PR head |
| 合併後，單一 commit 的 push | 上一個 commit | 這個 commit |
| 合併後，一次 push 多個 commit | push 之前的 main | push 之後的 main |

紀錄屬於哪個 commit，由 git 決定（新增它的那個 commit），檔案裡不寫 SHA。所以 squash 之後照樣成立，也不需要機器人回寫 main。revert 引入成本的 PR 時，紀錄會跟著程式碼一起被移除。

### 守住帳本本身

- 帳本不是合法 TOML、有未知欄位、缺必填欄位或型別不對時，判定步驟直接失敗（結束碼 2）。不會把壞掉的帳本當成沒有紀錄，也不會當成全部接受。
- 紀錄裡的每個 benchmark 名稱都必須存在於 threshold-exporter 的現行程式碼（bench 閘門量的就是它），而且只能定義在一處。改名或移除 benchmark 時，留下的紀錄會讓 CI 的 Python 測試轉紅，不會默默地什麼都不比對；同名定義在兩處時也轉紅，因為 benchstat 的輸出列不帶 package。
- 帳本檔在 CODEOWNERS 指定了 owner，改動它的 PR 會自動請 owner review。分支保護沒有要求 code owner 核准，所以這一步是請求，不是機械擋下。
- 基準為 0 的退化（例如本來不配置記憶體、現在配置），benchstat 算不出百分比，判定視為無上限的增加，任何上限都蓋不住。

## 背景

bench 閘門有兩道，用的是同一套判定：

- **PR 上**：PR head 對 merge-base，退化就留言、貼 `perf-regression` label，不擋 merge。
- **合併後**：main 的每個 commit 對它的上一個 commit（squash 合併下，一個 commit 就是一個 PR），退化就開一張票。這張票只在同一個 commit 重跑後不再判為退化時自動關。

有些退化是刻意的：為了正確性多做一次解碼、結構多一個欄位。沒有帳本時，作者只能在 PR 留言寫明取捨，然後 merge。合併後那道閘門讀不到 PR 留言，於是重量一次、再開一張票；而真正的取捨重跑幾次都不會消失，票就一直開著，每張都得有人看過再手動關。

[ADR-032](032-paired-interleaved-bench-measurement.md) §7 從夜跑監測的角度看到同一件事：監測需要第三條出口「已歸因、已接受」，並留下兩個子題——標記放在哪、粒度是什麼。本 ADR 的答案是：放在 repo 裡的帳本，粒度是「benchmark × 指標 × 上限」。

## 後果與已知限制

- **作者要估上限。** `B/op` 與 `allocs/op` 在同一份輸入下逐次相同，照 PR 上 bench 閘門量到的數字抓一點餘裕即可；`sec/op` 有噪音，上限要寬一些。
- **上限是寬鬆的那一邊。** 實測遠小於上限時，剩下的餘裕在這次判定裡沒有作用，但它也不會延續到之後的 commit：每道閘門只看這次新增的紀錄。
- **事後補宣告要人工關票。** 先 merge、之後才決定接受時，在後續的 PR 新增紀錄（可填 `issue` 欄位），合併後在票上留言附紀錄連結並關票。這一步不自動化。
- **多個 commit 一次 push 時，歸因仍只量最後一個 commit。** 帳本會納入整次 push 新增的紀錄，但中間那幾個 commit 的退化沒有被量到。
- **sub-benchmark（`b.Run` 產生的 `BenchmarkX/case`）無法宣告。** 名稱只接受 `BenchmarkX` 形式；threshold-exporter 目前的 benchmark 都沒有 sub-benchmark，出現時要擴充名稱規則。
- **merge-base 與上一個 commit 可能不同。** PR 上量到的 Δ 與合併後量到的 Δ 不一定一樣；開票的是合併後那道閘門，所以上限要能涵蓋兩者。
- **逐 commit 都低於門檻的累積漂移，目前沒有逐夜的自動偵測。** 夜跑的配對 ns/op 只寫執行摘要，不開票、也不讀帳本。發版時的比對（`bench-gate-release.yaml`，對前一個平台版本、α=0.05、|Δ| ≥ 10%）會把這類退化寫進報告，但不擋發版。接手的累積預算見下一節。

## 尚未決定

| 項目 | 什麼時候決定 |
|---|---|
| **確定性指標的累積預算**：夜跑在 main 對參考版本的比值上加入 `B/op`、`allocs/op`，抓逐 commit 都低於 5%、但累積起來很大的漂移。這是唯一規劃中的逐夜趨勢偵測器：絕對 ns/op 監測器已退役，配對 ns/op 只供參考 | 夜跑已開始記錄：配對量測成功的夜，輸出（`bench-paired/v3`）帶 `deterministic` 欄位，對兩側都有資料的 benchmark 記下 `B/op`、`allocs/op` 的中位數與比值，只記錄、不判定；記為無法評估的夜沒有這個欄位（`scripts/tools/dx/pair_bench_ratio.py`）。累積至少四週、量出它們真實的夜間噪音後再決定預算。設計時必須先解決一個已知問題：若預期值用「上限的連乘」，接受過的上限會變成之後的退化可以花用的額度，改善也會被當成額度 |
| **PR 上對「沒宣告的確定性退化」改為擋 merge** | 帳本上線滿四週，且這段期間每一次被標的確定性退化都能用帳本解掉時，由 owner 決定 |
| **換參考版本時帳本怎麼處理**（ADR-032「待決與已決的設計題」第 1 題） | 與累積預算一起決定。目前夜跑不開票、不讀帳本，所以換參考版本不會關到任何票；累積預算若要開票，ADR-032 的不變量（換參考版本不得關閉尚未結案的效能票）要在它的設計裡成立 |
| **雙峰、量不穩的 benchmark 要不要獨立一類** | 這裡指的是 `BenchmarkIncrementalLoad_1000_OneFileChanged`：在 GitHub runner 上重複量測，同一天內的 sec/op 比值分成高低兩群，相差約 26 個百分點（+30.38% 對 +3.98%，量測紀錄在 [#1497](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1497)）。等任一道 bench 閘門以 `timing-replicated` 標出它時再決定：只有 sec/op 被標，第二次獨立的交錯量測也標到同一支（PR 上的判定摘要寫 `sec/op only, confirmed`，合併後的歸因票寫 `sec/op only, confirmed by a second independent interleaved pass`）。這個判定本身就表示 `B/op`、`allocs/op` 沒有被標。到時以那次兩輪的數字決定：讓它獨立一類，或把它的 sec/op 寫進接受帳本。複驗不保證擋得住這種散佈：兩輪剛好落在同一側時，閘門仍可能標出不存在的退化。不再為它另做專門量測 |

## 考慮過的替代方案

| 做法 | 沒採用的原因 |
|---|---|
| issue 上的 label | 不進版控、不能綁上限、revert 時不會跟著消失 |
| commit 訊息的 trailer（例如 `Perf-Accepted:`） | squash 訊息在 merge 當下才寫，review 時看不到；多筆紀錄不好寫；合併的人改得掉 |
| 外部服務的「一鍵確認」 | 紀錄不在 repo 裡，無法隨程式碼審查與 revert |
| 接受之後自動把基準重設為新值 | 就是靜默吸收：之後在新基準上再退化一次，看起來像沒事 |
| 只寫 benchmark 名稱，不寫上限 | 在已接受的基準上再退化一次會被整個蓋掉 |
| 用正則指定 benchmark | 一筆紀錄可能蓋到原本沒打算接受的 benchmark；改名時也不會被發現 |

## 實作位置

- 帳本：`.github/bench-accepted.toml`（檔頭有欄位說明）
- 判定：`scripts/tools/dx/bench_verdict.py`（`parse_ledger`、`added_entries`、`apply_ledger`）
- 編排：`scripts/tools/ops/bench_gate_compare.sh`（`LEDGER_BASE_SHA`／`LEDGER_HEAD_SHA`）
- 合併後多 commit push 的基端：`.github/workflows/bench-attrib-main.yaml`
- 測試與帳本 lint：`tests/dx/test_bench_verdict.py`

## 相關

- [ADR-032](032-paired-interleaved-bench-measurement.md)：夜跑成對交錯量測；本 ADR 回答其 §7 的「標記放在哪」與「粒度」
- [#3468](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3468)：效能退化票的生命週期
