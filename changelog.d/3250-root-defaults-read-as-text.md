---
section: Changed
topic: exporter
issues: [3250]
created: 2026-10-27T15:40:00+00:00
---
- **根目錄 `defaults:` 與租戶值同一種讀法（[#3250](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3250)）**：每個值以原文經 `parseThresholdNumber` 讀，不再走 yaml.v3 的 float64 解碼。讀不了的純量只拒收那個 key（不送預設列），不再丟掉整份檔；只有 mapping、list 仍讓檔被丟。da-guard finding 與 served-values `keys[].reason` 由 `root_default_non_finite` 改名為 `root_default_refused`，`refused` 帶 `non_finite`／`unparsed`；served-values 新增頂層 `root_defaults_refused`。⚠️ 行為變更：`"70"`、`0x1p4`、`abc` 等由整份丟掉（exit 3）改為 0 或 1；`0x10`、`0o17`、`0b101`、`5_` 由照送改為拒收（exit 0 → 1）；`017` 由 15 改為 17；根目錄 `_x: .nan` 的 served-values 由 exit 2 改為 0。
