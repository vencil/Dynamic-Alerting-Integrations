---
section: Changed
topic: tools
issues: [4264]
created: 2026-11-28T12:30:00+00:00
---
- **以 regex 讀 PromQL 結構的工具改讀 Prometheus 的語法樹（[#4264](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4264)）**：observed-map（門檻與 observed record、縮放、比較方向）、metadata join 檢查（and／unless 右側的任何位置都不算 join：它不把右側的 label 帶進結果；原本只認右側開頭）、exporter-queries 的 `query` matcher 與內建 selector，都改由 `da-read promql-shape` 判讀；註解與字串裡的名字不再被當成讀取，換個寫法（門檻寫在比較左側、`{__name__="…"}`、`100 * x`）也讀得到；parser 拒收的運算式點名拒收。`all_threshold_keys` 原本掃 pack 原文，註解或 annotation 裡的 key 也算已覆蓋，改由 rule loader 讀出的 record 名稱與 expr 取得。
