---
section: Changed
topic: custom-alerts
issues: [3229]
created: 2026-10-22T15:08:53+00:00
---
- **⚠️ BREAKING：自訂告警 recipe 的列舉、格式、界限、生命週期、SLO 窗口與倍數、每租戶上限改寫在一份契約檔（custom-alerts；[#3229](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3229)）**：`pkg/config/recipe_contract.json` 由 exporter 以 go:embed 讀取，tenant-api 經 `pkg/config` 讀；schema、portal 的 `recipe-enums.json`／`recipe-status.json`、`rule-packs/recipes/*.yaml` 的 status／required／enum／default 與 schema 的各 recipe 必填規則改由 `make recipe-contract` 產生，`make recipe-contract-check` 擋漂移（取代 `make recipe-status-json`）。原本各自手寫而不一致的地方一律取較嚴者：exporter 的告警 `name` 改用 schema 的 `^[a-z][a-z0-9_]*$`，大寫或底線開頭的名稱不再送出；schema 補上 Go 早已執行的規則（ratio 必填 `denominator_metric`、absence／slo_burn_rate 與 `op: "=="` 不收 `group_by`）；portal 表單的 forecast 比例下限改為小於 0.5、quantile 只收十進位寫法、`==` 只在 threshold 出現。
