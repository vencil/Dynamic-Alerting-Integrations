---
section: Added
topic: lint
issues: [1200]
created: 2026-11-28T02:00:00+00:00
---
- **已退役的閾值 key 不得再出現在文件、範例、腳本與 portal（lint；[#1200](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1200)）**：新 hook `retired-keys-not-taught`（`scripts/tools/lint/check_retired_keys_not_taught.py`，每次 commit 與 CI Lint job 都跑）以 threshold registry 的 `retired_keys` 為唯一名單（含推導的 `<key>_critical`），掃所有被追蹤的檔案；registry、CHANGELOG、`changelog.d/`、測試與由 registry 生成的文字（含生成的 Go 表）除外。解釋退役的那一行在原地標 `retired-key-mention: <key>`（涵蓋該行與下一行），沒有對到任何提及、或沒寫 key 的標記同樣是錯；registry 讀不到、`retired_keys` 區段不見或一個檔都沒掃到時以 exit 2 結束。同時把 ADR-010／017／038、`docs/design/rule-packs` 、exporter 的 Go 註解與工具 docstring 裡仍拿 `mysql_connections` 當範例的地方換成現役的 `mysql_connection_usage_percent`，ADR-010 的 `/metrics` 範例輸出改為現行實測。
