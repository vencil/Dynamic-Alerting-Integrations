---
section: Fixed
topic: confd-reader-consistency
issues: [2117]
created: 2026-09-27T22:45:00+08:00
---
- **`/effective`、da-guard、`describe_tenant.py` 與 exporter 的 `merged_hash` 展開 `_profile`，與 `/metrics` 一致（exporter / tenant-api / tools；[#2117](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2117)）**：過去這些讀取端不套用 profile，對 `_profile` 租戶回報 defaults chain 的值，da-guard 還會把「移除後 `/metrics` 會改變」的覆寫判為冗餘。現依序合併：租戶檔 > 根目錄平台檔 `tenants:` > profile > defaults chain。回應新增選填欄位 `profile_overlay`（`[{profile, file, keys}]`）。選用 profile 的租戶 `merged_hash` 會改變；只改 `profiles:` 也會重算相關租戶並計入 reload 歸因（過去不計）。`/simulate` 只展開 chain 根層 `_defaults.yaml` 的 `profiles:`。
