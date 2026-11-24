---
section: Changed
topic: da-tools
issues: [4097]
created: 2026-11-23T09:00:00+00:00
---
- **`byo-check` 與 `onboard` 指出沒設 `da_cluster` 的 Prometheus（da-tools；[#4097](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4097)）**：每台 Prometheus 都要在 `global.external_labels` 設 `da_cluster`（[ADR-006](docs/adr/006-tenant-mapping-topologies.md)），先前只有 `federation-check edge` 檢查。⚠️ **行為變更**：`byo-check prometheus` 多了 `cluster_identity`，讀實際載入的設定，沒有值就失敗（結束碼 `1`，先前通過）；讀不到、或內容不是 Prometheus 設定是 `2`；端點回 HTTP 404（查詢層如 Thanos Querier／VictoriaMetrics，未實測）列為 skip，不影響結束碼。`federation-check edge`：`da_cluster` 只有空白現在算沒設（先前通過），讀到的內容不是設定是 `2`（先前 `1`）；UTF-8 label 名與多行值不再讓它誤判。`onboard --scrape-config` 把判定（`set`／`missing`／`unknown`）寫在 stderr、`scrape-analysis.yaml` 與 `--json` 報告頂層的 `cluster_identity`，沒有 `scrape_configs` 也寫；Phase 1 找到租戶、且沒帶 `--dry-run`／`--json` 時才寫的 `onboard-hints.json` 也帶；結束碼不變。
