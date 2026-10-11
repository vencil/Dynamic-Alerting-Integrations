---
section: Added
topic: tenant-api
issues: [2884]
created: 2026-10-11T11:12:21+00:00
---
- **`tenant-api state restore`：安全還原 state store 備份（tenant-api；[#2884](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2884)）**：備份就是 `kubectl get configmap tenant-api-state -o yaml`（或 dir 模式的目錄），但用 `kubectl apply` 蓋回會讓 `next_account_id` 倒退，備份後配發的 AccountID 被重發給下一個租戶（跨租戶 log 洩漏）。新子命令 `tenant-api state restore --from FILE|DIR|- [--keys k1,k2] [--dry-run]` 在 pod 內執行：帳本取現行、備份與有效 logs token 三者聯集，計數器只升不降，來源不一致時整次拒絕、不寫入；其他紀錄只有列在 `--keys` 才整筆覆蓋。dir 模式的 state store 改以 flock 序列化跨行程寫入。匯出與還原步驟見 tenant-api README。
