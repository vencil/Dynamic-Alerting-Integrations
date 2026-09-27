---
section: Fixed
topic: da-tools
issues: [1513]
created: 2026-09-27T17:08:53+00:00
---
- **五支 da-tools 工具不再默默給出誤導的結果（da-tools；[#1513](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1513)）**：`discover-mappings --endpoint` 在設了 `PROMETHEUS_URL` 時不再被注入 `--prometheus`，不再以 rc=2 拒收。`analyze-gaps` 的輸入路徑不存在時回 rc=2，不再當成沒有 `custom_` 指標；它與 `migrate` 在 repo 佈局下也能找到 `metric-dictionary.yaml`，都找不到時會警告，不再默默退化成前綴猜測。`diagnose` 在 `status: error` 時回 1，Prometheus 查詢失敗或沒有 `kubectl` 時回 2，不再一律 0 或 traceback；它先從 `tenant_expected_exporter` 讀 `db_type`，只有 `mariadb` 才跑為 MariaDB 寫的 Pod 與 exporter 檢查，其他資料庫或沒宣告的租戶列在 `skipped`，batch-diagnose 不再把它們全報成 error。`maintenance-scheduler` 在沒給 `--alertmanager` 或帶 `--dry-run` 時，摘要不再寫 `N created`，JSON 多一個 `mode` 欄位。
