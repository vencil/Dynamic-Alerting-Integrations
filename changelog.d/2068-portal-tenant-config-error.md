---
section: Fixed
topic: portal
issues: [2068]
created: 2026-09-28T17:09:55+00:00
---
- **Tenant Manager 會標出設定檔無法使用的租戶（portal；[#2068](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2068)）**：tenant-api 對設定檔壞掉的租戶回傳只帶 `id` 與 `config_error` 的降級列（[#1680](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1680)），portal 以前把它當成 environment 為 `unknown` 的一般租戶。現在這種卡片改顯示「設定檔無法使用」，附上原因（`unreadable`／`not_regular_file`／`malformed_yaml`／`invalid_config`，中英雙語）與原始代碼，不再顯示那些只是預設值的欄位。
  頂端統計改把這類租戶計入新的「設定錯誤」卡（有才顯示），不再混進 environment／模式的 `unknown`，environment／模式篩選也不會命中它們。批次產生維護／靜默 YAML 時也會排除它們，並在視窗中列出被排除的租戶與原因，因為片段貼進壞掉的檔案也不會生效；選取的全是這類租戶時，「複製」會停用。頁面頂端既有的解析失敗通知維持不變。
