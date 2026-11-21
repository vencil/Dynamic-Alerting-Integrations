---
section: Fixed
topic: tools
issues: [4102]
created: 2026-11-21T12:40:00+00:00
---
- **`tenant-config.schema.json` 的 `metadata` 不再有兩段 `$comment`，repo 內的 JSON 一律不准重複名（[#4102](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4102)）**：同一個 object 裡的兩段 `$comment`，常見的 JSON 讀者（Python `json`、Go `encoding/json`）只留後一段、不出聲，前一段（ADR-016 路徑推導的說明）等於不存在；嚴格的讀者則會整份拒收。兩段已併成一段。新的測試以工具讀呼叫端 JSON 的同一個判定（[#4064](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4064)）檢查每一份被追蹤的 `*.json`；JSON Lines 逐行檢查，帶註解的 JSONC 列名豁免，豁免清單只減不增。
