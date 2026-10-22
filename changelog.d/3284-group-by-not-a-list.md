---
section: Changed
topic: alertmanager-routing
issues: [3284]
created: 2026-10-22T18:46:53+00:00
---
- **⚠️ Breaking：`group_by` 不是 list 時所有模式整棵樹拒收（tools；[#3284](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3284)）**：`group_by: alertname` 這類字串、數字、布林或 mapping，過去被路由產生器當成沒寫——route 上沒有 `group_by`、改繼承上層分組，`--validate --strict` 照樣 rc 0。現在產生器讀 `group_by` 的每一處（租戶 `_routing` 與根目錄平台檔 `tenants:`、各層 `_routing_defaults`、routing profile 的主 route 與 `overrides`／`routes` 條目，以及 `_routing_enforced`）都判，render、`--dry-run`、`--validate`、`--strict` 一律 rc 1、什麼都不寫出（`ERROR (field type):`，kind `routing_group_by_invalid`），tenant-api 寫入回 422，`validate-config` 的 `schema` 與 `routes` 列 FAIL（`routes` 列對所有 field-type 問題都改為 FAIL）。不會把字串轉成 list；修法寫成 `group_by: [alertname]`。null 與 `[]` 照舊。
