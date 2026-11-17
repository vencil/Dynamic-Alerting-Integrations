---
section: Fixed
topic: portal
issues: [3974]
created: 2026-11-15T14:39:45+00:00
---
- **排程寫法形狀錯到 exporter 解不開時，Alert Preview、YAML Validator 與 Routing Trace 會顯示錯誤（portal；[#3974](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3974)）**：帶 `default` 的 mapping 會被 exporter 解成固定欄位，任一欄位的型別不符（例如 `{default: [1]}`、`{default: 80, overrides: 5}`、`overrides` 裡的 `window`／`value` 寫成 map 或 list），exporter 就丟掉整個租戶檔，而且只有 `da-guard served-values` 的 `parse_failed` 看得到，parse-tree 不報；頁面原本把它當成合法排程、沒有任何提示。現在 `parseTenantYaml` 照 exporter 的欄位型別檢查（`_` 開頭的 key 也算），不符就回一筆指名 key 與欄位的錯誤、不模擬。多出來的欄位與 null 不受影響，合法排程照舊。
