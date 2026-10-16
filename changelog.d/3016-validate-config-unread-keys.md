---
section: Added
topic: da-tools
issues: [3016]
created: 2026-10-16T11:30:00+00:00
---
- **`validate-config` 新增 `unread_keys`：沒有任何讀取端讀的鍵，只報不擋（[ADR-038](docs/adr/038-unread-keys-judged-by-readers.md)）**：以共用判決（與 `da-tools explain-keys` 同一份）列出拼錯的鍵、沒關到任何東西或只關到一部分的 `disable`、寫在讀取端不讀那一層的鍵，以及平台送出未登記的 `_` 鍵；每個問題一行 WARN，寫出檔案、各閘門日後擋下時的等級、key、原因與受影響的租戶，平台檔或 profile 的同一個問題只有一行。結束碼不變。da-guard 不可用或失敗時只說判決未完成，不會讓檢查失敗。各閘門何時改成擋下依 [#3016](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3016) 推進。
