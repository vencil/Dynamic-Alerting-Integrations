---
section: Changed
topic: da-tools
issues: [1200]
created: 2026-11-24T09:00:00+00:00
---
- **`backtest` 與 `threshold-recommend` 逐叢集判定，不再把查詢回的每一列串成一條（da-tools；[#1200](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1200)）**：`tenant:*` 逐（租戶，`da_cluster`）記錄、告警逐叢集觸發（[ADR-006](docs/adr/006-tenant-mapping-topologies.md)）。⚠️ **行為變更**，查詢只回一列時不變。(1) 多個叢集：`backtest` 每個叢集各自重放，JSON 多 `clusters` 與 `worst_cluster`，頂層數字與風險是最糟那個叢集的——100 步裡只有一個叢集 15 步超標，先前 `15/200`（MEDIUM、結束碼 `0`），現在 `15/100`（HIGH、`1`）。`threshold-recommend` 逐叢集算，候選值取自有建議值的叢集（上界取最高、下界取最低的 floor），再到每個叢集的樣本上重放：閒置或全 NaN 的叢集不擋建議，會多響時上界抬到蓋住它、下界不給建議；有叢集需人工審查則整個 key 跟著；JSON 多 `clusters`、`conservative_cluster`、`held_by_cluster`、`direction`，`threshold-govern` 多列被叢集擋下的 key。(2) **單一叢集也受影響**：同一個 `da_cluster`（或都沒 label）回了不只一列——常見是沒去重的 HA pair——先前被串起來照算，現在兩支工具都算沒量成、結束碼 `2`，訊息點名叢集與不同的 label，請改查會去重的端點。
