---
section: Changed
topic: da-tools
issues: [3133]
created: 2026-10-22T19:00:00+00:00
---
- **⚠️ 結束碼變更：byo-check、federation-check、shadow-verify 的查詢沒得到回答一律是失敗的檢查項，結束碼 2（[#3133](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3133) 第二批）**：三支工具的 instant 查詢改經 `_lib_prometheus` 檢查回應格式，`count(...)` 的值由 `vector_count` 讀取，`NaN`／`Inf` 不再讓工具 crash。下列情況先前不產生檢查項、只給 warn，或判成 pass／fail，現在都是 caller_error（0 或 1 → 2）：查詢失敗；回應格式不對，例如缺 `value`、`resultType` 錯、樣本讀不懂；Alertmanager 的 alerts／silences 讀不到，silences 改經新的 `alertmanager_silences`。federation-check central 的備援查詢失敗，先前報成「找不到」（1 → 2）。shadow-verify 的 Alertmanager 連不上仍是 skip。byo-check Step 1 的主查詢沒回答、備援有回答時，仍依備援判定，主查詢的錯誤附在 detail，結束碼不變。各步驟的對照表見 #3133。
