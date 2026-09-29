---
section: Fixed
topic: alertmanager-routing
issues: [2293]
created: 2026-09-28T12:49:11+00:00
---
- **`explain-route --trace` 改由 Alertmanager 判定整棵路由樹（[#2293](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2293)）**：先前只比對 tenant 主路由的子路由，看不到 Watchdog／custom／synthetic-probe／sentinel 四條頂層路由與 enforced 路由的 `match`，receiver 類型讀舊鍵而一律顯示 `webhook`（domain policy 也拿錯的類型判斷，且不看 policy 的 `tenants` 範圍），timing 寫死。現在組出 `--output-configmap` 會產出的整棵樹，交給 `amtool config routes test --tree`，命中路徑與投遞的 receiver 都由 Alertmanager 的 parser 決定；timing 依 Alertmanager 的繼承規則取值；新增 `--base-config` 指定 root。trace 需要 PATH 上有 `amtool`，沒有時印 WARN、receiver 顯示 unknown、改列路由樹摘要，結束碼仍為 0。產生器拒收的樹（租戶檔讀不進、同一租戶宣告在多個檔、#2326 的 routing-tree 錯誤）trace 用產生器同一套判定與順序，receiver 顯示 unknown，以 WARN 印出產生器的同一段訊息與結束碼，不另印開頭的拒收警告。`Enforced:` 只在命中時顯示；`_routing_enforced` 不再覆寫 `final` 裡 tenant 自己的 receiver。Inhibit 步驟先前讀錯鍵，永遠印「No inhibition applies」；現在改為列出組好設定裡的 inhibit rules 原文（含 `--base-config` 自帶的），不再判定是否抑制（取決於執行時 firing 的告警），JSON 移除 `inhibited`／`inhibit_reason`。
