---
section: Added
topic: tenant-api
issues: [1594]
created: 2026-10-21T15:00:01+00:00
---
- **⚠️ BREAKING：`helm/tenant-api` 加上 `values.schema.json`，拼錯的 key 在 render 時就擋下（helm；[#1594](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1594)）**：頂層 key 與整個 `writeBack` 區塊不接受未知 key（例如 `writeBack.mdoe`），`writeBack.mode` 只接受 `pr-github`／`pr-gitlab`／`direct`（留空仍由既有的 render 守衛說明為何必填）。升級時若 values 檔或 `--reuse-values` 帶著已移除的 key（例如 `policy.unavailableOpen`），`helm upgrade` 會失敗，刪掉該 key 即可。
