---
section: Fixed
topic: confd-family
issues: [2179]
created: 2026-09-28T00:47:23+08:00
---
- **`da-tools offboard` 的 pre-check 失敗改回 1，並點名無法解析的設定檔；`da-guard -h` 印出用法（tools／exporter；[#2179](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2179)）**：無法解析的設定檔過去只印 ⚠️、pre-check 仍判定通過；現在會被點名並讓 pre-check 失敗。pre-check 失敗時不論有沒有 `--execute` 都回 1（過去不帶 `--execute` 回 0）。`da-guard -h`／`--help` 過去什麼都不印，現在印出用法。
