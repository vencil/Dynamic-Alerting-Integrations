---
section: Added
topic: da-tools
issues: [3016, 3394]
created: 2026-10-16T11:30:00+00:00
---
- **`validate-config` 新增 `unread_keys`：有沒有讀取端讀的鍵就 FAIL（[ADR-038](docs/adr/038-unread-keys-judged-by-readers.md)）**：以共用判決（與 `da-tools explain-keys` 同一份）列出拼錯的鍵、沒關到任何東西或只關到一部分的 `disable`、寫在讀取端不讀那一層的鍵，以及平台送出未登記的 `_` 鍵；每個問題一行，寫出檔案、等級、key、原因與受影響的租戶，平台檔或 profile 的同一個問題只有一行。沒有讀取端讀的鍵一律 error，不分寫在租戶檔、profile 或平台檔（[#3394](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3394)），只有 `disable_noop`（warning）與 `_state_<filter>` 指向未宣告的 filter（notice）例外。判決的 `blocks` 為真（有 error，或沒讀到根平台檔而有判不了的閾值鍵）時這列 FAIL、結束碼 1；da-guard 失敗或判不完也 FAIL。
