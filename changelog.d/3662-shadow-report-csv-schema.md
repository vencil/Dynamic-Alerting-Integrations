---
section: Changed
topic: da-tools
issues: [3662]
created: 2026-11-07T02:58:22+00:00
---
- **⚠️ Breaking：`shadow-verify --report-csv` 只收 `validate` 產出的 report（[#3662](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3662)）**：表頭必須正好是 `validate` 寫出的 9 欄（同一份定義），至少一筆資料列、每列欄數相同、`Timestamp` 為 `YYYY-MM-DD HH:MM:SS`；空檔、只有表頭、別的 CSV、欄位過多的列、時間格式錯由結束碼 0 改為 2（先前都讀成「沒有 mismatch」：`runtime` 判 PASS，`convergence` 的 7 天檢查只給 warn），欄位過少的列由 traceback 改為具名錯誤；stderr 一行點名旗標與路徑。`validate` 寫入的 `Timestamp` 改為 UTC（先前是本地時間，卻被當成 UTC 讀）。
