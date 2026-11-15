---
section: Added
topic: tools
issues: [3830]
created: 2026-11-14T11:56:42+00:00
---
- **validate-config 點名「平台預設出貨、你的樹卻沒供給」的閾值鍵（`unsupplied_defaults`；[#3830](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3830)）**：`da-tools init` 會把平台預設複製進你的 `_defaults.yaml`，之後平台新增或更換的預設鍵（例如 #3778 的 `mysql_connection_usage_percent`）不會自己進到你的樹，對應的告警就靜默失效。新的一列會列出這些鍵：threshold registry 裡 `arming: default`、屬於你有部署的 pack、而根平台檔既沒寫在 `defaults:` 也沒列在 `optional_overrides:` 的鍵，並附平台的值。pack 有沒有部署看兩種證據：根平台檔在 `defaults:` 給了該 pack 任一鍵的值，或租戶在 `_metadata.db_type` 宣告了它（只列在 `optional_overrides:` 不算：那份清單是整份出貨的）。處理方式：把鍵加進 `defaults:`；刻意不啟用就列進 `optional_overrides:`。只是建議，最多 WARN、不影響 exit code——這支工具與你部署的 rule pack 是不同的發布線，版本有落差時可能誤報。
