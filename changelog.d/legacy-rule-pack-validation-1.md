---
section: Added
topic: rule-pack-validation
issues: [947, 975, 1174, 1175, 1177, 1189, 1200, 1203]
created: 2026-09-26T17:00:00+00:00
---
- **Rule Pack 偵測品質與後端相容的量測工具鏈（internal）**：新增 ADR-030 fault-waveform 決策層驗證（波形編譯器、注入 harness、catch-rate 計分器，含 air-gap 用 `--redact` 模式）與參考驗證庫；VictoriaMetrics（vmalert／MetricsQL）parity gate（required check：未登記於 `vm_deviation_catalog.yaml` 的新分歧擋 merge，[#947](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/947)）；另加告警 firing 覆蓋基線、pint 靜態檢查、閾值 key 可達性與 scrape 可達性 gate，以及語言中立的 `threshold-registry.yaml` 作為閾值契約來源。可達性量測揭露出貨 chart 預設下有一批告警結構上無法開火（[#1189](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1189)），由 ledger 列管。
