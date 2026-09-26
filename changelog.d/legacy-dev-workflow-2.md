---
section: Fixed
topic: dev-workflow
issues: [1254, 1264, 1374]
created: 2026-09-26T17:00:00+00:00
---
- **Windows host 與 dev container 開發環境可靠（internal、dx）**：修正 zh-TW Windows host 的編碼問題（`make` 匯出 `PYTHONUTF8=1`、mkdocs anchor ledger 不再被亂碼毀掉）、session guard 在 Windows 靜默失效、`recover_index.sh` 的診斷指令誤清暫存區；dev container 依賴改為 system-wide 安裝並補齊與 CI 相同的套件集（新增一致性 gate），`make dc-go-test` 支援 `MOD=`／`PKG=`，`make test` 預設平行。
