---
section: Fixed
topic: docs
issues: [2121]
created: 2026-09-28T04:37:29+00:00
---
- **da-tools README 的 rollback 驗證指令改用正確的 jq 路徑（[#2121](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2121)）**：`tenant-verify --all --json` 的輸出是 `{"tenants": [...]}`，原本的 jq 取到 `null`，照做的 rollback 驗證永遠回 exit 2。
