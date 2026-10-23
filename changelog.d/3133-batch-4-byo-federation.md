---
section: Changed
topic: da-tools
issues: [3133]
created: 2026-10-23T10:00:00+00:00
---
- **⚠️ 結束碼變更：byo-check 的 Step 3 rules 與 Alertmanager status、federation-check 的 config 與 alert rules 讀不到時結束碼 2（[#3133](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3133) 第四批）**：改經 lib 的 `prometheus_rules`（新增 `rule_type`）、`alertmanager_status`、新的 `prometheus_status_config` 讀。`status: error` 或缺 `groups` 先前被報成「沒有 rule group」或「沒設 external_labels」（1 → 2），或「0 條 alert rule」（0 → 2）；Alertmanager status 缺 `config`／`config.original`（AM v2 API 必填）或型別不對，先前被報成「沒有 tenant 路由」（0 → 2），shadow-verify 明確給了 `--alertmanager` 時同樣 0 → 2、預設 URL 時改為 skip；edge config 的 `data` 缺 `yaml` 先前判「沒設 external_labels」（1 → 2）；其餘格式錯誤先前 crash，現在是具名的錯誤。`/federate` 是探測，仍是 warn。`_lib_python` 不再 re-export `http_get_json`、`query_prometheus_instant`、`query_prometheus_range`，請改從 `_lib_prometheus` import。
