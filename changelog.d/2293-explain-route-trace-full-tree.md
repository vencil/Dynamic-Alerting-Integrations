---
section: Fixed
topic: alertmanager-routing
issues: [2293]
created: 2026-09-28T12:49:11+00:00
---
- **`explain-route --trace` 改由 Alertmanager 判定整棵路由樹（[#2293](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2293)）**：先前只比對 tenant 主路由的子路由，看不到 Watchdog／custom／synthetic-probe／sentinel 四條頂層路由與 enforced 路由的 `match`，receiver 類型讀舊鍵而一律顯示 `webhook`（domain policy 也拿錯的類型判斷，且不看 policy 的 `tenants` 範圍），timing 寫死。現在組出 `--output-configmap` 會產出的整棵樹，交給 `amtool config routes test --tree`，命中路徑與投遞的 receiver 都由 Alertmanager 的 parser 決定；timing 依 Alertmanager 的繼承規則取值；新增 `--base-config` 指定 root。trace 需要 PATH 上有 `amtool`，沒有時印 WARN、receiver 顯示 unknown、改列路由樹摘要，結束碼仍為 0。`Enforced:` 只在命中時顯示；`_routing_enforced` 不再覆寫 `final` 裡 tenant 自己的 receiver。Inhibit 步驟改讀實際的 severity dedup 設定：先前讀錯鍵，永遠印「No inhibition applies」；且只在告警帶非空 `metric_group` 時標 possible（產生器的 dedup inhibit rule 只抑制這種告警），組不出設定時標 unknown。
