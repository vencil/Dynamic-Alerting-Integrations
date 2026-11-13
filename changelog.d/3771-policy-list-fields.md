---
section: Changed
topic: tools
issues: [3771]
created: 2026-11-13T12:10:00+00:00
---
- **⚠️ breaking：policy DSL 對列表欄位逐元素比（tools；[#3771](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3771)）**：`_metadata.db_type` 改以列表送出後，`one_of`／`matches`／`not_equals` 要列表的**每個**元素都成立（列表不能夾帶 allowlist 以外的值），`equals`／`contains` 有一個元素成立即成立，空列表只滿足 `not_equals`；`when` 條件裡的 `one_of`／`matches` 則是任一元素成立即成立（多 DB 租戶不會逃過規則）；既有單值 `db_type` 規則讀法不變。**影響**：`_metadata.tags`／`_metadata.groups` 過去被當成整串文字（`['a', 'b']`）比，對它們寫 `equals`／`not_equals`／`one_of`／`matches` 的政策結果會改變，升級前請重跑 `evaluate-policy` 比對。
