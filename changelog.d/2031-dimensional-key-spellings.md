---
section: Fixed
topic: exporter
issues: [2031]
created: 2026-10-09T05:29:45+00:00
---
- **維度 key 換個 label 順序、引號或空白就讓 `/metrics` 整份回 500 的問題修正（threshold-exporter / da-guard / tenant-api；[#2031](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2031)）**：`x{a="1", b="2"}` 與 `x{b='2',a="1"}` 現在是同一個閾值：每一層在合併前改成 canonical 拼法，跨層（子目錄 `_defaults.yaml`、平台檔 `tenants:`、profile 與租戶檔）時較高層勝出、只送一條 series；parser 讀不無損的 key（如 `x{q=~"A,B"}`）保留原文不合併。`/effective`、`da-guard effective`／`served-values` 與 da-guard finding 照舊以勝出層的原文拼法顯示 key；`merged_hash` 也以這份原文拼法計算，只有跨層把同一閾值寫成不同拼法的租戶會變。⚠️ 行為變更：同一個 mapping 把一個閾值寫成兩種拼法（含 #1231 新舊兩名同層並存）時只送一種；該 mapping 是勝出層時，另一種在 `not_served` 標 `spelling_duplicate`、計入 `da_config_values_not_served`、`validate-config` 的 `values_not_served` 點名，da-guard 以 `value_not_served` 擋下（exit 0→1）。
