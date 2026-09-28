---
section: Changed
topic: confd-family
issues: [2326]
created: 2026-09-28T19:30:00+00:00
---
- **路由面改為階層：子目錄裡的租戶也產生 route（[#2326](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2326)）**：`generate-routes`、`explain-route`、`check_routing_profiles`、`validate-config` 的 schema / routes / policy 列與 da-guard 改讀整棵 conf.d。先前子目錄的租戶沒有 route、結束碼 0，告警落到 catch-all。`_routing_defaults` 沿租戶目錄鏈逐層淺合併（各層 `_defaults.yaml` 頂層，深層勝）；子目錄的 `_routing_profiles.yaml` / `_domain_policy.yaml` 只作用於該子樹，各層 policy 疊加判定。⚠️ 新的阻擋條件（generator 所有模式結束碼 2，da-guard 報 error）：子目錄有 `_routing_enforced`、子目錄層 `_routing_defaults` 的 `receiver` / `overrides` 為 null、profile 名稱定義在兩個檔（含根目錄 `.yaml` 與 `.yml`，先前後者靜默覆蓋）、同一租戶 id 在兩個檔宣告。語意見 ADR-017「Amendment 2026-09-28」。
