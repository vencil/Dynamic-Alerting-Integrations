---
section: Changed
topic: confd-family
issues: [3206]
created: 2026-10-21T20:30:00+00:00
---
- **讀 conf.d 的 Python 工具改讀 da-guard 的逐路徑判定，不再自己判斷路徑是不是設定檔（[#3206](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3206)，ADR-036 §7 步驟 9）**：`validate-config`、`check_confd_schema`、`configmap_assemble`、`assemble_config_dir`、`deprecate_rule`、`offboard_tenant`、`migrate_conf_d`、`init_project`、route generator 等改讀 `parse-tree` 的 `role`；已讀整棵樹的工具直接用同一份文件的 `entries`。stat 或開檔被拒的檔仍算設定檔，讀取時明確失敗，不再只留一行 WARN；連結指進無權限目錄時不再印 traceback。`check_confd_schema` 會列完所有讀不了的項目再以結束碼 2 結束，檔名像設定檔的目錄也算在內。任一檔名不是合法 UTF-8 時，整棵樹以結束碼 2 拒收。
