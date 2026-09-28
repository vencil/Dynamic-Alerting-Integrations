---
section: Fixed
topic: confd-family
issues: [2371]
created: 2026-09-29T02:25:00+08:00
---
- **`describe_tenant` 遇到 YAML 日期／`!!binary` 值或非字串 key 不再 traceback（da-tools；[#2371](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2371)）**：只要有一個未加引號的日期（例如 `expires: 2026-12-31`）、`!!binary` 值，或 `_defaults.yaml` 以日期當 key，三種模式都會以 `TypeError` 結束，一個租戶就拖垮 `--all`；exporter 照常 serve。現在照 exporter 的寫法：時間值依 yaml.v3 的規則判定（例如 `2026-12-31 23:59:59` 為 `2026-12-31T23:59:59Z`，`2026-13-01` 維持字串，不再讓整個租戶檔被略過）、`!!binary` 為 UTF-8 字串；租戶 ID 之下的非字串 key 拼成 exporter 的 `%v` 形式（例如 `2026-12-31 00:00:00 +0000 UTC`、`010` → `8`），`merged_hash` 與 exporter 相同。exporter 會拒收的檔案現在也一併拒收，與 exporter 一致：顯式 `!!timestamp` 而內容不是時間（例如 `!!timestamp foo`），或時區達 24 小時以上（例如 `+24:00`，exporter 無法為它算出 `merged_hash`）。這類檔案若是租戶檔會被略過，若是 `_defaults.yaml` 則走下述 traceback 路徑。含 `"\udfff"` 跳脫（UTF-16 surrogate）的檔案改為無法解析：租戶檔會被略過，`_defaults.yaml` 則仍以 traceback 結束（由 [#2413](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2413) 追蹤）。
