---
section: Fixed
topic: portal
issues: [3826]
created: 2026-11-14T12:08:30+00:00
---
- **在 tenant-manager 編輯既有的 custom alert，不動 severity 也不再被改回 warning（portal；[#3826](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3826)）**：既有告警的 severity 存在 `threshold` 裡（`70:critical`），RecipeBuilder 開啟時卻把下拉選單停在預設值，直接按儲存就寫回 `70:warning`。現在開啟時用 exporter 的規則（從最後一個 `:` 切）把閾值與 severity 拆到各自的欄位，沒動欄位就送出時 severity 與閾值維持原樣。⚠️ 升級前曾用表單編輯過 critical 告警的租戶，請檢查那些告警現在的 severity：被改成 warning 的不會自動復原。
