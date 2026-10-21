---
section: Changed
topic: confd-family
issues: [3206]
created: 2026-10-21T18:59:58+00:00
---
- **讀 conf.d 的 Python 工具改讀 da-guard 的逐路徑判定（[#3206](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3206)，ADR-036 §7 步驟 9）**：`validate-config`、`check_confd_schema`、`configmap_assemble`、`assemble_config_dir`、`deprecate_rule`、`offboard_tenant`、`migrate_conf_d`、`init_project`、route generator 等改讀 `parse-tree` 的 `role`，不另列檔。stat 或開檔被拒的檔仍算設定檔，讀取時明確失敗。列不出內容的目錄不論名稱一律點名，報告不給結束碼 0；指向目錄的 symlink 不再被 `migrate_conf_d` 當成已巢狀。`check_confd_schema` 列完讀不了的項目後以結束碼 2 結束；`validate-config` 的 `json_schema` 列遇到第一個讀不了的檔即 FAIL。`assemble_config_dir` 的「could not measure」只看列不出內容的目錄，名稱像設定檔的目錄照樣走進去並納入重複檢查。檔名不是合法 UTF-8 時整棵樹以結束碼 2 拒收。
