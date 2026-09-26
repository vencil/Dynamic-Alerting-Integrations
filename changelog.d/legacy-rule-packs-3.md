---
section: Fixed
topic: rule-packs
issues: [944, 1168, 1181, 1200]
created: 2026-09-26T17:00:00+00:00
---
- **修正從不觸發或顯示錯誤的告警（rule packs）**：`MariaDBHighCPU` 對 gauge 套 `rate()` 而永不觸發；`DB2HighSortOverflow` 改用速率比，不再隨 instance 運行時間漸進失敏（[#1181](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1181)）；kubernetes pack 兩個防靜默失效的 sentinel 在 federation 拓樸下永遠 inert，已改走 edge recording（[#1168](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1168)）；`OracleHighSessionUtilization`／`MongoDBConnectionSaturation` 把 100% 顯示成「1%」已修。沒設 CPU limit 的 pod 改以 node allocatable 計算 CPU%，同樣由 `PodContainerHighCPU` 涵蓋。`mysql_cpu` 校準遺留在部署 overlay、portal 推薦值、範例與 try-local 的舊數字全面更新；Rule Pack 總數統一為 16。
