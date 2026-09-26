---
section: Fixed
topic: threshold-governance
issues: []
created: 2026-09-26T17:00:00+00:00
---
- **`threshold-govern` 誤把 tenant-api 的 `no_changes` 回應當成錯誤（tools）**：`PUT /tenants/{id}` 在 PR 模式下新增 200 `status: "no_changes"` 後，治理迴路把它判為 error 並印出「tenant-api is not in PR write-mode」的錯誤診斷；變更已在上游 merge、pod 本地 base 尚未跟上時會整批落入此情況，可能觸發 systemic failure 讓治理 Job 失敗並告警。現新增 `no_changes` outcome：不算錯誤、不計入失敗占比，文字摘要與 JSON `summary` 同步。
