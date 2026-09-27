---
section: Fixed
topic: portal
issues: [2033, 1359]
created: 2026-09-27T01:03:28+00:00
---
- **Portal 的 YAML 解析改用 js-yaml，Playground 只檢查語法與結構（portal；[#2033](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2033)、[#1359](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1359)）**：兩支手寫 parser 與 exporter（yaml.v3）讀法不一致——重複鍵被合併判為合法、跳脫鍵與 complex key 讀錯、語法錯誤與 tab 縮排被靜默吸收。現在重複鍵、縮排錯誤等一律報錯並附行號。YAML Playground（改名「YAML 語法檢查器」）移除所有語意判定（閾值格式、已知 metric 清單、receiver 類型、duration 範圍、`_state_maintenance`／`_routing` 形狀），只檢查語法、`tenants:` 為 mapping、每個租戶為 mapping；過去被誤判的 `85.5`、`500:critical`、`_state_maintenance: enable`、`1h30m` 不再報錯。語意以 exporter 為準。多文件只讀第一份、自我參照 anchor 報錯，皆與 yaml.v3 一致；已知差異（工具頁有揭露）：`010` 在 portal 讀為 10、yaml.v3 讀為 8，`1_000` 為字串、yaml.v3 讀為 1000。
