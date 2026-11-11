---
section: Changed
topic: exporter
issues: [3639, 1200]
created: 2026-11-10T16:34:02+00:00
---
- **⚠️ BREAKING：週期維護窗改由 threshold-exporter 自己判斷，窗內規則不觸發（[#3639](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3639)）**：`_state_maintenance.recurring` 改寫成 `- weekdays: [sun]` 加 `window: "02:00-06:00"`（UTC；跨午夜的窗口算在開始那天）。窗內與一次性維護一樣開啟維護狀態，`maintenance: mute` 的規則不觸發、TSDB 無紀錄；以前是照常觸發、只由 Alertmanager silence 擋通知。`expires` 不限制週期窗；開關說關時窗內也不開。舊寫法 `- cron: "0 2 * * 0"` + `duration: 4h` 不再接受：整個值不被讀、維護**不開啟**（WARN），寫入閘門也拒收——連帶 `target: enable` 的進行中維護在升級後就關掉，旁邊寫 `target: disable` 的也一樣每次 scrape 都 WARN，請一併改寫或刪掉 `recurring`。新寫法只收 `HH:MM-HH:MM` 兩位數字、星期不可重複、`recurring` 至少一筆，與 schema 一致。遷移：把每條 cron + duration 改寫成 `weekdays` + `window`。da-tools 的 `maintenance-scheduler` 子命令、它的 CronJob 與 NetworkPolicy 已移除（已部署的請刪掉，它建的 silence 到期即消失），da-tools 映像不再帶 croniter；`da-guard effective` 不再輸出 `maintenance_recurring`。
