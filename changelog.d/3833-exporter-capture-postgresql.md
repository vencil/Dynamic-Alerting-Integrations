---
section: Added
topic: rule-packs
issues: [3833]
created: 2026-11-28T10:55:00+00:00
---
- **PostgreSQL pack 讀的原始指標改由真實 exporter 核對（rule-packs、ci；[#3833](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3833)）**：threshold registry 的 pack 可以帶 `exporters:`——exporter 映像（tag＋digest）、啟動旗標、對象資料庫映像、要擷取的狀態，以及每個狀態的抓取裡必須有的識別值（例如 replica 的 `pg_replication_is_replica 1`）。CI 的 `Exporter Capture (PostgreSQL)` 照這份登記啟動 postgres_exporter 與一組 PostgreSQL primary／standby 並抓 `/metrics`；pack 規則讀的每個原始選擇器（含 label matcher）都要選得到序列，否則失敗並指出規則、選擇器與缺的是什麼。例外是 `A or B`：一邊選得到、另一邊的指標在所有抓取裡都不存在時也算過，但那一邊要登記在 pack 的 `unchecked_alternatives:` 並寫原因，所以 CI 通過不代表兩個名稱都實測過（PostgreSQL 的 `pg_replication_lag` 就是這種）；另一邊的指標有抓到、label matcher 卻選不到時照樣失敗。版本與旗標寫進 rule pack 檔頭，也列在 ALERT-REFERENCE 該 pack 的段首。其餘九個第三方 exporter 的 pack 列在 `check_exporter_capture.py` 的未驗證清單；新增的 pack 兩者都沒有會被 pre-commit 擋下。本次為試行，只做 PostgreSQL。
