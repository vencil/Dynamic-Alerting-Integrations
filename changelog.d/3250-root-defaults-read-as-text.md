---
section: Changed
topic: exporter
issues: [3250, 3361]
created: 2026-10-27T15:40:00+00:00
---
- **閾值在每一層同一種讀法（[#3250](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3250)、[#3361](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3361)）**：根目錄與子目錄 `_defaults.yaml`、租戶、平台檔、profile 的閾值一律以原文經 `parseThresholdNumber` 讀，`/effective`、`merged_hash`、`redundant_override` 與 exporter 一致；YAML 型別會讀錯的值（`017`、`0x1F`、`.nan`、`true`）在 `/effective` 顯示原文。根目錄 `defaults:` 讀不了的純量只拒收那個 key，mapping、list 仍丟檔。`root_default_non_finite` 改名 `root_default_refused`；新增 finding `root_defaults_reserved_key`（error）、not_served `root_default_refused`／`root_defaults_reserved`、effective 頂層 `root_defaults_refused`／`root_defaults_reserved`。⚠️ 行為變更——`/metrics` 送的值：根目錄與子目錄的 `017`、`0_17` 由 15 改送 17，`0x1F`、`0o17` 改為拒收（子目錄改送上一層的值），根目錄 `"70"`、`0x1p4` 由整檔被丟改照送。結束碼：`"70"`、`0x1p4` 3 → 0；`abc`、`NaN`、保留鍵非數字值 3 → 1；`0x10`、`0o17`、`5_`、子目錄 `0x1F` 0 → 1；根目錄 `_x: .nan` 的 served-values 2 → 0。這類值的樹 `merged_hash` 變一次。
