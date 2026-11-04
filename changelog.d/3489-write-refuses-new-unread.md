---
section: Changed
topic: confd-family
issues: [3489]
created: 2026-11-01T14:35:51+00:00
---
- **寫進一個沒有讀取端讀的鍵，`da-guard write` 整批拒寫（confd-family；[#3489](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3489)）**：`patch-config` 與每支經 `_lib_confedit` 改 conf.d 的 Python 工具，現在寫入拼錯的鍵（`mysql_conections: 50`）、或標籤沒在根 `dimensions:` 宣告的維度鍵到租戶檔時會被拒（`worsens_tree`，`worsened` 的 `kind` 為新的 `unread`，`finding` 是原因，例如 `no_row`、`unregistered`）。比對寫入前後兩棵樹，只看這次寫到的租戶檔（與 tenant-api 寫入閘門同一個範圍）：寫入前就有的錯誤不擋、修掉的照常通過；只改平台檔而讓別的租戶鍵沒人讀，不在這裡擋，仍由 validate-config 與 CI 報出。判準是共用鍵判決（ADR-038）的 ERROR 層。整份判決已移植到 Go（`pkg/keyverdict`），新增 `da-guard key-verdict` 以 JSON 印出逐鍵判決、findings、`undecidable` 與 `key_checks`（`--tenant` 只判指定租戶）。validate-config、`generate-routes --validate`、`explain-keys` 與產生器容器都改讀它，Python 那份判決刪除。
