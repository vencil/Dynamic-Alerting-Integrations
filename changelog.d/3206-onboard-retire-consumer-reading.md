---
section: Changed
topic: confd-family
issues: [3206]
created: 2026-10-28T01:11:00+00:00
---
- **`onboard_platform`、`check_retire_drift`、`check_scrape_reachability` 改以消費者的讀法讀檔（[#3206](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3206)，[ADR-036](docs/adr/036-single-parser-effective-config.md) §12）**：⚠️ `onboard_platform` 的 Alertmanager 設定（Phase 1）與 `prometheus.yml`（Phase 3）先經 `da-crdecode` 讀 ConfigMap 外層，設定本身交給 `da-read alertmanager`／`da-read prometheus`，需要這兩支（`--da-crdecode-binary`／`--da-read-binary`、環境變數或 PATH）。值是消費者的讀法（matcher 正規化、relabel `action` 小寫），只取原文寫了的鍵；Alertmanager 或 Prometheus 會拒收的設定（沒有 `route`、不認得的鍵、重複的 `job_name`）改為 rc 2 並印出原因。⚠️ `check_retire_drift` 的 Namespace manifest 改經 `da-crdecode` 讀，未加引號的數字 label（`instance: 010` 讀成 8）現在報錯（rc 2）。`check_scrape_reachability` 與它的 pre-commit hook 需要 Go 或兩支 binary。
