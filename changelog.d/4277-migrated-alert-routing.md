---
section: Added
topic: tools
issues: [4277]
created: 2026-11-25T00:17:06+00:00
---
- **`generate-routes`：migrated 告警的影子路由與 `_routing_migrated`（tools；[#4277](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4277)）**：帶 `da_migrated="true"` 的告警（migrate-rule 寫回的 legacy 規則）由平台路由第 5 條送到不通知任何人的 `migrated-alerts-shadow`，排在 enforced NOC 路由之前，影子期同一件事不會 page 兩次。租戶新保留鍵 `_routing_migrated: active`（租戶檔或根目錄平台檔的 `tenants:`）以 `tenant!~` 把該租戶排除在外，它的 migrated 告警就與其他告警走同一條路；不寫是 `shadow`，其他值整棵樹拒收、結束碼 1。切換只改 conf.d，規則與告警的 label 不變。repo 自帶的 base 加上這條路由與 receiver。
