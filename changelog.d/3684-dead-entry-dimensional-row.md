---
section: Fixed
topic: exporter
issues: [3684]
created: 2026-11-10T12:30:00+00:00
---
- **送不出 row 的維度鍵不再被報死條目（exporter、tenant-api、da-guard；[#3684](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3684)）**：維度鍵的 label 沒在根 `dimensions:` 宣告時，exporter 不送這一列，共用判決已報 `unread`；寫入關卡的值檢查卻另報一筆 `dead_entry`，照提示刪掉條目也修不好這個鍵。現在死條目只判 exporter 會送出的維度列（與 resolver 同一個 `dimensionalRowRead`）。寫入關卡逐租戶判定用的單租戶副本（tenant-api 的合併、`ValidateTenantKeysByTenant`）以前沒帶根 `dimensions:`，改為整份複製根欄位，宣告了的維度鍵照舊判死條目。
