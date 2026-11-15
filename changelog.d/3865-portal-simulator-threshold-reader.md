---
section: Fixed
topic: portal
issues: [3865, 3919]
created: 2026-11-14T12:08:00+00:00
---
- **portal 的模擬器與 YAML 檢查改用 exporter 的讀法讀閾值（portal；[#3865](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3865)）**：Alert Preview、Alert Simulator、YAML Validator 與範本的反例註記原本用 `parseFloat`／`Number()` 讀閾值，`"1_000"` 被當成 1、`"0x1F"` 被當成 0、`"70:high"` 被當成 70 來模擬。現在與 RecipeBuilder 共用同一個讀者：exporter 不會讀的值在 Alert Preview 顯示成一列「exporter 不會讀這個值」並說明 exporter 會改用平台預設值（這一列不模擬預設值），在 YAML Validator 是一筆 error；`<key>_critical` 照 exporter 的規則整個值當數字讀，帶 `:severity` 的寫法會被指出這個 key 不會生效；`off`、`disabled`、`false` 與 `disable` 一樣顯示為已停用；`"80:critical"` 觸發時顯示為 CRITICAL。Alert Preview 與 YAML Validator 讀的是閾值在 YAML 裡的原始文字，和 exporter 一樣，所以數字與停用字有沒有加引號結果相同（[#3919](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3919)）：沒加引號的 `0x1F`、`0o17`、`0b101` 與超過 20 位的數字 exporter 不收，這兩頁也判為讀不懂，訊息裡列的是你寫的文字；`80`、`"80"`、`1e20`、`80:critical` 照舊是數字，值寫成 `~`、`null` 或留空視同沒寫這個 key。排程寫法仍不模擬。Routing Trace、Config Diff 等其他工具的 YAML 讀法不變。
