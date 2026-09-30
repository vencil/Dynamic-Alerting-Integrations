---
section: Fixed
topic: exporter
issues: [2368]
created: 2026-09-28T18:18:00+00:00
---
- **租戶用舊拼法寫的閾值不再被平台檔 `tenants:` 的新拼法蓋掉（exporter / tenant-api；[#2368](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2368)）**：租戶檔寫 `mysql_cpu`、根目錄平台檔的 `tenants:` 替同一租戶寫 `mysql_threads_running` 時，過去 `/metrics`（含雙發的 `metric="cpu"` series）與 `GET /api/v1/tenants/{id}` 送的是平台值，租戶的值只出現在 served-values 的 `unserved`。現在平台層與租戶層、以及多個平台檔之間，一律以同一個閾值（不分拼法）逐 key 比對，後面的層勝出：租戶寫任一拼法就蓋掉平台的所有拼法。`/effective`、`describe_tenant --show-sources`、`diagnose --show-inheritance` 同步修正，`platform_overlay` 的 `keys` 改記新拼法（與 `/metrics` 一致）；`da-guard` 的「覆寫多餘」改為按閾值判斷，租戶同一閾值寫兩種拼法時不判斷（已知例外：根 `_defaults.yaml` 同層「新拼法 null＋舊拼法值」見 #2418；租戶把閾值寫成 null 時子目錄 `_defaults.yaml` 的值不會送出，見 #2518；子目錄 `_defaults.yaml` 的其餘跨拼法情形已由 #2414 修正）。原本「舊拼法 key 被平台檔蓋過、未套用」的 notice 隨之移除，改回一般的改名提示。
