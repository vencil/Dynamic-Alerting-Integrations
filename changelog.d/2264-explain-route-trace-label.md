---
section: Added
topic: alertmanager-routing
issues: [2264]
created: 2026-09-28T07:51:09+00:00
---
- **`explain-route --trace` 新增 `--label KEY=VALUE`（[#2264](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2264)）**：追蹤的 alert 先前只有 `alertname`／`severity`／`tenant` 三個 label，`overrides` 的 `metric_group` 與 `routes` 的 `match` key 帶不進去，追蹤因此永遠落在主 receiver。`--label` 可重複、以第一個 `=` 切分（值可含 `=`、可為空），`--json` 的 `labels` 一併列出。沒有 `=`、key 不是合法 label 名稱、key 為 `alertname`／`severity`／`tenant`（改用對應旗標）、同 key 重複，或沒有 `--trace` 卻給 `--label`，一律以結束碼 2 拒絕；`trace_alert_routing(extra_labels=…)` 帶這三個 key 改為 `ValueError`，不再靜默覆蓋。
