---
section: Fixed
topic: tools
issues: [3415]
created: 2026-10-30T16:49:29+00:00
---
- **七個輸入路徑參數改走共用路徑判定，不再印 traceback 或把「不存在」說成「空」（tools；[#3415](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3415)）**：`alert-correlate --input`、`validate --mapping`、`cutover --readiness-json`、`scaffold --from-onboard`、`runtime-audit --runtime-json` 給的是不存在、不是一般檔案或無法存取的路徑時，改成點名參數與路徑的一行錯誤；`rule-pack-split --rule-packs-dir` 不存在或列不出內容時，不再先建輸出目錄、也不再說「找不到規則包檔案」。`scaffold --from-onboard` 給的檔案不是 JSON 時改為具名錯誤；`runtime-audit` 的錯誤訊息不再截斷；`generator-service --git` 執行不了時，錯誤訊息會點名 `--git`。結束碼都維持 2。
