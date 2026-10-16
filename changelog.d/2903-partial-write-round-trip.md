---
section: Changed
topic: tenant-api
issues: [2903]
created: 2026-10-15T13:58:02+00:00
---
- **部分更新不得改到沒編輯的內容（ADR-036 §4；[#2903](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2903)）**：批次 patch 與 `PUT .../custom-alerts` 會把租戶檔整份重新輸出。合併後把原檔與結果解析成節點逐一比對，編輯以外的寫法（style、引號、tag、原文、anchor、別名所指）必須完全相同；不同就不寫入，回 `TENANT_CONFIG_NOT_LOADABLE`（custom-alerts 409，批次是該筆結果），`config_error` 為 `not_round_trippable`。例如 flow 序列裡的 `1:20`、開頭有空行的 block scalar、別名共用的租戶 block。合併寫出的字串一律加雙引號；custom-alerts 裡含換行的字串改寫成 literal block（`|`），讀回來不變時才用，否則仍是雙引號。YAML 解碼失敗的訊息改為只說哪裡失敗與行號，parser 原文只進 server log。
