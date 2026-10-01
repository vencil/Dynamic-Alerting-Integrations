---
section: Changed
topic: tenant-api
issues: [2339]
created: 2026-09-30T23:37:45+00:00
---
- **group batch 改走與 `/tenants/batch` 同一條寫入管線（tenant-api；[#2339](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2339)）**：`POST /api/v1/groups/{id}/batch` 先前在 PR 模式仍直接 commit 到 base branch、不開 PR，且兩種模式都不檢查 domain policy 與 patch 值。現在每個成員展開成一筆 op：PR 模式整組合成**一支** PR（回應 `status: pending_review`，帶 `pr_url`、`pr_number`、`group_id`，PR 帶 `group` label），base branch 不再有直接 commit；兩種模式都套 domain policy（違反的成員該筆為 `error`、不寫入）與 patch 值檢查（違規回 400 `INVALID_BODY`）。PR 模式的錯誤碼（403 / 409 / 500 / 503）與 `/tenants/batch` 相同。PR 模式忽略 `?async=true`、一律同步回 200（先前回 202），與 `/tenants/batch` 相同。
