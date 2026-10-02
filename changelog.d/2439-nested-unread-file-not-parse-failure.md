---
section: Fixed
topic: exporter
issues: [2439]
created: 2026-10-02T00:29:07+00:00
---
- **子目錄裡 exporter 不讀的 `_` 檔，值的 tag 讓 Go 解碼失敗時不再算解析失敗（exporter；[#2439](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2439)）**：子目錄的 `_domain_policy.yaml`、`_routing_profiles.yaml`、`_profiles.yaml` 或其他 `_` 檔，語法正確、只是值的 tag 讓 Go 的 YAML 解碼失敗、route generator 卻讀得了時（例如 `!!null x`），原本會累加 `da_config_parse_failure_total`（觸發平台告警）、讓 da-guard 以 exit 3 結束、`served-values` 列進 `parse_failed`。現在三者都不再列為解析失敗；子目錄 `_domain_policy.yaml`／`_routing_profiles.yaml` 改與根目錄同名檔一樣，由 da-guard 報 `domain_policy_unusable`（error）／`routing_profiles_unusable`（warn）。這些檔的重複 key 仍會讓 route generator 拒讀整棵樹，da-guard 照舊 exit 3（子目錄 `_domain_policy.yaml` 為 `domain_policy_unusable`）。子目錄 `_defaults.yaml`（exporter 會讀）與 YAML 語法錯誤照舊算解析失敗。
