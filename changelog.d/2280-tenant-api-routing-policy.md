---
section: Fixed
topic: tenant-api
issues: [2280]
created: 2026-09-28T09:16:00+00:00
---
- **tenant-api 的 config watcher：檔案刪掉後原樣放回會重新載入（[#2280](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2280)）**：audit 的 `config_anchor` 在檔案被刪時改報 `unanchored`。
