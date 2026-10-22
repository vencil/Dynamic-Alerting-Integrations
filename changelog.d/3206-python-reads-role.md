---
section: Changed
topic: confd-family
issues: [3206]
created: 2026-10-21T18:59:58+00:00
---
- **讀 conf.d 的 Python 工具改讀 da-guard 的逐路徑判定（[#3206](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3206)，ADR-036 §7 步驟 9）**：`validate-config`、`check_confd_schema`、`configmap_assemble`、`assemble_config_dir`、`deprecate_rule`、`offboard_tenant`、`migrate_conf_d`、`init_project`、route generator 等改讀 `parse-tree` 的 `role`／`class`，不另列檔。stat 或開檔被拒的檔仍算設定檔，讀取時明確失敗；列不出內容的目錄不論名稱一律點名、結束碼不為 0，例外沿用既有行為、仍點名但結束碼 0：`assemble_config_dir` 對來源內的子目錄 WARN 且不合併（來源根目錄才是 2），`configmap_assemble` 印 WARN，`init_project` 印 NOTE；指向目錄的 symlink 不算已巢狀；`check_confd_schema` 列完讀不了的項目後以 2 結束，`validate-config` 的 `json_schema` 列遇到讀不了的檔即 FAIL。「宣告了哪些租戶」是一個答案的兩半：租戶檔，以及可能藏著租戶檔的項目，`check_retire_drift`、`generate_tenant_mapping_rules`、`validate-config`、`init_project` 等一起讀，例如 `team -> ../outside` 旁的 `check_retire_drift` 以 2 點名它。`migrate_conf_d` 對 `x.yaml/` 改為點名並視同巢狀目錄，結束碼由 2 改為 0。`config_diff` 在工具之內每個 conf.d 只取一份索引（route generator 子程序另讀一份），任一側拿不到文件即以 2 結束。檔名不是合法 UTF-8 時整棵樹以 2 拒收。
