---
section: Added
topic: tools
issues: [4225, 4211]
created: 2026-11-22T12:35:05+00:00
---
- **`da-tools compile-custom-alerts`：客戶可以把自己 conf.d 的 `_custom_alerts` 編成規則包（tools；[#4225](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4225)）**：先前只有本 repo 的 `make custom-alerts-compile` 能編譯，而且固定讀本 repo 的 conf.d，da-tools 映像沒有這個命令。現在 `--config-dir <conf.d> --out <規則包>` 寫出向量化規則包（規則數等於 shape 數），`--check` 在 CI 比對規則包是否落後 conf.d（漂移時結束碼 1）。判定與編譯都由映像內建的 da-guard 做，讀法與 exporter 送出的相同。BYO Prometheus 整合文件先前叫客戶跑 `make custom-alerts-compile`，已改正。
