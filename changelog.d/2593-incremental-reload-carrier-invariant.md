---
section: Changed
topic: exporter
issues: [2593]
created: 2026-10-02T14:41:20+00:00
---
- **flat 增量 reload 改為明確拒收含 `_defaults` carrier 的掃描，刪掉只服務 carrier 樹、生產走不到的分支（internal；[#2593](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2593)）**：watch path 只在樹中沒有任何 `_defaults` 檔、且尚未進入階層模式時才走增量 reload，所以 root carrier 重導與 refused-key set 重算從未在生產執行。兩者已移除，改在入口斷言：違反時回 error、保留上一份設定。exporter 的 reload 行為不變。
