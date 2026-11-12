---
section: Fixed
topic: da-tools
issues: [3611]
created: 2026-11-12T16:30:00+00:00
---
- **amtool 讀不到設定檔不再當成設定被拒（[#3611](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3611)）**：`generate-routes --validate`、`validate-config` 等經 `amtool check-config` 驗證的工具，amtool 回 `FAILED: open <檔案>: …`（讀不到檔案）時改判「無法驗證」（結束碼 `2`），不再判成設定被拒（`1`）。判讀 amtool 輸出的簽章改為單一函式，並以釘版 amtool 0.34.2 的實錄輸出做 fixture 測試，版本升級時會要求重錄。
