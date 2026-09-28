---
section: Fixed
topic: release-tooling
issues: [1614]
created: 2026-09-28T08:18:17+00:00
---
- **`bump_docs --platform` 與版號閘門看同一批 JSX front matter（[#1614](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1614)）**：寫入端原本只改 `tools/portal/src/interactive/tools`，檢查端卻掃整個 `tools/portal/src`，`getting-started/wizard.jsx` 因此會被檢查、卻不會被 bump——下一次升平台版號時以 `--fix` 修不掉的 error 擋住 CI。現在兩端共用同一個根目錄常數，並有測試比對兩端的檔案集合必須相等。
