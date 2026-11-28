---
section: Fixed
topic: da-tools
issues: [1818]
created: 2026-09-28T01:20:00+00:00
---
- **`validate --mapping` 的批次比對對得上 rate 類規則（da-tools；[#1818](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1818)）**：`prefix-mapping.yaml` 的 `old_query`／`new_query` 兩欄有值時，`validate --mapping` 用它們比對（舊值保留 `rate()`，與新值同一種聚合）；沒有這兩欄的舊版檔會警告並退回原本的比對。
