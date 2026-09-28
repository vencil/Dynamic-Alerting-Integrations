---
section: Fixed
topic: confd-family
issues: [2371]
created: 2026-09-29T02:25:00+08:00
---
- **`describe_tenant` 遇到 YAML 日期／`!!binary` 值不再 traceback（da-tools；[#2371](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2371)）**：租戶檔或 `_defaults.yaml` 裡只要有一個未加引號的日期（例如 `expires: 2026-12-31`）或 `!!binary` 值，單一租戶、`--all`、`--format yaml` 都會以 `TypeError` 結束，`--all` 等於一個租戶拖垮全部；exporter 對同一棵樹照常 serve。現在 `merged_hash` 與輸出共用同一個 JSON 轉換，照 exporter 的寫法輸出：日期為 `2026-12-31T00:00:00Z`、帶時區的時間為 RFC 3339、`!!binary` 為 UTF-8 解碼後的字串，`merged_hash` 與 exporter 相同。少數 PyYAML 已丟失原文而無法對齊的寫法（例如時分秒之間以空白分隔且無時區）列在 `describe_tenant.py` 內。
