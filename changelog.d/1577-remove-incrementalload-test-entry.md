---
section: Changed
topic: exporter
issues: [1577]
created: 2026-10-01T13:50:30+00:00
---
- **移除 `ConfigManager.IncrementalLoad()`，測試與 benchmark 改走 watch path 的 reload（internal；[#1577](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1577)）**：這個入口沒有非測試呼叫端，卻跳過 watch path 的跨檔重複租戶檢查，測試因此在斷言一條 exporter 從不走的路徑。exporter 的 reload 行為不變。⚠️ `BenchmarkIncrementalLoad_*_OneFileChanged` 的樹含 `_defaults.yaml`，watch path 對它走階層式 reload，所以 nightly 與 PR gate 這兩支會出現一次工作定義階梯，不是退化；`_NoChange` 系列改量一次 watch tick，成本形狀不變。
