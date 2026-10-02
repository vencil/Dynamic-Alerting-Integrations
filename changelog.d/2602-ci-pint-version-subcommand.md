---
section: Fixed
topic: ci
issues: [2602]
created: 2026-10-01T22:10:46+00:00
---
- **CI 的 pint 安裝步驟不再每次留下誤導的 error 註記（ci；[#2602](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2602)）**：`lint-rule-packs` job 安裝 pint 後原本執行 `pint --version`，但 pint 0.86.0 只認 `pint version` 子命令，`--version` 會以 exit 1 失敗；因該步驟是 `continue-on-error`，失敗被吞掉，只在每次 run 留下一個看似安裝出錯的 `##[error]`。改用 `pint version` 後步驟正常結束，pint lint 本身的行為不變。
