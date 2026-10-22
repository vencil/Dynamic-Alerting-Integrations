---
section: Changed
topic: da-tools
issues: [3133]
created: 2026-10-22T19:00:00+00:00
---
- **⚠️ 結束碼變更：byo-check、federation-check、shadow-verify 的查詢沒得到回答時一律是失敗的檢查項（[#3133](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3133) 第二批）**：這三支的 instant 查詢改經 `_lib_prometheus` 檢查回應格式，`count(...)` 的讀值由 `vector_count` 統一處理（`NaN`／`Inf` 先前會讓工具 crash），查詢失敗都是 caller_error、結束碼 2。byo-check：Step 3b、E2E 查詢失敗先前不產生檢查項，Step 4／5 的後續查詢失敗先前是 warn（結束碼 0 或 1 → 2）；Alertmanager 的 alerts／silences 改經 `alertmanager_alerts`／新的 `alertmanager_silences`，讀不到或回應格式不對先前是 warn，alerts 不是陣列時判 pass（0 → 2）。federation-check：central 的備援查詢失敗先前報成「找不到」（1 → 2），recording rules 與 e2e 跨叢集比對的查詢失敗先前不產生檢查項（0 → 2）。shadow-verify：`runtime` 的維護／靜默模式查詢失敗先前不產生檢查項（0 → 2）；Alertmanager 連不上仍是 skip。
