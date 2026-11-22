---
title: "ADR-034: 合法值不得同時當作無法辨識時的 fallback"
tags: [adr, config, reliability, dx]
audience: [platform-engineers, sre, contributors]
version: v2.9.0
lang: zh
id: ADR-034
tracking_kind: adr
status: accepted
domain: platform
created_at: 2026-08-23
updated_at: 2026-11-22
---

# ADR-034: 合法值不得同時當作無法辨識時的 fallback

## 狀態

✅ **Accepted**（2026-08-29）。

- 2026-10-13 修訂：補上「空值」的判準（設成空字串的 `TA_WRITE_MODE` 拒絕啟動），見「決策」的空值一段。
- 2026-11-05 修訂：布林環境變數一例原本舉的三個 RBAC 環境變數，已依 [ADR-027](027-tenant-api-caller-identity.md) 刪除，相關句子改寫為現況。

> 依語言政策，本 ADR 不另製 `.en.md`。

## 背景

fallback 指「輸入認不得時退回去用的值」。當一個設定的某個合法值**同時**被拿來當 fallback，打錯字的輸入就和那個合法值無法區分。如果這個設定決定了某道檢查跑不跑，打錯字的後果就是**檢查被關掉，而啟動當下沒有任何可辨別的訊號**。

這條規則來自 tenant-api 的兩個實例，兩個都已修正：

- **`--write-mode`**：決定寫入要直接 commit，還是開 PR／MR 讓人審核。修正前，解讀它的 `switch` 只為 PR 模式寫了分支，合法值 `direct` 沒有自己的分支，就是 `default`。於是 `pr-guthub`（打錯字）、`PR`（大小寫）全部落到 `default`，與刻意選 `direct` 執行同一段程式、印同一行啟動日誌 `direct write mode (commit-on-write)`。一個以為「每筆變更都要經過審核」的部署，可能每次寫入都直接 commit。時間拉長後「PR 不再出現」是線索，但那要有人注意到某件事**沒有發生**，這是最不可靠的偵測。
- **布林環境變數**：tenant-api 自己寫了一支把字串轉成布林的解析函式，接受 `true`/`1`/`yes`/`on`，其餘一律回 `false`，不報錯也不記錄。同一個打錯的值走命令列旗標會當場解析失敗（`flag.Bool` 用的是 `strconv.ParseBool`），走環境變數卻靜默變成 `false`。這不是「還沒做驗證」，而是繞過了標準庫已經做好的驗證。

## 決策

### 規則

> **決定「某道檢查是否執行」的列舉型設定值，不得把任何一個合法值同時當作無法辨識時的 fallback。**
>
> 必須先對照合法值集合驗證；驗不過就停下來報錯，不要落到任何一個合法值上。

終態必須是報錯。「先出一版只警告」等於仍舊落到合法值上、再多印一行日誌，而這條規則的論證正是那行日誌不可靠。過渡怎麼排是各案例自己的上線決定，但不改變終態。

### 適用條件

兩個條件都要成立：

1. **列舉型**：這個設定有一份明確、有限的合法值清單可以比對。
2. **決定某道檢查跑不跑**：這個值選錯，會不會讓某段本應執行的驗證、審核、授權或稽核動作不被執行？會，就屬於本規則。

「審核」明確包含**人工**審核。`--write-mode` 決定的正是「要不要有人看過再寫入」；動作由人還是由程式執行，不改變「它整段沒有發生」這件事。

| 設定 | 屬於嗎 | 理由 |
|:--|:--|:--|
| 走不走人工審核 | ✅ | 審核整段不執行 |
| 要不要啟用稽核日誌 | ✅ | 稽核紀錄整段不產生 |
| `--log-level` 打錯退成 `info` | ❌ | 沒有任何驗證、授權或稽核被跳過，只是輸出詳細度變了 |
| 顯示用時區打錯退成 UTC | ❌ | 同上，且與檢查無關 |

### 不適用的範圍

- **不是列舉型的值**（檔案路徑、端點位址、來源標記）：沒有合法集合可以比對，那是另一個問題，本 ADR 不主張。
- **不決定檢查跑不跑的值**（重試次數、顯示用時區、判斷檔案佈局）：寫錯的後果不是「一道檢查消失」。

限縮在這個交集的理由：沒有合法集合，「對照集合驗證」這個動作就不存在；而只有「決定檢查跑不跑」的值，打錯的樣子才會與「刻意不要檢查」一模一樣。`--retry-count` 打錯改變的是重試次數，那是程度差異，重試本身也會留下痕跡（請求次數、耗時）。

這條規則**不主張**交集之外的靜默 fallback 都是好的；交集之外，本 ADR 沒有意見。

### 套用時的三個判準

修正兩個實例時定下的細節，之後套用本規則也照做：

- **空值只能落到偏保守的預設值。** 預設值是比較開放的那一側時，設成空字串（常見於 Helm、Kustomize、Compose 模板沒渲染到值）必須拒絕，不能當成沒設。`TA_WRITE_MODE` 沒設時用預設的 `direct`，但 `direct` 是不經審核的那一側，所以設成空字串或只有空白就拒絕啟動。布林環境變數的預設值是 `false`（不放寬），所以那裡把空白當成沒設。
- **空白可以 trim，大小寫不折疊。** trim 前後空白不會讓認不得的值變成合法值（`" pr-guthub "` 照樣被拒）；折疊大小寫會（`PR` → `pr`），而且沒有標準庫的既有契約可以繼承，那是程式在猜意圖。布林值例外：`strconv.ParseBool` 本身就接受 `TRUE`／`True`，直接沿用它的契約。
- **`default` 分支不再是任何合法值的家。** 合法值都有自己的分支，`default` 改成「程式錯誤」並中止。這擋的是下一次的自己：往合法值集合加了新值卻忘了接線，會大聲失敗，而不是無聲落回某個合法值。

### 範例

`--write-mode` 現在的合法值是 `direct`、`pr-github`、`pr-gitlab` 三個，先 trim 前後空白，再逐字比對。直接呼叫解讀函式 `parseWriteMode` 的結果：

```text
"direct" -> mode="direct" err=<nil>
" pr-gitlab " -> mode="pr-gitlab" err=<nil>
"pr-guthub" -> mode="" err=not one of the three write-back modes
"PR" -> mode="" err=not one of the three write-back modes
"DIRECT" -> mode="" err=not one of the three write-back modes
```

被拒的值讓 tenant-api 拒絕啟動，訊息列出接受的三個值。以 `go build ./cmd/server` 建出的 binary 實際啟動，設成空字串的 `TA_WRITE_MODE` 在解析旗標之前就被擋下：

```text
$ env -i TA_WRITE_MODE= ./tenant-api; echo rc=$?
2026/11/21 23:29:39 FATAL: TA_WRITE_MODE is set but empty (""). Refusing to start: unset it for the default, or give it a value. An empty value is not read as unset because the default here is the more open choice (#2491, ADR-034).
rc=1
```

布林環境變數改由 `strconv.ParseBool` 判斷，未設或只有空白則用旗標自己的預設值。直接呼叫解析函式的結果：

```text
env "" -> false err=<nil>
env "true" -> true err=<nil>
env "t" -> true err=<nil>
env "yes" -> false err=not a boolean
env "on" -> false err=not a boolean
env "TRUE" -> true err=<nil>
```

解析不了就拒絕啟動：

```text
$ env -i TA_DEV_BYPASS_AUTH=yes ./tenant-api; echo rc=$?
2026/11/21 23:29:39 FATAL: TA_DEV_BYPASS_AUTH="yes" is not a boolean — accepted: 1, t, T, TRUE, true, True, 0, f, F, FALSE, false, False (and unset for the flag default). Refusing to start rather than silently treating it as false, because this switch decides whether a check runs (ADR-034).
rc=1
```

目前經過這支解析函式的只剩 `TA_DEV_BYPASS_AUTH`（本機開發用的身分注入）。它打錯的方向其實比較安全（維持需要認證），嚴格說不完全落在本規則的交集內；仍照規則處理，是因為所有布林環境變數共用同一支解析函式，留一半不改，下一個接上去的消費端就會再分歧。

## 後果與已知限制

- 新增一個「決定檢查跑不跑」的列舉型設定時，要多寫一段驗證。
- **會多出啟動期失敗**，而且不只打錯字的部署：
  - 過去被接受、現在不合法的值會讓部署開不起來。布林環境變數的 `yes`／`on` 過去是 `true`，現在拒絕啟動；`TA_WRITE_MODE=DIRECT` 過去拿到的正是它要的 `direct`，現在也拒絕啟動。
  - `t`／`T` 過去被靜默當成 `false`，現在是 `true`，是唯一往「更開」翻的輸入。
  - 這是刻意的：被拒的部署本來就沒有在做自己以為在做的事。repo 內沒有任何部署設定用到這些值：官方 Helm chart 的 `writeBack.mode` 以 schema 限定為三個合法值或空字串（空字串會讓渲染失敗），try-local 的 compose 用的是 `direct`。repo 外的現場部署有多少帶著這些值，沒有盤點過。
- **規則主要仍靠 code review。** 機械檢查只抓一種已知形狀，見下。

### 機械檢查只抓已知形狀

`scripts/tools/lint/check_env_bool_parsers.py`（pre-commit hook `env-bool-parser-guard`，只掃有變動的 Go 檔）禁止 Go 生產程式碼出現自製的「字串轉布林」解析函式，比對兩種**單行**形狀：

- 一個 `case` 分支列出兩個以上布林字面值，例如 `case "true", "1", "yes", "on":`。合法的列舉 `switch`（`case "pr", "pr-github":`）不會命中；單一個 `case "true":` 也不命中，因為一個字面值分不出布林解析函式與一般列舉，這是刻意選的精確度與召回率取捨。
- 同一行既讀環境變數、又拿它和布林字面值比較，例如 `strings.ToLower(os.Getenv(k)) == "true"`。

它抓不到的：跨行寫成別的樣子、Python 程式、字面值換個拼法；`_test.go` 也在範圍外（測試會合法地拿環境變數比對字串當子行程的哨兵）。它更抓不到 `--write-mode` 那種形狀：那裡的缺陷是「一個 `switch` 沒有對照合法值集合」，與布林解析函式無關。

repo 目前對它零命中，所以它是純粹的防迴歸檢查。為了不讓它在找不到東西時悄悄失效，它的自我測試把修正前那支布林解析函式的原始碼餵進去，必須命中，抓不到就轉紅（`tests/lint/test_check_env_bool_parsers.py`）。

要補齊其餘形狀，需要的不是更多 lint，而是在票流裡辨認出同型缺陷：固定的 label 與具名負責人。本 ADR 沒有指定兩者，那超出它能單方面決定的範圍。

## 考慮過的替代方案

**只修這一個地方，不立規則。**
不採用。只修一處，下一個同型的設定沒有判準可循，又得從頭論證一次。這類缺陷的失敗形狀都相同：某件事沒有發生，而且沒有訊號；值得一條能套到新情況上的通則。

**立一條更廣的原則：所有靜默 fallback 都要改成報錯。**
不採用。廣義版判不動它自己舉的案例：判斷檔案佈局那類值套進去會得到「該報錯」，但那不是想要的結果；「憑證可以明文寫進設定」那類缺陷（例如租戶經 tenant-api 送上來的告警接收端設定，直接把 token 明文寫在欄位裡）根本沒有「被寫錯的值」，套不進來。因此收窄成現在的交集。

**寫進 dev-rules，而不是 ADR。**
不採用。dev-rules 記的是「不要做 X」，這裡要記的是適用邊界與理由。dev-rules 可以加一行指過來。

## 相關

- [ADR-023: tenant-api 的寫入只能有一個寫者](023-write-plane-single-writer-invariant.md)：`--write-mode` 決定的寫入路徑
- [ADR-033: 與運維執行平面的協同介面](033-ops-execution-plane-interface.md)：本規則的思路借自該 ADR 記錄的協同產品，借的是「認不出來時絕不猜」這個態度，不是它的分層結構
- 實作位置：`components/tenant-api/cmd/server/wire.go`（`parseWriteMode`、`wirePRBackend`）、`components/tenant-api/cmd/server/main.go`（`envNonEmptyOrDefault`、`parseEnvBool`、`envBool`）；契約表在 `cmd/server/writemode_test.go`
- 追蹤：[#1559](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1559)（`--write-mode`）、[#1599](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1599)（布林環境變數）
