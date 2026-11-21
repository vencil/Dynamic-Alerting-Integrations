---
section: Fixed
topic: dx
issues: [3980]
created: 2026-11-21T12:40:00+00:00
---
- **ADR／planning index 產生器讀 `--target` 前先檢查（[#3980](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3980)）**：兩支工具都是把表格寫進既有文件的 sentinel 區塊，所以會先讀 `--target`。`generate_adr_index` 讀之前沒有任何檢查，`--target` 是目錄時以 `unexpected internal error (IsADirectoryError)` 收場；兩支在 `--target` 不存在時都是 `FileNotFoundError` traceback。現在都是 rc 2 加一行點名 `--target` 的錯誤：目錄或特殊檔沿用寫檔失敗的那一行，不存在則說明 index 要寫進一份已存在的文件。
