---
section: Changed
topic: confd-family
issues: [2123]
created: 2026-09-27T13:25:03+08:00
---
- **conf.d 裡同一個 mapping 寫兩次同一個 key 從「通過」改為「失敗」；`da-guard` 新增 exit 3（tools／exporter；[#2123](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2123)）**：YAML 不允許這種寫法，exporter（yaml.v3）也拒收這種檔（同一租戶下同一 key 兩次、同一 tenant id 兩次、頂層 `tenants:` 兩次），Python 工具卻取最後一個值照常 exit 0。現在 `validate-config`、`generate-routes --validate`、`compile_custom_alerts`、`policy_engine`／`policy_opa_bridge`、`offboard_tenant`、`gitops_check`、`describe_tenant`／`tenant-verify`、`assemble_config_dir` 與 `check_confd_schema`／`check_routing_profiles` 等 lint 改用共用的嚴格讀取，重複鍵走各工具既有的「YAML 不合法」路徑（沿用該工具對語法錯的 exit code，訊息指出檔案與行號；`offboard_tenant` 點名該檔並判定 pre-check 失敗）；鍵的同一性與 yaml.v3 一致（原文相同即重複，經 `<<` 併入的 key 不比）。`da-guard`／`da-tools guard defaults-impact` 遇到 exporter 會整份丟掉的檔，或 da-guard 自己無法 decode 的檔時 exit 3、報告列出這些檔（定義見 [cli-reference §guard](docs/cli-reference.md#guard)）。CI 腳本若把非 0/1/2 當成未預期錯誤，請加上 3。
