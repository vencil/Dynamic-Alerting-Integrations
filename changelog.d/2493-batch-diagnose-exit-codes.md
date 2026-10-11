---
section: Fixed
topic: da-tools
issues: [2493]
created: 2026-10-11T08:58:41+00:00
---
- **`da-tools batch-diagnose` 的結束碼反映租戶檢查結果（[#2493](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2493)）**：原本只要跑完就回 0，租戶檢查失敗、Prometheus 連不上也一樣，拿它當 CI 閘門永遠放行。現在每個租戶照單跑 `diagnose` 的規則分類後彙總：任一租戶 Prometheus 查詢失敗或缺少 `kubectl` 回 2（優先），否則任一租戶檢查失敗回 1，全部 `healthy` 或 `unchecked`（以及 `--dry-run`）回 0。報告照常輸出；`--workers` 小於 1 也改回 2，不再是 traceback。⚠️ 以 `batch-diagnose && …` 串接或跑在 CronJob 裡的，租戶失敗時現在會中斷或讓 Job 失敗。
