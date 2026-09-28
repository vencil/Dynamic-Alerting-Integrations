---
section: Fixed
topic: alertmanager-routing
issues: [2293]
created: 2026-09-28T12:49:11+00:00
---
- **`explain-route --trace` 改走產出的整棵路由樹（[#2293](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2293)）**：先前只比對 tenant 主路由的子路由，看不到 Watchdog／custom／synthetic-probe／sentinel 四條頂層路由與 enforced 路由的 `match`，receiver 類型讀舊鍵而一律顯示 `webhook`（domain policy 也拿錯的類型判斷，且不看 policy 的 `tenants` 範圍），timing 寫死。現在照 Alertmanager 規則走訪（first-match、`continue`、沒命中由父節點投遞、繼承 timing／`group_by`），matcher 支援值跳脫與一字串多 matcher，正規式照 RE2 的 ASCII 語意整串比對，解析不了（含 POSIX 字元類）視為不命中並警告；新增 `--base-config` 指定 root。`Enforced:` 只在命中時顯示；`_routing_enforced` 不再覆寫 `final` 裡 tenant 自己的 receiver。
