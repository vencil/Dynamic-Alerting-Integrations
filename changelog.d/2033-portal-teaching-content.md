---
section: Fixed
topic: portal
issues: [2033]
created: 2026-09-27T02:01:45+00:00
---
- **Portal 範本、Schema Explorer 與 Tenant Manager 產生器改教 schema 與 exporter 接受的形狀（portal；[#2033](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2033)）**：YAML Playground 範本與 Config Template Gallery 的 `_routing` 由扁平 `receiver_type`／`webhook_url`（exporter 會 WARN 並略過整段路由）改為 `_routing.receiver: {type, …}`；Gallery 移除不存在的 `_metadata.compliance` 與租戶檔內的 `_domain_policy`；`routing-profiles` 範本只示範 `_routing_profile: <name>`。Schema Explorer 只列租戶檔的鍵（移除 `_defaults`、`routing_profiles`、`_domain_policy`、`_instance_mapping` 與多個 schema 沒有的子鍵），「插入 Playground」產出以 `tenants:` 為根、依路徑巢狀的文件。Tenant Manager 的維護模式改產出貼到 `tenants.<id>:` 之下的 `_state_maintenance` 片段（原本的 `tenant-operational-modes` ConfigMap 沒有任何程式讀取），與靜默模式一樣只提供複製。範例中的過期時間一律依當下時間計算。以上皆由 Vitest 對 `tenant-config.schema.json` 驗證。
