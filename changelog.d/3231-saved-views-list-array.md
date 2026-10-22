---
section: Fixed
topic: portal
issues: [3231]
created: 2026-10-22T14:40:23+00:00
---
- **Tenant Manager 的已存視圖照 API 的陣列讀，套用與刪除不再打錯視圖（portal；[#3231](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3231)）**：`GET /api/v1/views` 回的是 `ViewResponse[]`，每筆自帶 `id`；SavedViewsPanel 卻把它當成以 id 為 key 的物件讀，於是選單的值變成陣列索引 `0`、`1`，套用拿不到篩選條件、刪除打到 `/api/v1/views/0`。現在以每筆的 `id` 為值；200 卻不是陣列的回應會跳錯誤提示，不再靜默顯示成「尚無已存視圖」。測試的回應 fixture 改經 `apiFixture('ViewResponse[]', …)` 對照 swagger 驗證，形狀不符直接紅。
