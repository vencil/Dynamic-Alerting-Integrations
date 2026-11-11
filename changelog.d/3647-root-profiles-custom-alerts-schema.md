---
section: Changed
topic: confd-family
issues: [3647]
created: 2026-11-11T15:57:52+00:00
---
- **⚠️ Breaking：根目錄平台檔 profile 的 `_custom_alerts` 違規從「通過」改為「失敗」（tools；[#3647](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3647)）**：租戶以 `_profile` 綁定的 profile，其 `_custom_alerts` 就是該租戶被送出的清單，但 `check_confd_schema` 與 `validate-config` 的 `json_schema`／`yaml_quoting` 列先前只檢查 `_profiles.yaml` 的 `tenants:` 區塊（#2743），`profiles:` 區塊不檢查：同一筆 `quantile: 95e-2` 寫在租戶檔是 rc 1，寫在 profile 是 rc 0。現在根目錄 `_` 檔（`_profiles.yaml`、根 `_defaults*`…，以 da-guard parse-tree 的判定為準）的 `profiles.<name>._custom_alerts` 以 `$ref` 對照 `tenant-config.schema.json` 的 `customAlertList`（與租戶檔同一份定義，含引號檢查），錯誤寫成 `_profiles.yaml: … @ /profiles/<name>/_custom_alerts/…`。子目錄平台檔的 `profiles:` 沒有讀取端，不判定。skipped 清單下的那一行改說 `tenants:` / `profiles:` 區塊。
