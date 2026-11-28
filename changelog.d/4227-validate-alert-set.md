---
section: Changed
topic: da-tools
issues: [4227, 4210, 1200]
created: 2026-11-28T16:21:13+00:00
---
- **⚠️ Breaking：`da-tools validate` 改比對 legacy 與 migrated 規則的告警集合（[#4227](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4227)）**：`--mapping` 讀 migrate-rule 寫的 `.migration/mapping/<租戶>.yaml`。每輪先確認兩端都載入了規則、評估間隔不大於 `--interval`，再數每組 selector（legacy 有、注入後是 0 → 結束碼 2；兩邊都是 0 → 不可驗），最後以 `max_over_time(ALERTS{…}[<interval>])` 比兩邊響過的序列，鍵是（租戶, `da_cluster`, alertname, 狀態）加其餘 label；歸不了租戶的 legacy 告警不可驗，不可驗不放行。新增 `--legacy-prometheus`。數值比對移除：`--old`／`--new`／`--tolerance` 刪除，prefix-mapping、舊格式的 report 與 readiness 一律結束碼 2；`cutover` 只收同租戶、`ready` 為布林的 readiness。`shadow-verify preflight` 零比對組改判失敗，改查 migrated 規則與影子 receiver。
