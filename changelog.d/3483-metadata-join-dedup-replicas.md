---
section: Changed
topic: rule-packs
issues: [3483]
created: 2026-11-01T01:33:54+00:00
---
- **告警不再帶 runbook_url／owner／tier label，改由 annotation 查詢租戶 metadata（rule-packs；breaking，[#3483](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3483)）**：過去 14 個 pack 共 91 處以 `* on(tenant) group_left(runbook_url, owner, tier) … tenant_metadata_info` 把 metadata 接進告警，metadata 因此成了告警身分。threshold-exporter 多副本逐 pod 抓取時，換版空窗內兩個副本送出不同的 metadata，join 會整條失敗或每次評估挑到不同的那筆，告警身分翻動、`for:` 重來（VictoriaMetrics 上該租戶空窗內不觸發）。現在規則不 join，`runbook_url`／`owner`／`tier` 等 annotation 以唯一的查詢樣板讀 `tenant_metadata_info`（沒有 metadata 時為空字串），自訂告警編譯器同步調整；`check_leftouterjoin_enrichment.py` 改為擋下任何 metadata join 與 `$labels.runbook_url` 之類的參照。依這三個 label 路由或抑制的 Alertmanager 設定要改用 `tenant`；自己寫的 Rule Pack 請改用同一個樣板（見 [高可用性設計 §4.3](docs/design/high-availability.md)）。
