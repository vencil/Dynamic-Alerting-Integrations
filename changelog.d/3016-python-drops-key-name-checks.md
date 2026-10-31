---
section: Changed
topic: da-tools
issues: [3016]
created: 2026-10-31T12:00:00+00:00
---
- **⚠️ breaking：generate-routes 與 validate-config 不再自己判鍵名（ADR-038 決定 11）**：`schema` 列與 `generate-routes`（含 render 模式）不再報 `unknown key … not in defaults`、`unknown reserved key`；一個 key 有沒有讀取端讀，只由 `unread_keys` 列與 `--validate` 的 `key verdict:` 行（共用判決）報。`version` label 與舊拼法（#1231）的提示改照 exporter 的措辭轉印，在 `--findings-json` 是 `key_version_label`、`key_renamed`，`blocks: never`；da-guard 太舊、沒有 `key_checks` 時印一行「key checks incomplete」。平台什麼都沒宣告時，`unread_keys` 的建議改成補上宣告、不要刪 key。
