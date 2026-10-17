---
section: Fixed
topic: da-tools
issues: [3105]
created: 2026-10-17T00:22:11+00:00
---
- **輸出檔只對一般檔案設 `0600`，`-o /dev/null` 不再改壞裝置或回 rc 2（[#3105](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3105)）**：⚠️ 結束碼變更：經 `_lib_io` 的 secure writer 寫輸出檔的工具（`-o`、`--markdown-output`、`--out`、`--output-dir` 等），先前寫完一律 `chmod 0600`：以 root 執行 `-o /dev/null` 會把 `/dev/null` 改成 `crw-------`（整台主機的非 root 行程從此寫不進去），非 root 則 chmod 失敗、結束碼 2。現在只對一般檔案設 `0600`，裝置與 FIFO 不動，非 root 寫入自己不擁有的裝置或 FIFO（例如 `-o /dev/null`）由 2 改為 0。別人擁有的一般檔案照舊回報寫入失敗。
