---
section: Changed
topic: da-tools
issues: [3599]
created: 2026-11-13T00:29:14+00:00
---
- **stderr 寫不進去時工具不再中止（da-tools；[#3599](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3599)）**：stderr 的讀端先關掉或寫入 `/dev/full` 時，那段文字丟掉、工具照常跑完並交付 stdout。結束碼：工具回 `0` 改成 `2`（有訊息沒送到）；非零碼原樣保留；`patch-config` 的寫入結果連 `0` 也保留。⚠️ 結束碼變更：之前第一次寫 stderr 就走 crash 路徑，一律 crash 碼（`2`；session-guards 為 `1`）、stdout 不交付。
