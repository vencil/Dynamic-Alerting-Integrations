---
section: Changed
topic: confd-family
issues: [2743]
created: 2026-10-22T16:20:17+00:00
---
- **⚠️ Breaking：根目錄平台檔 `tenants:` 區塊的 schema 違規從「通過」改為「失敗」（tools；[#2743](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2743)）**：exporter 與路由產生器把根目錄 `_` 檔（`_defaults*`、`_profiles.yaml`…，以 da-guard parse-tree 的判定為準）的 `tenants.<id>` 當成該租戶自己的值合併，但 `check_confd_schema` 與 `validate-config` 的 `json_schema` 列先前不檢查它：同一段 `_routing.group_by: alertname` 寫在租戶檔是 rc 1，寫在根目錄 `_defaults.yaml` 是 rc 0。現在每筆對照 `tenant-config.schema.json`（與租戶檔同一份定義），錯誤寫成 `_defaults.yaml: … @ /tenants/<id>/…`；`tenants: ~` 或 `<id>: ~` 也和租戶檔一樣被拒。子目錄平台檔的 `tenants:` 沒有讀取端，不判定。`check_confd_schema` 的 OK 行改為 `OK: N conf.d file(s) valid against the tenant-config / platform-defaults / routing-profiles schemas`（N 本來就含 `_defaults*`，不只租戶檔），skipped 清單下另列 `tenants:` 仍被檢查的根目錄平台檔。
