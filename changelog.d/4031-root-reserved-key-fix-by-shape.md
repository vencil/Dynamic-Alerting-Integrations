---
section: Fixed
topic: tools
issues: [4031]
created: 2026-11-19T02:31:00+00:00
---
- **`root_defaults_reserved_key` 的修法依鍵而定（tools；[#4031](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4031)）**：先前一律叫你把鍵寫進各租戶自己的條目。但 `_routingProfile`（帶 `_routing` 前綴卻沒有任何讀取端讀）和根目錄 `state_filters:` 沒宣告其 filter 的 `_state_<f>`，寫進租戶也一樣沒人讀。現在前者叫你刪掉，後者叫你宣告 filter 或刪掉。`_routingProfile` 也不再被 validate-config 的路由規則另外報一次。
