---
section: Changed
topic: da-tools
issues: [1200]
created: 2026-11-24T09:00:00+00:00
---
- **`backtest` 與 `threshold-recommend` 逐叢集判定，不再把查詢回的每一列串成一條（da-tools；[#1200](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1200)）**：`tenant:*` 逐（租戶，`da_cluster`）記錄、告警逐叢集觸發（[ADR-006](docs/adr/006-tenant-mapping-topologies.md)）。⚠️ **行為變更**，只回一列時不變。(1) 多個叢集：`backtest` 每個叢集各自重放，JSON 多 `clusters`、`worst_cluster`，頂層數字是最糟那個叢集的——100 步裡只有一個叢集 15 步超標，先前 `15/200`（MEDIUM、結束碼 `0`），現在 `15/100`（HIGH、`1`）。`threshold-recommend` 逐叢集算，取有建議值的叢集裡最保守的（上界最高、下界最低的 floor），再只對引擎沒給建議值的叢集重放：閒置叢集不擋建議，會多響時上界抬到蓋住它、下界不給建議；JSON 多 `clusters`、`conservative_cluster`、`held_by_cluster`、`direction`，`threshold-govern` 多列被擋下的 key。(2) **單一叢集也受影響**：同一個 `da_cluster` 回了時間重疊的多列（常見是沒去重的 HA pair），先前串起來照算，現在三支工具都算沒量成、結束碼 `2`，訊息點名不同的 label，請改查會去重的端點；時間不重疊的列（label 中途換過）接起來照常算。沒量成的 stderr／報告用語改為 `not measured (the query failed, or rows the da_cluster label does not tell apart)`。
