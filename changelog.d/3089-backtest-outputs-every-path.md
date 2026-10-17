---
section: Fixed
topic: da-tools
issues: [3089]
created: 2026-10-16T17:38:58+00:00
---
- **`backtest` 每一條結束路徑都寫出 `-o`／`--markdown-output`，並在開工前先檢查輸出路徑（[#3089](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3089)）**：⚠️ 結束碼變更：`-o` 或 `--markdown-output` 寫不進去時（父目錄不存在、路徑是目錄等），`--skip-if-unavailable` 的 skip 與沒有門檻變更兩種情況由 0 改為 2；檢查在讀 git、conf.d 與查 Prometheus 之前，一行 `ERROR:` 指名旗標，`--json` 輸出 `caller_error` envelope（`reason: output_unwritable`），不再先印一份正常報告。路徑寫得進去時結束碼都不變。`-o` 寫入該次 `--json` 的同一份 JSON；沒有回測時 markdown 寫明「Backtest not run」與原因。CI 的 backtest sticky 留言因此每次都會更新，沒有回測時會寫明未回測，不再停在上一次的結果。
