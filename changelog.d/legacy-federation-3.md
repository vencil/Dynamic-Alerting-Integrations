---
section: Fixed
topic: federation
issues: [1673, 1681, 1684, 1698]
created: 2026-09-26T17:00:00+00:00
---
- **tenant-api `_federation/` subset 檔的讀寫修正（tenant-api）**：subset 存成 `<id>.yml` 時，`GET /api/v1/tenants/{id}/federation` 回空子集、PUT 另建 `<id>.yaml`；現在讀寫都解析實際存在的那個檔。⚠️ 行為變更：同一租戶 `.yaml` 與 `.yml` 並存時 GET 與 PUT 皆回 `409`，須在 git 刪掉其中一個。讀取端另補上與寫入端相同的 tenant id 述詞，作為路徑穿越的縱深防禦（既有 handler 驗證本已擋下）。見 [#1698](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1698)、[#1684](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1684)。
