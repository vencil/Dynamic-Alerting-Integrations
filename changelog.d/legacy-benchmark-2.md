---
section: Fixed
topic: benchmark
issues: [1396, 1497, 1521, 1568, 1927]
created: 2026-09-26T17:00:00+00:00
---
- **nightly bench watchdog 與 harness 修正（internal）**：trend watchdog 改為只跟同一主機類別的夜比較，並以三態（FINDINGS／CLEAR／INCONCLUSIVE）判定，「今夜無法評估」不再被當成復原而關票；sleep 對照測試不再被當成產品 benchmark 開票。掃描器 bench 改名 `ScanDirTree_*` 並改量出貨的 walker（⚠️ 與舊系列不可比，下次 exporter release 重 pin 前無 nightly 趨勢）。另更正 v2.9.0 紀錄：純平面部署同樣承擔 `IncrementalLoad` 的新增成本。`benchmark.sh --under-load` 的合成租戶改為一租戶一個 `synth-NNNN.yaml`；⚠️ 舊版跑過的叢集可能殘留 `thresholds.yaml`（只剩空 `tenants:` 時新版不會發現），確認內容只有空 `tenants:` 或 `synth-*` 後需手動刪。見 [#1396](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1396)、[#1568](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1568)。
