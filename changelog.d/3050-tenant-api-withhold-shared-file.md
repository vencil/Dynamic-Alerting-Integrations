---
section: Security
topic: tenant-api
issues: [3050, 3064, 3039, 2486]
created: 2026-10-16T04:38:38+00:00
---
- **共用檔的租戶不再回原文（tenant-api；[#3050](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3050)、[#3064](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3064)）**：conf.d 一份檔可宣告多個租戶，原本 `GET /tenants/{id}` 的 `raw_yaml` 會連同檔內其他租戶的設定整份回傳。現在 `GET` 帶 `managed_by`：檔案只屬於該租戶（API 形狀）時是 `api`，照舊回 `raw_yaml`、`source_hash`、`custom_alerts`；其他形狀是 `git`，加 `git_managed_reason`，只回 effective 結果（`resolved_thresholds`、驗證訊息或 `config_error`），對任何呼叫者都不回原文、hash 與 `custom_alerts`。`/effective` 的 `source_hash` 同樣只給 API 管理的租戶，git 管理的租戶對任何呼叫者都為空並帶 `source_hash_withheld: true`；解碼錯誤的檔內有讀不到的租戶時改回固定文字。409 只在現在的檔仍是 API 形狀時附 `current_source_hash`。
