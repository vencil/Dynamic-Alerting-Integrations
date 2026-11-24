---
section: Changed
topic: da-tools
issues: [4097]
created: 2026-11-23T09:00:00+00:00
---
- **`byo-check` 與 `onboard` 指出沒設 `da_cluster` 的 Prometheus（da-tools；[#4097](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4097)）**：每台 Prometheus 都要在 `global.external_labels` 設 `da_cluster`（[ADR-006](docs/adr/006-tenant-mapping-topologies.md)），先前只有 `federation-check edge` 會檢查。⚠️ **行為變更**：`da-tools byo-check prometheus` 多了 `cluster_identity` 一項，讀實際那台 Prometheus 載入的設定，`da_cluster` 沒有值就失敗（結束碼 `1`，先前通過），設定讀不到是呼叫端錯誤（結束碼 `2`）。`da-tools onboard --scrape-config` 在 stderr、`scrape-analysis.yaml`、`--json` 報告與 `onboard-hints.json` 寫出判定：`set`、`missing`（要補上）或 `unknown`（檔案沒有 `global` 區段，改用 `byo-check prometheus` 檢查）；結束碼不變。三個工具與 repo 的抓取設定 lint 共用同一個判定（`_lib_cluster_identity`）。
