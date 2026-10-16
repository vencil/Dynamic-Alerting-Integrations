---
section: Security
topic: tenant-api
issues: [3050, 3039, 2486]
created: 2026-10-16T04:38:38+00:00
---
- **同檔有讀不到的租戶時，整份檔一律不給（tenant-api；[#3050](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3050)）**：conf.d 一份檔可宣告多個租戶，原本 `GET /tenants/{id}` 的 `raw_yaml` 會連同檔內其他租戶的設定整份回傳。現在檔內只要有 caller 讀不到的租戶，`raw_yaml`、`custom_alerts`、`source_hash` 留空並帶 `raw_yaml_withheld: true`（含 `config_error` 形狀）；不另行重組「只有自己那段」。同一規則套到：`POST /{id}/diff` 回 422 `TENANT_FILE_WITHHELD`；`/effective` 的 `source_hash` 留空並帶 `source_hash_withheld: true`，解碼錯誤改回固定文字；`PUT /{id}/custom-alerts` 回 422 `TENANT_FILE_WITHHELD`、不寫入；`PUT` 與 custom-alerts 的 409 不再附 `current_source_hash`。列不出檔內租戶（不是 YAML、用了 alias／merge key、key 帶 `!!str` 以外的 tag）時，只給對所有租戶都有讀取權限的人。讀得到檔內全部租戶的 caller 行為不變。判斷與 [#3039](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3039) 共用同一個讀取檢查，對同檔每個其他租戶各做一次；org 軸在 shadow 時，would-deny 會照常計入 `tenant_api_scope_would_deny_total{axis="org"}`。
