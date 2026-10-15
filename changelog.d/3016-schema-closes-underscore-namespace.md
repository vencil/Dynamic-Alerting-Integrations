---
section: Changed
topic: confd-family
issues: [3016]
created: 2026-10-15T00:05:06+00:00
---
- **tenant schema 關閉 `_` 命名空間，拼錯的保留鍵在編輯器與 validate-config 就被指出（[#3016](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3016)）**：`tenant-config.schema.json` 的 `tenantConfig` 新增 `propertyNames`：`_` 開頭的鍵只能是平台會讀的保留鍵、`_routing`／`_routing_profile`，或 `_state_<filter>`。清單由 `sync_schema.py --update` 從 Python SSOT 產生，`--check` 會報漂移。租戶層的 `_routing_defaults`／`_routing_enforced` 從 schema 與 portal 欄位說明移除（只有平台檔讀它們）。⚠️ 行為變更：裝了 `jsonschema` 時，`validate-config` 遇到 `_silense_mode` 這類拼法由 WARN（exit 0）變成 `json_schema` FAIL（exit 1）；da-tools 映像沒裝 `jsonschema`，不受影響。
