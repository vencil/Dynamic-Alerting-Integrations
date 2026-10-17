---
section: Added
topic: da-tools
issues: [3016]
created: 2026-10-16T09:30:00+00:00
---
- **`da-tools explain-keys`：逐鍵說明租戶設定，並可用 `--assert` 放進自己的 CI（[ADR-038](docs/adr/038-unread-keys-judged-by-readers.md) 決定 9）**：對每個租戶列出每個 key 的來源層與檔案、哪些讀取端讀它（exporter 實測，其他讀取端依保留鍵登錄表在該層的宣告，適用的 `_policies` 規則以 `target` 指名的 key 算 Policy-as-Code 讀了，萬用字元不算）、判決（`read`／`unread`／`undecidable`）與等級、`disable` 的作用（effective／partial／noop）、寫下的值與 `/metrics` 送出的值；平台檔或 profile 的同一個問題只列一筆。`disable_partial` 只在仍送出的同族鍵（`_critical`、維度鍵）不是租戶檔自己寫的時候報，並以 `served_from` 指名它來自哪個檔；租戶自己寫的視為刻意只留那一級（[#3046](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3046)）。`--assert <file>` 與預期結果比對，不同即結束碼 1；`--json` 輸出 `da-tools.explain-keys/v1`。沒讀到任何根平台檔時閾值鍵為 `undecidable`，結束碼 2。判決來自新的共用判決函式（`scripts/tools/_lib_key_verdict.py`），之後的閘門都呼叫它。需要 da-guard（映像內建）。
