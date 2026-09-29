---
section: Fixed
topic: confd-family
issues: [2412]
created: 2026-09-29T13:49:10+00:00
---
- **根目錄 `_routing_defaults` 不是 mapping 時不再 traceback（[#2412](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2412)）**：根目錄 `_` 檔把 `_routing_defaults` 寫成 list、字串、數字或布林時，`generate-routes`、`explain-route` 與 `validate-config` 先前以 Python traceback 結束。現在比照子目錄：印一行 `WARN: _routing_defaults in <檔名> must be a mapping, got <型別> — this level contributes nothing`，這一層不貢獻任何預設，結束碼 0，結果與 da-guard／threshold-exporter 的 Go 讀法一致。根目錄多個 `_` 檔都帶這個鍵時，名稱排序較後的檔仍整塊取代前者，即使它不是 mapping。`null` 維持原樣：不警告、不貢獻。
