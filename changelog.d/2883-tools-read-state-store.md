---
section: Changed
topic: tenant-api
issues: [2883]
created: 2026-10-11T11:31:33+00:00
---
- **`offboard` 與 `generate_tenant_metadata` 改讀 tenant-api 的 state store（tools；[#2883](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2883)）**：群組、views、federation 子集自 #2856 起存在 tenant-api 的 state store，兩支工具卻仍讀 conf.d 的 `_groups.yaml`。兩者新增 `--state PATH`（state 目錄，或 `kubectl get configmap tenant-api-state -o yaml|json` 存下的檔）。`da-tools offboard` 列出群組成員、views 的 `filters`、federation 子集對該租戶的引用與要呼叫的 tenant-api API（工具不寫 state store），AccountID 配發只列為資訊；**沒給 `--state` 時 `--execute` 回 2，需另加 `--skip-state-check`**。`generate_tenant_metadata` 的 `custom_groups` 取自 `--state`，沒給時為空。conf.d 裡殘留的 `_groups.yaml` 等舊檔只印警告、不再讀。
