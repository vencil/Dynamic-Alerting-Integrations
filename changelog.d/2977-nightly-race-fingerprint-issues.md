---
section: Changed
topic: ci
issues: [2977, 3343]
created: 2026-11-01T01:03:17+00:00
---
- **nightly race 追蹤 issue 改為「每個失敗指紋一張」，不再每個 module 一張（[#2977](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2977)、[#3343](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3343)）**：每晚的結果拆成 build／package 逾時／data race／失敗測試／未歸屬等事件，以 `module|kind|subject` 為指紋（body 第一行的標記，只比對開頭），各自有自己的 first-seen 與 streak；超過 5 個失敗測試合併成一張。沒再出現的指紋只靜默記一筆「not seen」，仍不自動關閉；沒有指紋標記的舊 issue 不動，只在 step summary 點名。`parse_go_test_json` 新增 `test_runs`（每個頂層測試的 pass/fail 次數），每輪都失敗的測試寫成一般失敗，只有部分輪次失敗才用 isolation 字眼。
