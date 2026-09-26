---
section: Added
topic: federation
issues: [903, 924, 926, 996, 1002, 1234, 1237]
created: 2026-09-26T17:00:00+00:00
---
- **Federation 撤銷 tamper-evidence（ADR-028）**：偵測有寫入權者偷刪未過期撤銷（un-revoke）。`revoke()` 發出不含租戶識別碼的事件進 VictoriaLogs；新增長駐的 `federation-reconciler` chart 直讀掛載的 `revoked.txt` 與事件對帳（fail-closed），附 tamper／staleness／gateway fail-open 平台告警與專屬 Grafana 儀表板。事件走 Vector 專屬 `federation_evidence` 通道；tenant-api 定期發 heartbeat canary，通道中斷時 `FederationRevocationEvidenceChannelDown` 告警；偵測查詢加上來源限定。屬 tamper-evident 而非防竄改保證。try-local 可用 `make chaos-tamper`／`make chaos-heal` 親手注入與復原竄改。見 [#924](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/924)、[#1234](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1234)。
