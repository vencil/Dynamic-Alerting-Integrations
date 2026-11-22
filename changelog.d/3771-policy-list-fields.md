---
section: Changed
topic: tools
issues: [3771]
created: 2026-11-13T12:10:00+00:00
---
- **⚠️ breaking：policy DSL 對列表欄位逐元素比（tools；[#3771](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3771)）**：列表欄位（`_expected_exporters`、`_metadata.tags`、`_metadata.groups`）的主規則（違規判定）的 `equals`／`not_equals`／`matches`／`one_of`／`contains` 一律要列表的**每個**元素都成立（列表不能夾帶規則以外的值，空列表只滿足 `not_equals`）；`when` 條件裡 `equals`／`matches`／`one_of`／`contains` 則是任一元素成立即成立（多 DB 租戶不會逃過規則）；一元素列表與它代表的純量同讀。⚠️ 以 `_metadata.db_type` 為 `target` 或 `when.target` 的規則要改成 `_expected_exporters`（[#4097](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4097)）；沒改的規則會被拒收（訊息指名新寫法），不會無聲地不再套用。**影響**：`_metadata.tags`／`_metadata.groups` 過去被當成整串文字（`['a', 'b']`）比，對它們寫值運算子的政策結果會改變；主規則用 `equals`／`contains` 問「有沒有這個 tag」的寫法，現在要求每個 tag 都符合；單搬進 `when` 或改用 `one_of` 都不等價，等價寫法是 `when: not_equals prod` 加主規則 `equals prod`（見 cli-reference §evaluate-policy），「某個 tag 含有某段字」目前沒有對應寫法。升級前請重跑 `evaluate-policy` 比對。
