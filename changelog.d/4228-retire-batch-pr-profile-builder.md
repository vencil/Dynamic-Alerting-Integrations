---
section: Removed
topic: migration-toolkit
issues: [4228, 4211]
created: 2026-11-22T12:42:42+00:00
---
- **⚠️ breaking：移除 `da-tools batch-pr`、`da-batchpr` binary 與 Profile Builder（da-tools、exporter、release；[#4228](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4228)）**：`da-tools batch-pr` 現在是未知命令（rc 2）；da-tools 映像不再帶 `/usr/local/bin/da-batchpr`，`tools/v*` release 不再附 `da-batchpr-*` 的 6 個 archive（`SHA256SUMS` 由 18 筆變 12 筆），`$DA_BATCHPR_BINARY` 不再有人讀。Profile Builder library 一併刪除。兩者從未接通：`apply` 必填的 `--plan`／`--emit-dir` 沒有任何出貨的工具產生，`refresh`／`refresh-source` 只作用在 `apply` 開出的分支上。沒有過渡期（決策見 [#4211](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4211)）。導入客戶規則請用 `da-tools migrate`；產出 `_custom_alerts` recipe 的批次導入見 [#4226](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4226)。
