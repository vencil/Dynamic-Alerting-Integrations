---
section: Fixed
topic: da-guard
issues: [4288, 4330]
created: 2026-11-24T23:32:38+00:00
---
- **`redundant_override` 對閾值鍵改為反事實判定（da-guard；[#4288](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4288)、[#4330](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4330)）**：以前比的是 YAML 的值與型別，根目錄寫數字 `80`、租戶檔依 schema 寫字串 `"80"` 的一般寫法抓不到重複覆寫。現在讓 exporter 把租戶建構兩次（照寫、刪掉那一行），送出的閾值列從現在起（每個時段切點、`expires:` 到期之後）都相同才報，所以 `"80"`、`"80.0"`、`"80:warning"` 會報，`"80:critical"`、會改變閾值的刪除（例如根目錄缺 `defaults:` 包裝）不報；已由別的 finding 負責的行（值被拒收、沒人讀的鍵、沒東西可關的 `disable`）也不報。`_` 開頭的保留鍵仍比 YAML 的值與型別。⚠️ 開著 `--warn-as-error` 的 CI 可能多出 warning 而轉紅，刪掉被點名的覆寫即可；寫的是舊拼法時，`da_config_deprecated_keys` 會跟著少一筆。
