---
section: Changed
topic: exporter
issues: [3881]
created: 2026-11-14T17:02:00+00:00
---
- **子目錄 `_defaults.yaml` 寫的保留鍵，共用判決也報 unread（da-guard key-verdict、validate-config、generate-routes；[#3881](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3881)）**：子目錄 `_defaults.yaml` 的保留鍵（`_silent_mode`、`_severity_dedup`、`_state_*` 等）不套用到任何租戶。主檢查有報 `subtree_default_reserved_key`，`da-guard key-verdict` 卻沒有 finding，讀共用判決的 `generate-routes --validate` 與 validate-config 的 `routes` 列因此不會說。現在共用判決對每個這種檔案與鍵報一筆 `unread`（error，原因 `subtree_not_applied`），列出繼承該檔的租戶；租戶自己有沒有寫同一個鍵都報，排程寫法也算。兩邊讀同一個判定（`config.SubtreeReservedReported`），在主檢查有判的樹上報出的（租戶、鍵）集合相同；樹裡有 exporter 丟棄的檔時主檢查 exit 3、什麼都不判，共用判決仍會判讀得進來的檔。⚠️ **breaking**：含這種鍵的樹會讓 `da-guard key-verdict` 的 `blocks` 變成 true，`generate-routes --validate` 與 validate-config 的 `routes` 列因此轉紅；這種樹在 da-guard 主檢查本來就不是 exit 0。`da-guard write` 的拒收範圍變大：主檢查以（租戶、鍵）判斷有沒有新增，共用判決以（檔案、鍵）判斷，所以在下層子目錄檔再寫一次租戶已繼承的同一個鍵、或把帶保留鍵的子目錄檔搬到別的路徑，以前會通過、現在拒收（`unread`，每個繼承的租戶一筆）。同一個檔裡原有的保留鍵不擋該檔的其他修改，改它的值或刪掉它也不擋。遷移：把鍵從子目錄檔刪掉，要讓值生效就寫進各租戶自己的 `tenants:` 條目。
