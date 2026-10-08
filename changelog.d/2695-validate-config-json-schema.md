---
section: Changed
topic: confd-family
issues: [2695]
created: 2026-10-07T16:20:41+00:00
---
- **`validate-config` 新增 `json_schema` 列，conf.d 不符 JSON Schema 時從「通過」改為「失敗」（tools；[#2695](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2695)）**：原本的 `schema` 列其實是 route generator 的 key 檢查，`check_confd_schema` 拒收的樹（例如 `_routing.group_by: alertname`）在這裡仍是 PASS、exit 0。新列呼叫 `check_confd_schema` 的同一支判定，違規時 FAIL、exit 1；`yaml_quoting` 已列出的未加引號值在這一列當成已加引號判定、不重報型別錯誤；加引號後仍不合 schema 的值（例如 `group_wait: 30`，`"30"` 沒有單位）照報。沒有 `jsonschema` 套件或旁邊沒有 `check_confd_schema.py`（da-tools 映像兩者皆無）時是 WARN、註明未檢查。`schema` 列保留原名，PASS 明細改寫成產生器 key 檢查。`platform-defaults.schema.json` 補上 `_defaults.yaml` 的 `_policies`（先前被拒收）；`check_confd_schema` 改以 exporter 的方式讀 key，遇到 `~:` 這類 null key 不再 traceback。
