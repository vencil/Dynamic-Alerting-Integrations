---
section: Fixed
topic: confd-family
issues: [2708]
created: 2026-10-04T01:04:51+00:00
---
- **排程裡的 null：沒有時段的 `{default: null}` 等於 null，帶時段的排程寫 null 在驗證期拒收（[#2708](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2708)）**：閾值寫成 `{default: null}`（可帶 `overrides: []`、`expires:`、`reason:`）時，`/metrics` 先前跳過子目錄 `_defaults.yaml`、profile 與根平台檔的值、直接退回根目錄預設值，`/effective` 與 da-guard 則把它當成這一層自己的值；現在租戶檔、根平台檔 `tenants:`、profile、子目錄 `_defaults.yaml` 寫它時都改用下一層的值，`describe_tenant` 同樣。以 YAML `<<:` 合併寫出的值不在此列。排程有時段、而時段本身、時段的 `window`／`value` 或 `default:` 是 null 時，da-guard 新增 error `schedule_null_value`、`validate-config` 新增 `schedule_null` 列，指名檔案、位置與 key；這類寫法的執行期行為不變。
