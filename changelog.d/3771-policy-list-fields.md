---
section: Changed
topic: tools
issues: [3771]
created: 2026-11-13T12:10:00+00:00
---
- **⚠️ breaking：policy DSL 對列表欄位逐元素比（tools；[#3771](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3771)）**：`_metadata.db_type` 改以列表送出後，主規則（違規判定）的 `equals`／`not_equals`／`matches`／`one_of`／`contains` 一律要列表的**每個**元素都成立（列表不能夾帶規則以外的值，空列表只滿足 `not_equals`）；`when` 條件裡 `equals`／`matches`／`one_of`／`contains` 則是任一元素成立即成立（多 DB 租戶不會逃過規則）；既有單值 `db_type` 規則讀法不變。**影響**：`_metadata.tags`／`_metadata.groups` 過去被當成整串文字（`['a', 'b']`）比，對它們寫值運算子的政策結果會改變；主規則用 `equals`／`contains` 問「有沒有這個 tag」的寫法，現在要求每個 tag 都符合，請改成 `when` 條件或 `one_of`。升級前請重跑 `evaluate-policy` 比對。
