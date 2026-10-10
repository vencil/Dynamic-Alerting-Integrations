---
section: Fixed
topic: tenant-api
issues: [2830]
created: 2026-10-10T14:55:20+00:00
---
- **PR 模式的 batch 在最新 base 上再判一次 metadata 寫入範圍（tenant-api；[#2830](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2830)）**：寫到 `_profile` 或字串 `_metadata` 的 batch op，原本只以 pod 本機樹判定寫入後的 metadata；PR 模式下本機樹只在 pod 啟動時同步，之後在 origin 合併的變更都看不到。開了 `--rbac-metadata-write-scope-enforce` 時，PR 模式現在另以 PR 分支所依據的最新 origin base 判定（同一租戶本批較早的 op 已疊上）；會把租戶移出呼叫者範圍時，整批回 403 `FORBIDDEN`、不開 PR、不留分支，回應帶 `tenant_id` 與 `operation`。shadow 模式行為不變，也不額外計入 `tenant_api_scope_would_deny_total{axis="metadata_write"}`。
