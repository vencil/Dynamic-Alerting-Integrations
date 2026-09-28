---
section: Fixed
topic: docs
issues: [2265]
created: 2026-09-28T04:37:29+00:00
---
- **BYO Alertmanager 文件的 override 優先級改成實際行為（[#2265](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2265)）**：`_routing.overrides` 依列表順序、第一個命中者生效，與比對的是 `alertname` 還是 `metric_group` 無關；原文寫「alertname 先於 metric_group」。補一張決策流程圖。路由行為沒有變。
