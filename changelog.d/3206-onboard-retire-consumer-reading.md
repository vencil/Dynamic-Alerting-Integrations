---
section: Changed
topic: confd-family
issues: [3206]
created: 2026-10-28T01:11:00+00:00
---
- **`onboard_platform`、`check_retire_drift`、`check_scrape_reachability` 改以消費者的讀法讀檔（[#3206](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3206)，[ADR-036](docs/adr/036-single-parser-effective-config.md) §12）**：⚠️ `onboard_platform` 的 Alertmanager 設定與 `prometheus.yml`（Phase 1、3）先經 `da-crdecode` 讀 ConfigMap 外層，再交 `da-read alertmanager`／`prometheus`，需要這兩支。值是消費者的讀法，只取原文寫了的鍵。以下改為 rc 2 並印原因：消費者會拒收的設定（沒有 `route`、不認得的鍵、重複的 `job_name`、根本不是 prometheus.yml，Phase 3 以前只 WARN）；`data.prometheus.yml` 不是字串；`external_labels` 含 `${VAR}` 或 `$VAR`；`scrape_config_files` 非空（da-read 不展開）；`--scrape-config` 檔不存在（以前 rc 1）。⚠️ `check_retire_drift` 的未加引號數字 label（`instance: 010`）現在報錯。`check_scrape_reachability` 與它的 hook 需要 Go 或兩支 binary。
