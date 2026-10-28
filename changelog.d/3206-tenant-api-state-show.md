---
section: Changed
topic: tenant-api
issues: [3206]
created: 2026-10-28T01:00:21+00:00
---
- **⚠️ Breaking：`--state` 改收 `tenant-api state show` 的 JSON（[#3206](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3206)，[ADR-036](docs/adr/036-single-parser-effective-config.md) §12）**：tenant-api 新增 `state show [--from FILE|DIR|-]`（每筆 state 紀錄經 server 的解析函式，印出 `record` 或 `problem`）與 `config check --kind rbac|tenant-orgs`。`offboard_tenant`／`generate_tenant_metadata` 的 `--state` 不再收 ConfigMap 或 state 目錄；遷移：`kubectl exec deploy/tenant-api -c tenant-api -- tenant-api state show > state.json`，備份用 `docker run -i --rm <tenant-api 映像> state show --from - < backup.yaml > state.json`。帶 `problem` 的紀錄算未判定：下架 `--execute` 需 `--skip-state-check`、元資料產生器結束碼 2。`check_admin_config_schema` 改經 `config check`／`da-crdecode`／parse-tree 讀檔。Helm chart 凡 tenant-api 有 `TA_*` 對應的設定（store、claim、federation）改以 env 傳入；`tenant_api_version` 取自映像建置時傳入的版本。
