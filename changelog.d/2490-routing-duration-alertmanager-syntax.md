---
section: Changed
topic: alertmanager-routing
issues: [2490]
created: 2026-10-03T16:05:40+00:00
---
- **路由時長改以 Alertmanager 的寫法為準（alertmanager-routing / exporter；[#2490](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2490)）**：⚠️ 行為變更。`group_wait`／`group_interval`／`repeat_interval` 只收整數加單位、由大到小、每個單位最多一次（`y` `w` `d` `h` `m` `s` `ms`）或單獨 `0`，規則只寫在 schema 的 `definitions.duration`。`1h30m` 不再被換成平台預設值；`1.5h`、`30m1h`、`1ns` 不再被接受（先前產生器把 `1.5h` 原樣交給 Alertmanager、exporter 照收）。無效時長在 `generate-routes --validate` 與 `validate-config` 改為阻擋（exit 1），render 照舊換成平台預設值並印 WARN；exporter 忽略該值。`500ms` 現在讀得到、由護欄拉到 5s。`explain-route` 新增「被產生器頂替」段落，`--json` 新增 `replaced_values`。domain policy 的時長比對在非 strict 時與 `--strict` 結論相同、改印 WARN（超界、`0s` 低於下限、讀不出的租戶值都會列出），所以 `validate-config` 的 `schema` 列可能由 PASS 變 WARN；非 strict 的結束碼不變。
