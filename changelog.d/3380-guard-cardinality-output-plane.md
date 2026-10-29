---
section: Fixed
topic: da-guard
issues: [3380]
created: 2026-10-29T01:58:44+00:00
---
- **da-guard 的 cardinality 檢查改數 exporter 實際會產生的列，不再數 effective config 的鍵（da-guard；[#3380](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3380)）**：先前數的是合併後 effective config 的頂層鍵（扣掉一份手寫的保留鍵清單），子目錄 `_defaults.yaml` 寫了根層沒宣告、exporter 根本不送的鍵也算一列，於是 /metrics 只有 1 條 series 的租戶在 `--cardinality-limit 1` 下被判 `cardinality_exceeded`。現在比的是 exporter 對該租戶產生的 `user_threshold` 列數：取一天中最多的時段、在 exporter 自己的 `max_metrics_per_tenant` 截斷之前；`_critical`、維度鍵（含 regex）、#1231 舊拼法的雙送列、custom alert 都照實算，`disable` 與不送的鍵不算。finding 的訊息改寫成列數。QUICKSTART 的 cardinality demo 補上根 `_defaults.yaml`（以 `optional_overrides:` 宣告那 60 個鍵），原本 exporter 一條也不送。
