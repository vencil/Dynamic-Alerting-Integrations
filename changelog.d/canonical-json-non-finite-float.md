---
section: Fixed
topic: exporter
issues: []
created: 2026-09-28T15:50:21+00:00
---
- **租戶設定裡的 YAML `.inf`／`-.inf`／`.nan` 不再讓 Go 端整個租戶解析失敗（`pkg/config` canonical JSON）**：先前 merged_hash 的 canonical JSON 遇到非有限浮點數即報 `json: unsupported value: +Inf`，da-guard 因此以 rc 2 結束，同一棵樹 `generate_alertmanager_routes.py` 與 `describe_tenant.py` 卻照常處理。現在改依 Python `json.dumps` 的寫法輸出 `Infinity`／`-Infinity`／`NaN`，merged_hash 與 `describe_tenant.py` 逐位元組一致；不含這類值的設定，雜湊不變。
