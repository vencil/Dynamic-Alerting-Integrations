---
section: Added
topic: da-tools
issues: [3042]
created: 2026-10-16T17:00:00+00:00
---
- **`validate-config` 新增 `custom_alert_sources`：自訂告警編譯器與 exporter 對不上的地方，只報不擋（[#3042](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3042)）**：把編譯器實際要產生規則的 recipe，和 exporter 送出門檻的 recipe 按規則實際 join 的（`recipe_id`、severity）與租戶比對。編譯了卻沒送的，規則永遠不會觸發（例如寫在 `_defaults.yaml` 頂層、由編譯器繼承的 recipe）；送了而整份規則包裡沒有同一個（`recipe_id`、severity）規則的，永遠不會告警（例如根 `_defaults.yaml` 的 `tenants:`、profile）；同一租戶多條同形狀、同 severity 的 recipe 只編一條，另列一行點名。exporter 視為關掉的（門檻 `disable`／`disabled`／`off`／`false`、SLO objective `disable`）不列入。每個問題一行 WARN，結束碼不變；只在裝有編譯器的地方（repo 內）出現。下一階段編譯器會改以 exporter 送出的為準，屆時前者不再編譯、後者開始觸發。
