---
section: Changed
topic: confd-family
issues: [3206]
created: 2026-10-21T18:59:58+00:00
---
- **讀 conf.d 的 Python 工具改讀 da-guard 的逐路徑判定（[#3206](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3206)，ADR-036 §7 步驟 9）**：`validate-config`、`check_confd_schema`、`configmap_assemble`、`assemble_config_dir`、`deprecate_rule`、`offboard_tenant`、`migrate_conf_d`、`init_project`、route generator 等改讀 `parse-tree` 的 `role`／`class`，不另列檔、不以名稱或 stat 自己判。stat 或開檔被拒的檔仍算設定檔，讀取時明確失敗；列不出內容的目錄不論名稱一律點名，結束碼不為 0；指向目錄的 symlink 不算已巢狀；`check_confd_schema` 列完讀不了的項目後以結束碼 2 結束，`validate-config` 的 `json_schema` 列遇到讀不了的檔即 FAIL，`assemble_config_dir` 的「could not measure」只看列不出內容的目錄。「宣告了哪些租戶」是同一個答案的兩半：租戶檔，以及可能藏著租戶檔的項目——`check_retire_drift`、`generate_tenant_mapping_rules`、`validate-config`、`init_project` 等一起讀兩半，例如 `team -> ../outside` 旁 `check_retire_drift` 以結束碼 2 點名 `team`。`migrate_conf_d` 點名名稱像檔案的目錄（`x.yaml/`）。`config_diff` 在工具之內每個 conf.d 只取一份索引（route generator 子程序另讀一份），任一側拿不到文件即以結束碼 2 結束。檔名不是合法 UTF-8 時整棵樹以結束碼 2 拒收。
