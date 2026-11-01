---
section: Fixed
topic: rule-packs
issues: [3483]
created: 2026-11-01T01:33:54+00:00
---
- **以 pod 為單位抓取 threshold-exporter 時，帶 metadata 的告警不再整條評估失敗（rule-packs；[#3483](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3483)）**：exporter 跑多個副本、每個副本都送一份 `tenant_metadata_info`。改用 Prometheus Operator 的 ServiceMonitor（一個 pod 一個 target）抓取後，同一個租戶會有好幾份序列，`* on(tenant) group_left(runbook_url, owner, tier) tenant_metadata_info` 會報 many-to-many，整條告警不觸發。14 個 pack 共 91 處 join 的右側改成 `topk by(tenant) (1, max by(tenant, runbook_url, owner, tier) (tenant_metadata_info))`，自訂告警編譯器同步調整：`max by` 收掉相同的副本，外層 `topk` 讓換版空窗內兩個副本送出不同 metadata 時，只有該租戶短暫帶到舊值，不會讓整條規則對所有租戶評估失敗（[高可用性設計 §4.3](docs/design/high-availability.md)）。`check_leftouterjoin_enrichment.py` 擋下右側不是這個寫法的 join，`by` 的 label 清單由測試對照 exporter 實際送出的 label。自己寫的 Rule Pack 若有同樣的 join，也要照這個寫法改。
