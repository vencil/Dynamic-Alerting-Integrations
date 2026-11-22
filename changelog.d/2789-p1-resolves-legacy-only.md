---
section: Changed
topic: docs
issues: [2789]
created: 2026-11-22T03:40:00+00:00
---
- **`Resolves:` trailer 只對舊編號追蹤項目必填（dev-rules §P1；[#2789](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2789)）**：修復三位數 `TRK-NNN`（含從 TD／HA／REG 換過來的）或 `Trap #N` 時仍必須寫 `Resolves:`——舊編號不是 issue 號，trailer 觸發的同步檢查是它唯一的偵測。新式 `TRK-<issue 號>` 條目可省略：編號就是追蹤 issue，`Refs: #<issue 號>` 已連到同一張票，完成與否以 issue 開關為準。寫了照樣觸發同步檢查；檢查工具與 CI 不變。
