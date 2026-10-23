---
section: Changed
topic: tools
issues: [3271]
created: 2026-10-22T14:40:53+00:00
---
- **⚠️ breaking：`threshold-govern --apply` 依 tenant-api 錯誤契約的 remedy 判讀被拒的 PUT（tools；[#3271](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3271)）**：不再寫死 `PENDING_PR_EXISTS` 與舊 `error: pending_pr_exists` 字串，改查 `tenant-api-errors.json`（tenant-api 的 `make error-contract` 產生，da-tools image 內附）：`existing` → `already_pending`；`refresh_retry`（`CONFLICT`）→ 重新 GET／合併／驗證後重送一次，先前直接記為 error；其他 remedy、沒有 code 或契約沒列的碼 → error。只帶舊 `error` 字串、沒有 `code` 的 409 不再當成 dedup。讀不到契約檔時 `--apply` 結束碼 2、不送任何寫入。
