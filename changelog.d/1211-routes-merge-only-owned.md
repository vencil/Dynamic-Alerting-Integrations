---
section: Changed
topic: alertmanager-routing
issues: [1211]
created: 2026-11-05T10:13:00+08:00
---
- **⚠️ BREAKING：`generate-routes` 移除 routing fragment 模式，三種輸出模式共用「只換掉產生器自己那份」的合併（[#1211](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1211)）**：不帶模式旗標（只帶 `-o`、只帶 `--dry-run`）改為結束碼 2——照 fragment 檔頭接在自有路由後面時，Watchdog 進客戶預設頻道、custom／sentinel 叫醒客戶 NOC。改用新增的 `--output-alertmanager --base-config <你的 alertmanager.yml>`（純 alertmanager.yml）或 `--output-configmap`；`--apply` 也從「整個取代 `route.routes`」改為同一個合併。只換掉 `tenant-*`／`platform-enforced*`／四個平台 receiver 與它們的路由、root fallthrough、severity-dedup inhibit，其餘原樣保留；順序是平台路由與 enforced NOC → 你的路由 → 租戶路由，可重複執行。base 裡落在保留命名空間的 receiver 改為被取代（不再拒收）。`watchdog-heartbeat` 沒接 integration 時報 `watchdog_undelivered`：WARN，你自己的 base 加 `--strict` 時 ERROR、結束碼 1。
