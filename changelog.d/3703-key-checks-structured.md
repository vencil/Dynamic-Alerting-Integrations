---
section: Changed
topic: exporter
issues: [3703]
created: 2026-11-14T14:00:00+00:00
---
- **⚠️ BREAKING：`key_checks` 改成結構化記錄，不再是 Go 引用整個鍵組好的句子（da-guard、route generator；[#3703](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3703)）**：served-values 與 key-verdict 的 `key_checks` 由 `{version_labels, renames}` 兩串句子改為記錄清單（形狀見 served-values 的 `key_checks` 條目），產生器與 validate-config 因此不再把作者寫的鍵與 label 值原樣印進 stderr 與 `--findings-json`。key-verdict 文件升為 `da-guard.key-verdict/v4`（v3 視為舊版 da-guard）；served-values 仍為 v1，舊形狀視為舊版 da-guard（兩者都尚未隨版本發布，只影響追 main 的使用者）。exporter 的 log 與 tenant-api 的訊息不變。
