---
section: Changed
topic: schema
issues: [3227]
created: 2026-10-22T01:46:07+00:00
---
- **`_silent_mode` 的 schema 詞表改為與 exporter 相同，portal 的保留鍵清單改由登錄表產生（schema／portal；[#3227](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3227)）**：`tenant-config.schema.json` 的 `_silent_mode`（純量與結構化 `target`）現在也接受 exporter 早就認得的關閉別名 `disabled`／`off`／`false`，先前這些合法值會被 schema 驗證（編輯器等）誤報；`false` 須加引號（未加引號時 yaml.v3 讀成布林，schema lint 會指出）；未加引號的 `off` 只有 repo 外的 YAML 1.1 loader 才會讀成布林。portal 設定驗證器的 `_` 鍵清單改由 `sync_schema.py` 從保留鍵登錄表產生，與 schema 判定一致：`_severity_dedup`／`_custom_alerts` 不再被標為未知；寫在租戶底下的平台層鍵（`_routing_defaults`、`_domain_policy`、`_instance_mapping` 等）改標為未知。
