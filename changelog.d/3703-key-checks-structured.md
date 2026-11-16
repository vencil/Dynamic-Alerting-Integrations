---
section: Changed
topic: exporter
issues: [3703, 3961]
created: 2026-11-14T14:00:00+00:00
---
- **⚠️ BREAKING：`key_checks` 改成結構化記錄，不再是 Go 引用整個鍵組好的句子（da-guard、route generator；[#3703](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3703)）**：served-values 與 key-verdict 的 `key_checks` 由 `{version_labels, renames}` 兩串句子改為記錄清單（形狀見 served-values 的 `key_checks` 條目），產生器與 validate-config 因此不再把作者寫的鍵與 label 值原樣印進 stderr 與 `--findings-json`。兩份文件的 schema 都升版：key-verdict 為 `da-guard.key-verdict/v5`（v5 帶 `gate_kind`，#3996）、served-values 為 `da-guard.served-values/v3`（v3 另帶 #3877 條目的 `ungathered`）。新工具配舊 da-guard：明說 da-guard 比工具舊、請升級或重建；舊工具配新 da-guard：說 schema 不合（寫出兩個值），不說較舊；兩者結束碼都是 2（兩者都尚未隨版本發布，只影響追 main 的使用者）。exporter 的 log 與 tenant-api 的訊息不變。同一個 served-values v2 下，每個租戶的 `custom_alerts`、`custom_alert_rows` 也是必要欄位（[#3961](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3961)）：缺了是壞掉的輸出（結束碼 2），不再當成「比這兩欄舊的 da-guard」——config-diff、route generator、recipe-preview、describe-tenant 原本為此各有的退路一併拿掉（describe-tenant 不再印 `custom_alerts not shown … older than this tool` 那行 WARN）。
