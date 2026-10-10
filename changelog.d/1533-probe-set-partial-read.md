---
section: Changed
topic: alertmanager-routing
issues: [1533]
created: 2026-10-10T15:20:00+08:00
---
- **平台告警 pack 只讀到一部分時，探針集也算降級（`generate-routes`；[#1533](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1533)）**：行為變更。原本 pack 裡一個 group 或 rule 格式壞掉，就默默少掉它包含的告警（實測一個 group 壞掉從 45 筆掉到 9 筆），不印 WARN、也不算降級。現在只要有格式壞掉而被跳過的元素、或內建 6 筆裡有名字讀不到，探針集就用「讀到的部分加回內建 6 筆」，stderr 印 `INCOMPLETE` 的 WARN，並適用降級時的拒絕規則（`--output-configmap`／`--apply` 遇到驗證不了的租戶觸發 inhibit 回 2）；`--validate` 結束碼不變、多一行說明。recording rule 等正常略過的規則不算。
