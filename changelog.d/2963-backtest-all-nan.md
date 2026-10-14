---
section: Fixed
topic: da-tools
issues: [2963]
created: 2026-10-14T12:57:53+00:00
---
- **`backtest` 不再把全是 NaN 的序列報成「已分析」（da-tools；[#2963](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2963)）**：回看期間查到的樣本全是 NaN 時，先前會報成 `analyzed`（一般變更與新啟用為 LOW、停用為 MEDIUM），現在改標 `no_data`（風險 `UNKNOWN`），訊息為 `Only NaN samples in lookback window`，與真的查無資料的 `No historical data found in Prometheus` 分開。結束碼不變（兩種情況都是 0），但 `--json` 的 `risk_summary` 與 `no_data` 計數會隨之改變。文字報表摘要行從 `metric not found in Prometheus` 改為 `no usable data in Prometheus`。觸發比例的分母照舊含 NaN（比例代表佔整段回看時間多少；NaN 時告警不會響），`+Inf`／`-Inf` 照常比較。
