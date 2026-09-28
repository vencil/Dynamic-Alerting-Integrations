---
section: Fixed
topic: portal
issues: [2068]
created: 2026-09-28T17:09:55+00:00
---
- **Tenant Manager 會標出設定檔無法使用的租戶（portal；[#2068](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2068)）**：tenant-api 對設定檔壞掉的租戶回傳只帶 `id` 與 `config_error` 的降級列（[#1680](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1680)），portal 以前把它當成 environment 為 `unknown` 的一般租戶顯示。現在這種卡片改顯示「設定檔無法使用」，附上原因（`unreadable`／`not_regular_file`／`malformed_yaml`／`invalid_config`，中英雙語）與原始代碼，並且不再顯示那些只是預設值的環境、tier、模式與其他欄位。頁面頂端既有的解析失敗通知維持不變。
