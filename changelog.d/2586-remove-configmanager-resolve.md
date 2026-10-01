---
section: Changed
topic: exporter
issues: [2586]
created: 2026-10-01T22:20:12+00:00
---
- **移除 `ConfigManager.Resolve`，測試改讀 exporter 已 commit 的狀態或 `config.ResolveEffective`（internal；[#2586](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2586)）**：這個方法沒有非測試呼叫端，每次呼叫都重讀磁碟，所以在 debounce 視窗內會回報還沒 commit 的值，與 `/metrics` 不同源。原本拿它當 oracle 的測試分成兩類改寫：要「exporter 實際服務的值」的，改讀 `/metrics` 的 series 或 commit 時快取的 hierarchy；要「resolver 對磁碟的答案」的，改用 tenant-api 與 da-guard 共用的 `config.ResolveEffective`。另修正把 exporter 寫成有 `/effective` 端點的註解。exporter 的 reload 與 `/metrics` 行為不變。
