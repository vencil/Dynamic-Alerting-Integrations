---
section: Changed
topic: da-tools
issues: [3267]
created: 2026-11-01T03:37:22+00:00
---
- **⚠️ Breaking：`backtest --tenant` 照 exporter 讀出的數字回測 `1_000`、`0x1p4` 這類寫法（[#3267](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3267)）**：拿掉工具自己的拼法表，數字的讀法只剩 exporter 的 `parseThresholdNumber`（經 `da-guard served-values`）。exporter 讀得懂的底線與十六進位浮點數寫法改為照它送的數字（1000、16）回測，JSON 的 `new_value`／`old_value` 是送出的數字；`1__0`、`0x10` 這類 exporter 也拒收的寫法仍不回測。結束碼：這類寫法以前一律不回測（UNKNOWN、結束碼 0），現在依回測結果——判 HIGH 時 0 → 1（例如 `70` → `1_000` 讓原本會響的點不再響），MEDIUM、LOW 維持 0（例如 `0x1p4`、`0x1p6`）。CI 裡帶這類值的 `--tenant` 呼叫可能因此轉紅。
