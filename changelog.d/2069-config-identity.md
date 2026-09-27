---
section: Added
topic: exporter
issues: [2069, 2132]
created: 2026-09-27T01:14:05+00:00
---
- **exporter 新增 `GET /api/v1/config/identity`，patch-config 改以它驗收（exporter、da-tools；[#2069](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2069)、[#2132](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2132)）**：給機器讀的 JSON 契約（`schema: 1`），回報目前服務的那一版設定的 `config_hash` 與因無法 parse 而被排除的檔案 `parse_failed`。`patch-config` 由寫入後的 ConfigMap 算出應有的 `config_hash`，等每個 pod 回報相同值才驗收，pod 載入別人較早寫入的位元組不再被當成通過；parse 失敗改看寫入後的 `parse_failed`，修好一個已被拒收的 key 不再被誤判失敗而回滾。沒有此端點（回 404）的舊 exporter 沿用舊驗法，並在 stderr 與 `--json` 的 `pods.<pod>.identity` 標示。
