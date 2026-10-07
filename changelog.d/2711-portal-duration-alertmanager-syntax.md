---
section: Fixed
topic: portal
issues: [2711]
created: 2026-10-07T14:58:54+00:00
---
- **portal 的路由時長檢查改用 Alertmanager 的寫法（portal；[#2711](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2711)）**：Tenant Self-Service Portal 的 YAML 驗證分頁（共用的告警引擎 `validateConfig`）檢查 `_routing` 的 `group_wait`／`group_interval`／`repeat_interval` 時，規則改讀 schema `definitions.duration` 產生的 `am-duration.json`（`make am-duration-json`，`am-duration-json-check` 擋漂移），不再自帶 regex。`1h30m`、`1d`、`500ms` 讀得到並照常套護欄；`1.5h`、`30m1h`、`1h1h`、`1ns` 先前不是被照收、就是被略過不提示，現在顯示為錯誤，並說明產生器會改用平台預設值、`--validate` 會拒收。Config Lint 的 `repeat-interval-too-long` 與 `group-wait-too-low` 也改用同一套解析：`1d1h` 讀成 25h（原本讀成 1h）、`4d` 讀成 96h（原本略過）；`group_wait` 低於 5s 一律提示（原本只認 `1s`–`4s`，漏掉 `500ms`、`0s`）；`1.5h` 不再被當成 1h。Config Lint 讀不出的值一律略過、不報（無效時長由上述 YAML 驗證分頁報錯）。
