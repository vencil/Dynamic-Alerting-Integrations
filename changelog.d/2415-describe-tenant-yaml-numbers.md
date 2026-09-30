---
section: Fixed
topic: confd-family
issues: [2415]
created: 2026-09-30T04:35:00+00:00
---
- **`describe_tenant` 讀未加引號的整數／浮點數改照 exporter（yaml.v3）判定，`merged_hash` 與 exporter 一致（da-tools；[#2415](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2415)）**：conf.d 各檔（租戶檔、`_defaults.yaml`、根層平台檔）與 `--what-if` 檔裡的值，下列形狀的輸出與 `merged_hash` 會改變：`1e3`、`1e-6`、`+.5`、`0o17`、`08`、`0X1F` 等現在是數字（先前是字串）；`12:30:45`、`1:30`、`1_2:30` 等六十進位寫法現在是字串（先前是整數 45045、90、750）；超出 64 位元的整數（`0x10000000000000000` 為字串、`18446744073709551616` 為浮點數）照 exporter 處理。浮點數改依 Go `encoding/json` 的寫法輸出：`1.0` 為 `1`、`1e20` 為 `100000000000000000000`、`1.5e-5` 為 `0.000015`。`0b_` 先前會讓整個檔被略過，現在是字串。`yes`／`on` 等 YAML 1.1 布林讀法不變（[#2164](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2164)）。顯式 `!!int`／`!!float` 內容與 exporter 讀法不同者（例如 `!!float 0x1F`）為已知差異。
