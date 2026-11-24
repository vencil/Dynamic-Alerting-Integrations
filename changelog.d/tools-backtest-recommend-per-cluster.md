---
section: Changed
topic: da-tools
issues: [1200]
created: 2026-11-24T09:00:00+00:00
---
- **`backtest` 與 `threshold-recommend` 逐叢集判定，不再把各叢集的樣本串成一條（da-tools；[#1200](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1200)）**：rule pack 把 `tenant:*` 逐（租戶，`da_cluster`）記錄、告警逐叢集觸發（[ADR-006](docs/adr/006-tenant-mapping-topologies.md)），先前兩支工具把查詢回的每一列混在一起算。⚠️ **行為變更（只影響匯集多個叢集資料的 Prometheus）**：`backtest` 每個叢集各自重放，JSON 每筆變更多了 `clusters`（逐叢集的點數與風險）與 `worst_cluster`，頂層的 `data_points`、觸發點數與風險改為最糟那個叢集的數字——例如 100 步裡只有一個叢集 15 步超標，先前報 `15/200`（MEDIUM、結束碼 `0`），現在是 `15/100`（HIGH、結束碼 `1`）。`threshold-recommend` 逐叢集算百分位數，建議取不讓任何叢集多響的值（上界取最高、下界取最低的 floor；有叢集需人工審查或沒有建議值時整個 key 跟著它），信心取最弱的叢集，JSON 多了 `clusters` 與 `conservative_cluster`。同一個叢集回了不只一列時兩支都不相加：`backtest` 標 `not_backtested`，`threshold-recommend` 標人工審查。只回一列時輸出與先前相同。
