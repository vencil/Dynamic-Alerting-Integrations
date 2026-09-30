---
section: Fixed
topic: confd-family
issues: [2421]
created: 2026-09-30T12:12:10+00:00
---
- **`diagnose --show-inheritance` 的 `resolved` 對「沒有值」的閾值改成與 `/metrics` 一致（[#2421](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2421)）**：profile 層或租戶層把閾值寫成 `null`、`''`，或其他既非數字、也非 `disable` 的內容（例如 `abc`、`true`）時，exporter 會記一筆 `unknown value` 並退回 defaults，但 `diagnose` 過去把這個原始值當成最終結果顯示。現在這兩層都會略過這類值、改顯示下層實際生效的值；租戶層這樣寫仍會擋住 profile 的值，與 `/metrics` 相同。各層 `chain` 仍照檔案原樣列出。
