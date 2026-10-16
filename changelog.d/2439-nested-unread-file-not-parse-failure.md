---
section: Fixed
topic: exporter
issues: [2439]
created: 2026-10-02T00:29:07+00:00
---
- **子目錄裡 exporter 不讀的 `_` 檔，值的 tag 讓 Go 解碼失敗時不再算解析失敗（exporter；[#2439](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2439)）**：子目錄的 `_domain_policy.yaml`、`_routing_profiles.yaml`、`_profiles.yaml` 或其他 `_` 檔，語法正確、只是值的 tag 讓 Go 的 YAML 解碼失敗、route generator 卻讀得了時（例如 `!!null x`），原本會累加 `da_config_parse_failure_total`（觸發平台告警）、讓 da-guard 以 exit 3 結束、`served-values` 列進 `parse_failed`。現在三者都不再列為解析失敗。da-guard 把這種值的 tag 讓 Go 解碼失敗的檔列為未判定證據 `generator_unreadable`（`field` 為 `tag_not_allowed` 或 `decode_error`，exit 4），子目錄裡重複 key 的這些檔同樣列為 `generator_unreadable`（[#2905](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2905)），都不算 exit 3（根目錄的 `_` 檔有重複 key 時 exporter 整份丟掉，照舊是 exit 3）；子目錄 `_domain_policy.yaml`／`_routing_profiles.yaml` 本身乾淨可解時，才與根目錄同名檔一樣列為 `domain_policy`／`routing_profiles_file`（exit 4）。兩種情況都要由 `da-tools generate-routes --validate --strict` 判定。子目錄 `_defaults.yaml`（exporter 會讀）與 YAML 語法錯誤照舊算解析失敗。
