---
section: Fixed
topic: alertmanager-routing
issues: [2659]
created: 2026-10-03T08:30:00+00:00
---
- **tenant-api 不再把 `domain_policies: null` 當成空 policy（[#2659](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2659)）**：`_domain_policy.yaml`（或 `.yml`）的 `domain_policies:` 寫成 `null`、`~` 或留空時，da-guard 與 `generate-routes --strict` 都報不可用，tenant-api 卻讀成「沒有任何 policy」——熱重載到這樣的檔案，會把原本禁止的 receiver type 全部放行。現在 tenant-api 與 da-guard 用同一個形狀判準：這種檔案視為讀不了，熱重載時保留上一份可用的 policy 並記失敗，PR 模式以 policy 讀不了拒絕寫入（PUT 回 403）。寫成 list 或純量本來就會報錯，行為不變。
