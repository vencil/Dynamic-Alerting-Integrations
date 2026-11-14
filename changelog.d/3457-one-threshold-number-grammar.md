---
section: Changed
topic: exporter
issues: [3457, 3827]
created: 2026-11-13T17:00:00+00:00
---
- **⚠️ Breaking：閾值的數字只剩一份文法，exporter 一起收窄（exporter／portal／schema／tools；[#3457](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3457)、[#3827](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3827)）**：文法寫在 recipe contract 的 `patterns.thresholdNumber`——十進位，可帶正負號、小數點、指數；尾數最多 20 位（前導零也算，指數的位數不計）——exporter、da-guard、tenant-api、RecipeBuilder、Python 工具與 schema 的 `thresholdScalar` 都讀它。有限性另外判：schema 的 pattern 寫不出來，`1e999` 過得了 schema、由 exporter 拒收。⚠️ `1_000`、`0x1p4` 這類寫法、尾數超過 20 位的數字（含過去被讀錯的 800 位以上尾數），exporter 不再送：租戶值被拒收時 base key 退回上層預設值（沒有就不送）、其他 key 不送該列；根 `defaults:` 的值被拒收而租戶沒覆寫時，該 key 什麼都不送。da-guard 報 `value_not_served`／`root_default_refused`（0 → 1），tenant-api 新寫入回 400／422；請改十進位。RecipeBuilder 不再放行 `0x1F`、`Infinity`；schema 會擋 `abc`、`1_000`。`threshold_recommend` 把這類現行值列入人工審查，超過 20 位的建議值寫成指數形；`config_diff` 判 `modified`；`generate_tenant_fixture --extra-defaults`、`inject_default_key` 拒收（rc 2／1）。寫入端不寫指數：`da_assembler`、`migrate_rule` 寫出後尾數超過 20 位的數字（如 `1e20`）改為結束碼 2不寫檔。
