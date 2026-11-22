---
section: Changed
topic: tools
issues: [4136]
created: 2026-11-22T06:18:46+00:00
---
- **`lint` 判定租戶規則檔只讀、只寫自己的租戶（tools；[#4136](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4136)）**：⚠️ breaking。路徑給的規則檔要放在 `<掃描目錄>/<租戶>/`（例如 `rule-packs/custom/db-a/`），目錄名就是所屬租戶；直接放在掃描目錄底下是 ERROR。新政策鍵 `isolated_label: tenant`：每個 selector 都要以等號寫 `tenant="<租戶>"`（KSM 寫 `namespace="<租戶>", tenant=""`），`labels:` 的 tenant 只能是該租戶或 `{{ $labels.tenant }}`，`label_replace`／`label_join`／`count_values`／`group_left(tenant)` 不得寫入 tenant。過去 `x{tenant=~".*"}`、沒寫 matcher、讀別的租戶都是 rc 0。`--rule-pack-dir` 的平台 pack 不判定。這是防失誤，不是安全邊界。
