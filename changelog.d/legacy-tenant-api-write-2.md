---
section: Fixed
topic: tenant-api-write
issues: [1097, 1385, 1680, 1718, 1911, 2070, 2074, 2078]
created: 2026-09-26T17:00:00+00:00
---
- **tenant-api 寫入不再靜默遺失或錯寫資料**：批次寫入從整檔覆寫改為部分合併，保留未 patch 的 key 與註解；群組／檢視編輯改在寫入鎖內讀磁碟現況，一次失敗的編輯不會再被下一次編輯抹掉，形狀認不出的 `_groups.yaml`／`_views.yaml` 改回錯、不再當空檔重建；批次以純量覆寫 `_metadata`／`_custom_alerts` 改回 400；檔案改為原子替換，避免讀到寫到一半的 YAML。⚠️ 租戶已由其他檔宣告時，寫入改回 409 `TENANT_DECLARED_ELSEWHERE`（直寫模式 batch 仍 200、該筆帶此 `code`），不再另建 `<id>.yaml` 造成「一個 id 兩個檔」。內容沒變的寫入不再被誤報成 409 衝突，PR 模式遇到無變更回 200 `no_changes`、不再推空 branch；⚠️ PR 模式 push 後切回 base 失敗改回 500，不再回報成功。長壽 pod 的 stale base 不再誤拒合法寫入。設定檔壞掉的租戶不再從 `GET /api/v1/tenants` 消失，改以 `{id, config_error}` 降級列出（僅 `environments`／`domains` 不設限的呼叫者看得到）（[#2078](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2078)、[#1097](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1097)、[#1680](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1680)）。
