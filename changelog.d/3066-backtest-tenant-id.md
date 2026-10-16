---
section: Fixed
topic: da-tools
issues: [3066]
created: 2026-10-16T11:41:02+00:00
---
- **`backtest --tenant` 以租戶 id 規則驗證（[#3066](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3066)）**：⚠️ 結束碼變更：`--tenant` 不是合法租戶 id（ADR-035 的 DNS-1123 label，例如 `a"b`、大寫、超過 63 字元）時 exit 2，`--json` 輸出 `caller_error` envelope（`reason: invalid_tenant_id`），帶 `--skip-if-unavailable` 也一樣，不查 Prometheus。先前這個值原樣組進 `{tenant="..."}`：`a"b` 會送出不合法的 PromQL、結束碼 0；大寫、`a-`、超過 63 字元這類 Prometheus 接受的值會照常查詢，結束碼 0 或 1（HIGH）。
