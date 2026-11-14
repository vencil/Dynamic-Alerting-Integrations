---
section: Changed
topic: alertmanager-routing
issues: [3703]
created: 2026-11-14T05:30:00+00:00
---
- **⚠️ BREAKING：Alertmanager 載入器拒收時，預設不印它的理由（alertmanager-routing；[#3703](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3703)）**：那段理由是 Alertmanager 的原文，會把寫在 conf.d 的值原樣引出（例如 `http_config.proxy_url`、`group_by` 的 label 名稱、TLS 版本）。`da-read alertmanager` 拒收時改印一句固定的話；`generate-routes` 的 `--validate`／`--output-*`／`--apply` 閘門、`--base-config` 與 `--apply` 讀叢集設定時同樣如此，`FAIL:` 那行點名新旗標 `--show-loader-reason`——在本機除錯時加上才印理由（`da-read alertmanager --show-loader-reason` 同）。產生器容器（tenant-api 的寫入判定）從不帶它。比對拒收訊息全文的腳本要更新。
