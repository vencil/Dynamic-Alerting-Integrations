---
section: Fixed
topic: exporter
issues: [2913, 2486]
created: 2026-10-11T16:54:07+00:00
---
- **租戶檔同一個 mapping 有非純量 key 又有 `<<` 時不再讓程序 crash（exporter、tenant-api；[#2913](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2913)）**：`? [a]`、`? {k: v}` 這類 key 與 merge key `<<` 寫在同一個 mapping，vendored yaml.v3 的 typed decode 會 panic（`hash of unhashable type`），exporter 熱重載與 tenant-api 的寫入驗證、批次寫入都會整個程序結束。現在比照上游 `go.yaml.in/yaml/v3` v3.0.2 的修正（第 4 份 vendored patch，逐字取用）回一般的解析錯誤：exporter 把該檔當成一般的解析失敗（WARN、`da_config_parse_failure_total`；該檔的租戶照壞檔規則處理，其餘租戶不受影響；單檔模式沿用上一份設定），tenant-api 的 `PUT /tenants/{id}` 回 400 `invalid YAML`，批次對這種現有檔回 `TENANT_CONFIG_NOT_LOADABLE`。
