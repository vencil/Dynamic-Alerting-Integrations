---
section: Fixed
topic: portal
issues: [3959]
created: 2026-11-15T09:12:27+00:00
---
- **Alert Preview、YAML Validator 與 Routing Trace 對 exporter 拒收的兩種形狀有反應了（portal；[#3959](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3959)）**：有第二份文件（`---`）的 YAML，exporter 會整個檔拒收，三頁原本照第一份照常模擬；現在改為顯示錯誤、不模擬（只有頁面讀閾值用的 `parseTenantYaml` 這樣處理，`parseYaml` 的其他使用端不變）。閾值寫成 map 或 list，或排程的 `default` 是 exporter 不收的值（例如 `{default: 0x1F}`），exporter 判為讀不懂並改用平台預設值，頁面原本沒有任何一列；現在和讀不懂的純量一樣顯示一列說明。帶可讀 `default` 的排程維持不模擬。
