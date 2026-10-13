---
section: Fixed
topic: alertmanager-routing
issues: [2926]
created: 2026-10-13T12:44:13+00:00
---
- **`explain-route` 沒帶 `--trace` 時拒絕 `--alertname`／`--severity`（[#2926](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2926)）**：這兩個旗標只餵給 `--trace` 的模擬告警，先前沒帶 `--trace` 時照樣接受、結束碼 `0`，使用者以為指定的告警被檢查過了。現在與 `--label`／`--base-config` 一致，以結束碼 `2` 拒絕，即使給的值就是預設值；帶 `--trace` 時行為與預設值（`GenericAlert`、`warning`）不變。
