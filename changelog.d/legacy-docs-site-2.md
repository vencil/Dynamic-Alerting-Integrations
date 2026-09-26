---
section: Fixed
topic: docs-site
issues: [1321, 1353, 1447]
created: 2026-09-26T17:00:00+00:00
---
- **照抄就會出錯的文件範例全面更正（docs）**：平台工程師指南的 `_defaults.yaml` 範例原本整份載入不進去（閾值加引號、`_routing_defaults` 縮排錯層），已依真 loader 改寫，並新增以出貨 loader 驗證文件內 `_defaults.yaml` 範例的測試；兩份 scenario 教的是從未實作過的設定模型（`alerts.threshold.<AlertName>`、`receivers:` 陣列），照做只會得到一個幽靈租戶，已改為真實形狀。hands-on-lab 的 `_routing` 改為巢狀 `receiver:`（扁平寫法會讓整條路由被丟掉），`_silent_mode` 補上必填的 `target:`；ArgoCD Application 補 `spec.project`、Operator 系列 16 處 SecretKeySelector 多包的一層 `secret:` 移除。GitOps 手動路徑文件補上 `optional_overrides` 未設定就不會告警的警語與正確的 `diagnose` 指令；try-local 文件更正 `--dev-bypass-auth` 自 `tenant-api/v2.9.7` 起已有 published image。`.github/CODEOWNERS` 不再指向與本專案無關的外部帳號。詳 [#1321](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1321)、[#1353](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1353)。
