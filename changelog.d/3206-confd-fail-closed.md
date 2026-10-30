---
section: Changed
topic: confd-family
issues: [3206]
created: 2026-10-22T21:00:00+00:00
---
- **⚠️ Breaking：conf.d 上有 da-guard 驗證會點名的路徑時，每支 conf.d 工具都在寫入前拒絕、結束碼 2（[#3206](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3206)，ADR-036 §7 步驟 9）**：exporter 讀不了或解不開的檔（語法錯、多份文件、懸空或迴圈 symlink、FIFO、列不出內容的目錄）與路由產生器因它拒收整棵樹的路徑（指向目錄的 symlink、名稱像設定檔的目錄 `x.yaml/`），stderr 逐條 `ERROR:` 全部點名，什麼都不寫，`--json` 的 `reason` 為 `conf_d_not_servable`。以前印 WARN／NOTE 照常結束碼 0 或 1 的：`configmap-assemble`、`assemble_config_dir`、`init`、`offboard`、`deprecate`、`migrate_conf_d`、`explain-keys`、`describe-tenant`、`compile_custom_alerts`、`generate_tenant_metadata`、`check_path_metadata_consistency`、`analyze-gaps`、`explain-route`、`maintenance-scheduler`、`evaluate-policy`、`opa-evaluate`、`gitops-check`、`blind-spot`、`threshold-recommend`、`threshold-govern`、`backtest`、`diagnose`；`tenant-verify` 以 1 結束。`validate-config`、`generate-routes`、`check_confd_schema`、`config-diff` 不變。升級前以 `da-tools guard defaults-impact` 驗 conf.d，結束碼 3 或 4 列出的路徑要先修好或移走。
