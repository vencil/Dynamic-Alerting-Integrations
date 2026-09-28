---
section: Fixed
topic: confd-reader-consistency
issues: [2220]
created: 2026-09-28T11:25:18+00:00
---
- **`deprecate_rule --execute` 與 `patch_config` 寫回時不再改變沒加引號的值（tools；[#2220](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2220)）**：兩支工具刪或改一個 key 後會把整份檔（`patch_config` 是整個 ConfigMap key，legacy 與 multi-file 皆然；`deprecate_rule` 含 `_defaults.yaml`）重新寫出，過去其餘沒加引號的值會被改成 YAML 1.1 的型別：`010` 寫成 `8`、`12:30` 寫成 `750`、`0x1F` 寫成 `31`、`yes` 寫成 `true`、`2099-01-01T00:00:00Z` 寫成 `2099-01-01 00:00:00+00:00`，exporter 的生效值也跟著變（例如閾值 10 變 8、12 變 750）。現在沒被動到的值，exporter 讀到的生效值不變（`010` 仍寫成 `010`，`12:30` 仍寫成 `12:30`；數字、布林等欄位不會被加上引號）；文字可能被正規化（例如 flow 變 block、`~` 變 `null`）。新寫入的值格式與先前相同。`deprecate_rule` 會保留檔頭連續的註解，其餘註解仍會遺失，這是既有行為，本次未處理。
