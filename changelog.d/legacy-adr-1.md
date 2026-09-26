---
section: Added
topic: adr
issues: [870, 1092, 1396, 1430, 1432, 1439, 1559, 1560]
created: 2026-09-26T17:00:00+00:00
---
- **新增 ADR-026／031／032／033／034（adr）**：ADR-026（proposed）決定 node／叢集維護期間的告警抑制以 HA exporter 為主、不建 cordon-aware 子系統；ADR-031（accepted）定案 `slo_burn_rate` recipe 的宣告式 burn-rate 編譯設計（[#1092](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1092)）；ADR-032（accepted）把夜跑效能監測改為同一 runner 內成對交錯量測，取代跨夜滑動錨點（實作追蹤 [#1439](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1439)）；ADR-033（accepted）界定與運維執行平面的協同介面，主要結論是不建機器面即時靜音入口；ADR-034（proposed）規定列舉型、且決定某道檢查是否執行的設定，其合法值不得兼作無法辨識時的 fallback（實例 [#1559](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1559)）。
