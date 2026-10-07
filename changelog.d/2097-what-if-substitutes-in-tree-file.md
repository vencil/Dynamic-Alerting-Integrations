---
section: Fixed
topic: confd-reader-consistency
issues: [2097]
created: 2026-10-07T18:02:30+00:00
---
- **`describe_tenant.py --what-if` 指向樹內不在 defaults chain 上的檔時，改為取代同路徑的檔再重新求值（tools；[#2097](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2097)）**：以前 `--what-if <conf.d>/_profiles.yaml` 即使內容完全沒改，也會把整份檔當成新插入的 defaults 層合併，回報 `insert` 與 `would_trigger_reload: true`。現在只要檔案是 conf.d 裡既有的檔（根平台檔、別的分支的 `_defaults.yaml`、租戶自己的檔），就以它的內容取代原檔、chain 不變，`substitution_type` 為 `substitute`：內容不變就不報 reload，改到租戶選中的 profile 值才報變動，結果與 `da-guard effective` 對替換後的樹一致。樹外的檔（`append-external`）與樹內不在掃描清單上的路徑（`insert`）維持原行為。
