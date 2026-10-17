---
section: Changed
topic: alertmanager-routing
issues: [2906]
created: 2026-10-17T12:01:00+00:00
---
- **⚠️ Breaking：`check_confd_schema.py`、`make configmap-assemble`、`assemble_config_dir.py`（`make sharded-assemble`／`sharded-check`）需要 da-guard（tools；[#2906](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2906)）**：`$DA_GUARD_BINARY`，否則 `$PATH` 上的 `da-guard`；da-tools 映像已內建（`/usr/local/bin/da-guard`）。沒有時結束碼 2：`check_confd_schema.py` 一個檔都不讀；`configmap-assemble`／`assemble_config_dir.py` 不寫產物，訊息帶出 `validate-config` 說的原因（先前只寫「沒有回報 `tenant_uniqueness`」）。repo 內：`make sharded-assemble`／`sharded-check` 沒設 `DA_GUARD_BINARY` 時自己建 `.build/da-guard`；`make configmap-assemble` 的 recipe 刻意不帶 make 展開，請在命令列帶 `DA_GUARD_BINARY=`（先 `make da-guard-build`）；pre-commit 的 `confd-schema-check`／`confd-schema-check-shipped` 經 `scripts/ops/with_da_guard.sh` 執行。
