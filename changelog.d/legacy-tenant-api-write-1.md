---
section: Security
topic: tenant-api-write
issues: [1339, 1529, 1597, 1681, 1718, 1722]
created: 2026-09-26T17:00:00+00:00
---
- **⚠️ tenant-api 寫入平面的授權與隔離補洞**：`PUT /api/v1/tenants/{id}` 的 body 只能宣告該 id 的區塊，新夾帶其他租戶的區塊（含多文件 YAML）會被拒，避免凍結全平台設定重載或生出幽靈租戶；`_` 開頭的 id 一律拒絕，保留控制檔（如 `_domain_policy.yaml`）不能再被當成租戶覆寫。PR 模式在 checkout 新 base 後重新驗證，`DELETE /groups/{id}` 改依磁碟上的成員清單授權，送出空白 `X-DA-Base-Hash` 回 400 而非略過前置條件。`_rbac.yaml` 的 `environments:`／`domains:` 過去對寫入零約束，現在由 `--rbac-metadata-write-scope-enforce` 控制（預設 shadow，只記 `tenant_api_scope_would_deny_total{axis="metadata_write"}`；⚠️ enforce 下新建租戶會被 403）。新增 parse 前上限：單份租戶文件 `TA_MAX_TENANT_DOC_BYTES`（64 KiB）、批次 body `TA_MAX_BATCH_BODY_BYTES`（256 KiB，超量 413）、patch 最多 1000 個 key；⚠️ 合併路徑量的是合併後的整份檔，共用大檔的租戶可能因一行 patch 被 400，避免單一租戶的請求卡住其他租戶的寫入（[#1681](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1681)、[#1597](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1597)、[#1722](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1722)）。
