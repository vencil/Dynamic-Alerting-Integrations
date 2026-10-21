---
section: Fixed
topic: portal
issues: [3202, 3123]
created: 2026-10-21T20:26:52+00:00
---
- **portal 依錯誤碼的處理方式顯示訊息，不再把每個 409 都當成「被別人更新，請重整重試」（tenant-manager；[#3202](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3202)）**：自訂告警儲存遇到 `TENANT_DECLARED_ELSEWHERE`／`TENANT_TREE_UNREADABLE`／`TENANT_FILE_AMBIGUOUS` 時改為說明須在 git 修正、不提供重試，編輯內容保留；只有 `CONFLICT` 且帶 `current_source_hash` 才顯示 base_hash 衝突框；未知或缺少 code 不套用任何處理方式的文案（不會被猜成重整重試）：有伺服器訊息就顯示它，沒有才顯示通用訊息。建立群組遇到 409 `GROUP_ID_UNAVAILABLE`（[#3123](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3123)）改為提示換一個 ID，不再誤導重整。處理方式取自產生的契約 `tenant-api-errors.json`，每種處理方式一份文案。
