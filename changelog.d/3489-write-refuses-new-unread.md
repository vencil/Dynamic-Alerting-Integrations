---
section: Changed
topic: confd-family
issues: [3489]
created: 2026-11-01T14:35:51+00:00
---
- **寫進一個沒有讀取端讀的鍵，`da-guard write` 整批拒寫（confd-family；[#3489](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3489)）**：`patch-config` 與每支經 `_lib_confedit` 改 conf.d 的 Python 工具，現在寫入拼錯的鍵（`mysql_conections: 50`）、或標籤沒在根 `dimensions:` 宣告的維度鍵到租戶檔時會被拒（`worsens_tree`，`worsened` 的 `kind` 為新的 `unread`，`finding` 是原因，例如 `no_row`、`unregistered`）。比對寫入前後兩棵樹，只看這次寫到的檔（租戶檔、profile 檔、子目錄 `_defaults.yaml` 或根平台檔），以租戶＋鍵（鍵來自 profile 等繼承的檔時再加上檔案；沒有租戶時以檔案＋鍵）比新舊：寫入前就有的錯誤不擋、修掉的照常通過，搬租戶、解除 profile 綁定不算新出現。判準是判決 finding 的 `blocks`。整份判決已移植到 Go（`pkg/keyverdict`），新增 `da-guard key-verdict` 以 JSON 印出逐鍵判決、findings、`undecidable` 與 `key_checks`（`--tenant` 只判指定租戶）。validate-config、`generate-routes --validate`、`explain-keys` 與產生器容器都改讀它，Python 那份判決刪除。
