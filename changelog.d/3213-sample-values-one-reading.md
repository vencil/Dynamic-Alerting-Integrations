---
section: Fixed
topic: da-tools
issues: [3213, 3133]
created: 2026-10-22T17:00:00+00:00
---
- **Prometheus 樣本值改由 lib 統一解析；`+Inf`／`-Inf`／`NaN` 不再讓 alert-quality、cardinality-forecasting crash（[#3213](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3213)、[#3133](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3133)）**：range 查詢的每個樣本值在 `_lib_prometheus` 讀一次，讀不懂的值（例如 `"abc"`，或不是字串）是回應格式錯誤，整份回答以具名的 `prometheus_query_error` 拒收（rc 2，先前 cardinality-forecasting 報 `internal_error`、alert-quality 悄悄略過）。`alert-quality` 與 `cardinality-forecasting` 遇到非有限值時不再 crash（先前 rc 2 `internal_error`），改為不計入評估／迴歸，並在 `--json` 的 `summary.non_finite_samples_dropped`（alert-quality 另有每筆的同名欄位）與文字報告揭露丟掉的數量，結束碼依分析結果（rc 2 → 0）；cardinality-forecasting 的租戶若樣本全是非有限值，視為沒有資料（`no_cardinality_data`，rc 2，照舊）。backtest-threshold 照舊把 `+Inf`／`-Inf` 當成樣本比較、`NaN` 當成缺資料。Alertmanager 回應裡 label 值不是字串時，改以 `alertmanager_query_error` 拒收。
