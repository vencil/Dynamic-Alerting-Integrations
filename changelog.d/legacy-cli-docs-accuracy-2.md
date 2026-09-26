---
section: Fixed
topic: cli-docs-accuracy
issues: [1358, 1416, 1492, 1513, 1514, 1556, 1619, 1620]
created: 2026-09-26T17:00:00+00:00
---
- **cli-reference 的預設值與結束碼表對齊程式（docs）**：「預設值」欄多組與實際不符的值已更正（如 `validate --tolerance`、`--rounds`、`baseline --duration`／`--interval`、`alert-correlate --lookback`／`--window`、`backtest --lookback`），`offboard`／`deprecate` 的 `--config-dir` 據實標明須明確指定（[#1556](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1556)）。結束碼表補齊 `2` 列並改正倒置的 `1` 列（[#1514](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1514)），`config-diff` 的 `1` 是「偵測到變更」而非錯誤、目錄無效是 `2`（[#1358](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1358)）；縮寫旗標寫全、`--output <檔>` 改為實際的 `--output-dir <目錄>`。⚠️ 消費端須留意：以 `rc==1` 判斷 `check-alert` 連不上 Prometheus 的腳本要改看 `2`；照舊文件用 `analyze-gaps --config` 得到的「沒有缺口」應重跑；`shadow-verify runtime` 在 Prometheus 連不上時回 `0`。
