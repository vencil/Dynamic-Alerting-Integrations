---
section: Fixed
topic: docs
issues: [2184]
created: 2026-09-27T16:23:39+00:00
---
- **遷移 playbook 的 Failure Mode Catalog 刪掉「AM shadow matcher 規則寫錯」一列（docs；[#2184](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2184)）**：這一列給的兩個原因與舉例都不成立。Alertmanager 的 `=~` 是全錨定比對，`migration_status=~"shadow"` 與 `="shadow"` 路由相同，不會 fall through；matcher 解析錯誤會讓 config 驗證失敗，不是靜默放行；而 `matchers` 為空的 route 會接住所有告警，後果是 production 告警一起被送進 `null`，與這一列寫的「漏到 production」方向相反。照這一列排查只會把 `=~` 改成 `=`（沒有作用），所以整列刪除。「shadow 漏到 production」仍由同表的 route 順序那一列涵蓋。中英兩版同步。
