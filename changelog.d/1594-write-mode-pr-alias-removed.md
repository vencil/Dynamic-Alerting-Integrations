---
section: Removed
topic: tenant-api
issues: [1594]
created: 2026-10-21T15:00:02+00:00
---
- **⚠️ BREAKING：`--write-mode`／`TA_WRITE_MODE` 不再接受 `pr`（tenant-api；[#1594](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1594)）**：合法值只剩 `direct`／`pr-github`／`pr-gitlab`。`pr` 原是 `pr-github` 的別名，但 Helm chart（唯一部署來源）從未接線它，等於 binary 與 chart 各認一套值；現在 `TA_WRITE_MODE=pr` 會在啟動時被拒，改寫 `pr-github` 即可，行為相同。chart 的 `values.schema.json` 與 binary 的合法值集合由 Go 測試釘成完全相等。
