---
section: Fixed
topic: rule-pack-validation
issues: [1134, 1219, 1250, 1285, 1286, 1291, 1392, 1393, 1402, 1411, 1413, 1434, 1443]
created: 2026-09-26T17:00:00+00:00
---
- **平台告警契約與規則驗證 gate 不再假綠（internal）**：平台告警契約掃描器改為依內容與 provenance 辨識規則（涵蓋 `operator-manifests/` 的 PrometheusRule、非 UTF-8 檔等，並拒收 helm values 內的規則形狀），修正多個 fail-open 缺口；閾值 defaults 各道 floor 改為逐棵樹量 key、每個 defaults 檔一律過 schema 檢查、豁免清單關不掉這道 schema 檢查（[#1411](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1411)、[#1443](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1443)）；並修掉一批結構上不可能變紅的 gate 與既有 flaky 測試。
