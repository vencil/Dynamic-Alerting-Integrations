---
section: Changed
topic: tenant-api
issues: [3685]
created: 2026-11-10T13:40:00+00:00
---
- **⚠️ BREAKING：直寫時根平台檔與 HEAD 不一致也拒寫（tenant-api；[#3685](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3685)）**：直寫模式的鍵檢查從 tenant-api 工作樹讀根平台檔，共用鍵判決卻判 HEAD；兩邊讀的根不同時可能都不擋，例如未 commit 的修改拿掉某鍵的宣告，死條目就被提交進去。現在 #1535 的租戶檔比對擴大到 conf.d 根目錄每個名稱以 `_` 開頭的項目，工作樹有的、HEAD 有的都算，被刪除的也算。不一致時回 409 `TENANT_FILE_DIRTY`（批次 422），代碼不變，固定訊息改為同時點出根平台檔。以前根平台檔有未 commit 的修改時照常寫入。
