---
section: Changed
topic: exporter
issues: [2296]
created: 2026-10-08T21:20:00+00:00
---
- **`/effective` 與 `da-guard effective` 標出 defaults 鏈、根目錄包裝與平台／租戶層值解析中 `/metrics` 不送的值（threshold-exporter / tenant-api / da-guard；[#2296](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2296)）**：`effective_config` 照原文保留，每個租戶新增 `not_served`（逐 key 的 `reason` 與 `file`；原因取自 exporter 自己的判定：`parse_failed`、`root_defaults_unwrapped`、`value_rejected`、`value_unparsed`、`value_unparsed_dropped`、`window_invalid`（[#2065](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2065)）、`undeliverable`、`root_null_undeclared`）與 `chain_parse_failed`；`/effective` 只在非空時帶，`da-guard effective` 一律輸出。profile 層被丟掉的值與過期 override 目前不標。`served-values` 新增頂層 `unread_keys`。⚠️ 行為變更：defaults 鏈上有語法錯、exporter 整份不讀的 `_defaults.yaml` 不再讓租戶消失——tenant-api `/effective` 由 500 改回 200，`da-guard effective` 照常列出該租戶（結束碼仍為 3），該檔當成空檔並列在 `chain_parse_failed`，`merged_hash` 以略過它的鏈計算。da-guard 主 gate 不變。
