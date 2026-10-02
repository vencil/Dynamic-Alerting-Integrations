---
section: Fixed
topic: alertmanager-routing
issues: [2341]
created: 2026-10-02T01:09:07+00:00
---
- **不合法的租戶 id 不再產生路由，空 id 不再吸走平台告警（[#2341](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2341)）**：租戶 id 為空字串，或含英數字、`_`、`-` 以外的字元（空白、`!`、`.` 等）時，`generate-routes`、da-guard 先前都放行；空 id 產生 `tenant=""` 的 matcher，Alertmanager 視為「沒有 tenant label」，`PlatformDown` 等平台告警被導到 `tenant-`。行為變更：產生器對這種租戶不產出任何 route、receiver、inhibit rule（所有模式），印 `WARN … skipping`（`--validate` 失敗），`--strict` 另報 `ERROR`；da-guard 報 `invalid_tenant_id`（error）；tenant-api 對這種 id 的 PUT、custom-alerts PUT、validate 與 batch op 回 400／錯誤。大寫仍可用。規則是 repo 內三份既有租戶名稱規則都會拒絕的部分；exporter 本身不變。
