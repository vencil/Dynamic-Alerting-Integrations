---
section: Changed
topic: da-tools
issues: [3387]
created: 2026-10-29T10:00:00+00:00
---
- **⚠️ Breaking：`validate-config` 以 `guard_errors` 一列列出 da-guard 的全部 error，刪除逐列手寫的鏡像（da-tools；[#3387](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3387)）**：新列執行 da-guard 自己的檢查（`--format json`），`severity: error` 的 finding 不論 kind 一律 FAIL，每筆寫出 kind、鍵、租戶與訊息，同一 finding 影響多個租戶時合併成一行。da-guard 結束碼 4（路由由 `routes` 列判）不算 FAIL；3 FAIL 並點名被丟掉的檔。`values_not_served`、`defaults_wrapper`、`schedule_null`、`custom_alert_cap` 四列與 `root_defaults` 中對應 `root_default_refused`、`root_defaults_reserved_key`、`root_defaults_unwrapped` 的部分已刪，`--json` 消費端改讀 `guard_errors`。先前沒有任何一列鏡像的 error 現在也擋：例如子目錄 `_defaults.yaml` 寫了根目錄沒宣告的閾值鍵（`subtree_default_undeliverable`），同一棵樹結束碼 0 → 1。
