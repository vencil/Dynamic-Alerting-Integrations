---
section: Added
topic: tools
issues: [4368]
created: 2026-11-28T16:30:00+00:00
---
- **`_routing_enforced.match` 寫了 `da_migrated` 時 `generate-routes` 印 WARN（tools；[#4368](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4368)）**：生效中的 enforced 區塊，`match` 任一元素有 matcher 寫在 `da_migrated` 上（任何運算子與值），就給 finding kind `routing_enforced_match_migrated`，Field 為 `_routing_enforced.match[i]`。這個 label 只有 migrate-rule 的告警帶，影子期的 migrated 告警在 enforced 路由之前就被影子路由收走，所以這種 matcher 讓 NOC 路由只收、或完全不收已切換租戶的 migrated 告警。路由照寫的產出，任何模式都不擋（`--validate --strict` 與 tenant-api 寫入照常通過）。修法：從 `match` 拿掉它。
