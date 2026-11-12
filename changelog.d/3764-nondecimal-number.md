---
section: Changed
topic: tools
issues: [3764]
created: 2026-11-12T08:00:00+00:00
---
- **⚠️ Breaking：`validate-config` 的 `yaml_quoting` 列（與 `check_confd_schema`）也擋下數值欄位裡不是一般十進位的寫法（[#3764](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3764)）**：schema 只收數值的欄位（目前是 custom alert 的 `min_events`）寫成 `0x10`、`010`、`1e1`、`+5`、`1.0` 時 FAIL，訊息給出改法（`min_events: 16`）。原因是結構化保留鍵值在送到 exporter 讀取端的途中會被 yaml.v3 解碼再寫回，這些寫法因此被改寫（`0x10` 變 16、`010` 變 8），而 `min_events` 是照文字判、還會寫進 `recipe_id` 的欄位，只收十進位的驗證器因此形同虛設。判準與字串欄位那條相同：parse-tree 的 tag 加上 schema 的欄位型別，不列寫法清單。exporter 的讀法不變：改寫後的值不在保證範圍內，入口擋下這些寫法；字串欄位寫成數字（`code: 0x1F`、`version: 1.10`）本來就由同一列擋下，要加引號。
