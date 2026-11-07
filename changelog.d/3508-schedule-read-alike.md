---
section: Changed
topic: tenant-api
issues: [3508]
created: 2026-11-07T00:36:07+00:00
---
- **寫入關卡判「排程沒變」改以讀取結果比較（tenant-api；[#3508](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3508)）**：檔案原有、exporter 讀不了的門檻值（例如 `default: abc` 加時段），寫入沒有改變它時照收並附 notice（ADR-038 決定 12）。判斷「沒改變」現在比的是一天中每一分鐘讀出的值與 `expires` 生效的時刻，不再逐位置比原文：互不重疊的時段換順序、只改或新增 `reason:`、`expires` 換成同一時刻的另一種時區寫法，都不再被當成改過而回 400。重疊時段換順序、時段移動、`expires` 換了時刻仍算改過。
