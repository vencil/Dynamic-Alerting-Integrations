---
section: Changed
topic: confd-family
issues: [2858]
created: 2026-10-11T08:29:07+00:00
---
- **`check_retire_drift` 的 `db_type` 改取 exporter 實際送出的值（lint；[#2858](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2858)）**：先前只讀租戶檔自己的 `_metadata`、略過所有 `_` 開頭的檔，所以從 profile 或根層平台檔 `tenants:` 帶入的 `db_type` 認不得（exporter 仍會為它送 `tenant_expected_exporter`），非字串的 `db_type` 也讀成空；這些租戶拿掉 K8s target 時檢查照樣通過。現在 `db_type` 取自 `da-guard served-values`，與 exporter 同一套疊層規則；判定哪些租戶仍沿用原本的走檔與 `examples/` 排除。這支檢查因此需要 da-guard：pre-commit hook 與 CI 都經 `scripts/ops/with_da_guard.sh` 執行（沒有 Go 時以名稱報錯、不放行），直接執行時讀 `$DA_GUARD_BINARY` 或 `PATH` 上的 `da-guard`，找不到就 exit 2。
