---
section: Fixed
topic: da-tools
issues: [3424]
created: 2026-10-30T12:40:14+00:00
---
- **`validate_all.py` 以相對路徑設定的 `DA_READ_BINARY`／`DA_CRDECODE_BINARY` 也找得到（[#3424](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3424)）**：它在切到 repo 根目錄之前只把 `DA_GUARD_BINARY` 轉成絕對路徑，另外兩支讀者 binary 以相對路徑設定時，切換後就找不到。現在三支讀者 binary（da-guard、da-read、da-crdecode）的環境變數在切換前一併以啟動時的目錄轉成絕對路徑；未設或空白不變。
