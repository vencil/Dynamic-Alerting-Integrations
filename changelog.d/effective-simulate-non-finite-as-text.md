---
section: Fixed
topic: exporter
issues: []
created: 2026-09-29T00:20:11+00:00
---
- **`/effective`（tenant-api）與 `/simulate`（exporter）能讀出含 YAML `.inf`／`-.inf`／`.nan` 的租戶**：JSON 沒有這些數值，先前 `/effective` 回 `500`、`/simulate` 回 `200` 加空 body。現在 `effective_config` 內的這類值以字串 `"Infinity"`／`"-Infinity"`／`"NaN"` 輸出（與 Python `json.dumps` 的寫法相同），`merged_hash` 仍由原始值計算、與 `describe_tenant.py` 一致。⚠️ 因此在 `effective_config` 裡，字串 `"Infinity"` 可能代表原本的浮點數，也可能是租戶真的寫了這個字串。`/simulate` 也改為先 encode 再送 status。
