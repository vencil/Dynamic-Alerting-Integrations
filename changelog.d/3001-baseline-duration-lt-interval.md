---
section: Fixed
topic: da-tools
issues: [3001]
created: 2026-10-14T23:36:55+00:00
---
- **`baseline --duration` 小於 `--interval` 改回結束碼 `2`（[#3001](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3001)）**：兩個值各自合法，但組合起來一次都採樣不到；先前照樣產出空的 CSV 與 summary（每列 `samples=0`），結束碼 `0`。現在在解析參數時就拒絕，`--dry-run` 也一樣。兩者相等時採樣一次，照常執行。
