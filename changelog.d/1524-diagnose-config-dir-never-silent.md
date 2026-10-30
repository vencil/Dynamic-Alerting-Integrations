---
section: Changed
topic: da-tools
issues: [1524, 3392]
created: 2026-10-30T12:55:47+00:00
---
- **diagnose 給了 `--config-dir` 就一定查到，查不到就是呼叫端錯誤（[#1524](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1524)）**：`--config-dir` 不存在、不是目錄（含空字串）或進不去時，在任何 Prometheus／kubectl 查詢之前以結束碼 2 停下，`--json` reason 為 `dir_not_found`／`dir_not_accessible`（改走 [#3276](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3276) 的共用路徑判定）；樹裡沒有這個租戶時結束碼 2、reason `tenant_not_found`，與 tenant-api `/effective` 對未知租戶回 404 同義。**Breaking**：這幾種情況先前都是結束碼 0——`--show-inheritance` 印 `{"error": "Could not resolve inheritance chain"}`，一般模式的輸出與沒給 `--config-dir` 完全相同。以 import 使用的 `resolve_inheritance_chain`／`lookup_tenant_profile`／`check` 在同樣情況改為 raise `CallerError`（租戶不在時是子類別 `TenantNotFoundError`），不再回 `None` 或略過繼承鏈；沒給 `config_dir`（`None`）時行為不變。
