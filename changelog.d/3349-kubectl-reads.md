---
section: Changed
topic: da-tools
issues: [3349]
created: 2026-10-29T02:22:48+00:00
---
- **diagnose／batch-diagnose／grafana-import 的叢集讀取改走 `_lib_kubectl`（[#3349](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3349)）**：kubectl 沒有回答（forbidden、叢集連不上、沒有 kubectl、逾時）一律是 rc 2、`--json` reason `kubectl_error`，不再被讀成「沒有」。**Breaking**：diagnose 在 Pod 讀取 forbidden／連不上時由 rc 1「Pod not found」改為 rc 2；batch-diagnose 某 tenant 遇到同樣情況時批次由 rc 1 改為 rc 2，探索 tenant 時讀不到改為具名錯誤（不再是「No tenants found」），ConfigMap 不存在也會具名；grafana-import `--verify --json` 讀不到時輸出 `kubectl_error` envelope（原為 `checks[]` 中一列 caller_error），沒有 kubectl 或逾時不再 traceback。diagnose 的 log 讀取失敗記在 `recent_logs_error`，不影響結束碼。
